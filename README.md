# testpick

Runs only the tests a change can affect. It records, once, which tests execute which
functions (per-test coverage), then for any diff picks the tests that ran the changed
functions. Python and pytest.

```
pip install -e .
testpick map    --source mypkg --tests tests     # run the full suite once under coverage (e.g. nightly)
testpick select --base origin/main               # what would run, and why (JSON)
testpick run    --base origin/main               # run just those tests
testpick shard  --n 4 --i 0                      # split the suite across machines by recorded time
```

## How it decides

- **The map** comes from one full run with `pytest-cov --cov-context=test`: for every line
  of the package, which tests ran it. Lines are rolled up to the function that contains
  them (`sqlglot/parser.py::Parser._parse_alter`), so the map survives later edits that
  shift line numbers: changed code is looked up by function name, not by line.
- **The diff** (`git diff -U0`) gives removed lines (looked up in the old version of the
  file) and added lines (looked up in the new version). Blank lines and comments are
  skipped.
- **Changed function** → the tests that ran it. **Changed module-level code** (imports,
  constants, class attributes) → every test that touched the file. **Changed test file**
  → that whole file, so new tests run too. **Docs, CI config** → nothing.
- **Run everything** when the map can't be trusted: packaging/pytest config, `conftest.py`,
  shared test helpers or test data, an added or deleted module, or a file that doesn't parse.
- Changed functions that no test runs are listed as `uncovered_changes`, so a reviewer can
  see what nothing would catch.

## Does it miss bugs? Replaying real history

`bench/evaluate.py` replays 45 consecutive commits (Sept 30 – Oct 4, 2026) of
[sqlglot](https://github.com/tobymao/sqlglot), an open-source SQL parser with 1,294 test
functions and 20,000+ subtests. The map was built once, at the commit before the first
one, and reused for all 45, the way a nightly map would be. For each commit it picks
tests, then plants small bugs on lines the commit changed (flip a comparison, swap
`and`/`or`, alter a string or number, return `None`) and checks whether the picked tests
fail. When they don't, it runs the full suite to see if anything would have caught it.

Results (`results/sqlglot-history.json`):

| | |
|---|---|
| Test time picked, all 45 commits together | **34%** of running the full suite every time |
| Median commit | **9%** of the suite (13 commits needed no tests: docs, CI, changelog, a submodule pointer) |
| Planted bugs that the full suite catches | 21, **all 21 caught by the picked tests** |
| Planted bugs the full suite also misses | 18 (no test in the project exercises that behaviour) |
| Commits that fell back to running everything | 0 |

The full suite takes ~101 s without coverage on this machine (2 CPUs); picked runs
took 1–98 s, median 22 s, including pytest start-up (`selected_run_seconds`). The map run
takes ~4.5 min because of per-test coverage (`results/map-build.log`).

Splitting by recorded time (`bench/shards.py`, `results/shards.json`): with 2 machines the
slowest finishes in half the time (129.5 s vs 236.4 s by test count); beyond that one
110-second test sets the floor.

`tests/test_testpick.py` builds small throwaway git repos and checks each rule:
function lookup (decorators, nested functions, class bodies), diff parsing, picking by
function, module-level changes, docs-only changes, lookups that survive line shifts and
new functions, whole-file runs for changed tests, and the run-everything fallbacks.

## Limitations

- Only Python changes are traced. Code that reads data files, environment variables or
  network services outside the package can change behaviour without changing a function.
- Dynamic dispatch on data the map didn't see (e.g. a new dialect name in a lookup table)
  is only caught through module-level changes in the file holding the table.
- The map is as good as its last full run; rebuild it regularly (nightly, or on main).
- 39 planted bugs over 45 commits is a sanity check, not a proof.
