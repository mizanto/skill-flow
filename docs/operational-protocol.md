# SkillFlow operational protocol (v0)

The canonical Claude-facing operational rules for the four SkillFlow
commands: which command to run, when, in which order, what is
prohibited, and what to report. The deterministic lifecycle logic
behind these commands lives in the runtime
([`skillflow/`](../src/skillflow/)); this document states operating
procedure only.

Sources: Execution Boundary v0 (SF-A-3 §§1–11), Command Contract v0
(SF-A-5 §§3–12), Implementation Plan v0 (SF-A-6 SF-029/SF-030).

## Boundaries

- **Operational instructions only.** This document says when to invoke
  each command and what to do with its output. It does not re-specify
  validation, Context Selection, or Lifecycle Evaluation — those are
  runtime behavior (SF-A-5, SF-A-4).
- **Runtime owns business logic.** If this document and the runtime
  disagree, the runtime and SF-A-5 are authoritative; fix the document.
- **Project work uses normal tools.** Lifecycle state changes go
  through the four commands; all actual work (code, research, writing)
  uses ordinary Claude Code tools (SF-A-3 §5).
- **Skill files are SF-29.** This document defines the protocol; the
  `/skillflow:*` Skill/skill implementation that embodies it is a
  separate issue and may choose its own file format.
- **No new commands.** The v0 surface is exactly the four commands
  below (SF-A-5 §12). Anything else in the prohibited list is out of
  scope for v0.

## Canonical Run sequence

```text
new Claude Code session
  ↓
/skillflow:resolve-task <task-id>      (rule 1: resolve before work)
  ↓
bounded work with normal tools         (rule 2: only this Run)
  ↓
/skillflow:prepare-artifacts           (rule 3)
  ↓
create missing durable outputs         (normal tools → Artifacts)
  ↓
/skillflow:complete-run                (rule 4)
  ↓
Result + Lifecycle Evaluation         (runtime-owned)
  ↓
report next action                     (rule 7)
  ↓
session ends                           (rule 5: no next Run here)
```

When the Task is `waiting_for_human`, the lifecycle command is instead:

```text
/skillflow:decide <decision> [--comment "..."]   (rules 4, 7)
```

## Command → runtime mapping

Each Skill command invokes exactly one deterministic runtime operation.
Skills must call the runtime, never duplicate its logic (SF-A-3 §4,
SF-A-6 SF-030).

| Skill command | Runtime operation | Implementation |
|---|---|---|
| `/skillflow:resolve-task <task-id>` | `skillflow resolve-task <task_id> [--workflow <id>]` | [`cli.py`](../src/skillflow/cli.py) `_run_resolve_task`, [`resolve_task.py`](../src/skillflow/resolve_task.py) |
| `/skillflow:prepare-artifacts` | `skillflow prepare-artifacts [--task <task-id>]` | [`cli.py`](../src/skillflow/cli.py) `_run_prepare_artifacts`, [`prepare_artifacts.py`](../src/skillflow/prepare_artifacts.py) |
| `/skillflow:complete-run` | `skillflow complete-run [--task <task-id>] [--outcome <d>] [--artifact N:T:P ...]` | [`cli.py`](../src/skillflow/cli.py) `_run_complete_run`, [`complete_run.py`](../src/skillflow/complete_run.py) |
| `/skillflow:decide <decision>` | `skillflow decide <decision> [--task <task-id>] [--comment <text>]` | [`cli.py`](../src/skillflow/cli.py) `_run_decide`, [`decide.py`](../src/skillflow/decide.py) |

## Rule 1 — Resolve before work

Start every Run with `/skillflow:resolve-task <task-id>` in a new
Claude Code session, before any lifecycle work (SF-A-5 §4, SF-A-3 §2).

- Must: resolve the Task first; use the returned RunInput (step, skill,
  instructions, selected context, expected outputs) as the assignment
  for this session.
- Must: when the Task has no Workflow, supply the explicit Workflow
  selection the command asks for (`--workflow <id>`); the runtime
  never infers it (SF-A-5 §4.4).
- Must not: perform Task work, create Runs, or assume a step/skill
  before resolving.
- On `HumanDecisionRequired`: the Task waits for a human — stop and
  report that `/skillflow:decide` is needed (rule 4); do not work
  around it.
