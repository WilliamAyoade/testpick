"""Replay a project's real history to check testpick's choices.

For each commit (in order), testpick picks tests using one map recorded before the first
commit, the way a nightly map would be used. Then small bugs ("mutants") are planted on
lines the commit changed: a flipped comparison, ``and``/``or`` swapped, an altered string or
number, ``return None``. Each bug is run against:

  * the picked tests, through the pytest plugin, in testpick's order (most likely to fail
    first) and, when they catch it, again in pytest's normal order to compare how soon the
    first failure shows up;
  * the full suite, when the picked tests miss it, to tell a real miss (the full suite
    catches it) from a bug no test in the project can see.

Usage:
  python bench/evaluate.py REPO MAP BASE..HEAD OUT.json [--mutants 5] [--tests-dir tests] [--python PY]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import subprocess
import sys
import time

from testpick.config import Config
from testpick.gitdiff import changes, git
from testpick.select import select
from testpick.testmap import TestMap

SWAP = {
    ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.GtE: "<", ast.Gt: "<=", ast.LtE: ">",
    ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is",
}  # fmt: skip


def mutants_for_line(src: str, line: int) -> list[tuple[str, str]]:
    """(description, mutated source) for simple bugs on one line."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
        and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
    }  # fmt: skip
    out: list[tuple[str, str | None]] = []

    def replace(node: ast.AST, text: str) -> str | None:
        if node.lineno != node.end_lineno:  # type: ignore[attr-defined]
            return None
        b = lines[node.lineno - 1].encode()  # type: ignore[attr-defined]
        new = b[: node.col_offset].decode() + text + b[node.end_col_offset :].decode()  # type: ignore[attr-defined]
        return "".join(lines[: node.lineno - 1] + [new] + lines[node.lineno :])  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP:
            left, right = ast.get_source_segment(src, node.left), ast.get_source_segment(src, node.comparators[0])
            if left and right:
                op = SWAP[type(node.ops[0])]
                out.append((f"comparison flipped to {op}", replace(node, f"{left} {op} {right}")))
        elif isinstance(node, ast.BoolOp):
            parts = [ast.get_source_segment(src, v) for v in node.values]
            if all(parts):
                op = " or " if isinstance(node.op, ast.And) else " and "
                out.append(("and/or swapped", replace(node, "(" + op.join(p for p in parts if p) + ")")))
        elif isinstance(node, ast.Constant) and id(node) not in docstrings:
            if isinstance(node.value, bool):
                out.append(("boolean flipped", replace(node, str(not node.value))))
            elif isinstance(node.value, int):
                out.append(("number changed", replace(node, str(node.value + 1))))
            elif isinstance(node.value, str) and node.value:
                out.append(("string changed", replace(node, repr(node.value + "_X"))))
        elif isinstance(node, ast.Return) and node.value is not None:
            out.append(("returns None", replace(node, "return None")))
    good = []
    for desc, m in out:
        if m is None:
            continue
        try:
            compile(m, "<mutant>", "exec")
        except SyntaxError:
            continue
        good.append((desc, m))
    return good


PYTHONPATH: str | None = None


