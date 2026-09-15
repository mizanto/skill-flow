---
description: Record a human decision on a Task waiting for human input, evaluate the lifecycle, and report the next action. Use only while the Task is waiting_for_human.
argument-hint: '<decision> [--comment "..."]'
allowed-tools: "Bash(skillflow:*)"
---

# /skillflow:decide

> Manual entry point: /skillflow:work is the normal path; use this command directly for manual or recovery operation.

Record a Human Decision while the Task is `waiting_for_human`. On success the
runtime persists the decision (the previous Result is left unchanged),
evaluates the lifecycle, and reports the next action. It never creates the
next Run, and a decision never yields another `human` action.

## Prerequisites

- The `skillflow` CLI must be on PATH. Check with `skillflow --version`; if it
  is missing, install it per the SkillFlow quick-start (`docs/quick-start.md`),
  then continue. Do not proceed without the runtime.
- Run every command from inside the target repository (its root is
  recommended). SkillFlow locates lifecycle state by walking up from the
  working directory.

## Arguments

The first `$ARGUMENTS` token is the decision; it must be a value the current
Workflow step allows. Anything after `--comment` is free-text comment stored
with the decision; quote it when building the command below. If no decision
was given, ask the user for one. Never invent a decision.

## Procedure

1. Run exactly this command via the Bash tool:

   ```text
   skillflow decide <decision> [--comment <text>] [--task <task-id>]
   ```

   substituting the decision (and comment when given); include
   `--task <task-id>` only when several Tasks are waiting.
2. On success the runtime prints `Human decision recorded:` plus the evaluated
   next action. Report to the user: the recorded decision, a concise summary
   of what it concludes (write this yourself -- the runtime reports lifecycle
   facts, not the reasoning), the next action, and the exact entry command
   from the output:
   - step-targeted `run`: give the `Next: /skillflow:work` line from the
     output (manual alternative: `/skillflow:resolve-task <task-id>`);
   - skill-only `run`: report the skill and reason, and give the
     `Next: /skillflow:work` line (manual alternative:
     `/skillflow:resolve-task <task-id>`);
   - `complete` / `cancel`: report `Task completed.` / `Task cancelled.`
   A decision never yields another `human` action, so never print a second
   `/skillflow:decide` pointer.
3. End this context's lifecycle work here. Never start another Run in this
   context; continue with `/skillflow:work`.

## Recovery

On any rejection the Task stays `waiting_for_human` with no decision
persisted and no evaluation performed. Fix the named cause and re-invoke this
command; never work around a rejection by editing anything under
`.skillflow/`.

- `HumanDecisionNotExpected`: the Task is not waiting; there is no decision
  to record.
- `TaskAlreadyCompleted` / `TaskCancelled`: the Task is terminal; there is
  nothing to decide.
- `TaskNotFound`: check the Task id.
- `AmbiguousCurrentTask`: re-run the command above with `--task <task-id>`.
- `InvalidHumanDecision`: use a decision the current step allows.
- `RunNotFound` / `RunNotCompleted` / `ResultMissing` / `StepUnresolved` /
  `WorkflowMismatch`: history is inconsistent with a decision. Escalate to the
  user; do not invent a decision.

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
