"""Settings from ``[tool.testpick]`` in pyproject.toml, with sensible defaults."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_RUN_ALL = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "noxfile.py",
    "requirements*.txt",
    "constraints*.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
]
DEFAULT_IGNORE = [
    "*.md",
    "*.rst",
    "*.txt",
    "docs/*",
    ".github/*",
    "LICENSE*",
    "CHANGELOG*",
    "AUTHORS*",
    ".gitignore",
    ".gitattributes",
    ".pre-commit-config.yaml",
    "*.png",
    "*.svg",
    "*.jpg",
]
DEFAULT_TEST_FILES = ["test_*.py", "*_test.py"]


@dataclass
class Config:
    map: str = ".testpick/map.json.gz"
    run_all: list[str] = field(default_factory=lambda: list(DEFAULT_RUN_ALL))
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    test_files: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_FILES))

    @staticmethod
    def load(repo: str) -> Config:
        cfg = Config()
        path = os.path.join(repo, "pyproject.toml")
        if not os.path.exists(path):
            return cfg
        with open(path, "rb") as f:
            section = tomllib.load(f).get("tool", {}).get("testpick", {})
        cfg.map = section.get("map", cfg.map)
        cfg.run_all = DEFAULT_RUN_ALL + section.get("run_all", [])
        cfg.ignore = section.get("ignore", DEFAULT_IGNORE)
        cfg.test_files = section.get("test_files", DEFAULT_TEST_FILES)
        return cfg
