# SkillFlow user guide (v0)

How to run work through SkillFlow: install it, initialize a repository,
create a Task, and drive it to Done one independent Run at a time. For a
complete copy-paste transcript, see
[`reference-example.md`](reference-example.md). For the per-command
operating rules that Claude Code follows, see
[`operational-protocol.md`](operational-protocol.md); for the Workflow
Definition schema, see [`workflow-schema.md`](workflow-schema.md).

## 1. Concepts

- **Task** — a unit of work that can span multiple Runs. Statuses:
  `active`, `waiting_for_human`, `completed`, `cancelled`.
- **Run** — one bounded execution, always in its own session. Reachable
  statuses in v0: `running`, `completed`, `failed`. (`waiting_for_human`
  and `cancelled` exist on Runs but no v0 operation sets them: when a
  Run needs a human or is cancelled, it is the *Task* that waits or is
  cancelled.) Only one Run may be `running` per Task, and a Run is
  never resumed — a retry is always a new Run.
- **Result** — exactly one per Run: the canonical observation of what
  happened, including the Run's outcome decision when the step declares
  outcomes.
- **Artifact** — durable context a Run produced: a file under
  `.skillflow/artifacts/` plus one metadata row (name, type, version).
  Artifacts are logically immutable; new content is a new version.
- **Workflow Definition** — the normal procedure for a type of Task
  (steps, expected outputs, outcome rules), read from
  `workflows/<id>.yaml`. It is configuration, not runtime state.
- **Human Decision** — optional human input resolving a step that
  evaluated to `human`.

**Independent Runs.** Each Run executes in its own session with no
shared memory: only string ids cross the Run boundary, and every
command re-resolves its state from SQLite plus workspace files. A
completed Run never continues into another Run in the same session —
the next Run starts in a new session.

**Durable context.** Runs are connected by Artifacts, not by
conversation history. Each step declares the artifact *types* it
consumes (`context:`); `resolve-task` passes only the latest version
of each declared type that a previous Run produced (plus the
unresolved declarations, so the Run knows what is missing). Full
history, previous Results, and transcripts are never passed
automatically.

**Deterministic evaluation.** After each completion, Lifecycle
Evaluation maps the outcome to exactly one action — `run`, `human`,
`complete`, or `cancel` — by plain lookup in the Workflow Definition.
No LLM is involved.

## 2. Installation

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.14 or newer
(`requires-python = ">=3.14"`). From your SkillFlow checkout:

```bash
SF_CHECKOUT=~/skillflow  # ← your SkillFlow checkout
cd "$SF_CHECKOUT"
uv sync
export PATH="$PWD/.venv/bin:$PATH"
skillflow --version
```

`uv sync` needs the package index once; afterwards the CLI never needs
the network. Keep `skillflow` on `PATH`: the Claude Code plugin shells
out to it, and every command below assumes it resolves.

## 3. Initializing a repository

Pick (or create) the target repository — the project the Task will
change. Run `skillflow` commands from anywhere inside the target
repository; the root is found by walking up to the nearest
`.git` or `.skillflow` marker. There is no `init` or `create-task`
subcommand in v0: initialization is three explicit steps.

**1. Create the target repo and install the Workflow Definition.**
`init_workspace` creates `.skillflow/` only — never `workflows/` —
so create that directory and copy the reference definition into it:

```bash
mkdir -p /tmp/sf-demo/workflows && cd /tmp/sf-demo
git init -q .
cp "$SF_CHECKOUT/workflows/software-change.yaml" workflows/
```

**2. Initialize the workspace database** (idempotent — safe to re-run).
It runs on the checkout's interpreter, from the target root (with no
argument, `init_workspace()` resolves the root from the working
directory):

```bash
export SKILLFLOW_PY="$SF_CHECKOUT/.venv/bin/python"
"$SKILLFLOW_PY" -c "from skillflow.workspace import init_workspace; init_workspace()"
```

**3. Register the definition and create the Task**, capturing the id:

