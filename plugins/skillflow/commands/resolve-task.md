---
description: Resolve a SkillFlow Task into a new running Run and start this session's bounded work. Use first, in a new Claude Code session, before doing any Task work.
argument-hint: '<task-id>'
allowed-tools: "Bash(skillflow:*)"
---

# /skillflow:resolve-task

Start a SkillFlow Run: validate the Task, create exactly one `running` Run,
and perform only that Run's bounded work in this session.

## Prerequisites

- The `skillflow` CLI must be on PATH. Check with `skillflow --version`; if it
  is missing, install it per the SkillFlow README Development section, then
  continue. Do not proceed without the runtime.
- Run every command from inside the target repository (its root is
  recommended). SkillFlow locates lifecycle state by walking up from the
  working directory.

## Arguments

`$ARGUMENTS` is the Task id. If it is empty, ask the user for the Task id.
Never invent a Task id, step, skill, or Workflow.

## Procedure

1. Run exactly this command via the Bash tool:

   ```text
   skillflow resolve-task <task-id>
   ```

   substituting the Task id for `<task-id>`.
2. On success the runtime prints the RunInput: Task, Run id, Workflow step,
   skill, execution parameters, selected durable context, and expected
   outputs. Treat it as the assignment for this session.
3. Do the bounded work for the resolved step with ordinary Claude Code tools
   (Read, Write, Edit, Bash, MCP). Nothing else: do not start, continue, or
   resume any other Run in this session.
4. When the work is done, continue with `/skillflow:prepare-artifacts`.

## Recovery

The runtime rejects invalid resolutions without changing state. Fix the named
cause and re-invoke this command; never work around a rejection by editing
anything under `.skillflow/`.

- `WorkflowSelectionRequired`: the Task has no Workflow. Ask the user which
  Workflow to use, then re-run the command above with `--workflow <id>`
  appended. The runtime never infers the Workflow.
- `HumanDecisionRequired`: the Task is `waiting_for_human`. Stop: report that
  `/skillflow:decide` is needed and do no Task work.
- `ActiveRunExists`: a Run is already `running` for this Task. Do not resolve
  again.
- `TaskNotFound`: check the Task id.
- `TaskAlreadyCompleted` / `TaskCancelled`: the Task is terminal; there is
  nothing to run.
- `WorkflowMismatch` / `NoLifecycleAction` / `RunNotCompleted` /
  `ResultMissing`: history is inconsistent with a new Run. Escalate to the
  user; do not invent a step.

## Rules

- One Run per session. A Run is never resumed; retry or continuation is always
  a new Run in a new session via this command. A failed Run does not fail the
  Task.
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

  Retry is a new Run via this command, not a `retry` command.
