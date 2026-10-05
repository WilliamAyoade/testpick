# Changelog

## 0.2.0

- pytest plugin: `--testpick-record` records a map, `--testpick=BASE` runs only affected tests.
- Recording uses `sys.monitoring` on Python 3.12+ (one callback per function per test) instead
  of per-test line coverage; falls back to `sys.setprofile` on 3.10/3.11.
- Data files: tests that open a file are recorded, so fixture changes pick only their readers.
- Module-level changes follow imports: a changed constant or class attribute picks tests that
  run code in files importing it, including re-exports through packages.
- Reformatting, comments and other changes that leave the syntax tree identical pick nothing.
- Renames since the map was recorded are followed; new tests and new parameters always run.
- Likely failures first: changed tests, then tests touching the most changed code. Whole modules
  and classes move together, so module/class fixtures and `setUpClass` aren't set up repeatedly
  (interleaving them made a 1,268-test run take 181 s instead of 99 s on sqlglot).
- pytest-xdist support for recording; map refresh from partial runs.
- `testpick select` / `explain` say why each test was picked.

## 0.1.0

- First version: per-test coverage via pytest-cov contexts, `map` / `select` / `run` / `shard`.
