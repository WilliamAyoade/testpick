"""Summarize bench/evaluate.py results: python bench/summarize.py results/v2/*.json"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter


def summarize(path: str) -> dict[str, object]:
    with open(path) as f:
        rows = json.load(f)
    full = sum(r["full_seconds"] for r in rows)
    picked = sum(r["seconds"] for r in rows)
    file_level = sum(r["file_level_seconds"] for r in rows)
    shares = sorted(r["seconds"] / r["full_seconds"] for r in rows)
    verdicts = Counter(m["verdict"] for r in rows for m in r["mutants"])
    caught = [m for r in rows for m in r["mutants"] if m["verdict"] == "caught by picked tests"]
    full_catches = verdicts["caught by picked tests"] + verdicts["MISSED: full suite catches it"]
    ordered = [m["first_failure_s"] for m in caught]
    return {
        "commits": len(rows),
        "commits_needing_no_tests": sum(1 for r in rows if r["tests"] == 0),
        "commits_running_everything": sum(1 for r in rows if r["run_all"]),
        "picked_share_of_suite_time": round(picked / full, 3),
        "file_level_share_of_suite_time": round(file_level / full, 3),
        "median_commit_share": round(st.median(shares), 3),
        "bugs_planted": sum(verdicts.values()),
        "bugs_the_full_suite_catches": full_catches,
        "caught_by_picked": verdicts["caught by picked tests"],
        "missed": verdicts["MISSED: full suite catches it"],
        "no_test_catches": verdicts["no test catches it"],
        "skipped": sum(v for k, v in verdicts.items() if k.startswith("skipped")),
        "median_first_failure_s": round(st.median(ordered), 2) if ordered else None,
        "commits_skipped_because_tests_already_fail": sum(1 for r in rows if "note" in r),
        "missed_details": [dict(m, commit=r["commit"]) for r in rows for m in r["mutants"] if m["verdict"].startswith("MISSED")],
    }


if __name__ == "__main__":
    print(json.dumps({p: summarize(p) for p in sys.argv[1:]}, indent=1))
