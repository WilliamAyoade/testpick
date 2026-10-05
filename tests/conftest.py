from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


class Project:
    """A small git repo with a package and tests, for exercising testpick end to end."""

    def __init__(self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
        self.pt = pytester
        self.mp = monkeypatch
        self.root = Path(pytester.path)

    def write(self, path: str, text: str) -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))

    def read(self, path: str) -> str:
        return (self.root / path).read_text()

    def edit(self, path: str, old: str, new: str) -> None:
        text = self.read(path)
        assert old in text, f"{old!r} not in {path}"
        (self.root / path).write_text(text.replace(old, new, 1))

    def git(self, *args: str) -> str:
        return subprocess.run([*GIT, *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self, msg: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", msg, "--allow-empty")
        return self.git("rev-parse", "HEAD").strip()

    def pytest(self, *args: str, **env: str) -> pytest.RunResult:
        for k, v in env.items():
            self.mp.setenv(k, v)
        return self.pt.runpytest_subprocess("-p", "no:cacheprovider", *args)

    def record(self, *args: str) -> pytest.RunResult:
        r = self.pytest("--testpick-record", *args)
        assert (self.root / ".testpick/map.json.gz").exists(), r.stdout.str()
        return r

    def ran(self, result: pytest.RunResult) -> set[str]:
        return {line.split(" ")[0] for line in result.outlines if " PASSED" in line or " FAILED" in line}


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Project:
    p = Project(pytester, monkeypatch)
    p.write("calc/__init__.py", "")
    p.write(
        "calc/consts.py",
        """\
        RATE = 2
        GREETING = "hi"
        """,
    )
    p.write(
        "calc/ops.py",
        """\
        from calc import consts


        def add(a, b):
            return a + b


        def scale(x):
            return x * consts.RATE


        class Shape:
            sides = 0

            def describe(self):
                return f"{self.sides} sides"


        class Square(Shape):
            sides = 4
        """,
    )
    p.write(
        "calc/text.py",
        """\
        import os


        def shout(s):
            return s.upper() + "!"


        def load_words():
            here = os.path.dirname(__file__)
            with open(os.path.join(here, "..", "data", "words.txt")) as f:
                return f.read().split()
        """,
    )
    p.write("data/words.txt", "alpha beta\n")
    p.write("tests/__init__.py", "")
    p.write(
        "tests/test_ops.py",
        """\
        from calc.ops import Square, add, scale


        def test_add():
            assert add(1, 2) == 3


        def test_scale():
            assert scale(2) == 4


        def test_square():
            assert Square().describe() == "4 sides"
        """,
    )
    p.write(
        "tests/test_text.py",
        """\
        import pytest

        from calc.text import load_words, shout


        @pytest.mark.parametrize("word", ["hi", "yo"])
        def test_shout(word):
            assert shout(word) == word.upper() + "!"


        def test_words():
            assert load_words() == ["alpha", "beta"]
        """,
    )
    p.write("README.md", "# demo\n")
    p.write(".gitignore", ".testpick/\n__pycache__/\n")
    p.git("init", "-q", "-b", "main")
    p.commit("base")
    return p
