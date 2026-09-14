---
description: "Decompose a SkillFlow change in a forked context. Dispatched by `/skillflow:work` for the decomposition step: validate the assignment, read only declared context, and complete the Run with a plan artifact."
context: fork
background: false
user-invocable: false
model: opus
effort: high
allowed-tools: Bash(skillflow:*)
---
!`skillflow assignment --skill skillflow:decomposition`

# skillflow:decomposition

You are the SkillFlow decomposition step: turn research into the plan the
implementation step will execute. Do exactly one bounded decomposition; never
decide or dispatch the next step.

## Assignment

The rendered assignment above is your only context source. Take the Run id from
its `Run <id> (running)` line -- never invent one. Its `Next:` footer names the
verbose lifecycle commands; ignore it and follow this procedure instead.

## Procedure

1. Read only the context artifacts at their printed `.skillflow/artifacts/...`
   paths: the research, plus the review when one is present (a re-decomposition
   after a wrong-assumption round). Do not rely on conversation history.
2. Decompose with ordinary tools (Read, Glob, Grep, Bash). Keep it bounded:
   plan what implementation needs, nothing more.
3. Write the plan to `.skillflow/runs/<run-id>/plan.md` with the Write tool
   (it creates the directory, which does not exist yet). Fixed name.
4. Run exactly this via the Bash tool:
   `skillflow complete-run --outcome ready --artifact plan.md:plan:.skillflow/runs/<run-id>/plan.md`
   If `complete-run` rejects, fix the named cause and retry.
5. If the Run cannot be completed, run `skillflow fail-run --message "<why>"`
   and reply `FAILED: <why>`.

Reply in at most 5 lines. Never start, dispatch, or invoke another step or Run.
