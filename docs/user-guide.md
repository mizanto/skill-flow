# SkillFlow user guide (v0)

How to run work through SkillFlow: start with the
[`quick-start.md`](quick-start.md), which takes you from install to a
completed Task with one command. When you need the manual CLI path — one Run
operated by hand — this guide covers the concepts, human decisions, failure
handling, inspection, repository layout, and the command reference. For a
complete copy-paste transcript, see
[`reference-example.md`](reference-example.md). For the per-command
operating rules that Claude Code follows, see
[`operational-protocol.md`](operational-protocol.md); for the Workflow
Definition schema, see [`workflow-schema.md`](workflow-schema.md).

## 1. Concepts

- **Task** — a unit of work that can span multiple Runs. Statuses:
  `active`, `waiting_for_human`, `completed`, `cancelled`.
- **Run** — one bounded execution. Reachable
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

**Independent Runs.** The isolation unit of a Run is its execution context:
a `context: fork` Skill invocation or a new independent Claude Code session.
Only string ids cross the Run boundary, and every
command re-resolves its state from SQLite plus workspace files. Normally
`/skillflow:work` drives the loop and dispatches each Run's Execution Skill
in a forked context; an Execution context finishes exactly one Run and never
starts another.

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

## 2. Quick start

New here? Follow [`quick-start.md`](quick-start.md): install the plugin,
run `/skillflow:work "<task>"`, and let the driver take the Task to Done.
The sections below document the manual CLI path — one Run operated by hand
via the four `/skillflow:*` commands — plus the concepts around it.

## 3. Human decisions

A step whose outcome evaluates to `human` parks the Task as
`waiting_for_human` — no new Run can resolve until a human answers.
Normally the `/skillflow:work` driver asks you automatically; record the
answer by hand with a decision from the step's decision table, plus
an optional free-text comment:

```bash
skillflow decide request_changes --comment "Rework the plan first"
```

`decide` validates the decision, records it, evaluates, and moves the
Task (back to `active` for a `run` decision, to `completed` /
`cancelled` for terminal ones). It works only while the Task waits;
on any other status it fails without recording anything. Decisions
never map back to `human`. Then continue with `/skillflow:work`, or resolve
the next Run by hand.

## 4. Failure and retry

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
Run — the failed Run's step (or skill, for
skill-targeted Runs) is retried. Failed Runs are never resumed and a
failed Run never fails its Task. Inspect the failure first with
`skillflow show-task "$TASK"`.

## 5. Inspection and errors

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

## 6. Repository layout

A working target repository looks like this:

```text
<repo-root>/
├── .git/                  # (or pre-existing .skillflow/) marks the root
├── workflows/
│   └── software-change.yaml   # staged by `skillflow start`; init never creates this dir
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

## 7. Command reference

The driver skill and the four lifecycle skills with their runtime
subcommands:

| Skill | Runtime | Purpose |
|---|---|---|
| `/skillflow:work "<task>"` | `skillflow start` / `assignment` / `resolve-task` / `decide` | Drive the Task loop; dispatch each Run's skill |
| `/skillflow:resolve-task <task-id>` | `skillflow resolve-task` | Start the next Run; print RunInput |
| `/skillflow:prepare-artifacts` | `skillflow prepare-artifacts` | Report existing/missing outputs; writes nothing |
| `/skillflow:complete-run` | `skillflow complete-run` | Validate, record Result, evaluate, print next action |
| `/skillflow:decide <decision>` | `skillflow decide` | Record a human decision; evaluate |

CLI-only operator commands (deliberately no corresponding skill):

| Command | Purpose |
|---|---|
| `skillflow start --title <t> --description <d>` | Create a Task from a bundled Workflow Definition |
| `skillflow fail-run` | Record the running Run as failed, with optional diagnostics |
| `skillflow show-task <task-id>` | Read-only lifecycle view for any Task |
| `skillflow assignment [--skill NAME]` | Re-print the running Run's Assignment; `--skill` verifies the Run's skill |

Per-command operating rules: `operational-protocol.md`. Definition
schema: `workflow-schema.md`. Worked transcript:
`reference-example.md`.