def run(wt: str, py: str, args: list[str], timeout: int = 400) -> tuple[int, float]:
    env = dict(os.environ)
    if PYTHONPATH:
        env["PYTHONPATH"] = os.path.join(wt, PYTHONPATH)
    t0 = time.time()
    try:
        r = subprocess.run(
            [py, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *args],
            cwd=wt,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        return r.returncode, round(time.time() - t0, 2)
    except subprocess.TimeoutExpired:
        return -1, round(time.time() - t0, 2)


def caught(rc: int) -> bool:
    return rc in (1, 2)  # tests failed, or collection broke (the bug breaks an import)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("map")
    ap.add_argument("range")
    ap.add_argument("out")
    ap.add_argument("--mutants", type=int, default=3, help="bugs planted per commit")
    ap.add_argument("--check-baseline", action="store_true", help="first check the picked tests pass on the unchanged commit")
    ap.add_argument("--tests-dir", default="tests")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--source", default=None, help="only plant bugs under this folder (default: anywhere but tests)")
    ap.add_argument("--pythonpath", default=None, help="folder inside the checkout to import from, e.g. src")
    a = ap.parse_args()
    global PYTHONPATH
    PYTHONPATH = a.pythonpath

    tmap = TestMap.load(a.map)
    cfg = Config.load(a.repo)
    full_seconds = sum(tmap.durations.values())
    base, head = a.range.split("..")
    commits = git(a.repo, "rev-list", "--first-parent", "--reverse", f"{base}..{head}").split()
    wt = os.path.abspath(a.repo.rstrip("/") + "-eval-wt")
    if not os.path.exists(wt):
        git(a.repo, "worktree", "add", "-q", "--detach", wt, commits[0])
    results = json.loads(open(a.out).read()) if os.path.exists(a.out) else []  # noqa: SIM115
    done = {r["commit"] for r in results}
    pick = [f"--testpick-map={os.path.abspath(a.map)}"]

    for c in commits:
        if c[:9] in done:
            continue
        parent = git(a.repo, "rev-parse", f"{c}^").strip()
        sel = select(a.repo, tmap, parent, c, cfg)
        chosen = sel.chosen(tmap)
        changed_py = [ch for ch in changes(a.repo, parent, c) if ch.path.endswith(".py")]
        file_level = set()  # what "tests that touch any changed file" would pick
        for ch in changed_py:
            file_level |= tmap.tests_for_file(ch.old_path or ch.path)
        file_level |= {t for t in tmap.tests if t.split("::")[0] in sel.test_files}
        rec: dict[str, object] = {
            "commit": c[:9],
            "subject": git(a.repo, "log", "-1", "--format=%s", c).strip(),
            "run_all": sel.run_all,
            "tests": len(chosen),
            "seconds": round(sel.estimated_seconds(tmap), 2),
            "file_level_tests": len(file_level) if not sel.run_all else len(tmap.tests),
            "file_level_seconds": round(sum(tmap.durations.get(t, 0) for t in file_level), 2)
            if not sel.run_all
            else full_seconds,
            "full_seconds": round(full_seconds, 2),
            "uncovered": sel.uncovered,
            "mutants": [],
        }
        git(wt, "checkout", "-q", "--detach", "--force", c)
        candidates = []
        for ch in changed_py:
            if ch.new_path is None or ch.new_path.startswith(a.tests_dir + "/"):
                continue
            if a.source and not ch.new_path.startswith(a.source + "/"):
                continue
            with open(os.path.join(wt, ch.new_path)) as f:
                src = f.read()
            for line in sorted(ch.new_lines):
                for desc, m in mutants_for_line(src, line):
                    candidates.append((ch.new_path, line, desc, src, m))
        rng = random.Random(c)
        rng.shuffle(candidates)
        seen_lines: set[tuple[str, int]] = set()
        planted = []
        for cand in candidates:  # spread bugs over different lines
            if (cand[0], cand[1]) not in seen_lines:
                planted.append(cand)
                seen_lines.add((cand[0], cand[1]))
            if len(planted) == a.mutants:
                break
        if planted and a.check_baseline:
            rc, secs = run(wt, a.python, [f"--testpick={parent}", *pick])
            rec["picked_run_seconds"] = secs
            if rc not in (0, 5):
                rec["note"] = f"picked tests fail on the unchanged commit (exit {rc}); bugs not planted"
                planted = []
        full_ok: bool | None = None
        for path, line, desc, src, m in planted:
            target = os.path.join(wt, path)
            with open(target, "w") as f:
                f.write(m)
            entry: dict[str, object] = {"file": path, "line": line, "bug": desc}
            try:
                rc, secs = run(wt, a.python, [f"--testpick={parent}", *pick])
                if caught(rc):
                    entry["verdict"] = "caught by picked tests"
                    entry["first_failure_s"] = secs
                else:
                    if full_ok is None:
                        with open(target, "w") as f:
                            f.write(src)
                        full_ok = run(wt, a.python, [a.tests_dir, "-n", "2"])[0] == 0
                        with open(target, "w") as f:
                            f.write(m)
                    if not full_ok:
                        entry["verdict"] = "skipped: the full suite already fails on this commit"
                    else:
                        rc3, _ = run(wt, a.python, [a.tests_dir, "-n", "2"])
                        entry["verdict"] = "MISSED: full suite catches it" if caught(rc3) else "no test catches it"
            finally:
                with open(target, "w") as f:
                    f.write(src)
            rec["mutants"].append(entry)  # type: ignore[attr-defined]
        results.append(rec)
        with open(a.out, "w") as f:
            json.dump(results, f, indent=1)
        print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    main()
