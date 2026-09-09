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
- **Loaded separately.** `skillflow/workflow.py` is stdlib-only and performs no
  IO; reading a definition file into these objects is
  [`skillflow/workflow_loader.py`](../src/skillflow/workflow_loader.py), covered
  in [Loading](#loading) below. The reference `software-change` definition now
  ships at [`workflows/software-change.yaml`](../workflows/software-change.yaml)
  (SF-8).
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
| `context`   | `tuple[str, ...]`             | Default `()`. Artifact **types** this step consumes. Flat list of identifiers (stripped, non-blank, no duplicates after stripping). No conditions, patterns, or versions. |
| `outcomes`  | `Mapping[str, OutcomeRule]`   | Default empty. Keys are Result-outcome decision strings (stripped, non-blank, no duplicates after stripping). |
| `decisions` | `Mapping[str, OutcomeRule]`   | Default empty. Keys are Human-Decision strings. A rule mapping to `action: human` is rejected (SF-A-5 §7.7). |

A step with no `outcomes` is allowed (SF-A-5 §6.5), and it is terminal: its Run
completes without an outcome (`skillflow.completion.validate_outcome` returns
`None` and rejects any supplied outcome as `OutcomeNotExpected`), and Lifecycle
Evaluation resolves it to `{action: complete}` with reason `no_outcome`. Any
`decisions` such a step declares are unreachable, because only an `outcomes`
rule can produce the `human` action that reaches them. A step that should be
followed by another step must declare the rule that says so — the evaluator
never infers "the next step in the list". A step with no `outputs` is allowed
(the reference `implementation` step declares none).

### `ExpectedOutput`

| Field      | Type   | Notes |
|------------|--------|-------|
| `type`     | `str`  | Required, non-blank, stripped. |
| `required` | `bool` | Default `True`. Must be a real `bool` (an `int` is rejected). |

Declares an expected durable output. Checking a step's declared outputs against
the artifacts a Run registered is `skillflow.outputs.validate_outputs` (SF-16):
the match scope is the current Run (the `(task_id, name)` version chain spans
Runs), matching is by `type` exactly, and the declared constraint set is exactly
`{type, required}`.

### Context Selection

`WorkflowStep.context` lists the artifact **types** a step consumes. Resolving
that declaration against a Task's artifact metadata is
`skillflow.context.select_context` (SF-17), a pure, LLM-free operation:

- **Task scope.** Selection ranges over every artifact the Task has produced,
  across Runs — unlike output validation, which is per-Run. This is what lets a
  rework `implementation` Run receive the previous Run's `review`.
- **Latest version per name.** For each declared type, every distinct artifact
  `name` of that type is resolved to its chain head (highest `version`);
  superseded versions are never selected. Survivors are ordered by name;
  entries follow `context` declaration order.
- **Unresolved types are reported, not raised.** A declared type the Task has
  never produced (a first-pass `implementation` Run has no `review`) appears in
  `ContextSelection.unresolved`; it is not an error.
- **Metadata only.** Selection returns artifact references, never file content.

Declaring `context: [review]` on `implementation` says "this step reads
reviews" — it is not a transition. Rework routing stays in
`review.outcomes.changes_requested`.

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
| Continue to a skill | `{action: run, skill: <name>}` | Next Run executes skill `<name>` with no workflow step (SF-A-4 §9; see Skill-targeted Runs below). |
| Request a human decision | `{action: human}` | Task → `waiting_for_human`. |
| Finish | `{action: complete}` | Task → `completed`. |
| Abandon | `{action: cancel}` | Task → `cancelled`. |

`run` requires **exactly one** of `step` / `skill`. `human` / `complete` /
`cancel` must carry neither.

### Skill-targeted Runs

A `run` action carrying only `skill` creates a Run with no workflow step
(SF-A-4 §9). The v0 runtime meaning is SF-32's gap-fill (no specification
defines it; a spec follow-up is recorded):

- **RunInput**: `step_id` is `None`; `skill` comes from the action;
  `model` / `effort` are `None`; `outputs` is empty; context is the
  triggering Run's artifacts grouped by type
  (`skillflow.context.select_trigger_context`).
- **Completion**: a decision is validated against the triggering step's
  outcome table and persisted with that step as `Outcome.type`; a
  decisionless skill Run completes with no outcome and no action.
- **Evaluation**: the outcome is interpreted through the step named by
  `Outcome.type`. A skill-targeted Run resolves to a step, `human`,
  `complete`, or `cancel` — never to another skill-targeted Run.
- **Protocol**: `prepare-artifacts` has no declared outputs to inspect and
  keeps reporting `StepUnresolved`; `decide` resolves its step from the
  outcome that parked the Task.

## Validation rules

All violations raise `ValueError` at construction.

- **`ExpectedOutput`**: `type` non-blank; `required` is a real `bool`.
- **`OutcomeRule`**: `action` is a known `ActionType`; `step` / `skill`
  non-blank when present; `run` ⇒ exactly one of `step` / `skill`; non-`run` ⇒
  neither.
- **`WorkflowStep`**: `id`, `skill` non-blank; `model`, `effort` non-blank when
  present; `outputs` iterable and all `ExpectedOutput` with no duplicate `type`;
  `context` iterable of non-blank strings with no duplicate type (after
  stripping); `outcomes` / `decisions` are mappings with non-blank keys (no duplicates after
  stripping) and `OutcomeRule` values; no `decisions` rule maps to `action:
  human`; a step with an `outcomes` rule of `action: human` **must** declare at
  least one `decisions` entry — otherwise `/skillflow:decide` would reject every
  decision (SF-A-5 §7.5) and strand the Task in `waiting_for_human`.
