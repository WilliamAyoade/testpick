"""Replays a project's real commit history to check testpick's choices.

For each commit: pick tests with a map recorded at an older commit (as a nightly map would
be), then plant small bugs ("mutants") on the lines that commit changed, e.g. flipping a
comparison or altering a string. A bug counts as caught by the picked tests if they fail on
it. If they don't, the full suite runs too: a bug the full suite catches but the picked
tests miss is a real miss for testpick.

Usage: python bench/evaluate.py REPO MAP BASE..HEAD OUT.json [--mutants-per-commit 3]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import subprocess
import sys
import tempfile
import time

from testpick.gitdiff import changes, git
from testpick.select import select
from testpick.testmap import TestMap

SWAP = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.GtE: "<", ast.Gt: "<=", ast.LtE: ">",
        ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is"}


def mutants_for_line(src: str, line: int) -> list[tuple[str, str]]:
    """(description, mutated source) for simple bugs on one line."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []

    def replace(node, new_text):
        if node.lineno != node.end_lineno:
            return None
        text = lines[node.lineno - 1]
        b = text.encode()
        mutated = b[:node.col_offset].decode() + new_text + b[node.end_col_offset:].decode()
        return "".join(lines[:node.lineno - 1] + [mutated] + lines[node.lineno:])

    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        seg = ast.get_source_segment(src, node)
        if seg is None:
            continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP:
            left, right = ast.get_source_segment(src, node.left), ast.get_source_segment(src, node.comparators[0])
            if left and right:
                out.append((f"comparison -> {SWAP[type(node.ops[0])]}", replace(node, f"{left} {SWAP[type(node.ops[0])]} {right}")))
        elif isinstance(node, ast.BoolOp):
            op = " or " if isinstance(node.op, ast.And) else " and "
            parts = [ast.get_source_segment(src, v) for v in node.values]
            if all(parts):
                out.append(("and/or swapped", replace(node, "(" + op.join(parts) + ")")))
        elif isinstance(node, ast.Constant) and id(node) not in docstrings:
            if isinstance(node.value, bool):
                out.append(("boolean flipped", replace(node, str(not node.value))))
            elif isinstance(node.value, int):
                out.append(("number + 1", replace(node, str(node.value + 1))))
            elif isinstance(node.value, str) and node.value:
                out.append(("string changed", replace(node, repr(node.value + "_X"))))
        elif isinstance(node, ast.Return) and node.value is not None:
            out.append(("returns None", replace(node, "return None")))
    good = []
    for desc, m in out:
        if m is None:
            continue
        try:
            compile(m, "<m>", "exec")
            good.append((desc, m))
        except SyntaxError:
            pass
    return good


def pytest(wt: str, args: list[str], extra: list[str], timeout=900) -> tuple[int, float]:
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as f:
        f.write("\n".join(args))
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *extra, f"@{f.name}"],
                           cwd=wt, capture_output=True, timeout=timeout)
        return r.returncode, time.time() - t0
    except subprocess.TimeoutExpired:
        return -1, time.time() - t0
    finally:
        os.unlink(f.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("map")
    ap.add_argument("range")
    ap.add_argument("out")
    ap.add_argument("--mutants-per-commit", type=int, default=3)
    ap.add_argument("--mutant-commits", type=int, default=1000, help="plant bugs in only this many commits; the rest get selection stats only")
    a = ap.parse_args()
    tmap = TestMap.load(a.map)
    base, head = a.range.split("..")
    commits = git(a.repo, "rev-list", "--first-parent", "--reverse", f"{base}..{head}").split()
    wt = os.path.abspath(a.repo.rstrip("/") + "-eval-wt")
    if not os.path.exists(wt):
        git(a.repo, "worktree", "add", "-q", "--detach", wt, commits[0])
    results = json.load(open(a.out)) if os.path.exists(a.out) else []
    done = {r["commit"] for r in results}
    for c in commits:
        if c in done:
            continue
        parent = git(a.repo, "rev-parse", f"{c}^").strip()
        subject = git(a.repo, "log", "-1", "--format=%s", c).strip()
        sel = select(a.repo, tmap, parent, c)
        args = sel.pytest_args(tmap)
        rec = {"commit": c[:9], "subject": subject, "run_all": sel.run_all, "selected_tests": len(sel.tests),
               "test_files": len(sel.test_files), "estimated_seconds": round(sel.estimated_seconds(tmap), 2),
               "full_seconds": round(sum(tmap.durations.values()), 2), "uncovered": len(sel.uncovered), "mutants": []}
        git(wt, "checkout", "-q", "--detach", c)
        src_changes = [ch for ch in changes(a.repo, parent, c)
                       if ch.new_path and ch.new_path.startswith(tmap.source + "/") and ch.new_path.endswith(".py")]
        candidates = []
        for ch in src_changes:
            src = open(os.path.join(wt, ch.new_path)).read()
            for line in sorted(ch.new_lines):
                for desc, m in mutants_for_line(src, line):
                    candidates.append((ch.new_path, line, desc, src, m))
        rng = random.Random(c)
        rng.shuffle(candidates)
        with_mutants = sum(1 for r in results if r["mutants"])
        picked = candidates[: a.mutants_per_commit] if with_mutants < a.mutant_commits else []
        if sel.run_all:
            picked = []  # everything runs anyway; nothing to compare
        if picked and args:
            rc, secs = pytest(wt, args, [])
            rec["selected_run_seconds"] = round(secs, 1)
            if rc != 0:
                rec["note"] = "picked tests already fail on this commit; mutants skipped"
                picked = []
        full_ok = None
        for path, line, desc, src, m in picked:
            full_path = os.path.join(wt, path)
            with open(full_path, "w") as f:
                f.write(m)
            try:
                if args:
                    rc, _ = pytest(wt, args, [])
                    caught_sel = rc != 0
                else:
                    caught_sel = False
                verdict = "caught by picked tests"
                if not caught_sel:
                    if full_ok is None:
                        with open(full_path, "w") as f:
                            f.write(src)
                        full_ok = pytest(wt, [tmap.tests_dir], ["-n", "2"])[0] == 0
                        with open(full_path, "w") as f:
                            f.write(m)
                    if not full_ok:
                        verdict = "skipped: full suite fails on this commit"
                    else:
                        rc, _ = pytest(wt, [tmap.tests_dir], ["-n", "2"])
                        verdict = "MISSED: full suite catches it" if rc != 0 else "no test catches it"
            finally:
                with open(full_path, "w") as f:
                    f.write(src)
            rec["mutants"].append({"file": path, "line": line, "bug": desc, "verdict": verdict})
        results.append(rec)
        json.dump(results, open(a.out, "w"), indent=1)
        print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    main()
