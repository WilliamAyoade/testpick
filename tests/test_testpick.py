"""Tests on a tiny throwaway repo: a package with two modules and tests for each."""
import os
import subprocess
import textwrap

import pytest

from testpick.functions import FunctionIndex
from testpick.gitdiff import changes
from testpick.select import select
from testpick.shard import by_count, by_duration
from testpick.testmap import build


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def write(root, path, text):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "calc/__init__.py", "")
    write(tmp_path, "calc/ops.py", """\
        RATE = 2

        def add(a, b):
            return a + b

        def scale(x):
            return x * RATE

        class Box:
            def size(self):
                return 3
    """)
    write(tmp_path, "calc/text.py", """\
        def shout(s):
            return s.upper() + "!"
    """)
    write(tmp_path, "tests/__init__.py", "")
    write(tmp_path, "tests/test_ops.py", """\
        from calc.ops import add, scale, Box

        def test_add():
            assert add(1, 2) == 3

        def test_scale():
            assert scale(2) == 4

        def test_box():
            assert Box().size() == 3
    """)
    write(tmp_path, "tests/test_text.py", """\
        from calc.text import shout

        def test_shout():
            assert shout("hi") == "HI!"
    """)
    write(tmp_path, "README.md", "hello\n")
    sh(tmp_path, "git", "init", "-q")
    sh(tmp_path, "git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "base", "--allow-empty")
    sh(tmp_path, "git", "add", "-A")
    sh(tmp_path, "git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base")
    return tmp_path


def commit(repo, msg="change"):
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", msg)


def test_function_index_finds_innermost_function():
    fi = FunctionIndex("x = 1\n\nclass A:\n    y = 2\n    @staticmethod\n    def f():\n        def g():\n            return 1\n        return g\n")
    assert fi.at(1) == "<module>"
    assert fi.at(4) == "<module>"  # class body
    assert fi.at(5) == "A.f"       # decorator belongs to the function
    assert fi.at(8) == "A.f.g"
    assert fi.at(9) == "A.f"


def test_diff_reports_old_and_new_lines(repo):
    p = repo / "calc/ops.py"
    p.write_text(p.read_text().replace("return a + b", "return b + a"))
    commit(repo)
    [ch] = changes(str(repo), "HEAD~1", "HEAD")
    assert ch.old_path == ch.new_path == "calc/ops.py"
    assert 4 in ch.old_lines and 4 in ch.new_lines


def test_map_and_select_only_the_affected_tests(repo):
    tmap = build(str(repo), "calc", "tests")
    assert set(tmap.durations) == {
        "tests/test_ops.py::test_add", "tests/test_ops.py::test_scale",
        "tests/test_ops.py::test_box", "tests/test_text.py::test_shout"}
    # edit inside one function -> only its test
    p = repo / "calc/ops.py"
    p.write_text(p.read_text().replace("return a + b", "return (a + b)"))
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert sel.tests == {"tests/test_ops.py::test_add"} and not sel.run_all

    # a module-level constant -> every test that touches that file
    p.write_text(p.read_text().replace("RATE = 2", "RATE = 3"))
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert sel.tests == {"tests/test_ops.py::test_add", "tests/test_ops.py::test_scale", "tests/test_ops.py::test_box"}

    # docs only -> nothing
    (repo / "README.md").write_text("changed\n")
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert not sel.tests and not sel.test_files and not sel.run_all


def test_new_function_and_line_shifts_still_resolve_by_name(repo):
    tmap = build(str(repo), "calc", "tests")
    p = repo / "calc/ops.py"
    # add a new uncovered function at the top, shifting every line below, then edit Box.size
    src = p.read_text().replace("RATE = 2\n", "RATE = 2\n\ndef unused(z):\n    return z\n\n")
    p.write_text(src.replace("return 3", "return 1 + 2"))
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert "tests/test_ops.py::test_box" in sel.tests
    assert "tests/test_ops.py::test_add" not in sel.tests
    assert "calc/ops.py::unused" in sel.uncovered


def test_changed_tests_run_whole_file_and_config_runs_all(repo):
    tmap = build(str(repo), "calc", "tests")
    write(repo, "tests/test_text.py", (repo / "tests/test_text.py").read_text() + "\ndef test_new():\n    assert True\n")
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert sel.test_files == {"tests/test_text.py"}
    assert sel.estimated_seconds(tmap) >= 0
    write(repo, "pyproject.toml", "[project]\nname='x'\n")
    commit(repo)
    assert select(str(repo), tmap, "HEAD~1", "HEAD").run_all == "pyproject.toml changed"
    write(repo, "tests/fixtures/cases.sql", "SELECT 1\n")
    commit(repo)
    assert "test data changed" in select(str(repo), tmap, "HEAD~1", "HEAD").run_all
    write(repo, "calc/new_mod.py", "X = 1\n")
    commit(repo)
    assert "module added" in select(str(repo), tmap, "HEAD~1", "HEAD").run_all


def test_test_data_runs_the_files_that_read_it(repo):
    write(repo, "tests/data/cases.csv", "a,b\n")
    write(repo, "tests/test_text.py", (repo / "tests/test_text.py").read_text() + "\nDATA = 'data/cases.csv'\n")
    commit(repo)
    tmap = build(str(repo), "calc", "tests")
    (repo / "tests/data/cases.csv").write_text("a,b\n1,2\n")
    commit(repo)
    sel = select(str(repo), tmap, "HEAD~1", "HEAD")
    assert sel.test_files == {"tests/test_text.py"} and not sel.run_all
    write(repo, "tests/data/orphan.json", "{}")
    commit(repo)
    assert "no test names it" in select(str(repo), tmap, "HEAD~1", "HEAD").run_all


def test_sharding_by_time_beats_sharding_by_count():
    durations = {f"t{i:02d}": (30.0 if i < 4 else 1.0) for i in range(40)}
    tests = sorted(durations)
    def worst(shards):
        return max(sum(durations[t] for t in s) for s in shards)
    assert worst(by_duration(tests, durations, 4)) == 39.0  # one slow test + 9 fast each
    assert worst(by_count(tests, 4)) == 126.0               # all four slow tests land together
    assert sorted(t for s in by_duration(tests, durations, 4) for t in s) == tests
