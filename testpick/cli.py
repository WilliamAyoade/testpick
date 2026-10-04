"""testpick map | select | run | shard"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from .select import select
from .shard import by_duration
from .testmap import TestMap, build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="testpick")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="run the full suite under coverage and save the test map")
    m.add_argument("--repo", default=".")
    m.add_argument("--source", required=True)
    m.add_argument("--tests", default="tests")
    m.add_argument("--out", default=".testpick.json.gz")
    for name in ("select", "run"):
        s = sub.add_parser(name)
        s.add_argument("--repo", default=".")
        s.add_argument("--map", default=".testpick.json.gz")
        s.add_argument("--base", default="origin/main")
        s.add_argument("--head", default="HEAD")
    sh = sub.add_parser("shard", help="print the tests for shard i of n, balanced by recorded time")
    sh.add_argument("--map", default=".testpick.json.gz")
    sh.add_argument("--n", type=int, required=True)
    sh.add_argument("--i", type=int, required=True)
    a = ap.parse_args(argv)

    if a.cmd == "map":
        tmap = build(a.repo, a.source, a.tests)
        tmap.save(a.out)
        print(f"mapped {len(tmap.tests)} tests over {len(tmap.covers)} functions at {tmap.commit[:9]} in {tmap.full_seconds:.0f}s")
        return 0
    if a.cmd == "shard":
        tmap = TestMap.load(a.map)
        print("\n".join(by_duration(sorted(tmap.durations), tmap.durations, a.n)[a.i]))
        return 0
    tmap = TestMap.load(a.map)
    sel = select(a.repo, tmap, a.base, a.head)
    if a.cmd == "select":
        print(json.dumps({
            "run_all": sel.run_all, "tests": len(sel.tests), "test_files": sorted(sel.test_files),
            "estimated_seconds": round(sel.estimated_seconds(tmap), 1), "full_seconds": round(sum(tmap.durations.values()), 1),
            "uncovered_changes": sel.uncovered, "why": sel.reasons,
        }, indent=2))
        return 0
    args = sel.pytest_args(tmap)
    if not args:
        print("no tests are affected by this change")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as f:
        f.write("\n".join(args))
    try:
        return subprocess.run([sys.executable, "-m", "pytest", "-q", f"@{f.name}"], cwd=a.repo).returncode
    finally:
        os.unlink(f.name)


if __name__ == "__main__":
    sys.exit(main())
