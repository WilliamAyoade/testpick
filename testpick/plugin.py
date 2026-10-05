"""pytest plugin.

    pytest --testpick-record                 record which functions/files each test uses (full run)
    pytest --testpick origin/main            run only tests affected by changes since origin/main
    pytest --testpick origin/main --testpick-record   ...and refresh those tests' entries in the map

Works with pytest-xdist: each worker records its own tests and the main process merges them.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Any

import pytest

from .config import Config
from .recorder import Recorder
from .select import Selection, select
from .testmap import TestMap

PARTS_ENV = "TESTPICK_PARTS_DIR"


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("testpick", "run only the tests a change can affect")
    g.addoption(
        "--testpick",
        metavar="BASE",
        dest="testpick_base",
        default=None,
        help="only run tests affected by changes since BASE (a branch, tag or commit), including uncommitted changes",
    )
    g.addoption(
        "--testpick-record",
        action="store_true",
        dest="testpick_record",
        default=False,
        help="record which functions and data files each test uses into the map",
    )
    g.addoption(
        "--testpick-map",
        dest="testpick_map",
        default=None,
        help="map file (default: [tool.testpick] map, or .testpick/map.json.gz)",
    )
    g.addoption(
        "--testpick-no-reorder",
        action="store_true",
        dest="testpick_no_reorder",
        default=False,
        help="keep pytest's order instead of running the tests most likely to fail first",
    )


class TestpickPlugin:
    def __init__(self, config: pytest.Config):
        self.config = config
        self.root = str(config.rootpath)
        self.cfg = Config.load(self.root)
        self.map_path = config.getoption("testpick_map") or os.path.join(self.root, self.cfg.map)
        self.base = config.getoption("testpick_base")
        self.record = config.getoption("testpick_record")
        self.worker = getattr(config, "workerinput", None) is not None
        # pytest-xdist main process: workers run (and record) the tests, this process merges
        self.xdist_main = (
            not self.worker
            and bool(getattr(config.option, "numprocesses", None))
            and (getattr(config.option, "dist", "no") != "no")
        )
        self.recorder: Recorder | None = None
        self.records: dict[str, tuple[set[str], set[str], float]] = {}
        self.selection: Selection | None = None
        self.tmap: TestMap | None = None
        self.summary: list[str] = []
        self.t0 = time.time()
        self.parts_dir: str | None = None
        if self.record:
            if self.xdist_main:
                self.parts_dir = tempfile.mkdtemp(prefix="testpick-")
                os.environ[PARTS_ENV] = self.parts_dir
            else:
                self.recorder = Recorder(self.root)
                self.recorder.start()

    # ------------------------------------------------------------------ selection

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
        if not self.base:
            return  # (with xdist this runs in each worker; they all reach the same selection)
        if not os.path.exists(self.map_path):
            self.summary.append(f"no map at {self.map_path}: running everything (create one with `pytest --testpick-record`)")
            return
        top = subprocess.run(["git", "-C", self.root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(self.root):
            self.summary.append(
                f"pytest's rootdir ({self.root}) is not the root of a git repository; running everything. "
                "Run pytest from the repository root, and pass option values with '=' (--testpick-map=PATH)."
            )
            return
        self.tmap = tmap = TestMap.load(self.map_path)
        sel = self.selection = select(self.root, tmap, self.base, None, self.cfg)
        known = set(tmap.tests)
        keep, drop, unknown = [], [], 0
        for item in items:
            if item.nodeid not in known:
                unknown += 1
                keep.append(item)  # new or renamed test: never skip what the map hasn't seen
            elif sel.includes(item.nodeid):
                keep.append(item)
            else:
                drop.append(item)
        if not config.getoption("testpick_no_reorder"):
            keep = _reorder(keep, lambda it: sel.priority(it.nodeid, tmap) if it.nodeid in known else (0, 0, 0.0))
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep
        msg = f"running {len(keep)} of {len(keep) + len(drop)} tests"
        if unknown:
            msg += f" ({unknown} not in the map, so always run)"
        self.summary.append(msg)
        if sel.run_all:
            self.summary.append(f"running everything: {sel.run_all}")
        self.summary.extend(f"warning: {w}" for w in sel.warnings)
        if sel.uncovered:
            shown = ", ".join(sel.uncovered[:5]) + (" ..." if len(sel.uncovered) > 5 else "")
            self.summary.append(f"changed code no test runs: {shown}")

    # ------------------------------------------------------------------ recording

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None):  # type: ignore[no-untyped-def]
        if self.recorder is None:
            yield
            return
        self.recorder.begin_test()
        t0 = time.perf_counter()
        try:
            yield
        finally:
            funcs, files = self.recorder.end_test()
            self.records[item.nodeid] = (funcs, files, time.perf_counter() - t0)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if not self.record:
            return
        if self.recorder is not None:
            self.recorder.stop()
        if self.worker:
            parts = os.environ.get(PARTS_ENV)
            if parts:
                wid = self.config.workerinput["workerid"]  # type: ignore[attr-defined]
                with open(os.path.join(parts, f"{wid}.json"), "w") as f:
                    json.dump({t: [sorted(a), sorted(b), d] for t, (a, b, d) in self.records.items()}, f)
            return
        if self.parts_dir:
            for name in os.listdir(self.parts_dir):
                with open(os.path.join(self.parts_dir, name)) as f:
                    for t, (a, b, d) in json.load(f).items():
                        self.records[t] = (set(a), set(b), d)
        if not self.records:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.map_path)), exist_ok=True)
        if self.base and os.path.exists(self.map_path):
            tmap = TestMap.load(self.map_path).updated(self.records)
            self.summary.append(f"updated {len(self.records)} tests in {self.map_path}")
        else:
            commit = _head(self.root)
            tmap = TestMap.from_records(
                commit, self.records, self.recorder.mode if self.recorder else "xdist", time.time() - self.t0
            )
            self.summary.append(f"recorded {len(self.records)} tests at {commit[:9]} into {self.map_path}")
            if _dirty(self.root):
                self.summary.append("warning: uncommitted changes; the map describes them, not the commit")
        tmap.save(self.map_path)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        for line in self.summary:
            terminalreporter.write_line(f"testpick: {line}")


def _reorder(items: list[pytest.Item], key: Any) -> list[pytest.Item]:
    """Likely failures first, without splitting a module or class apart.

    Moving single tests between modules or classes would make pytest tear down and set up
    module/class fixtures (and unittest setUpClass) again and again, which can cost more than
    the selection saves. So whole groups move, ordered by their most promising test, and tests
    are sorted inside each group."""
    groups: dict[str, list[pytest.Item]] = {}
    for it in items:
        parent = it.parent.nodeid if it.parent is not None else ""
        groups.setdefault(parent, []).append(it)
    by_module: dict[str, list[list[pytest.Item]]] = {}
    for parent, members in groups.items():
        members.sort(key=key)
        by_module.setdefault(parent.split("::")[0], []).append(members)
    modules = []
    for mod_groups in by_module.values():
        mod_groups.sort(key=lambda g: key(g[0]))
        modules.append(mod_groups)
    modules.sort(key=lambda gs: key(gs[0][0]))
    return [it for gs in modules for g in gs for it in g]


def _head(root: str) -> str:
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _dirty(root: str) -> bool:
    out = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True
    ).stdout
    return bool(out.strip())


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("testpick_base", None) or config.getoption("testpick_record", None):
        config.pluginmanager.register(TestpickPlugin(config), "testpick-session")
