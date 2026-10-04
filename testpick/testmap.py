"""Build and load the test map: which tests run which functions, plus how long each test takes."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

from .functions import FunctionIndex
from .gitdiff import git


@dataclass
class TestMap:
    commit: str
    source: str              # source package folder, e.g. "sqlglot"
    tests_dir: str           # e.g. "tests"
    tests: list[str]         # pytest node ids
    durations: dict[str, float]
    covers: dict[str, list[int]]  # "path::qualname" -> indexes into tests
    full_seconds: float

    def tests_for(self, key: str) -> set[str]:
        return {self.tests[i] for i in self.covers.get(key, ())}

    def tests_for_file(self, path: str) -> set[str]:
        out: set[str] = set()
        prefix = path + "::"
        for k, idx in self.covers.items():
            if k.startswith(prefix):
                out.update(self.tests[i] for i in idx)
        return out

    def save(self, path: str):
        with gzip.open(path, "wt") as f:
            json.dump(self.__dict__, f)

    @staticmethod
    def load(path: str) -> "TestMap":
        with gzip.open(path, "rt") as f:
            return TestMap(**json.load(f))


def nodeid_from_junit(classname: str, name: str, repo: str) -> str:
    # "tests.dialects.test_bigquery.TestBigQuery" + "test_x" -> "tests/dialects/test_bigquery.py::TestBigQuery::test_x"
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        path = "/".join(parts[:i]) + ".py"
        if os.path.isfile(os.path.join(repo, path)):
            return "::".join([path, *parts[i:], name])
    return f"{classname}::{name}"


def build(repo: str, source: str, tests_dir: str, python: str = sys.executable, extra: list[str] | None = None) -> TestMap:
    """Runs the whole suite once under coverage, recording which test ran each line."""
    commit = git(repo, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory() as tmp:
        cov_file, junit = os.path.join(tmp, ".coverage"), os.path.join(tmp, "junit.xml")
        env = {**os.environ, "COVERAGE_FILE": cov_file}
        t0 = time.time()
        subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--cov={source}", "--cov-context=test",
             "--cov-report=", f"--junitxml={junit}", tests_dir, *(extra or [])],
            cwd=repo, env=env, check=False,
        )
        elapsed = time.time() - t0

        durations: dict[str, float] = {}
        for tc in ET.parse(junit).getroot().iter("testcase"):
            nid = nodeid_from_junit(tc.get("classname", ""), tc.get("name", ""), repo)
            durations[nid] = durations.get(nid, 0.0) + float(tc.get("time", 0))

        from coverage import CoverageData
        data = CoverageData(basename=cov_file)
        data.read()
        tests: list[str] = []
        index: dict[str, int] = {}
        covers: dict[str, set[int]] = defaultdict(set)
        for abs_path in data.measured_files():
            rel = os.path.relpath(abs_path, repo)
            with open(abs_path) as f:
                fi = FunctionIndex(f.read())
            by_line = data.contexts_by_lineno(abs_path)
            for line, contexts in by_line.items():
                key = f"{rel}::{fi.at(line)}"
                for ctx in contexts:
                    if not ctx:
                        continue  # lines run at import time, outside any test
                    nid = ctx.rsplit("|", 1)[0]
                    if nid not in index:
                        index[nid] = len(tests)
                        tests.append(nid)
                    covers[key].add(index[nid])
    unknown = [n for n in index if n not in durations]
    if len(unknown) > len(index) // 10:
        raise RuntimeError(f"{len(unknown)} covered tests have no timing; node ids don't line up: {unknown[:3]}")
    for nid in durations:
        if nid not in index:  # tests that touched no source lines still exist
            index[nid] = len(tests)
            tests.append(nid)
    return TestMap(commit, source, tests_dir, tests, durations, {k: sorted(v) for k, v in covers.items()}, elapsed)
