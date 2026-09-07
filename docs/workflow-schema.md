# Workflow Definition schema (v0)

The executable v0 shape of a **Workflow Definition** — the normal procedure for
a type of Task. Defined as frozen Python value objects in
[`skillflow/workflow.py`](../src/skillflow/workflow.py).

Sources: Domain Model v0 (SF-A-1 §8), Lifecycle Evaluation v0 (SF-A-4 §5–§10),
Command Contract v0 (SF-A-5 §4.8, §6.5, §7), Implementation Plan v0 (SF-A-6 §2).

## Boundaries

- **Not persisted.** SQLite stores only `domain.WorkflowDefinition` — an
  `(id, name)` identity row a Task references. The loaded procedure
  (`Workflow` / `WorkflowStep`) is configuration read from disk. Binding the two
  by id is a later issue (SF-008 / SF-010).
- **Not loaded here.** YAML parsing and file validation are SF-008. The
  reference `software-change` definition file is SF-009. This module is
  stdlib-only and performs no IO.
- **Not a runtime instance.** Only immutable value objects — no current-step
  pointer, no mutable state. The actual lifecycle is the sequence of Runs.
- **Not a generic workflow DSL.** A fixed field set, four action types, and
  outcome rules that are plain dict lookups keyed by a decision string. No
  conditions, expressions, predicates, nesting, or graph analysis
  (reachability / terminal-state detection is deliberately absent).

## Types

### `Workflow`

| Field   | Type                        | Notes |
|---------|-----------------------------|-------|
| `name`  | `str`                       | Required, non-blank, stripped. |
| `steps` | `tuple[WorkflowStep, ...]`  | Required, non-empty. Step `id`s must be unique. |

Behaviour:

- `initial_step -> WorkflowStep` — the normal starting step, i.e. `steps[0]`
  (SF-A-5 §4.5).
- `find_step(step_id) -> WorkflowStep | None`.

Construction also validates that every `OutcomeRule.step` referenced by any
step's `outcomes` or `decisions` names an existing step in the same workflow.
`OutcomeRule.skill` is **never** validated.

Every `WorkflowStep` and `Workflow` is unhashable — the `outcomes` / `decisions`
fields default to an empty `MappingProxyType` (not `None`, as in
`skillflow.domain`), so an instance is unhashable even with no mappings supplied.
Identify steps by `id`, not by `hash()`.

### `WorkflowStep`

| Field       | Type                          | Notes |
|-------------|-------------------------------|-------|
| `id`        | `str`                         | Required, non-blank, stripped. Unique within the workflow. |
| `skill`     | `str`                         | Required — `resolve-task` cannot build a `RunInput` without it (SF-A-5 §4.8). |
| `model`     | `str \| None`                 | Optional execution parameter. Free string (e.g. `opus`), not an enum. |
| `effort`    | `str \| None`                 | Optional execution parameter. Free string (e.g. `high`), not an enum. |
| `outputs`   | `tuple[ExpectedOutput, ...]`  | Default `()`. Must be iterable; duplicate `type` rejected. |
| `outcomes`  | `Mapping[str, OutcomeRule]`   | Default empty. Keys are Result-outcome decision strings (stripped, non-blank, no duplicates after stripping). |
| `decisions` | `Mapping[str, OutcomeRule]`   | Default empty. Keys are Human-Decision strings. A rule mapping to `action: human` is rejected (SF-A-5 §7.7). |

A step with no `outcomes` is allowed (SF-A-5 §6.5). A step with no `outputs` is
allowed (the reference `implementation` step declares none).

### `ExpectedOutput`

| Field      | Type   | Notes |
|------------|--------|-------|
| `type`     | `str`  | Required, non-blank, stripped. |
| `required` | `bool` | Default `True`. Must be a real `bool` (an `int` is rejected). |

Declares an expected durable output. Checking artifacts against it is SF-017.

### `OutcomeRule`

| Field    | Type          | Notes |
|----------|---------------|-------|
| `action` | `ActionType`  | Coerced from a string; an unknown value raises `ValueError`. |
| `step`   | `str \| None` | A step id in this workflow. |
| `skill`  | `str \| None` | A skill name; need not be a step, and is never validated. |

