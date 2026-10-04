"""Decide which tests to run for the changes between two commits."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .functions import MODULE, FunctionIndex
from .gitdiff import changes, show
from .testmap import TestMap

# Changing any of these can affect every test.
RUN_ALL_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "requirements.txt", "conftest.py"}
# Changing these never affects test results.
IGNORE_EXT = {".md", ".rst", ".txt", ".html", ".css", ".png", ".svg", ".yml", ".yaml", ".json", ".lock", ".cfg-docs"}


@dataclass
class Selection:
    tests: set[str] = field(default_factory=set)
    test_files: set[str] = field(default_factory=set)   # run these files whole (changed or new tests)
    run_all: str | None = None                          # reason, if everything must run
    uncovered: list[str] = field(default_factory=list)  # changed functions no recorded test runs
    reasons: dict[str, str] = field(default_factory=dict)

    def pytest_args(self, tmap: TestMap) -> list[str]:
        if self.run_all:
            return [tmap.tests_dir]
        files = sorted(self.test_files)
        rest = sorted(t for t in self.tests if t.split("::")[0] not in self.test_files)
        return files + rest

    def estimated_seconds(self, tmap: TestMap) -> float:
        if self.run_all:
            return sum(tmap.durations.values())
        chosen = {t for t in tmap.durations if t.split("::")[0] in self.test_files} | self.tests
        return sum(tmap.durations.get(t, 0.0) for t in chosen)


def test_files_mentioning(repo: str, rev: str, tests_dir: str, filename: str) -> set[str]:
    import subprocess
    r = subprocess.run(["git", "-C", repo, "grep", "-l", "-F", os.path.splitext(filename)[0], rev, "--", f"{tests_dir}/*.py"],
                       capture_output=True, text=True)
    return {line.split(":", 1)[1] for line in r.stdout.splitlines() if os.path.basename(line).startswith("test_")}


def select(repo: str, tmap: TestMap, base: str, head: str) -> Selection:
    sel = Selection()
    src_prefix, tests_prefix = tmap.source + "/", tmap.tests_dir + "/"
    for ch in changes(repo, base, head):
        path = ch.new_path or ch.old_path or ""
        name, ext = os.path.basename(path), os.path.splitext(path)[1]
        if name in RUN_ALL_FILES:
            sel.run_all = f"{path} changed"
            continue
        if ext != ".py":
            if path.startswith(tests_prefix) and ch.new_path:
                # test data (fixtures): run the test files that mention it by name
                users = test_files_mentioning(repo, head, tmap.tests_dir, name)
                if users:
                    sel.test_files |= users
                    sel.reasons[path] = f"read by {len(users)} test files"
                else:
                    sel.run_all = f"test data changed and no test names it: {path}"
                continue
            if ext in IGNORE_EXT or path.startswith(("docs/", ".github/")):
                continue
            if path.startswith(src_prefix):
                sel.run_all = f"non-Python file in the package changed: {path}"
            continue
        if path.startswith(tests_prefix):
            if name.startswith("test_") and ch.new_path:
                sel.test_files.add(ch.new_path)
            elif name.startswith("test_"):
                pass  # deleted test file: nothing to run
            else:
                sel.run_all = f"shared test code changed: {path}"  # helpers, fixtures
            continue
        if not path.startswith(src_prefix):
            continue  # scripts, benchmarks, docs tooling
        if ch.old_path is None or ch.new_path is None:
            sel.run_all = f"module added or removed: {path}"
            continue
        keys: set[str] = set()
        for rev, p, lines in ((base, ch.old_path, ch.old_lines), (head, ch.new_path, ch.new_lines)):
            src = show(repo, rev, p)
            fi = FunctionIndex(src or "")
            if not fi.ok:
                sel.run_all = f"could not parse {p} at {rev[:9]}"
                break
            text = (src or "").splitlines()
            for line in lines:
                code = text[line - 1].strip() if 0 < line <= len(text) else ""
                if not code or code.startswith("#"):
                    continue  # blank lines and comments can't change behaviour
                keys.add(f"{p}::{fi.at(line)}")
        for key in sorted(keys):
            if key.endswith("::" + MODULE):
                # imports, constants, class attributes: anything using the file may change
                found = tmap.tests_for_file(key.rsplit("::", 1)[0])
            else:
                found = tmap.tests_for(key)
            if found:
                sel.tests |= found
                sel.reasons[key] = f"{len(found)} tests"
            else:
                sel.uncovered.append(key)
    return sel
