"""Command line: testpick record | select | explain | run | shard."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__
from .config import Config
from .gitdiff import git
from .select import select
from .shard import by_duration
from .testmap import TestMap


def _root(path: str) -> str:
    return git(path, "rev-parse", "--show-toplevel").strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="testpick", description="Run only the tests a change can affect.")
    ap.add_argument("--version", action="version", version=f"testpick {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="run the whole suite once, recording what each test uses")
    r.add_argument("pytest_args", nargs=argparse.REMAINDER, help="extra pytest arguments, e.g. -n 4")

    for name, text in (("select", "show which tests a change needs, and why"), ("run", "run only those tests")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--base", default="origin/main", help="compare against this commit or branch (default: origin/main)")
        if name == "select":
            s.add_argument("--head", default=None, help="compare up to this commit (default: the working tree)")
            s.add_argument("--json", action="store_true", help="machine-readable output")
            s.add_argument("--list", action="store_true", help="print the pytest node ids, one per line")
        else:
            s.add_argument("pytest_args", nargs=argparse.REMAINDER)

    e = sub.add_parser("explain", help="why a test was (or wasn't) picked")
    e.add_argument("test", help="pytest node id")
    e.add_argument("--base", default="origin/main")

    sh = sub.add_parser("shard", help="list the tests for machine I of N, balanced by recorded time")
    sh.add_argument("--n", type=int, required=True)
    sh.add_argument("--i", type=int, required=True, help="0-based")

    for p in sub.choices.values():
        p.add_argument("--map", default=None, help="map file (default from [tool.testpick], else .testpick/map.json.gz)")
    a = ap.parse_args(argv)

    root = _root(".")
    cfg = Config.load(root)
    map_path = a.map or os.path.join(root, cfg.map)

    if a.cmd == "record":
        return subprocess.run(
            [sys.executable, "-m", "pytest", "--testpick-record", f"--testpick-map={map_path}", *a.pytest_args], cwd=root
        ).returncode
    if a.cmd == "run":
        return subprocess.run(
            [sys.executable, "-m", "pytest", f"--testpick={a.base}", f"--testpick-map={map_path}", *a.pytest_args], cwd=root
        ).returncode

    if not os.path.exists(map_path):
        print(f"no map at {map_path}; create one with `testpick record`", file=sys.stderr)
        return 2
    tmap = TestMap.load(map_path)

    if a.cmd == "shard":
        if not 0 <= a.i < a.n:
            print("--i must be between 0 and --n - 1", file=sys.stderr)
            return 2
        print("\n".join(by_duration(sorted(tmap.durations), tmap.durations, a.n)[a.i]))
        return 0

    sel = select(root, tmap, a.base, getattr(a, "head", None), cfg)
    chosen = sorted(sel.chosen(tmap), key=lambda t: sel.priority(t, tmap))

    if a.cmd == "explain":
        if a.test not in set(tmap.tests):
            print(f"{a.test} is not in the map, so it always runs (new or renamed test)")
        elif sel.run_all:
            print(f"runs: everything runs because {sel.run_all}")
        elif a.test in chosen:
            reasons = sorted(sel.why.get(a.test, ())) or ["its test file changed"]
            print(f"runs because it used: {', '.join(reasons)}")
        else:
            print("skipped: none of the changed functions or files were used by this test when the map was recorded")
        return 0

    if a.list:
        print("\n".join(chosen))
    elif a.json:
        print(
            json.dumps(
                {
                    "base": a.base,
                    "head": a.head or "working tree",
                    "map_commit": tmap.commit,
                    "run_all": sel.run_all,
                    "tests": len(chosen),
                    "of": len(tmap.tests),
                    "estimated_seconds": round(sel.estimated_seconds(tmap), 1),
                    "full_seconds": round(sum(tmap.durations.values()), 1),
                    "changed_test_files": sorted(sel.test_files),
                    "uncovered_changes": sel.uncovered,
                    "warnings": sel.warnings,
                    "notes": sel.notes,
                    "selected": [{"test": t, "because": sorted(sel.why.get(t, ()))} for t in chosen],
                },
                indent=2,
            )
        )
    else:
        full = sum(tmap.durations.values())
        est = sel.estimated_seconds(tmap)
        for w in sel.warnings:
            print(f"warning: {w}")
        if sel.run_all:
            print(f"Run everything: {sel.run_all}")
        else:
            pct = f" ({est / full:.0%} of the suite's time)" if full else ""
            print(f"{len(chosen)} of {len(tmap.tests)} tests, about {est:.1f}s of {full:.1f}s{pct}")
            by_reason: dict[str, int] = {}
            for t in chosen:
                for reason in sel.why.get(t, ()):
                    by_reason[reason] = by_reason.get(reason, 0) + 1
            for f in sorted(sel.test_files):
                print(f"  {f}: changed, runs whole")
            for reason, n in sorted(by_reason.items(), key=lambda x: -x[1]):
                print(f"  {reason}: {n} tests")
        for u in sel.uncovered:
            print(f"  not run by any test: {u}")
        for note in sel.notes:
            print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