### `ActionType`

`run` · `human` · `complete` · `cancel` (SF-A-4 §5).

## Action shapes

| Shape | Rule | Meaning |
|-------|------|---------|
| Continue to a step | `{action: run, step: <id>}` | Next Run executes workflow step `<id>`. |
| Continue to a skill | `{action: run, skill: <name>}` | Next Run executes skill `<name>` with no workflow step (SF-A-4 §9). |
| Request a human decision | `{action: human}` | Task → `waiting_for_human`. |
| Finish | `{action: complete}` | Task → `completed`. |
| Abandon | `{action: cancel}` | Task → `cancelled`. |

`run` requires **exactly one** of `step` / `skill`. `human` / `complete` /
`cancel` must carry neither.

## Validation rules

All violations raise `ValueError` at construction.

- **`ExpectedOutput`**: `type` non-blank; `required` is a real `bool`.
- **`OutcomeRule`**: `action` is a known `ActionType`; `step` / `skill`
  non-blank when present; `run` ⇒ exactly one of `step` / `skill`; non-`run` ⇒
  neither.
- **`WorkflowStep`**: `id`, `skill` non-blank; `model`, `effort` non-blank when
  present; `outputs` iterable and all `ExpectedOutput` with no duplicate `type`;
  `outcomes` / `decisions` are mappings with non-blank keys (no duplicates after
  stripping) and `OutcomeRule` values; no `decisions` rule maps to `action:
  human`; a step with an `outcomes` rule of `action: human` **must** declare at
  least one `decisions` entry — otherwise `/skillflow:decide` would reject every
  decision (SF-A-5 §7.5) and strand the Task in `waiting_for_human`.
- **`Workflow`**: `name` non-blank; `steps` iterable, non-empty and all
  `WorkflowStep`; step `id`s unique; every referenced `OutcomeRule.step` exists.

## Illustrative example

> This example is **illustrative only**. It is not the SF-009 reference
> workflow, which is a YAML file loaded through SF-008. The v0 schema has no
> YAML loader yet; the example below shows the equivalent structure.

```yaml
name: software-change

steps:
  - id: requirements          # A: initial step (Workflow.initial_step)
    skill: requirements-analysis
    model: opus
    effort: high
    outputs:
      - type: requirements
        required: true
    outcomes:
      ready: { action: run, step: decomposition }   # A: normal progression

  - id: decomposition
    skill: decomposition
    model: opus
    effort: high
    outputs:
      - type: plan
        required: true
    outcomes:
      ready: { action: run, step: implementation }   # A

  - id: implementation          # no outputs / no outcomes declared here
    skill: implementation
    model: sonnet
    effort: high
    outcomes:
      ready: { action: run, step: review }           # A

  - id: review
    skill: code-review
    model: opus
    effort: high
    outputs:
      - type: review
        required: true
    outcomes:
      approved:                     { action: complete }                     # A: happy path
      changes_requested:            { action: run, step: implementation }     # B: review / rework
      fundamental_assumption_wrong: { action: run, skill: research }          # C: skill target, not a step
      human_required:               { action: human }                        # D: human decision
    decisions:
      approve:         { action: complete }                                  # D
      request_changes: { action: run, step: implementation }                 # D
      cancel:          { action: cancel }                                    # D
```

### Acceptance scenarios (SF-A-4 §14)

| Scenario | How the schema expresses it |
|----------|-----------------------------|
| **A — happy path** | Each step's `ready` outcome is `run` to the next step; `review/approved` is `complete`. |
| **B — review / rework** | `review/changes_requested` is `run` with `step: implementation`. No `Rework` / `Loop` entity. |
| **C — fundamental assumption wrong** | `review/fundamental_assumption_wrong` is `run` with `skill: research` (`step` is `None`); `research` is not a step and is not validated. |
| **D — human decision** | `review/human_required` is `human`; then `decisions` maps `approve → complete`, `request_changes → run/implementation`, `cancel → cancel`. A decision never maps back to `human`. |
