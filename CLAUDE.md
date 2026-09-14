# CLAUDE.md

## Project

You are working on Skill Flow: a lightweight lifecycle orchestration layer for independent bounded Claude Code Runs.

Claude Code performs the actual work. Skill Flow owns durable lifecycle state, artifacts, deterministic Context Selection, and deterministic Lifecycle Evaluation.

Keep the implementation small and explicit.

## Working environment

Do not create or use git worktrees. Do not call `EnterWorktree`, and do not run
`git worktree add`. Work in the main checkout, on the currently checked-out
branch. If a session is launched inside `.claude/worktrees/`, report that and
stop rather than proceeding there.

## Source of truth

Before non-trivial changes, read the relevant specifications. They live in the
YouTrack knowledge base (project `SF`), not in this repository:

| Article | Title | Covers |
|---|---|---|
| [SF-A-1](https://bendak.youtrack.cloud/articles/SF-A-1) | Domain Model v0 | Task, Run, Result, Artifact, Workflow Definition, Human Decision |
| [SF-A-2](https://bendak.youtrack.cloud/articles/SF-A-2) | Persistence v0 | SQLite tables, `.skillflow/` workspace, filesystem/DB boundary |
| [SF-A-3](https://bendak.youtrack.cloud/articles/SF-A-3) | Execution Boundary v0 | Claude Code as execution layer, Run session model |
| [SF-A-4](https://bendak.youtrack.cloud/articles/SF-A-4) | Lifecycle Evaluation v0 | Deterministic outcome-to-action rules, acceptance scenarios |
| [SF-A-5](https://bendak.youtrack.cloud/articles/SF-A-5) | Command Contract v0 | `resolve-task`, `prepare-artifacts`, `complete-run`, `decide` |
| [SF-A-6](https://bendak.youtrack.cloud/articles/SF-A-6) | Implementation Plan v0 | `SF-NNN` plan references and their wave ordering |

`SF-NNN` identifiers in plans and commit messages are planning references from
SF-A-6; the actual work items are YouTrack issues (`SF-1`, `SF-2`, …), which map
to the plan references but are numbered independently.

If specifications contradict each other, do not silently reinterpret the architecture. Report the contradiction.

## Architecture rules

- Claude Code is the execution layer.
- Skill Flow is the lifecycle orchestration layer.
- Runs are bounded and independent.
- Artifacts are durable context between Runs.
- Lifecycle Evaluation is deterministic.
- Context Selection is deterministic in v0.
- Workflow Definition is a definition, not a runtime instance.
- Skill Flow does not automatically launch Claude Code.
- Runs execute in isolated context: a `context: fork` Skill invocation or a new session. A driver Skill (`/skillflow:work`) may dispatch the next Run in the same session; an Execution Skill executes only its own Run.

Do not introduce Router, Transition, Loop, Iteration, Rework, Handoff, Workflow Instance, or Stage as first-class concepts.

Do not turn Skill Flow into a generic workflow engine.

## Development protocol

Develop Skill Flow using three primary Runs:

```text
Plan — Opus
    ↓
Implement + Test — Sonnet
    ↓
Review — Opus
```

### Run 1 — Plan — Opus

Use Opus in Plan Mode.

Read:

- the YouTrack task;
- relevant specifications;
- `AGENTS.md`;
- repository structure;
- relevant code and tests.

Do not modify source code.

Produce `plan.md` containing:

- objective;
- current state;
- proposed approach;
- files to create/change;
- implementation steps;
- domain/data model changes;
- API/command changes;
- test strategy;
- edge cases/failure modes;
- architectural constraints;
- explicitly out-of-scope items;
- Definition of Done;
- risks/open questions.

Before finishing, challenge the plan:

- Is there a simpler solution?
- Are abstractions necessary?
- Is responsibility in the correct layer?
- Does it introduce excluded MVP concepts?
- Is lifecycle logic deterministic?
- Is context propagation explicit?
- Are important tests missing?
- Is scope creeping?

Do not implement until the user has approved the plan.

### Run 2 — Implement/Test — Sonnet

Use Sonnet.

Read:

- the YouTrack task;
- `plan.md`;
- relevant specifications;
- `AGENTS.md`;
- current repository state.

Implement the approved plan.

Rules:

- do not expand scope;
- reuse existing abstractions;
- add/update tests;
- do not weaken tests;
- preserve existing behavior unless explicitly required.

If implementation reveals that the plan is materially wrong:

1. stop the affected work;
2. explain the discrepancy;
3. update `plan.md`;
4. document the deviation;
5. continue using the corrected plan.

After implementation:

- run focused tests;
- run the full suite;
- fix implementation failures;
- rerun tests.

Produce:

- `implementation-report.md`
- `test-report.md`

`implementation-report.md` must include:

- what was implemented;
- files changed;
- important decisions;
- deviations from the plan;
- reasons for deviations;
- known limitations;
- reviewer attention points.

`test-report.md` must include:

- commands executed;
- focused-test results;
- full-suite result;
- warnings or limitations.

Do not perform the final review in this Run.

### Run 3 — Review — Opus

Use Opus in read-only mode.

Read:

- the YouTrack task;
- `plan.md`;
- `implementation-report.md`;
- `test-report.md`;
- relevant specifications;
- `AGENTS.md`;
- actual git diff;
- changed files;
- relevant tests.

Review against:

1. original task;
2. approved plan;
3. Skill Flow architecture.

Check:

- correctness;
- lifecycle/state consistency;
- persistence behavior;
- error handling;
- test coverage;
- architectural boundaries;
- unnecessary abstractions;
- hidden coupling;
- scope expansion;
- plan compliance.

Specifically verify that the implementation does not introduce:

- automatic Claude Code launching;
- automatic next-Run creation;
- LLM lifecycle routing;
- LLM context selection;
- generic workflow DSL;
- excluded domain entities;
- unnecessary infrastructure.

Do not modify source code.

Produce `review.md`:

```markdown
# Review

## Verdict

APPROVED or CHANGES_REQUESTED

## Summary

...

## Findings

...

## Questions

...

## Good Decisions

...

## Plan Compliance

...

## Test Assessment

...

## Architecture Assessment

...

## Final Recommendation

APPROVED or CHANGES_REQUESTED
```

Each actionable finding must include:

- severity: `BLOCKER`, `MAJOR`, `MINOR`, or `NIT`;
- location;
- problem;
- why it matters;
- recommended fix.

Do not report stylistic preferences as defects.

## Changes requested

If Review returns `CHANGES_REQUESTED`:

```text
Review
  ↓
Sonnet implementation/fix
  ↓
Opus review
```

Use the smallest change that addresses the finding.

## Skill Flow command protocol

Public commands:

```text
/skillflow:resolve-task <task-id>
/skillflow:prepare-artifacts
/skillflow:complete-run
/skillflow:decide <decision>
```

Do not invent additional lifecycle commands.

When operating inside a Skill Flow Run:

1. Resolve the Task before lifecycle work.
2. Execute only the resolved Run (Execution Skills never start another Run).
3. Only the driver Skill (`/skillflow:work`) may dispatch the next Run in the same session.
4. Use Skill Flow commands for lifecycle state changes.
5. Use normal Claude Code tools for project work.
6. Prepare durable artifacts before completing the Run.
7. Complete the current Run before reporting the next action.
8. Never manually edit Skill Flow lifecycle state.
9. Report the next action and command needed for the next Run.

Before completing a Run:

```text
/skillflow:prepare-artifacts
```

must be used to verify expected durable outputs.

`/skillflow:complete-run` performs final validation and lifecycle evaluation. It does not create or launch the next Run. `resolve-task` remains the only Run creator; at most one Run is `running` per workspace; reachable Run statuses are `running`/`completed`/`failed`.

## Durable context

Use these artifacts to connect development Runs:

```text
plan.md
implementation-report.md
test-report.md
review.md
```

Do not pass entire previous conversations between Runs.

Prefer durable artifacts plus the actual repository state.

## Scope discipline

Do not add:

- speculative infrastructure;
- generic workflow abstractions;
- premature LLM integration;
- telemetry;
- budget management;
- distributed execution;
- unnecessary persistence infrastructure.

When uncertain, prefer the smallest implementation consistent with the specifications.

## Verification

Before declaring a coding task complete:

- run relevant tests;
- run the full test suite when practical;
- inspect the final diff;
- verify no unrelated files changed;
- verify the task is satisfied;
- verify architecture constraints;
- ensure durable artifacts are up to date.