```bash
TASK=$("$SKILLFLOW_PY" - <<'EOF'
import contextlib
from skillflow import store, workspace
from skillflow.service import create_task, register_workflow
from skillflow.workflow_loader import load_workflow

ws = workspace.Workspace(root=workspace.find_repo_root())
with contextlib.closing(store.open_store(ws)) as conn:
    register_workflow(conn, load_workflow(ws.workflows_dir / "software-change.yaml"))
    task = create_task(conn, title="Ship it", workflow_definition_id="software-change")
    print(task.id)
EOF
)
echo "$TASK"
```

Registration must precede creation when the Task names a Workflow at
creation time (otherwise `UnknownWorkflowError`). Alternatively, create
the Task with no `workflow_definition_id` and assign one on first
resolve (see §4). Confirm the setup:

```bash
skillflow show-task "$TASK"
```

## 4. Workflow selection

A Task needs a Workflow before its first Run. If it has none,
`resolve-task` refuses with `WorkflowSelectionRequired` and lists the
available definition ids (the stems of `workflows/*.yaml`; a missing
directory simply lists none). Assign one explicitly:

```text
$ skillflow resolve-task "$TASK" --workflow software-change
```

For a worked transcript of the prompt and the assignment, see
[`reference-example.md`](reference-example.md) §5. The file stem and
the definition's `name:` must agree exactly, or
loading fails with `WorkflowLoadError`. Assignment is permanent:
passing a *different* `--workflow` later fails with
`WorkflowAssignmentError` — re-run without the flag to keep the current
assignment. Omitting `--workflow` for an already-assigned Task is a
no-op success.

## 5. The Run loop

Each Run follows the same sequence, always in a fresh session:

```text
resolve-task → bounded work → prepare-artifacts → complete-run → next action
```

**Start the Run.** `resolve-task` validates the Task, resolves the
current step, selects the durable context, creates one `running` Run,
and prints its `RunInput`:

```bash
skillflow resolve-task "$TASK"
```

The RunInput names the Task, the Run, the Workflow, the step's
skill/model/effort, the resolved context artifacts (name, type, version,
plus the repo-relative content path under `.skillflow/artifacts/`), the
unresolved declared types, and the step's expected outputs. It fails
while another Run is `running` (`ActiveRunExists`), while the Task waits
for a human (`HumanDecisionRequired`), or on a terminal Task (nothing to
run). The same Assignment can be re-printed at any time while the Run is
`running` with `skillflow assignment` (add `--skill NAME` to verify the
Run targets that skill); it reads only and changes nothing.

**Do the bounded work** in this session only: read the selected
context, do the step's job, write ordinary project files. Durable
outputs that later Runs need are ordinary files for now; they become
Artifacts at completion.

**Check expected outputs.** Before completing, inspect the gap:

```bash
skillflow prepare-artifacts
```

It compares the step's expected outputs against the artifacts
registered for the running Run so far, reports each as existing or
missing, and writes nothing. In the normal CLI flow nothing is
registered until completion, so expect everything listed as missing on
a fresh run — that is normal; the report tells you exactly what to
produce. Create the missing files, then complete.

**Complete the Run**, submitting an outcome decision when the step
declares outcomes, plus one `--artifact NAME:TYPE:PATH` per durable
output (repeatable; `PATH` is read as UTF-8 relative to the working
directory):

```bash
skillflow complete-run --outcome ready --artifact research.md:research:research.md
```

Steps that declare no outputs take zero `--artifact` flags; steps that
declare no outcomes take no `--outcome`. Completion validates the
outcome, registers the artifacts, records exactly one Result, marks the
Run completed, evaluates the lifecycle, and prints the next action.
Anything missing fails with `RequiredArtifactsMissing` (or
`InvalidOutcome` / `OutcomeRequired`) and the Run stays `running` —
fix the cause and re-run the same command.

**Start the next Run in a new session** when the next action is `run`:

```bash
skillflow resolve-task "$TASK"
```

A completed Task rejects further resolves: the lifecycle is over.

A workspace has at most one `running` Run: `resolve-task` refuses with
`ActiveRunExists` while any Task's Run is running. With the Task id
omitted, `resolve-task` uses the single active or waiting Task
(`AmbiguousCurrentTask` when there are several). `prepare-artifacts`,
`complete-run`, `fail-run`, and `decide` (which needs it when several
Tasks wait) still accept `--task <task-id>` to disambiguate.

