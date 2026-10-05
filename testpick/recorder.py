"""Record which functions, and which data files, each test uses.

On Python 3.12+ this uses ``sys.monitoring`` (PEP 669): each function's first call in a test
triggers one callback, after which that call site is switched off until the next test. So
the cost is roughly one callback per function per test, instead of one per executed line
as with line coverage. Older Pythons fall back to ``sys.setprofile``, which is slower.

Files opened during a test are tracked with an audit hook on ``open`` (this includes
modules imported lazily and ``.py`` files read as test data), so a change to a fixture
only selects the tests that actually read it.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
from types import CodeType, FrameType
from typing import Any

from .functions import MODULE, FunctionIndex, normalize_qualname

_SKIP_DIRS = ("site-packages", "dist-packages", "/.git/", "/.venv/", "/venv/", "/.tox/", "/node_modules/")


class Recorder:
    """Collects, for the test currently running, the set of ``path::function`` keys it ran."""

    def __init__(self, root: str, mode: str | None = None):
        self.root = os.path.realpath(root) + os.sep
        self.current: set[str] | None = None
        self.files: set[str] | None = None
        self._paths: dict[str, str | None] = {}  # filename -> repo-relative path, or None if not ours
        self._keys: dict[CodeType, str | None] = {}
        self._indexes: dict[str, FunctionIndex] = {}  # Python 3.10 only: code objects lack co_qualname
        self.mode = mode or os.environ.get("TESTPICK_RECORDER") or ("monitoring" if sys.version_info >= (3, 12) else "profile")
        self._tool: int | None = None
        self._audit_installed = False

    # ---------------------------------------------------------------- helpers

    def _rel(self, filename: str) -> str | None:
        try:
            return self._paths[filename]
        except KeyError:
            pass
        rel: str | None = None
        if filename and not filename.startswith("<"):
            real = os.path.realpath(filename)
            if real.startswith(self.root) and not any(s in real for s in _SKIP_DIRS):
                rel = real[len(self.root) :].replace(os.sep, "/")
        self._paths[filename] = rel
        return rel

    def _key(self, code: CodeType) -> str | None:
        try:
            return self._keys[code]
        except KeyError:
            pass
        rel = self._rel(code.co_filename)
        key = None
        if rel is not None and rel.endswith(".py"):
            # A module or class body running during a test (a lazy import, or a class built on
            # the fly) means the test depends on that file's module-level code.
            name = self._qualname(code) if code.co_flags & inspect.CO_NEWLOCALS else MODULE
            key = f"{rel}::{name}"
        self._keys[code] = key
        return key

    def _qualname(self, code: CodeType) -> str:
        qualname = getattr(code, "co_qualname", None)
        if qualname is not None:
            return normalize_qualname(qualname)
        # Python 3.10: only the bare name is known, so look the function up by its first line
        # (which, like the index's spans, starts at the first decorator)
        index = self._indexes.get(code.co_filename)
        if index is None:
            files, self.files = self.files, None  # don't count our own read as the test's
            try:
                with open(code.co_filename, encoding="utf-8") as f:
                    index = FunctionIndex(f.read())
            except (OSError, UnicodeDecodeError):
                index = FunctionIndex("")
            finally:
                self.files = files
            self._indexes[code.co_filename] = index
        name = index.at(code.co_firstlineno)
        return name if name != MODULE else normalize_qualname(code.co_name)

    # ---------------------------------------------------------------- start / stop

    def start(self) -> None:
        if self.mode == "monitoring":
            mon = sys.monitoring  # type: ignore[attr-defined]
            for tool in (mon.PROFILER_ID, 3, 4, 5):
                if mon.get_tool(tool) is None:
                    mon.use_tool_id(tool, "testpick")
                    self._tool = tool
                    break
            else:
                raise RuntimeError("no free sys.monitoring tool id (another profiler or debugger is active)")
            # PY_RESUME too: a generator created in one test and resumed in another counts for both
            events = mon.events.PY_START | mon.events.PY_RESUME
            mon.register_callback(self._tool, mon.events.PY_START, self._on_start)
            mon.register_callback(self._tool, mon.events.PY_RESUME, self._on_start)
            mon.set_events(self._tool, events)
        else:
            sys.setprofile(self._on_profile)
            threading.setprofile(self._on_profile)
        if not self._audit_installed:
            sys.addaudithook(self._on_audit)  # can't be removed; it checks self.files instead
            self._audit_installed = True

    def stop(self) -> None:
        if self.mode == "monitoring" and self._tool is not None:
            mon = sys.monitoring  # type: ignore[attr-defined]
            mon.set_events(self._tool, 0)
            mon.register_callback(self._tool, mon.events.PY_START, None)
            mon.register_callback(self._tool, mon.events.PY_RESUME, None)
            mon.free_tool_id(self._tool)
            self._tool = None
        elif self.mode != "monitoring":
            sys.setprofile(None)
            threading.setprofile(None)  # type: ignore[arg-type]
        self.current = self.files = None

    def begin_test(self) -> None:
        self.current, self.files = set(), set()
        if self.mode == "monitoring":
            sys.monitoring.restart_events()  # type: ignore[attr-defined]

    def end_test(self) -> tuple[set[str], set[str]]:
        funcs, files = self.current or set(), self.files or set()
        self.current = self.files = None
        return funcs, files

    # ---------------------------------------------------------------- callbacks

    def _on_start(self, code: CodeType, offset: int) -> Any:
        cur = self.current
        if cur is not None:
            key = self._key(code)
            if key is not None:
                cur.add(key)
        # Either way, this function needn't report again until the next test restarts events.
        return sys.monitoring.DISABLE  # type: ignore[attr-defined]

    def _on_profile(self, frame: FrameType, event: str, arg: Any) -> None:
        if event == "call" and self.current is not None:
            key = self._key(frame.f_code)
            if key is not None:
                self.current.add(key)

    def _on_audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or self.files is None:
            return
        path = args[0]
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            return  # file descriptors
        rel = self._rel(os.path.abspath(path))
        if rel is not None and not rel.endswith(".pyc") and "__pycache__" not in rel:
            self.files.add(rel)
