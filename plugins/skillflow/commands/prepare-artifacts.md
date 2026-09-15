---
description: Report which durable outputs the current running Run still needs, so the missing artifacts can be created before completion. Use after the bounded work and before /skillflow:complete-run.
allowed-tools: "Bash(skillflow:*)"
---

# /skillflow:prepare-artifacts

> Manual entry point: /skillflow:work is the normal path; use this command directly for manual or recovery operation.

Check the current `running` Run's expected durable outputs and create every
missing one. This command never changes lifecycle state: no Result, no
evaluation, no new Run. Re-running it is always safe. It inspects registered
artifact metadata only, not the working tree: a file just created still
reports as missing until `/skillflow:complete-run` registers it. That is the
normal flow, not an error.

## Prerequisites

- The `skillflow` CLI must be on PATH. Check with `skillflow --version`; if it
  is missing, install it per the SkillFlow quick-start (`docs/quick-start.md`),
  then continue. Do not proceed without the runtime.
- Run every command from inside the target repository (its root is
  recommended). SkillFlow locates lifecycle state by walking up from the
  working directory.

## Procedure

1. Run exactly this command via the Bash tool:

   ```text
   skillflow prepare-artifacts
   ```

2. Read the report: one line per declared output, marked satisfied or missing
   in declaration order.
3. If every required output is satisfied, continue with
   `/skillflow:complete-run`.
4. Otherwise create each missing output as a normal file with ordinary Claude
   Code tools, then continue with `/skillflow:complete-run`, which takes the
   created files as `--artifact NAME:TYPE:PATH` submissions (NAME is the
   filename chosen, TYPE the declared output type, PATH the file location).
   There is no `create-artifact` command: Claude owns content creation,
   SkillFlow owns metadata and lifecycle.

## Recovery

The runtime reports inspection failures without changing state. Fix the named
cause and re-invoke this command; never work around a failure by editing
anything under `.skillflow/`.

- `AmbiguousCurrentRun`: several Runs are `running` in this workspace. Re-run
  the command above with `--task <task-id>` appended to select which Run to
  read; the flag changes nothing else.
- `RunNotFound` / `RunNotActive`: no `running` Run exists in this workspace.
  Start one with `/skillflow:resolve-task <task-id>`; a Run is never resumed.
- `TaskNotFound`: check the Task id.
- `StepUnresolved` / `WorkflowMismatch`: the Run has no declared usable
  outputs. Record the outcome with `/skillflow:complete-run`.

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