- On `ActiveRunExists`: a Run is already `running` for this Task —
  do not resolve again (SF-A-5 §3.1).

## Rule 2 — One Run per session

Execute only the resolved Run in the current session (SF-A-5 §3.3,
SF-A-3 §6).

- Must: do the bounded work for the resolved step and nothing else.
- Must not: start, continue, or resume any other Run in this session.
  A Run is never resumed; retry/continuation is always a new Run in a
  new session (SF-A-5 §3.2).
- A failed Run does not fail the Task (SF-A-5 §3.6); reporting a
  failure never means abandoning the Task.
- After a failed Run, start the retry with rule 1 as usual: it resolves
  to the same step (or skill) as a clean assignment. The retry never
  receives the failure diagnostics automatically (SF-A-5 §3.4); only
  declared durable artifacts carry over.

## Rule 3 — Prepare artifacts before completing

Run `/skillflow:prepare-artifacts` before `/skillflow:complete-run`,
and create every missing required output before completing (SF-A-5
§5, §6.4).

- Must: read the ✓/✗ report; create each missing output as a normal
  file with ordinary Claude Code tools. There is no `create-artifact`
  command — Claude owns content creation, SkillFlow owns metadata and
  lifecycle (SF-A-5 §5.5).
- Must: when the report says all required artifacts are prepared,
  proceed to `/skillflow:complete-run`.
- Must not: invoke `/skillflow:complete-run` while required outputs
  are missing — it will be rejected with `RequiredArtifactsMissing`
  and the Run stays `running`.
- `prepare-artifacts` changes no lifecycle state, creates no Result,
  performs no evaluation, and creates no Run (SF-A-5 §5.6). Running
  it twice is always safe.
- When several Runs are `running` in one workspace, disambiguate with
  `--task <task-id>` (`AmbiguousCurrentRun`); the flag selects which
  Run to read and changes nothing else.

## Rule 4 — Complete (or decide) exactly once

Finish the Run with `/skillflow:complete-run`. When the Task is
`waiting_for_human`, record the outcome with `/skillflow:decide
<decision>` instead (SF-A-5 §6, §7, §8).

- Must: complete the current `running` Run exactly once. Success
  registers artifacts, creates exactly one canonical Result, marks
  the Run `completed`, evaluates lifecycle, and updates the Task
  (SF-A-5 §6.5–§6.9).
- Must: pass every durable output created under rule 3 as a
  `--artifact NAME:TYPE:PATH` submission (PATH is read as UTF-8).
  Required-output validation is satisfied from already-registered
  artifacts plus these submission types; required types covered by
  neither are rejected with `RequiredArtifactsMissing`
  (`complete_run.py:200-215`; SF-A-5 §6.2, §6.4).
- Must: pass `--outcome <decision>` only with a value the current
  step declares; omit it when the step declares no outcomes (SF-A-5
  §6.5). An undeclared outcome is rejected with `InvalidOutcome`
  (`OutcomeRequired` / `OutcomeNotExpected` are the same class of
  rejection); the runtime never guesses one (SF-A-5 §6.6).
- Must: use `/skillflow:decide` only while the Task is
  `waiting_for_human` (`HumanDecisionNotExpected` otherwise), with a
  decision the current Workflow/step allows (`InvalidHumanDecision`
  otherwise). The decision never modifies the previous Result
  (SF-A-5 §7.5–§7.6).
- Must not: complete a Run that is not `running` (`RunNotFound` /
  `RunNotActive`); only `running → completed` is allowed (SF-A-5
  §6.3).
- Must not: complete failed work as successful. When the Run cannot be
  completed at all, record it with `skillflow fail-run --task <task-id>
  [--message <text>] [--artifact …]` instead: the Run becomes `failed`
  with a failed Result, diagnostics land in `runs/<run-id>/output.log`,
  partial outputs are registered for the retry (SF-A-2 §9), and the Task
  stays `active`.
- On any validation failure (missing artifacts, invalid outcome,
  unexpected decision) the Run stays `running` — or the Task stays
  `waiting_for_human` — with no Result created and no evaluation
  performed. Fix the cause and re-invoke the same command (SF-A-5
  §6.4, §6.6, §7.5, §8).

## Rule 5 — No next Run in this session

