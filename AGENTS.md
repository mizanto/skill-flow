# AGENTS.md

## Project

Skill Flow is a lightweight lifecycle orchestration layer for connecting independent bounded Runs.

Claude Code is the execution layer. Skill Flow owns durable lifecycle state, durable artifacts, deterministic context selection, and deterministic lifecycle evaluation.

Keep the project small. Do not turn it into another AI-agent runtime or a generic workflow engine.

## Core domain model

First-class concepts:

- Task — work whose lifecycle can span multiple independent Runs.
- Run — a bounded execution intended to advance a Task.
- Result — the canonical observation of a Run.
- Artifact — durable context produced by a Run.
- Workflow Definition — the normal procedure for a type of Task.

Other concepts:

- Context Selection — deterministic selection of durable context.
- Lifecycle Evaluation — deterministic selection of the next lifecycle action.
- Human Decision — optional human input.

Do not introduce first-class entities for Router, Transition, Loop, Iteration, Rework, Handoff, Workflow Instance, or Stage.

## Task and Run rules

Task statuses:

- `active`
- `waiting_for_human`
- `completed`
- `cancelled`

Run statuses:

- `running`
- `completed`
- `failed`
- `waiting_for_human`
- `cancelled`

There is no `pending` Run state.

A failed Run does not automatically fail its Task.

A retry/continuation is a new Run. A Run is never resumed.

Only one Run may be `running` for a Task.

Run provenance uses `triggered_by_run_id` and `trigger_reason`. The initial Run uses `trigger_reason: initial`.

## Results and Artifacts

There is exactly one canonical Result per Run.

Result is an immutable observation. Human-readable outputs belong in Artifacts.

Artifacts are the durable context connecting independent Runs.

MVP:

- artifact content is stored in the filesystem;
- artifact metadata is stored in SQLite;
- content and Skill Flow metadata are separate;
- artifacts are logically immutable;
- new content creates a new artifact object/version;
- Git is not required for artifact versioning.

During a Run, normal project files may be created or modified freely. Expected durable outputs are checked before completion.

## Workflow Definition

A Workflow Definition is a definition, not a runtime Workflow Instance.

A step may define:

- id
- skill
- model
- effort
- expected outputs/artifacts
- allowed outcomes
- outcome-to-action mappings
- workflow-specific human decisions

Do not build a generic workflow DSL.

## Lifecycle Evaluation

Lifecycle Evaluation is deterministic in v0.

Conceptual input:

- Task
- Workflow Definition
- current Run
- Result
- optional Human Decision

Output action:

- `run`
- `human`
- `complete`
- `cancel`

It must not:

- launch Claude Code;
- create the next Run;
- perform project work;
- act as an AI agent.

Invalid outcomes must be rejected, not turned into invented transitions.

## Context Selection

Context Selection is deterministic in v0.

Pass only the durable context required by the next Run.

Do not automatically pass:

- full Task history;
- previous Results;
- full Claude Code transcripts;
- accumulated conversation context.

Do not use an LLM for Context Selection in v0.

## Persistence

MVP persistence:

```text
SQLite     = lifecycle state + metadata + events
Filesystem = artifact content + diagnostics
Claude Code = transcript
```

Conceptual SQLite tables:

- tasks
- runs
- results
- artifacts
- workflow_definitions
- human_decisions
- lifecycle_events

Lifecycle events are audit/debug/history data, not event sourcing. Current state must be directly queryable without replaying events.

Skill Flow-owned state lives under `.skillflow/` at the target repository root.

## Execution boundary

Each Run is one independent Claude Code session in the MVP.

Skill Flow does not automatically launch Claude Code.

A completed Run must not continue into another Run in the same session.

Intended flow:

```text
new Claude Code session
  ↓
/skillflow:resolve-task <task-id>
  ↓
bounded work
  ↓
/skillflow:prepare-artifacts
  ↓
create missing durable artifacts
  ↓
/skillflow:complete-run
  ↓
Result + Lifecycle Evaluation
  ↓
next action
```

The next Run is started in a new Claude Code session.

## Public commands

```text
/skillflow:resolve-task <task-id>
/skillflow:prepare-artifacts
/skillflow:complete-run
/skillflow:decide <decision>
```

Do not add `start-run`, `create-artifact`, `create-result`, `transition`, `next-step`, `retry`, `rework`, `loop`, `iteration`, or `handoff`.

## Command responsibilities

### `resolve-task`

- validates Task state;
- requires explicit Workflow selection if missing;
- resolves the current Workflow step;
- selects durable context;
- creates a `running` Run;
- returns RunInput.

It does not launch Claude Code.

### `prepare-artifacts`

- checks expected outputs;
- reports existing/missing durable artifacts;
- guides creation of missing artifacts.

It does not change lifecycle state, create a Result, evaluate lifecycle, or create another Run.

### `complete-run`

- validates required artifacts;
- validates the completion outcome;
- registers artifacts;
- creates exactly one canonical Result;
- marks the Run completed;
- evaluates lifecycle;
- updates Task status if necessary.

On validation failure, the Run remains `running`; no Result/evaluation occurs.

It does not create the next Run.

### `decide`

- accepts decisions only while the Task is `waiting_for_human`;
- validates the decision against the Workflow/step;
- persists a Human Decision;
- evaluates lifecycle;
- updates Task status.

It does not create the next Run. `human → human` is not supported in v0.

## Engineering principles

1. Prefer the smallest implementation that satisfies the requirement.
2. Reuse existing abstractions.
3. Keep domain logic independent from Claude Code.
4. Keep lifecycle logic deterministic.
5. Keep durable context explicit.
6. Avoid speculative abstractions.
7. Do not expand task scope without a concrete requirement.
8. Preserve existing behavior unless change is required.
9. Add tests for new behavior and edge cases.
10. Never weaken tests just to make them pass.
11. Keep state mutations atomic where applicable.
12. Treat SQLite/filesystem consistency as an explicit boundary.
13. Make failures actionable and preserve useful diagnostics.
14. Document architectural deviations instead of hiding them.

## Out of scope for MVP

Do not introduce:

- automatic Claude Code launching;
- distributed execution;
- PostgreSQL;
- Temporal;
- LangGraph;
- OpenHands;
- LLM-based lifecycle routing;
- LLM-based context selection;
- embeddings/vector databases;
- generic workflow DSLs;
- Router/Transition/Loop/Iteration/Rework/Handoff entities;
- Workflow Instance;
- Git-based artifact versioning;
- object storage;
- multi-user concurrency infrastructure beyond one active Run per Task;
- complex permission/policy engines;
- telemetry platforms;
- budget engines.

## Development workflow

Use bounded development stages:

```text
Plan
  ↓
Implement + Test
  ↓
Review
  ↓
Approved / Changes Requested
```

Durable artifacts:

- `plan.md`
- `implementation-report.md`
- `test-report.md`
- `review.md`

If implementation materially deviates from the approved plan, update `plan.md` and document the deviation.

Review against:

1. the task;
2. the approved plan;
3. the Skill Flow architecture;
4. tests;
5. MVP constraints.

The first architectural milestone is:

```text
resolve-task
→ prepare-artifacts
→ complete-run
```

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
