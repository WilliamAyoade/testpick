from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from testpick.functions import FunctionIndex, normalize_qualname
from testpick.gitdiff import changes
from testpick.recorder import Recorder
from testpick.shard import by_count, by_duration
from testpick.testmap import TestMap


def test_normalize_qualname():
    assert normalize_qualname("add") == "add"
    assert normalize_qualname("A.m") == "A.m"
    assert normalize_qualname("f.<locals>.g") == "f.g"
    assert normalize_qualname("A.m.<locals>.<lambda>") == "A.m"
    assert normalize_qualname("f.<locals>.Inner.m") == "f.Inner.m"
    assert normalize_qualname("<lambda>") == "<module>"


def test_function_index_finds_innermost_function():
    src = textwrap.dedent(
        """\
        x = 1

        class A:
            y = 2
            @staticmethod
            def f():
                def g():
                    return 1
                return g
        """
    )
    fi = FunctionIndex(src)
    assert fi.at(1) == "<module>"
    assert fi.at(4) == "<module>"  # class body runs at import time
    assert fi.at(5) == "A.f"  # the decorator belongs to the function
    assert fi.at(8) == "A.f.g"
    assert fi.at(9) == "A.f"
    assert not FunctionIndex("def broken(:\n").ok


def test_diff_lines_renames_and_working_tree(project):
    project.edit("calc/ops.py", "return a + b", "return b + a")
    project.commit()
    [ch] = changes(str(project.root), "HEAD~1", "HEAD")
    assert ch.old_path == ch.new_path == "calc/ops.py" and 5 in ch.old_lines and 5 in ch.new_lines

    project.git("mv", "calc/text.py", "calc/strings.py")
    project.edit("calc/strings.py", 'return s.upper() + "!"', 'return s.upper() + "!!"')
    project.commit()
    [ch] = changes(str(project.root), "HEAD~1", "HEAD")
    assert ch.renamed and (ch.old_path, ch.new_path) == ("calc/text.py", "calc/strings.py")

    project.edit("calc/consts.py", "RATE = 2", "RATE = 5")  # uncommitted
    [ch] = changes(str(project.root), "HEAD", None)
    assert ch.path == "calc/consts.py" and ch.new_lines == {1}


def test_sharding_by_time_beats_sharding_by_count():
    durations = {f"t{i:02d}": (30.0 if i < 4 else 1.0) for i in range(40)}
    tests = sorted(durations)

    def slowest(shards: list[list[str]]) -> float:
        return max(sum(durations[t] for t in s) for s in shards)

    assert slowest(by_duration(tests, durations, 4)) == 39.0  # one slow test + nine fast each
    assert slowest(by_count(tests, 4)) == 126.0  # the four slow tests land on one machine
    assert sorted(t for s in by_duration(tests, durations, 4) for t in s) == tests


def test_map_round_trip_and_update(tmp_path):
    m = TestMap.from_records(
        "abc",
        {"t::a": ({"x.py::f"}, {"d.txt"}, 0.5), "t::b": ({"x.py::f", "x.py::g"}, set(), 1.0)},
        "monitoring",
        2.0,
    )
    m.save(str(tmp_path / "m.json.gz"))
    m2 = TestMap.load(str(tmp_path / "m.json.gz"))
    assert m2.tests_for("x.py::f") == {"t::a", "t::b"}
    assert m2.tests_for_file("x.py") == {"t::a", "t::b"}
    assert m2.tests_reading("d.txt") == {"t::a"}
    m3 = m2.updated({"t::a": ({"x.py::g"}, set(), 0.4)})
    assert m3.tests_for("x.py::f") == {"t::b"} and m3.tests_for("x.py::g") == {"t::a", "t::b"}
    assert m3.tests_reading("d.txt") == set()


MODULE = """\
import os

def outer():
    def inner():
        return [x for x in range(3)]
    return inner()

class C:
    def method(self):
        return (lambda: 1)()

def gen():
    yield 1
    yield 2

def reads(path):
    with open(path) as f:
        return f.read()

def unused():
    return 0
"""


@pytest.mark.parametrize("mode", ["monitoring", "profile"] if sys.version_info >= (3, 12) else ["profile"])
def test_recorder_sees_each_tests_functions_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    pkg = f"pkg_{mode}"
    (tmp_path / pkg).mkdir()
    (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / pkg / f"mod_{mode}.py").write_text(textwrap.dedent(MODULE))
    (tmp_path / "data.txt").write_text("hello")
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = importlib.import_module(f"{pkg}.mod_{mode}")
    rel = f"{pkg}/mod_{mode}.py"

    rec = Recorder(str(tmp_path), mode=mode)
    rec.start()
    try:
        rec.begin_test()
        mod.outer()
        mod.C().method()
        g = mod.gen()
        next(g)
        first, files1 = rec.end_test()

        rec.begin_test()  # the generator resumes in the second test: it counts there too
        next(g)
        mod.reads(str(tmp_path / "data.txt"))
        mod.outer()  # functions called again in a new test are seen again
        second, files2 = rec.end_test()
    finally:
        rec.stop()
    assert first == {f"{rel}::outer", f"{rel}::outer.inner", f"{rel}::C.method", f"{rel}::gen"}
    assert files1 == set()
    expected_second = {f"{rel}::reads", f"{rel}::outer", f"{rel}::outer.inner"}
    if mode == "monitoring":
        expected_second.add(f"{rel}::gen")  # PY_RESUME; the profile hook can't tell resumes apart
    assert expected_second <= second
    assert f"{rel}::unused" not in first | second
    assert files2 == {"data.txt"}


def test_reorder_moves_whole_classes_and_modules():
    from types import SimpleNamespace

    from testpick.plugin import _reorder

    def item(nodeid: str):
        parent = nodeid.rsplit("::", 1)[0]
        return SimpleNamespace(nodeid=nodeid, parent=SimpleNamespace(nodeid=parent))

    items = [item(n) for n in ["a.py::A::t1", "a.py::A::t2", "a.py::B::t1", "b.py::t1", "b.py::t2"]]
    score = {"a.py::B::t1": 0, "b.py::t2": 1}  # lower runs first
    out = [i.nodeid for i in _reorder(items, lambda i: score.get(i.nodeid, 9))]  # type: ignore[arg-type]
    # a.py holds the best test, so it goes first, with class B before A; classes stay together
    assert out == ["a.py::B::t1", "a.py::A::t1", "a.py::A::t2", "b.py::t2", "b.py::t1"]