- **`Workflow`**: `name` non-blank; `steps` iterable, non-empty and all
  `WorkflowStep`; step `id`s unique; every referenced `OutcomeRule.step` exists.

## Loading

A Workflow Definition is written as a YAML file in exactly the format of the
[example](#file-format) below, and read with
[`skillflow/workflow_loader.py`](../src/skillflow/workflow_loader.py):

```python
from skillflow.workflow_loader import WorkflowLoadError, load_workflow, parse_workflow

workflow = load_workflow("workflows/software-change.yaml")   # from a path
workflow = parse_workflow(text, source="inline.yaml")        # from a string
```

The loader owns **syntax and shape**; the schema above owns **meaning**. No
validation rule is implemented twice: the loader parses the document, checks its
structure, and constructs the value objects, letting their `ValueError` carry
every semantic rule.

There is no discovery: `load_workflow` reads the path it is given. The reference
definition lives at [`workflows/software-change.yaml`](../workflows/software-change.yaml);
how a *target* repository obtains and selects a definition remains SF-11's
decision. A loaded `Workflow` is configuration, not a runtime entity, and is not
persisted.

### Lookup by definition id

`resolve-task` maps a Task's `workflow_definition_id` to a file with
`workflow_loader.load_definition(directory, definition_id)`: the file is
`<directory>/<definition-id>.yaml`, and the loaded `Workflow.name` must equal
the id exactly (no case folding). File stem, `name:` and the Task's
`workflow_definition_id` therefore always agree. `list_definition_ids`
lists the available ids (sorted file stems) for the "select a Workflow"
prompt. The directory is `<repo-root>/workflows/` (`Workspace.workflows_dir`);
it holds committed project source and is never created by `init_workspace`.

### Strictness rules

Three rejections belong to the loader because they are invisible by the time
value objects exist:

- **Unknown keys are rejected** at every level (top level, step, output, rule).
  `outcome:` — the singular typo for `outcomes:` — fails loudly instead of
  silently producing a step with no outcomes. A fixed key set is also what keeps
  the file format from drifting into a generic workflow DSL.
- **Duplicate YAML keys are rejected.** PyYAML keeps the last duplicate
  silently, so two `approved:` entries under one `outcomes:` would otherwise
  load as valid with one rule discarded.
- **Null-valued keys are rejected**, not coerced to empty. A key written with no
  value is always an error: `outcomes:` alone, and equally `model:` alone. Where
  an empty collection is meaningful, write it explicitly — `{}` for `outcomes` /
  `decisions`, `[]` for `outputs`. This does not apply to `steps`, which is
  required and may not be empty.
- **Merge keys (`<<`) are rejected.** Anchors and aliases are fine — `skill:
  *shared` reuses a *value* — but `<<: *base` inherits one mapping's keys into
  another, which is definition reuse and the DSL drift this format avoids. Write
  each step out in full.

Only the **mapping form** of an outcome rule is accepted
(`approved: { action: complete }`). The shorthand `approved: complete` is not
supported — two syntaxes for one concept is the first step toward a DSL — and it
is rejected with a message that says so rather than a bare type error.

Note that YAML 1.1 booleans apply: `required: no` loads as `False` (correct),
while `skill: yes` loads as `True` and is then rejected as not a string. Quote
such values.

### Errors

Every failure — a missing path, a directory, undecodable bytes, invalid YAML, a
shape violation, or a schema violation — raises `WorkflowLoadError`. It is a
flat exception class (like `store.InvariantViolationError`) and deliberately
**not** a `ValueError` subclass, so catching it does not also swallow
programming errors. Every message names the source file and the location within
it:

```text
software-change.yaml: steps[3].outcomes['approved']: OutcomeRule with action
'complete' must not carry a 'step' or 'skill'
```

## File format

> The example below illustrates the file format. The real reference definition
> is [`workflows/software-change.yaml`](../workflows/software-change.yaml); this
> shows the equivalent structure.

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
    context: [requirements, research]               # 'research' resolves only on a post-research re-decomposition (SF-32)
    outputs:
      - type: plan
        required: true
    outcomes:
      ready: { action: run, step: implementation }   # A

  - id: implementation          # no outputs / no outcomes declared here
    skill: implementation
    model: sonnet
    effort: high
    context: [requirements, plan, review]           # 'review' resolves only on a rework Run
    outcomes:
      ready: { action: run, step: review }           # A

  - id: review
    skill: code-review
    model: opus
    effort: high
    context: [requirements, plan]
    outputs:
      - type: review
        required: true
    outcomes:
      approved:                     { action: complete }                     # A: happy path
      changes_requested:            { action: run, step: implementation }     # B: review / rework
      fundamental_assumption_wrong: { action: run, skill: research }          # C: skill target, not a step
      replan:                       { action: run, step: decomposition }      # C: the skill Run's continuation edge (SF-32)
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
| **C — fundamental assumption wrong** | `review/fundamental_assumption_wrong` is `run` with `skill: research` (`step` is `None`); `research` is not a step and is not validated. `review/replan` is `run` with `step: decomposition` — the skill Run's continuation edge (SF-32); `decomposition` consumes `research`. |
| **D — human decision** | `review/human_required` is `human`; then `decisions` maps `approve → complete`, `request_changes → run/implementation`, `cancel → cancel`. A decision never maps back to `human`. |
