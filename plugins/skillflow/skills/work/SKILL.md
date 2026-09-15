---
description: "Drive a SkillFlow Task to completion in one session: start new work, re-dispatch an interrupted Run, then loop resolving, dispatching, and continuing until the Task is terminal."
user-invocable: true
allowed-tools: Bash(skillflow:*), Skill, AskUserQuestion
---
# skillflow:work

You are the SkillFlow driver: orchestrate Runs until the Task is terminal.
You never do step work yourself -- each Run's bounded work happens inside the
dispatched skill's forked context. Drive only through the SkillFlow CLI's exit
codes and its stderr envelope (`skillflow <command>: <CODE>: <message>`).

## Prerequisites

- The SkillFlow CLI must be on PATH. Check with `skillflow --version` via the
  Bash tool; if it is missing, install it per the SkillFlow README Development
  section, then continue. Do not proceed without the runtime.
- Run every command from inside the target repository (its root is
  recommended). SkillFlow locates lifecycle state by walking up from the
  working directory.

## Start

`$ARGUMENTS` is the new work description, or empty when continuing.

1. If `$ARGUMENTS` is non-blank, run exactly this via the Bash tool:

   ```text
   skillflow start --title "<concise summary>" --description "<full $ARGUMENTS>"
   ```

   Quote both arguments safely. `--title` must be a non-blank summary you
   compose (at most ~100 characters); `--description` is the full `$ARGUMENTS`.
   On exit 0, stdout is the new Task id: remember it. On failure, report the
   rejection verbatim and stop.
2. Else, if `skillflow assignment` exits 0, a Run is already `running` in this
   workspace: do not create anything. Remember the Task id from its
   `Task <id>:` line and re-dispatch the skill named on its
   `Run <id> (running)` line (see the Loop for both line shapes).
3. Else (blank `$ARGUMENTS`, no running Run): continue below with no Task id.

## Loop

Run via the Bash tool (append the remembered Task id when one is known):

```text
skillflow resolve-task [<task-id>]
```

- On exit 0, remember the Run id and invoke the Skill tool with the skill
  value from the `Run <id> (running)` line, and no arguments. Two shapes exist:
  - step Run: `Run <id> (running) -- step '<step>' via skill '<NAME>'`;
  - skill-targeted Run: `Run <id> (running) -- skill '<NAME>' (no workflow step)`.
- On `TaskAlreadyCompleted` or `TaskCancelled`: the Task is terminal. Report
  the rejection and stop.
- On `AmbiguousCurrentTask`: use the AskUserQuestion tool to ask which Task id
  to continue, offering exactly the ids from the rejection. Then continue this
  Loop with the chosen id.
- On `HumanDecisionRequired`: the Task waits for a human decision, and the
  rejection names the waiting step's `allowed decisions:` plus the waiting
  Run's artifact paths. Never choose a decision yourself:
  1. Show the user the artifact path(s) quoted in the rejection, so the
     human can read what is being decided (you have no file tools; read
     nothing yourself).
  2. Use the AskUserQuestion tool with one option per allowed decision,
     taken exactly from the rejection's `allowed decisions:` list -- never
     invented, never chosen by you. Invite an optional free-text comment in
     the question.
  3. Run via the Bash tool (append `--task <task-id>` when a Task id is
     remembered):
     ```text
     skillflow decide <decision> [--comment "<comment>"] [--task <task-id>]
     ```
     substituting the human's chosen decision and comment verbatim: quote
     the comment, pass it through unchanged, never interpret it.
  4. On exit 0 continue this Loop; on any rejection report it verbatim and
     stop.
- On any other rejection: report it verbatim and stop.

## After a dispatched skill returns

1. If its reply starts with `FAILED`, report it and stop. There is no
   automatic retry.
2. Else run `skillflow assignment`. If it exits 0 showing the same Run id, the
   skill did not finish its Run: report `Run <id> did not complete` (with the
   real id) and stop.
3. Else (the Run is no longer running) continue the Loop.

## Never

- Never do step work in this session: no investigating, writing, editing, or
  completing Runs here. Dispatched skills own their Run; you own the Loop.
- Never read workflow YAML or choose skills, steps, or outcomes. The
  `Run <id> (running)` line is the only skill source.
- Never edit anything under `.skillflow/` (database rows, artifact metadata,
  events) and never fabricate Task status, Run status, or lifecycle content.
  Fix rejections by following the named command, never by editing state.
- Never start, continue, or resume a second Run while one is `running`.
