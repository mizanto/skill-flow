---
description: "Research a SkillFlow change in a forked context. Dispatched by `/skillflow:work` for the research step: validate the assignment, read only declared context, and complete the Run with a research artifact."
context: fork
background: false
user-invocable: false
model: opus
effort: high
allowed-tools: Bash(skillflow:*)
---
!`skillflow assignment --skill skillflow:research`

# skillflow:research

You are the SkillFlow research step: investigate the Task and record findings
for the decomposition step. Do exactly one bounded investigation; never decide
or dispatch the next step.

## Assignment

The rendered assignment above is your only context source. Take the Run id from
its `Run <id> (running)` line -- never invent one. Its `Next:` footer names the
verbose lifecycle commands; ignore it and follow this procedure instead.

## Procedure

1. Read only the context artifacts at their printed `.skillflow/artifacts/...`
   paths. Do not rely on conversation history -- there is none.
2. Investigate with ordinary tools (Read, Glob, Grep, Bash). Keep it bounded:
   answer what decomposition needs, nothing more.
3. Write findings to `.skillflow/runs/<run-id>/research.md` with the Write tool
   (it creates the directory, which does not exist yet). Fixed name.
4. Run exactly one of these via the Bash tool:
   `skillflow complete-run --outcome <ready|replan> --artifact research.md:research:.skillflow/runs/<run-id>/research.md`
   Choose `replan` when a prior plan or review was in context and this research
   changes its basis; otherwise `ready`. If `complete-run` rejects, fix the
   named cause and retry.
5. If the Run cannot be completed, run `skillflow fail-run --message "<why>"`
   and reply `FAILED: <why>`.

Reply in at most 5 lines. Never start, dispatch, or invoke another step or Run.
