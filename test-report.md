# SF-1 — Test Report

> Task: [SF-1](https://bendak.youtrack.cloud/issue/SF-1) · See [implementation-report.md](implementation-report.md)

Environment: macOS (darwin 25.6.0), CPython 3.11.16 (downloaded by uv per
`.python-version`), uv 0.12.7, pytest 9.1.1, ruff 0.16.6.

This report reflects the state **after** applying the Review Run's six findings
(test count 6 → 7; ruff file count 9 → 7 with Markdown excluded).

## Commands executed

| Command | Result |
| --- | --- |
| `uv sync` | OK — resolved 8 packages, created `.venv`, generated `uv.lock` |
| `uv run pytest` | **7 passed** in 0.05s, 0 failed, 0 warnings |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 7 files already formatted (Markdown excluded) |
| `uv run skillflow --version` | `skillflow 0.1.0`, exit 0 |
| `uv run python -m skillflow --version` | `skillflow 0.1.0`, exit 0 |
| `uv run skillflow` (no args) | help text on stdout, exit 0 |
| `uv run skillflow bogus` | argparse error on stderr, exit 2 |
| `python -c "import skillflow.__main__"` | returns normally (no CLI execution on import) |

## Focused test results

`pytest -v`:

```
platform darwin -- Python 3.11.16, pytest-9.1.1, pluggy-1.6.0
configfile: pyproject.toml
testpaths: tests
collected 7 items

tests/test_cli.py ....                                                   [ 57%]
tests/test_entry_point.py ..                                             [ 85%]
tests/test_workspace.py .                                                [100%]

7 passed
```

Coverage by file:

- `tests/test_cli.py` (4) — `--version` exits 0 and prints `skillflow {version}`;
  no-args returns 0 and prints `usage: skillflow`; `--help` exits 0;
  unknown command exits 2.
- `tests/test_entry_point.py` (2) — `python -m skillflow --version` subprocess
  returns 0 with `skillflow {version}` on stdout; **`python -m skillflow` with no
  args** returns 0 through `main()`'s `return 0` via `raise SystemExit(main())`,
  exercising return-value propagation (review finding 2).
- `tests/test_workspace.py` (1) — the four `.skillflow` convention constants
  equal their expected literals (change-detector).

## Full-suite result

`uv run pytest` → **7 passed, 0 failed, 0 skipped, 0 warnings** in ~0.05s.

## Fresh-checkout verification

Tracked working tree copied to `/tmp/sf-verify` (`git ls-files -o -c
--exclude-standard`, excluding `.venv/` and `plan.md`), then:

```
uv sync            → OK (resolved 8 packages, new .venv)
uv run pytest      → 7 passed
uv run skillflow --version → skillflow 0.1.0, exit 0
```

`git clone` was not used because nothing is committed yet (out of scope for
this Run); the copied file set matches what a clone would carry. Worth
re-running as a real `git clone` once the first commit exists.

## Warnings / limitations

- No test asserts the `skillflow` console script is on `PATH` — verified
  manually via `uv run skillflow` (asserting it would test uv's shim, not this
  package).
- `uv sync` needs network once for `pytest`/`ruff` and, on a machine without
  CPython 3.11, the interpreter download (~25.9 MiB). The installed CLI needs
  no network.
- No tests were weakened or removed. The only test changes were tightening the
  two version assertions to `f"skillflow {__version__}"` and adding the
  propagation test — both per the review.
