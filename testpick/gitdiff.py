"""Changed lines between two commits, from ``git diff -U0``."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileChange:
    old_path: str | None  # None for an added file
    new_path: str | None  # None for a deleted file
    old_lines: set[int] = field(default_factory=set)  # removed or modified, numbered as in the old file
    new_lines: set[int] = field(default_factory=set)  # added or modified, numbered as in the new file

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def renamed(self) -> bool:
        return bool(self.old_path and self.new_path and self.old_path != self.new_path)


class GitError(RuntimeError):
    pass


def git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git -C {repo} {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _unquote(p: str) -> str:
    # git quotes paths with unusual characters: "a/caf\303\251.py"
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
    return p


def changes(repo: str, base: str, head: str | None) -> list[FileChange]:
    """Files and line ranges changed from ``base`` to ``head`` (or to the working tree if ``head`` is None)."""
    revs = [base] if head is None else [f"{base}..{head}"]
    out = git(repo, "diff", "-U0", "-M", "--no-color", "--no-ext-diff", *revs)
    files: list[FileChange] = []
    cur: FileChange | None = None
    for line in out.splitlines():
        if line.startswith("diff --git"):
            cur = FileChange(None, None)
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("--- "):
            cur.old_path = None if line[4:] == "/dev/null" else _unquote(line[4:])[2:]
        elif line.startswith("+++ "):
            cur.new_path = None if line[4:] == "/dev/null" else _unquote(line[4:])[2:]
        elif line.startswith("rename from "):
            cur.old_path = _unquote(line[len("rename from ") :])
        elif line.startswith("rename to "):
            cur.new_path = _unquote(line[len("rename to ") :])
        elif m := HUNK.match(line):
            o, oc, n, nc = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            # A pure insertion has oc == 0, a pure deletion nc == 0. Inserted lines are looked up
            # in the new file, so a line added inside a function is charged to that function.
            cur.old_lines.update(range(o, o + oc))
            cur.new_lines.update(range(n, n + nc))
    if head is None:  # new files that aren't committed or staged yet
        for p in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if p:
                try:
                    with open(f"{repo}/{p}", "rb") as f:
                        count = f.read().count(b"\n") + 1
                except OSError:
                    continue
                files.append(FileChange(None, p, set(), set(range(1, count + 1))))
    return files


def show(repo: str, rev: str | None, path: str) -> str | None:
    """A file's contents at ``rev``, or in the working tree if ``rev`` is None."""
    if rev is None:
        try:
            with open(f"{repo}/{path}", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    try:
        return git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def is_ancestor(repo: str, maybe_ancestor: str, rev: str) -> bool:
    r = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", maybe_ancestor, rev], capture_output=True)
    return r.returncode == 0
