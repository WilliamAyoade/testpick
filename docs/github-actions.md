# Using testpick in GitHub Actions

The map is recorded on `main` (nightly, or on every merge) and saved as a cache entry.
Pull requests restore the most recent one and run only the tests their changes can affect.

```yaml
# .github/workflows/tests.yml
name: tests
on:
  push:
    branches: [main]
  pull_request:

jobs:
  record:            # on main: run everything once, recording what each test uses
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e . testpick pytest-xdist
      - run: pytest -n auto --testpick-record
      - uses: actions/cache/save@v4
        with:
          path: .testpick/map.json.gz
          key: testpick-${{ github.sha }}

  affected:          # on pull requests: only the affected tests
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }          # testpick needs the history to diff against main
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e . testpick
      - uses: actions/cache/restore@v4
        with:
          path: .testpick/map.json.gz
          key: testpick-${{ github.event.pull_request.base.sha }}
          restore-keys: testpick-       # fall back to the newest map from main
      - run: pytest --testpick=origin/${{ github.base_ref }}
```

Without a map, `--testpick` runs every test, so the first pull request after setup is
still safe. A map recorded a few commits behind `main` still works: functions are looked
up by name, not line number, and anything the map hasn't seen (new tests, new parameters)
always runs. `testpick select` prints a warning when the map's commit isn't an ancestor of
the base branch.

To split the suite across machines by recorded time:

```yaml
    strategy:
      matrix: { shard: [0, 1, 2, 3] }
    steps:
      # ... checkout, setup, restore the map ...
      - run: pytest $(testpick shard --n 4 --i ${{ matrix.shard }})
```
