---
description: 'Complete the current running Run: validate artifacts and outcome, create the canonical Result, evaluate the lifecycle, and report the next action. Use once, after creating the outputs named by /skillflow:prepare-artifacts.'
allowed-tools: "Bash(skillflow:*)"
---

# /skillflow:complete-run

Finish the current `running` Run exactly once. On success the runtime
registers artifacts, creates exactly one canonical Result, marks the Run
`completed`, evaluates the lifecycle, and reports the next action. It never
creates the next Run.

## Prerequisites

- The `skillflow` CLI must be on PATH. Check with `skillflow --version`; if it
  is missing, install it per the SkillFlow README Development section, then
  continue. Do not proceed without the runtime.
- Run every command from inside the target repository (its root is
  recommended). SkillFlow locates lifecycle state by walking up from the
  working directory, and artifact file paths resolve from it.

## Procedure

1. Assemble one `--artifact NAME:TYPE:PATH` submission per durable output
   created for this Run: NAME is a plain filename (no directories, separators,
   or drive letters), TYPE is the declared output type, PATH is the file to
   read (UTF-8), relative to the repository root. Repeat the flag for each
   output. Required outputs are satisfied from artifacts already registered
   for this Run plus these submissions.
2. Choose `--outcome <decision>` only when the current step declares outcomes,
   using a declared value; omit the flag when the step declares none. Never
   guess an outcome.
3. Run exactly this command via the Bash tool:

   ```text
   skillflow complete-run [--task <task-id>] [--outcome <decision>] [--artifact NAME:TYPE:PATH ...]
   ```

   including `--task <task-id>` only when several Runs are `running` in this
   workspace.
4. On success the runtime prints the evaluated next action. Report to the
   user: that the Run is complete, a concise summary of what the work
   accomplished (write this yourself -- the runtime reports lifecycle facts,
   not the work), the next action, and the exact entry command from the
   output. Never invent a different next step or command:
   - step-targeted `run`: give the `/skillflow:resolve-task <task-id>`
     pointer and state that it runs in a new Claude Code session;
   - skill-only `run`: report the skill and reason, and give the
     `/skillflow:resolve-task <task-id>` pointer for a new Claude Code
     session (skill-targeted Runs resolve);
   - `human`: report that a human decision is required and give
     `/skillflow:decide <decision>`;
   - `complete` / `cancel`: report the terminal Task status.
5. End the session's lifecycle work here. Never invoke
   `/skillflow:resolve-task` again in this session: completing never creates
   the next Run, not even for a `run` action. A `run` result means "Task stays
   `active`; start the next Run later", never "continue working now".

## If the Run failed

When the bounded work cannot be completed at all (blocked, broken
environment, wrong assignment), do NOT complete the Run as successful: a
completed Result for failed work is a false observation. Instead report the
failure to the user -- what failed, and which partial outputs exist as
ordinary files -- and recommend the operator command that records it:

```text
skillflow fail-run --task <task-id> --message "<failure summary>" [--artifact NAME:TYPE:PATH ...]
```

It marks the Run `failed` with a failed Result, stores the summary in
`runs/<run-id>/output.log`, registers the partial outputs so the retry can
use them, and leaves the Task `active`. The retry is a new Run via
`/skillflow:resolve-task <task-id>` in a new Claude Code session; it
receives a clean same-step assignment, never the diagnostics.

## Recovery

On any rejection the Run stays `running` with no Result created and no
evaluation performed. Fix the named cause and re-invoke this command; never
work around a rejection by editing anything under `.skillflow/`.

- `RequiredArtifactsMissing`: run `/skillflow:prepare-artifacts` and create
  the missing outputs.
- `InvalidArtifactSubmission`: fix the `NAME:TYPE:PATH` spec or file
  (malformed, unreadable, non-UTF-8, invalid or duplicate name, or TYPE
  mismatch with the established chain).
- `InvalidOutcome` / `OutcomeRequired` / `OutcomeNotExpected`: use a
  step-declared outcome, or omit the flag.
- `StepUnresolved`: the skill-targeted Run names no workflow, trigger, or
  triggering step to validate the outcome against. Re-run without
  `--outcome`, or escalate to the user.
- `RunNotFound` / `RunNotActive`: resolve and start the Run first with
  `/skillflow:resolve-task <task-id>`.
- `WorkflowMismatch`: history is inconsistent with completion. Escalate to the
  user; do not invent an outcome.

## Rules

- Lifecycle state changes only through the four `/skillflow:*` commands.
  Never hand-edit anything under `.skillflow/` (database rows, artifact
  metadata, events) and never fabricate Task status, Run status, Result,
  HumanDecision, or lifecycle-event content.
- These are not SkillFlow commands. Never invoke, invent, or emulate them:

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
