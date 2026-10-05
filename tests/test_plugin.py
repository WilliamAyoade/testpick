"""End to end: record a map with the pytest plugin, change the project, check what runs."""

from __future__ import annotations

import json
import subprocess
import sys

from testpick.testmap import TestMap

ALL = {
    "tests/test_ops.py::test_add",
    "tests/test_ops.py::test_scale",
    "tests/test_ops.py::test_square",
    "tests/test_text.py::test_shout[hi]",
    "tests/test_text.py::test_shout[yo]",
    "tests/test_text.py::test_words",
}


def picked(project, base="main", *extra):
    r = project.pytest("-v", f"--testpick={base}", *extra)
    assert r.ret in (0, 1, 5), r.stdout.str()  # 1 = a test failed, 5 = nothing to run
    return project.ran(r), r


def load(project) -> TestMap:
    return TestMap.load(str(project.root / ".testpick/map.json.gz"))


def test_record_builds_the_map(project):
    r = project.record()
    r.assert_outcomes(passed=6)
    assert "testpick: recorded 6 tests" in r.stdout.str()
    m = load(project)
    assert set(m.tests) == ALL
    assert m.tests_for("calc/ops.py::scale") == {"tests/test_ops.py::test_scale"}
    assert m.tests_for("calc/ops.py::Shape.describe") == {"tests/test_ops.py::test_square"}
    assert m.tests_reading("data/words.txt") == {"tests/test_text.py::test_words"}
    # test code is recorded too, so helper and fixture changes can be traced
    assert m.tests_for("tests/test_ops.py::test_add") == {"tests/test_ops.py::test_add"}
    assert all(d >= 0 for d in m.durations.values())