## 6. Human decisions

A step whose outcome evaluates to `human` parks the Task as
`waiting_for_human` — no new Run can resolve until a human answers.
Record the answer with a decision from the step's decision table, plus
an optional free-text comment:

```bash
skillflow decide request_changes --comment "Rework the plan first"
```

`decide` validates the decision, records it, evaluates, and moves the
Task (back to `active` for a `run` decision, to `completed` /
`cancelled` for terminal ones). It works only while the Task waits;
on any other status it fails without recording anything. Decisions
never map back to `human`. Then resolve the next Run as usual.

## 7. Failure and retry

When a Run cannot complete at all, record the failure explicitly. Both
diagnostics flags are optional and mutually exclusive — `--message`
for inline text, `--diagnostics-file` for a UTF-8 file's content
(stored verbatim as `runs/<run-id>/output.log`); omit both and no
diagnostics file is written. Partial durable outputs may be submitted
with repeatable `--artifact` flags and remain usable by later Runs:

```bash
skillflow fail-run --message "Test harness hung; see output.log"
```

`fail-run` records a failed Result, marks the Run `failed`, and always
evaluates to `run`: the Task stays `active`. A retry is simply the next
Run — resolve again and the failed Run's step (or skill, for
skill-targeted Runs) is retried. Failed Runs are never resumed and a
failed Run never fails its Task. Inspect the failure first with
`skillflow show-task "$TASK"`.

## 8. Inspection and errors

`skillflow show-task <task-id>` prints the Task's lifecycle — status,
ordered Runs with provenance, per-Run Results, artifact references,
recorded decisions, and chronological lifecycle events. It is read-only
(changes nothing, records no event) and works for any status, with zero
events present, and even with missing or broken definition files.

Every domain rejection — a failure with a `CODE`, as opposed to a
flag-parsing usage error (which exits 2 with usage text) — uses one
envelope on stderr (exit code 1, stdout empty):

```text
skillflow <command>: <CODE>: <what happened and how to recover>
<what state was left unchanged>
```

The `CODE` keys into the per-command recovery tables in
[`operational-protocol.md`](operational-protocol.md) ("Rejection and
recovery"), and the second line always states the preserved state
(e.g. no Run was created; no Result was created and no Run status
changed). Recovery is always to fix the named cause and re-run the
same command — never to edit `.skillflow/` by hand.

## 9. Repository layout

A working target repository looks like this:

```text
<repo-root>/
├── .git/                  # (or pre-existing .skillflow/) marks the root
├── workflows/
│   └── software-change.yaml   # copied by you; init never creates this dir
├── .skillflow/
│   ├── skillflow.db       # tasks, runs, results, artifacts,
│   │                      # workflow_definitions, human_decisions,
│   │                      # lifecycle_events (SQLite)
│   ├── artifacts/         # durable artifact content
│   └── runs/<run-id>/     # per-run diagnostics (output.log on failure)
├── research.md            # ordinary project files (become artifacts
├── plan.md                #   only when submitted at completion)
└── ...
```

Whether `.skillflow/` is committed or ignored is each repository's own
choice.

## 10. Command reference

The four lifecycle skills and their runtime subcommands:

| Skill | Runtime | Purpose |
|---|---|---|
| `/skillflow:resolve-task <task-id>` | `skillflow resolve-task` | Start the next Run; print RunInput |
| `/skillflow:prepare-artifacts` | `skillflow prepare-artifacts` | Report existing/missing outputs; writes nothing |
| `/skillflow:complete-run` | `skillflow complete-run` | Validate, record Result, evaluate, print next action |
| `/skillflow:decide <decision>` | `skillflow decide` | Record a human decision; evaluate |

CLI-only operator commands (deliberately no corresponding skill):

| Command | Purpose |
|---|---|
| `skillflow fail-run` | Record the running Run as failed, with optional diagnostics |
| `skillflow show-task <task-id>` | Read-only lifecycle view for any Task |
| `skillflow assignment [--skill NAME]` | Re-print the running Run's Assignment; `--skill` verifies the Run's skill |

Per-command operating rules: `operational-protocol.md`. Definition
schema: `workflow-schema.md`. Worked transcript:
`reference-example.md`.
