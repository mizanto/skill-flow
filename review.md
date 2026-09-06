# Review

> Task: [SF-1](https://bendak.youtrack.cloud/issue/SF-1) — Initialize SkillFlow project
> Reviewed: `plan.md`, `implementation-report.md`, `test-report.md`, specs SF-A-2 / SF-A-3 / SF-A-5 / SF-A-6, and the working tree.
> No `CLAUDE.md` present in this repository.

## Verdict

`APPROVED`

## Summary

SF-1 delivers exactly what it was asked to deliver and nothing more. The entire
production surface is four modules totalling ~70 lines: a version string, an
argparse parser with two flags, a `__main__` shim, and four naming constants.
Every acceptance criterion and every Definition-of-Done item was re-verified
independently rather than taken from the reports:

| Check | Independently verified |
| --- | --- |
| `uv run pytest` | 6 passed, 0 warnings |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | clean |
| `uv run skillflow --version` | `skillflow 0.1.0`, exit 0 |
| `uv run python -m skillflow --version` | `skillflow 0.1.0`, exit 0 |
| bare `uv run skillflow` | help, exit 0 |
| `uv run skillflow bogus` | argparse error on stderr, exit 2 |
| interpreter | CPython 3.11.16, per `.python-version` |

The reports are accurate. The one deviation the implementation report declares
(an `E501` line wrap on the parser `description`) is real and is the only
difference from the plan's code sketches. Nothing in the plan's §11
out-of-scope list leaked in — verified by reading all four source modules in
full, not by grep.

No `BLOCKER` and no `MAJOR` findings. The four `MINOR` items below are cheap,
genuinely worth fixing, and none of them breaks a stated requirement — which is
why the verdict is approval rather than a change request. Items 1 and 2 are
worth clearing before SF-3 builds on this entry point.

## Findings

### 1. `MINOR` — `__main__.py` runs the CLI on import, not just on execution

**Location:** [src/skillflow/\_\_main\_\_.py:5](src/skillflow/__main__.py:5)

**Problem:** `raise SystemExit(main())` sits at module scope with no
`if __name__ == "__main__":` guard. Any *import* of `skillflow.__main__` — not
just `python -m skillflow` — executes the CLI and terminates the interpreter.
Verified:

```
$ uv run python -c "import skillflow.__main__; print('IMPORT RETURNED NORMALLY')"
usage: skillflow [-h] [--version]
...
```

The sentinel never prints; the process exits from inside the import.

**Why it matters:** `pkgutil.iter_modules(skillflow.__path__)` already lists
`__main__` alongside `cli` and `workspace`, so anything that walks the package
kills the process with a help dump instead of an error — coverage collection,
`--import-mode=importlib` collection, API-doc generation, or a future test that
wants to assert on the module rather than shell out. The failure mode is silent
(exit 0) and hard to attribute. The plan did not call for the unguarded form:
§4 wrote `sys.exit(main())` and §5 step 4 wrote `raise SystemExit(main())` —
neither addresses the guard.

**Recommended fix:**

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

### 2. `MINOR` — the entry-point test cannot fail if exit-code propagation breaks

**Location:** [tests/test_entry_point.py:7](tests/test_entry_point.py:7)

**Problem:** The single subprocess test invokes `python -m skillflow --version`.
Argparse's `version` action raises `SystemExit(0)` from inside `parse_args`, so
`main()` never returns and `__main__.py`'s `raise SystemExit(main())` is never
reached with a value. Replacing `__main__.py` with a bare `main()` call — losing
propagation entirely — leaves this test green.

**Why it matters:** Plan §9 lists "`main()` returning non-zero later" as a
handled scenario on the grounds that "`__main__.py` and the console script both
propagate it." That claim is currently untested. This is precisely a test that
passes while the requirement it stands for is broken, and it will silently stop
protecting SF-19 onward, when lifecycle commands start returning non-zero.

**Recommended fix:** Add a no-args subprocess case — `python -m skillflow` with
no arguments returns through `main()`'s `return 0`, exercising the propagation
path. Asserting `returncode == 0` and `usage: skillflow` in stdout covers it
without any new machinery.

### 3. `MINOR` — `--version` output is asserted too loosely for a contract the plan fixes exactly

**Location:** [tests/test_cli.py:11](tests/test_cli.py:11),
[tests/test_entry_point.py:14](tests/test_entry_point.py:14)

**Problem:** Both tests assert `__version__ in output`, i.e. that `0.1.0`
appears somewhere. Plan §7 and DoD item 3 both specify the exact output
`skillflow 0.1.0`. Dropping the `skillflow ` prefix from
[cli.py:27](src/skillflow/cli.py:27) — or argparse defaulting to `prog` only —
would leave both tests passing while the documented contract is broken.

**Why it matters:** The version banner is the observable evidence for
"executable starts," and the README and DoD quote the literal string. Asserting
against the imported `__version__` (a good instinct — it survives version
bumps) is worth keeping; it just needs the prefix too.

**Recommended fix:** Assert `f"skillflow {__version__}"` rather than
`__version__` in both tests. This keeps the bump-safety and pins the contract.

### 4. `MINOR` — ruff formats the Run's durable artifacts

**Location:** [pyproject.toml:29](pyproject.toml:29)

**Problem:** `ruff format --check .` reports **11** files, not the 7 Python
files in the tree. The extra four are the repository's Markdown files, and ruff
0.16 reformats Python code blocks inside them. Verified on a scratch file:

```
$ uv run ruff format --diff probe.md
-x   =  1
+x = 1
1 file would be reformatted
```

So `plan.md`, `implementation-report.md`, `test-report.md` — and this
`review.md` — are governed by the source formatter. (This also explains the
implementation report's "9 files": two of the artifacts did not exist yet.)

**Why it matters:** Artifacts are SkillFlow's durable record of a Run; a source
formatter should not have edit authority over them. Today they happen to be
clean, but the first plan containing a Python snippet that ruff would restyle
makes `uv run ruff format --check .` — a command the README and DoD item 5 both
publish — fail on an artifact rather than on code, and "fixing" it rewrites a
record that is supposed to be immutable. This will bite harder as SF-11 and
SF-39 land more artifact conventions.

**Recommended fix:** Add `extend-exclude = ["*.md"]` under `[tool.ruff]`, or
narrow the published commands to `uv run ruff format --check src tests`. The
exclude is preferable — it keeps `.` working as documented.

### 5. `NIT` — `args` names the raw argv list, shadowing the argparse convention

**Location:** [src/skillflow/cli.py:39-41](src/skillflow/cli.py:39)

**Problem:** `args` holds the raw `list[str]`, while the `Namespace` returned by
`parse_args` is discarded. In argparse code `args` conventionally *is* the
Namespace.

**Why it matters:** Not a defect today, but the next person adding a flag will
reach for `args.something` on a list. Renaming now costs one line and removes
the trap before SF-19 adds real options.

**Recommended fix:** `argv_list = argv if argv is not None else sys.argv[1:]`,
and bind the parse result if it is ever needed.

### 6. `NIT` — the `-> int` annotation does not hold on every path

**Location:** [src/skillflow/cli.py:32](src/skillflow/cli.py:32)

**Problem:** `main` is annotated `-> int`, but `--version` and `--help` exit via
`SystemExit` from inside `parse_args` and never return. This is standard
argparse behaviour and the tests correctly expect it.

**Why it matters:** Only that the docstring currently promises "Returns a
process exit code" without qualification, and a later reader may write a caller
that assumes a return on all paths.

**Recommended fix:** One clause in the docstring noting that argparse's
`version` and `help` actions exit directly. No code change.

## Questions

1. **Global install** (carried from plan §13, unresolved). Should `skillflow`
   eventually be installed via `uv tool install` for use inside *other*
   repositories? This directly shapes SF-3's repository-root detection —
   whether the root is derived from the CWD or from the installed package's
   location — and is cheaper to settle before SF-3 than after.
2. **`DB_FILE_NAME = "skillflow.db"`.** SF-A-2 §2 explicitly calls the exact
   layout an implementation detail, so this is a new concrete decision made
   here, not one inherited from a spec. It becomes a cross-issue contract the
   moment SF-3 consumes it. Confirming it now is worth one sentence.
3. **Artifact placement.** `plan.md`, `implementation-report.md`, and
   `test-report.md` sit at the repository root, untracked and *not* gitignored,
   so the first commit sweeps them in as if they were project files. SF-A-2 §5
   places artifact content under `.skillflow/artifacts/`. Is root placement the
   interim protocol until artifact storage lands (SF-11), and are these meant
   to be committed? Related to finding 4.
4. **Bare `skillflow` exiting 0.** Plan §13 flags this as a judgement call.
   Once real subcommands exist (SF-19), the conventional choice is exit 2 for a
   missing subcommand. Is exit 0 a stable contract, or provisional until then?
   Test [test_cli.py:15](tests/test_cli.py:15) will need updating either way.
5. **`CLAUDE.md`.** Plan §11 defers it, correctly. Worth raising as its own
   issue rather than letting it drift, since it will shape how later Runs
   behave in this repository.

## Good Decisions

These are the choices most worth preserving:

- **No `add_subparsers()`.** The single most valuable restraint here. An empty
  subparser container would have frozen a command contract before SF-A-5's
  preconditions and error types exist, and SF-19/21/25/27 would have inherited
  it. The plan named this risk explicitly (§10.1) and the code holds the line.
- **`workspace.py` is constants only** — no `Path`, no `mkdir`, no root
  detection. SF-A-6 lists root detection, workspace init, SQLite init, and
  idempotent setup as SF-3's deliverables; doing any of it here would have
  produced a competing API. The module boundary is exactly right.
- **`main() -> int` instead of `sys.exit`.** Keeps `main` directly testable and
  leaves later issues a seam for mapping lifecycle errors onto exit codes with
  no restructuring. (Finding 2 asks only that the seam be tested.)
- **Dynamic version from `__init__.py`.** Version drift between
  `pyproject.toml` and `__version__` is made structurally impossible rather
  than merely documented.
- **Zero runtime dependencies.** PyYAML correctly deferred to SF-8 rather than
  added speculatively. The installed CLI never needs the network.
- **Tests assert the imported `__version__`, not a literal.** The right instinct
  — the tests survive a version bump without edits. Finding 3 refines it rather
  than reversing it.
- **`.skillflow/` gitignored as an explicitly local decision.** SF-A-2 §3 leaves
  the commit/ignore question open for target repositories; the implementation
  ships no `.gitignore` template and claims no global rule, and the README says
  so. Correct reading of an intentionally open spec.

## Plan Compliance

The implementation follows `plan.md` closely. Every file in the plan's §4
create/change table exists with the stated purpose, and no file outside it was
added. `pyproject.toml` matches the §5 sketch verbatim.

Deviations:

1. **Declared and justified.** The parser `description` is wrapped across two
   lines because the single-line form in the plan's §5 sketch was 92 characters
   against ruff's 88-character default (`E501`). Behaviour identical, no
   interface change. The implementation report discloses this; verified
   accurate.
2. **Undeclared, and caused by the plan itself.** Plan §4 specifies
   `sys.exit(main())` for `__main__.py` while §5 step 4 specifies
   `raise SystemExit(main())`. The implementation follows §5. The plan is
   internally inconsistent here; the choice made is the better of the two and
   is not a deviation in substance. See finding 1 for the separate guard issue,
   which neither variant of the plan covered.

No scope creep. The §11 out-of-scope list — domain types, root detection,
SQLite, the four lifecycle commands, slash commands, PyYAML, lifecycle events,
CI, mypy, coverage thresholds — is fully respected. The README addition is a
developer note of the size §8 called for, not the user documentation SF-39 owns.

## Test Assessment

Six tests, all passing, re-run independently: 6 passed, 0 warnings, ~0.04s.
The suite maps cleanly onto SF-1's acceptance criteria — "installs locally" via
the `src` layout plus `uv sync`, "tests run" by the suite existing, "executable
starts" by the subprocess test, "no external services" by the empty dependency
list.

Adequate as evidence for SF-1's narrow scope. Three weaknesses, none fatal:

- The entry-point test does not exercise return-value propagation (finding 2).
  This is the one place where a passing test does not evidence the requirement
  it stands for.
- The version assertion is looser than the contract the plan fixes exactly
  (finding 3).
- `test_workspace.py` is tautological — it asserts four constants equal four
  literals, which tests implementation rather than behaviour. Normally a
  defect; here it is declared, justified, and correct: `.skillflow` is a
  cross-issue naming contract that SF-3 and everything after it depends on, and
  silent drift should fail loudly. The comment in the test says so. Keep it.

Not tested, and correctly so: that the `skillflow` console script lands on
`PATH`. That would assert uv's shim behaviour rather than this package. Manual
verification (`uv run skillflow --version`) is the right level here; I
reproduced it.

The fresh-checkout DoD was verified by copying the tracked file set rather than
by `git clone`, because nothing is committed yet. The substitution is sound —
`git ls-files -o -c --exclude-standard` minus `.venv/` carries the same file set
a clone would — and the limitation is disclosed in both reports rather than
papered over. Worth re-running as a real `git clone` once the first commit
exists.

`build_parser()` is public and has no direct test, but it is fully exercised
through `main`; a separate test would add nothing.

## Architecture Assessment

Compliant. Checked against SF-A-1, SF-A-2, SF-A-3, SF-A-5, and SF-A-6.

**Forbidden constructs — none present.** Verified by reading all four source
modules in full. No Router, Transition, Loop, Iteration, Rework, Handoff, or
Workflow Instance. No generic workflow DSL. No LLM-based lifecycle routing and
no LLM-based Context Selection — there is no evaluation logic of any kind. No
automatic Claude Code launching: the CLI spawns no processes (the only
`subprocess` call in the repository is in a test). No automatic next-Run
creation — no Run concept exists yet. No PostgreSQL, no distributed execution,
no concurrency infrastructure, no Git-based artifact versioning, no object
storage. `sqlite3` is not imported anywhere.

**Layering.** Clean. `cli.py` is pure argument parsing with no domain
knowledge; `workspace.py` is four strings and a docstring, with no I/O and no
types. Nothing sits in the wrong layer because there is, correctly, almost
nothing yet.

**Claude Code coupling in the core.** None structural. Claude Code is named in
the package docstring and the CLI `description` string — prose describing what
the tool orchestrates, not a dependency. SF-A-6 §2's requirement that "the
domain model must not depend on Claude Code" is not yet in play (no domain
model exists), and nothing here prejudices it.

**Spec alignment.** The `.skillflow/` layout documented in
[workspace.py:9-14](src/skillflow/workspace.py:9) and in the README matches
SF-A-2 §2 and §3 — SQLite for lifecycle state and metadata, filesystem for
artifact content and per-run diagnostics, rooted at the target repository. The
`skillflow.db` filename is an addition, correctly flagged by the implementation
report as a new decision (see Question 2).

**MVP boundaries.** Respected in both directions: nothing premature was built,
and nothing that SF-1 owed was deferred. No abstraction exists that is not used
by something today — no base classes, no protocols, no registry, no config
layer. For a "project skeleton" issue, that restraint is the main thing worth
grading, and it holds.

## Final Recommendation

`APPROVED`

All four acceptance criteria and all seven Definition-of-Done items were
independently verified as met. No `BLOCKER` or `MAJOR` findings. Findings 1–4
are `MINOR` and 5–6 are `NIT`; none blocks SF-2 or SF-3, and none requires
re-review. Recommend clearing findings 1 and 2 before SF-3 builds on this entry
point, and settling Questions 1 and 2 in the same pass, since both become
cross-issue contracts the moment SF-3 starts.