def test_reformatting_runs_nothing(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return (a+b)  # same code, new layout")
    assert picked(project)[0] == set()


def test_editing_a_function_runs_only_its_tests(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return a + b + 0")
    ran, r = picked(project)
    assert ran == {"tests/test_ops.py::test_add"}
    assert "testpick: running 1 of 6 tests" in r.stdout.str()


def test_docs_only_change_runs_nothing(project):
    project.record()
    project.edit("README.md", "demo", "demo project")
    ran, r = picked(project)
    assert ran == set() and r.ret == 5


def test_module_constant_selects_tests_using_it_from_other_files(project):
    project.record()
    project.edit("calc/consts.py", "RATE = 2", "RATE = 2  # doubled")  # comment only: nothing
    assert picked(project)[0] == set()
    project.edit("calc/consts.py", "RATE = 2  # doubled", "RATE = 3")
    ran, _ = picked(project)
    # RATE is read inside ops.scale (via consts.RATE), so test_scale must run; test_add needn't.
    # (consts.py has no functions of its own, so the name lookup is what finds the user.)
    assert "tests/test_ops.py::test_scale" in ran
    assert "tests/test_text.py::test_words" not in ran


def test_class_attribute_change_reaches_subclass_users(project):
    project.record()
    project.edit("calc/ops.py", "sides = 4", "sides = 5")
    ran, _ = picked(project)
    assert "tests/test_ops.py::test_square" in ran
    assert "tests/test_text.py::test_words" not in ran


def test_data_file_change_runs_only_its_readers(project):
    project.record()
    project.write("data/words.txt", "alpha\nbeta\n")  # same words, different layout
    ran, _ = picked(project)
    assert ran == {"tests/test_text.py::test_words"}


def test_new_tests_and_new_parameters_always_run(project):
    project.record()
    project.edit("tests/test_text.py", '["hi", "yo"]', '["hi", "yo", "hey"]')
    project.write(
        "tests/test_new.py",
        """\
        def test_brand_new():
            assert True
        """,
    )
    ran, r = picked(project)
    assert {"tests/test_text.py::test_shout[hey]", "tests/test_new.py::test_brand_new"} <= ran
    assert "not in the map, so always run" in r.stdout.str()


def test_config_change_runs_everything(project):
    project.record()
    project.write("pyproject.toml", "[tool.pytest.ini_options]\n")
    ran, r = picked(project)
    assert ran == ALL
    assert "running everything: pyproject.toml changed" in r.stdout.str()


def test_conftest_module_level_change_runs_its_directory(project):
    project.write("tests/conftest.py", "VALUE = 1\n")
    project.commit()
    project.record()
    project.edit("tests/conftest.py", "VALUE = 1", "VALUE = 2")
    ran, _ = picked(project, "HEAD")
    assert ran == ALL


def test_renamed_module_is_traced_through_its_old_name(project):
    project.record()
    project.git("mv", "calc/text.py", "calc/strings.py")
    project.edit("tests/test_text.py", "from calc.text import", "from calc.strings import")
    project.commit("rename")
    project.edit("calc/strings.py", 'return s.upper() + "!"', 'return s.upper() + "!" * 1')
    project.commit("edit after rename")
    ran, _ = picked(project, "HEAD~1")
    assert ran == {"tests/test_text.py::test_shout[hi]", "tests/test_text.py::test_shout[yo]"}


def test_likely_failures_run_first(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return a - b")  # breaks test_add
    project.edit("tests/test_text.py", "def test_words():", "def test_words():\n    assert True")
    r = project.pytest("-v", "--testpick=main")
    order = [line.split(" ")[0] for line in r.outlines if " PASSED" in line or " FAILED" in line]
    # the edited test first, then the rest of its (changed) file, then tests of changed code
    assert order[0] == "tests/test_text.py::test_words"
    assert set(order[1:3]) == {"tests/test_text.py::test_shout[hi]", "tests/test_text.py::test_shout[yo]"}
    assert order[3:] == ["tests/test_ops.py::test_add"]


def test_selection_run_can_refresh_the_map(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return int.__add__(a, b) if False else scale(a) // 2 + b")
    project.pytest("--testpick=main", "--testpick-record")
    m = load(project)
    # test_add now also runs scale(); the map learned that without a full re-record
    assert "tests/test_ops.py::test_add" in m.tests_for("calc/ops.py::scale")
    assert len(m.tests) == 6


def test_xdist_records_the_same_map(project):
    project.record()
    serial = load(project)
    (project.root / ".testpick/map.json.gz").unlink()
    r = project.record("-n", "2")
    r.assert_outcomes(passed=6)
    parallel = load(project)
    assert parallel.tests == serial.tests
    assert parallel.functions == serial.functions
    assert parallel.files == serial.files


def test_stale_map_warns(project):
    project.git("checkout", "-q", "-b", "other")
    project.edit("README.md", "demo", "x")
    project.commit()
    project.record()  # recorded on a branch main doesn't contain
    project.git("checkout", "-q", "main")
    r = project.pytest("--testpick=main")
    assert "not an ancestor" in r.stdout.str()


def test_cli_select_explain_and_shard(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return a + b + 0")
    cli = [sys.executable, "-m", "testpick.cli"]
    out = subprocess.run(
        [*cli, "select", "--base", "main", "--json"], cwd=project.root, capture_output=True, text=True, check=True
    )
    data = json.loads(out.stdout)
    assert data["tests"] == 1 and data["selected"][0] == {"test": "tests/test_ops.py::test_add", "because": ["calc/ops.py::add"]}
    text = subprocess.run([*cli, "select", "--base", "main"], cwd=project.root, capture_output=True, text=True, check=True).stdout
    assert "1 of 6 tests" in text and "calc/ops.py::add: 1 tests" in text
    why = subprocess.run(
        [*cli, "explain", "tests/test_ops.py::test_add", "--base", "main"],
        cwd=project.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "calc/ops.py::add" in why
    skip = subprocess.run(
        [*cli, "explain", "tests/test_ops.py::test_scale", "--base", "main"],
        cwd=project.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert skip.startswith("skipped")
    shards = [
        subprocess.run(
            [*cli, "shard", "--n", "2", "--i", str(i)], cwd=project.root, capture_output=True, text=True, check=True
        ).stdout.split()
        for i in range(2)
    ]
    assert sorted(shards[0] + shards[1]) == sorted(ALL)


def test_profile_fallback_records_the_same_functions(project):
    project.record()
    fast = load(project)
    (project.root / ".testpick/map.json.gz").unlink()
    project.pytest("--testpick-record", TESTPICK_RECORDER="profile")
    slow = load(project)
    assert slow.recorder == "profile"
    assert {k: v for k, v in slow.functions.items() if not k.startswith("tests/")} == {
        k: v for k, v in fast.functions.items() if not k.startswith("tests/")
    }


def test_lazily_imported_module_counts_for_the_test_that_imports_it(project):
    project.write("calc/lazy.py", "TABLE = {'a': 1}\n")
    project.write(
        "tests/test_lazy.py",
        """\
        def test_lazy():
            from calc.lazy import TABLE  # imported during the test, not at collection

            assert TABLE["a"] == 1
        """,
    )
    project.commit()
    project.record()
    assert "tests/test_lazy.py::test_lazy" in load(project).tests_for("calc/lazy.py::<module>")
    project.edit("calc/lazy.py", "{'a': 1}", "{'a': 1, 'b': 2}")
    ran, _ = picked(project, "HEAD")
    assert ran == {"tests/test_lazy.py::test_lazy"}


def test_python_file_read_as_data_is_traced(project):
    project.write("data/case.py", "x = 1\n")
    project.write(
        "tests/test_cases.py",
        """\
        import pathlib


        def test_case_file():
            text = (pathlib.Path(__file__).parent.parent / "data" / "case.py").read_text()
            assert "x =" in text
        """,
    )
    project.commit()
    project.record()
    project.edit("data/case.py", "x = 1", "x = 2")
    ran, _ = picked(project, "HEAD")
    assert ran == {"tests/test_cases.py::test_case_file"}


def test_names_reexported_by_a_package_are_followed(project):
    project.write("calc/__init__.py", "from .consts import GREETING\n")
    project.write(
        "calc/greet.py",
        """\
        from calc import GREETING


        def greet(name):
            return f"{GREETING} {name}"
        """,
    )
    project.write(
        "tests/test_greet.py",
        """\
        from calc.greet import greet


        def test_greet():
            assert greet("bob").endswith("bob")
        """,
    )
    project.commit()
    project.record()
    project.edit("calc/consts.py", 'GREETING = "hi"', 'GREETING = "hello"')
    ran, _ = picked(project, "HEAD")
    # consts.GREETING -> re-exported by calc/__init__.py -> imported by greet.py -> test_greet
    assert ran == {"tests/test_greet.py::test_greet"}


def test_python_test_data_that_does_not_parse_runs_its_readers(project):
    project.write("data/broken_case.py", "x = (\n")  # deliberately invalid, like a formatter's test case
    project.write(
        "tests/test_broken.py",
        """\
        import pathlib


        def test_reads_case():
            assert (pathlib.Path(__file__).parent.parent / "data" / "broken_case.py").read_text()
        """,
    )
    project.commit()
    project.record()
    project.edit("data/broken_case.py", "x = (", "x = ((")
    ran, r = picked(project, "HEAD")
    assert ran == {"tests/test_broken.py::test_reads_case"}


def test_syntax_error_in_package_code_runs_everything(project):
    project.record()
    project.edit("calc/ops.py", "return a + b", "return a +")
    r = project.pytest("--testpick=main")
    assert "running everything: calc/ops.py does not parse" in r.stdout.str()


def test_new_python_test_case_file_is_data_not_a_reason_to_run_everything(project):
    # like black's tests/data/cases/*.py: discovered by listing a folder, never imported
    project.write("cases/one.py", "x = 1\n")
    project.write(
        "tests/test_cases.py",
        """\
        import pathlib

        import pytest

        CASES = sorted((pathlib.Path(__file__).parent.parent / "cases").glob("*.py"))


        @pytest.mark.parametrize("case", CASES, ids=lambda p: p.stem)
        def test_case(case):
            assert case.read_text()
        """,
    )
    project.commit()
    project.record()
    project.write("cases/two.py", "y = 2\n")
    ran, r = picked(project, "HEAD")
    assert ran == {"tests/test_cases.py::test_case[two]"}  # the new case is a new test id
    assert "running everything" not in r.stdout.str()