Never start another Run in the session that completed one (SF-A-5
§3.3, SF-A-3 §11).

- Must not: invoke `/skillflow:resolve-task` again after
  `/skillflow:complete-run` or `/skillflow:decide` in the same
  session. The next Run starts in a new Claude Code session.
- `complete-run` and `decide` never create the next Run — not even
  when evaluation returns a `run` action (SF-A-5 §6.9, §7.7). A `run`
  result means "Task stays `active`; start the next Run later", never
  "continue working now".
- A `human → human` step is unsupported in v0: a decision never
  produces another `human` action, so `/skillflow:decide` output
  never points back at itself (SF-A-5 §7.7).

## Rule 6 — No manual state changes

Change lifecycle state only through the four commands (SF-A-5 §3.5).

- Must not: hand-edit anything under `.skillflow/` (SQLite rows,
  artifact metadata, events) or fabricate Task status, Run status,
  Result, HumanDecision, or lifecycle-event content.
- Must not: work around a rejection by editing state. Recovery is
  always "fix the cause in project files, re-invoke the protocol
  command" (see Rejection and recovery).
- Project files outside `.skillflow/` may be created or modified
  freely during the Run; expected durable outputs become Artifacts at
  completion (SF-A-5 §6.4).

## Rule 7 — Report the next action

After `/skillflow:complete-run` or `/skillflow:decide`, report to the
user: that the Run is complete, a concise Run summary, the evaluated
next action, and the exact command that starts the next step
(SF-A-5 §6.10, §7.8; SF-A-3 §10).

- Must: take the next action and the entry command from the runtime's
  printed output (templates below); never invent a different next
  step or command.
- Must: write the Run summary yourself — the runtime reports
  lifecycle facts, not what the work accomplished.
- Must: when the output names a step or a skill, give the
  `/skillflow:resolve-task <task-id>` pointer and state that it runs
  in a new session (skill-targeted Runs resolve since SF-32). When it
  names neither (terminal status, or no lifecycle action), report the
  status without inventing a resolve pointer.

Report shapes (from [`cli.py`](../src/skillflow/cli.py)
`format_completion:257-302` and `format_decision:305-341`):

Next Run (step-targeted):

```text
Run <run-id> completed.

Next action:
Run <step>.

Start the next Run in a new Claude Code session:

/skillflow:resolve-task <task-id>
```

Next Run (skill-targeted):

```text
Run <run-id> completed.

Next action:
Run skill '<skill>' (reason: <reason>).

Start the next Run in a new Claude Code session:

/skillflow:resolve-task <task-id>
```

Human decision required (completion only — `decide` never yields this):

```text
Run <run-id> completed.

Next action:
Human decision required.

Use:

/skillflow:decide <decision>
```

Task finished or cancelled (completion):

```text
Run <run-id> completed.

Task status:
<completed|cancelled>
```

After `/skillflow:decide`, the same shapes apply under the header
`Human decision recorded: <decision>.`, with terminal outcomes
rendered as `Task <completed|cancelled>.` (SF-A-5 §7.8).

## Prohibited commands

These are not v0 commands and must never be invoked, invented, or
emulated (SF-A-5 §11):

```text
start-run
create-run
create-artifact
create-result
transition
next-step
retry
rework
loop
iteration
handoff
```

Their behavior is either internal to the four commands or emerges
from the domain model (e.g. retry is a new Run via `resolve-task`,
not a `retry` command).

## Rejection and recovery

A rejected command never partially applies a lifecycle transition
(SF-A-5 §8). Every rejection below leaves the Run/Task exactly as it
was; recovery is always to fix the named cause and re-invoke the same
command — never to edit `.skillflow/` state (rule 6).

Every rejection is printed to stderr (exit code 1, stdout empty) in one
uniform envelope:

```text
skillflow <command>: <CODE>: <what happened and how to recover>
<what state was left unchanged>
```

`<CODE>` is the stable rejection identifier from the tables below (or the
lower-layer error name, e.g. `EvaluationError`, when the rejection carries
no code). The second line always states the preserved state, e.g. no Run
was created, or no Result was created and no Run status changed.

`resolve-task` (codes from [`resolve_task.py`](../src/skillflow/resolve_task.py)):

