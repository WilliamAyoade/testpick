"""Changed line ranges between two commits, from `git diff -U0`."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileChange:
    old_path: str | None
    new_path: str | None
    # lines removed or modified, numbered as in the old file
    old_lines: set[int] = field(default_factory=set)
    # lines added or modified, numbered as in the new file
    new_lines: set[int] = field(default_factory=set)


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def changes(repo: str, base: str, head: str) -> list[FileChange]:
    out = git(repo, "diff", "-U0", "--no-renames", "--no-color", f"{base}..{head}")
    files: list[FileChange] = []
    cur: FileChange | None = None
    for line in out.splitlines():
        if line.startswith("diff --git"):
            cur = FileChange(None, None)
            files.append(cur)
        elif cur is not None and line.startswith("--- "):
            cur.old_path = None if line[4:] == "/dev/null" else line[6:]
        elif cur is not None and line.startswith("+++ "):
            cur.new_path = None if line[4:] == "/dev/null" else line[6:]
        elif cur is not None and (m := HUNK.match(line)):
            o, oc, n, nc = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            # a pure insertion has oc == 0 and a pure deletion nc == 0; the inserted lines are
            # looked up in the new file, so a new line inside a function is charged to that function
            cur.old_lines.update(range(o, o + oc))
            cur.new_lines.update(range(n, n + nc))
    return files


def show(repo: str, rev: str, path: str) -> str | None:
    try:
        return git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None
