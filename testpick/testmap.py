"""The test map: which functions and data files each test used, and how long each test took."""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field

FORMAT = 2


@dataclass
class TestMap:
    __test__ = False  # not a pytest test class

    commit: str  # commit the map was recorded at
    tests: list[str]  # pytest node ids
    durations: dict[str, float]  # node id -> seconds (setup + call + teardown)
    functions: dict[str, list[int]]  # "path::qualname" -> indexes into tests
    files: dict[str, list[int]]  # data file path -> indexes into tests
    recorder: str = "monitoring"
    created: float = field(default_factory=time.time)
    record_seconds: float = 0.0  # wall time of the recording run
    format: int = FORMAT

    # ------------------------------------------------------------------ lookups

    def tests_for(self, key: str) -> set[str]:
        return {self.tests[i] for i in self.functions.get(key, ())}

    def tests_for_file(self, path: str) -> set[str]:
        """Tests that ran any function defined in ``path``."""
        prefix = path + "::"
        idx: set[int] = set()
        for k, v in self.functions.items():
            if k.startswith(prefix):
                idx.update(v)
        return {self.tests[i] for i in idx}

    def tests_reading(self, path: str) -> set[str]:
        return {self.tests[i] for i in self.files.get(path, ())}

    def knows_path(self, path: str) -> bool:
        return any(k.startswith(path + "::") for k in self.functions)

    # ------------------------------------------------------------------ building

    @staticmethod
    def from_records(commit: str, records: dict[str, tuple[set[str], set[str], float]], recorder: str, seconds: float) -> TestMap:
        tests = sorted(records)
        index = {t: i for i, t in enumerate(tests)}
        functions: dict[str, set[int]] = defaultdict(set)
        files: dict[str, set[int]] = defaultdict(set)
        durations: dict[str, float] = {}
        for t, (funcs, data, dur) in records.items():
            i = index[t]
            for k in funcs:
                functions[k].add(i)
            for f in data:
                files[f].add(i)
            durations[t] = round(dur, 4)
        return TestMap(
            commit,
            tests,
            durations,
            {k: sorted(v) for k, v in sorted(functions.items())},
            {k: sorted(v) for k, v in sorted(files.items())},
            recorder,
            time.time(),
            round(seconds, 2),
        )

    def records(self) -> dict[str, tuple[set[str], set[str], float]]:
        out: dict[str, tuple[set[str], set[str], float]] = {t: (set(), set(), self.durations.get(t, 0.0)) for t in self.tests}
        for k, idx in self.functions.items():
            for i in idx:
                out[self.tests[i]][0].add(k)
        for f, idx in self.files.items():
            for i in idx:
                out[self.tests[i]][1].add(f)
        return out

    def updated(self, fresh: dict[str, tuple[set[str], set[str], float]]) -> TestMap:
        """A copy where the given tests' entries are replaced by newly recorded ones."""
        rec = self.records()
        rec.update(fresh)
        m = TestMap.from_records(self.commit, rec, self.recorder, self.record_seconds)
        m.created = time.time()
        return m

    # ------------------------------------------------------------------ storage

    def save(self, path: str) -> None:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(self.__dict__, f, separators=(",", ":"))

    @staticmethod
    def load(path: str) -> TestMap:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") != FORMAT:
            raise ValueError(f"{path} was written by another testpick version; record it again with `testpick record`")
        return TestMap(**data)