| Code | Meaning | Recovery |
|---|---|---|
| `TaskNotFound` | No Task with that id | Check the id |
| `TaskAlreadyCompleted` / `TaskCancelled` | Task is terminal | Nothing to run |
| `HumanDecisionRequired` | Task waits for human | Use `/skillflow:decide` |
| `ActiveRunExists` | A Run is already `running` | Do not resolve again |
| `WorkflowSelectionRequired` | Task has no Workflow | Ask the user, re-run with `--workflow <id>` |
| `WorkflowMismatch` / `NoLifecycleAction` / `RunNotCompleted` / `ResultMissing` / `StepUnresolved` | History/workflow inconsistent with a new Run | Escalate; do not invent a step |

`prepare-artifacts` (codes from
[`prepare_artifacts.py`](../src/skillflow/prepare_artifacts.py)):

| Code | Meaning | Recovery |
|---|---|---|
| `RunNotFound` / `RunNotActive` | No `running` Run to inspect | Start one via `resolve-task`; a Run is never resumed |
| `AmbiguousCurrentRun` | Several Runs `running` in workspace | Re-run with `--task <task-id>` |
| `TaskNotFound` | No Task with that id | Check the id |
| `StepUnresolved` / `WorkflowMismatch` | Run has no declared outputs | Record the outcome with `/skillflow:complete-run` |

`complete-run` (codes from [`cli.py`](../src/skillflow/cli.py)
`_load_submissions`, [`complete_run.py`](../src/skillflow/complete_run.py),
[`completion.py`](../src/skillflow/completion.py)):

| Code | Meaning | Recovery |
|---|---|---|
| `RunNotFound` / `RunNotActive` | No `running` Run to complete | Resolve/start the Run first |
| `RequiredArtifactsMissing` | Required outputs absent | Run `/skillflow:prepare-artifacts`, create them |
| `InvalidArtifactSubmission` | Submission malformed, file unreadable/non-UTF-8, name invalid or duplicate, or type-chain mismatch | Fix the `NAME:TYPE:PATH` spec or file, re-run `/skillflow:complete-run` |
| `InvalidOutcome` / `OutcomeRequired` / `OutcomeNotExpected` | Outcome missing, unexpected, or undeclared | Use a declared outcome, or omit it |
| `StepUnresolved` | Skill Run names no workflow/trigger/step to validate the outcome | Re-run without `--outcome`, or escalate |
| `WorkflowMismatch` | Step/history inconsistent | Escalate; do not invent an outcome |

`decide` (codes from [`decide.py`](../src/skillflow/decide.py),
[`decisions.py`](../src/skillflow/decisions.py)):

| Code | Meaning | Recovery |
|---|---|---|
| `HumanDecisionNotExpected` | Task is not `waiting_for_human` | No decision to record |
| `TaskAlreadyCompleted` / `TaskCancelled` | Task is terminal | Nothing to decide |
| `TaskNotFound` | No Task with that id | Check the id |
| `AmbiguousCurrentTask` | Several Tasks waiting | Re-run with `--task <task-id>` |
| `InvalidHumanDecision` | Decision not allowed by step | Use an allowed decision |
| `RunNotFound` / `RunNotCompleted` / `ResultMissing` / `StepUnresolved` / `WorkflowMismatch` | History/workflow inconsistent | Escalate; do not invent a decision |

`fail-run` (codes from [`fail_run.py`](../src/skillflow/fail_run.py) and
[`cli.py`](../src/skillflow/cli.py) `_load_submissions`/`_load_diagnostics`):

| Code | Meaning | Recovery |
|---|---|---|
| `RunNotFound` / `RunNotActive` | No `running` Run to fail | Resolve/start the Run first |
| `AmbiguousCurrentRun` | Several Runs `running` in workspace | Re-run with `--task <task-id>` |
| `TaskNotFound` | No Task with that id | Check the id |
| `StepUnresolved` | Retry target cannot be resolved | Escalate; do not invent a target |
| `InvalidDiagnostics` | Blank `--message`, or unreadable/non-UTF-8 diagnostics file | Fix the flag or file, re-run |
| `InvalidArtifactSubmission` | Submission malformed, file unreadable/non-UTF-8, name invalid or duplicate, or type-chain mismatch | Fix the `NAME:TYPE:PATH` spec or file, re-run |

