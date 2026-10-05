"""Split tests across machines so they finish at about the same time."""

from __future__ import annotations

import heapq


def by_duration(tests: list[str], durations: dict[str, float], n: int) -> list[list[str]]:
    """Longest first, each test to the machine with the least work so far (LPT scheduling)."""
    known = sorted(durations.values())
    default = known[len(known) // 2] if known else 1.0  # unknown tests count as a typical one
    heap = [(0.0, i) for i in range(n)]
    shards: list[list[str]] = [[] for _ in range(n)]
    for t in sorted(tests, key=lambda t: (-durations.get(t, default), t)):
        load, i = heapq.heappop(heap)
        shards[i].append(t)
        heapq.heappush(heap, (load + durations.get(t, default), i))
    return [sorted(s) for s in shards]


def by_count(tests: list[str], n: int) -> list[list[str]]:
    """What a plain split does: the same number of tests per machine, in file order."""
    tests = sorted(tests)
    k = -(-len(tests) // n)
    return [tests[i * k : (i + 1) * k] for i in range(n)]
