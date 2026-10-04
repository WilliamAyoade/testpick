"""How long the slowest machine takes when the suite is split n ways: by test count vs by recorded time."""
import json
import sys

from testpick.shard import by_count, by_duration
from testpick.testmap import TestMap

m = TestMap.load(sys.argv[1])
tests = sorted(m.durations)
out = {"tests": len(tests), "total_seconds_recorded": round(sum(m.durations.values()), 1)}
for n in (2, 4, 8):
    worst = lambda shards: round(max(sum(m.durations[t] for t in s) for s in shards), 1)
    out[f"{n}_machines"] = {"split_by_count_slowest_s": worst(by_count(tests, n)), "split_by_time_slowest_s": worst(by_duration(tests, m.durations, n)),
                            "ideal_s": round(sum(m.durations.values()) / n, 1)}
print(json.dumps(out, indent=1))
