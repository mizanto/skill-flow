"""Skill Flow Workflow Definition schema (SF-6).

The executable v0 shape of a Workflow Definition: an ordered list of steps, each
with execution parameters, expected outputs, outcome-to-action rules, and
workflow-specific human decisions (SF-A-1 §8, SF-A-4 §6-§10, SF-A-6 §2).

Three boundaries, mirroring :mod:`skillflow.domain`:

* **No persistence.** SQLite holds only the identity a ``Task`` references --
  ``domain.WorkflowDefinition`` is ``(id, name)`` (SF-A-2 §4). The loaded
  procedure modelled here is configuration read from disk (SF-A-2 §4:
  "Workflow Definitions can be persisted as definitions/configuration. They are
  not runtime entities"). This module has no ``to_row`` / ``from_row``.
* **No YAML / IO.** Parsing a definition file into these objects is
  :mod:`skillflow.workflow_loader`; the reference ``software-change`` definition
  ships at ``workflows/software-change.yaml``. This module imports the standard
  library only and reads neither disk nor clock.
* **Not a runtime instance.** Only frozen value objects -- no current-step
  pointer, no mutable state, no ``WorkflowInstance``. A Workflow Definition
  describes normal procedure; the actual lifecycle is the sequence of Runs
  (SF-A-1 §8).

It is deliberately **not** a generic workflow DSL: a fixed field set, four
action types, and outcome rules that are plain dict lookups keyed by a decision
string -- no conditions, expressions, predicates, or nesting.

Construction is total: an invalid Workflow cannot be instantiated. Identifiers
are stripped and required non-empty; unknown ``action`` values raise
``ValueError``; mappings are wrapped in ``types.MappingProxyType`` (which makes
the instance unhashable -- identify steps by ``id``, not by ``hash()``).
"""

import types
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ActionType",
    "ExpectedOutput",
    "OutcomeRule",
    "WorkflowStep",
    "Workflow",
]


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.domain` on purpose: keeping this module's
    "imports no ``skillflow`` module" boundary mechanically checkable is worth
    ~a dozen lines, and matches how ``domain`` and ``workspace`` already stay
    independent. The alternative is a shared helper module no issue asks for.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _clean(instance: object, prefix: str, *names: str) -> None:
    """Strip and require each named string attribute on ``instance`` in place."""
    for name in names:
        value = _require_text(getattr(instance, name), f"{prefix}.{name}")
        object.__setattr__(instance, name, value)


def _clean_optional(instance: object, prefix: str, *names: str) -> None:
    """Like :func:`_clean`, but skip attributes that are ``None``."""
    for name in names:
        if getattr(instance, name) is not None:
            _clean(instance, prefix, name)


def _require_sequence(value: object, field_name: str) -> tuple:
    """Return ``value`` as a tuple, or raise ``ValueError`` if it is not iterable.

    A non-iterable (notably ``None`` -- what an empty YAML ``outputs:`` /
    ``steps:`` block parses to) is a caller/definition mistake, and this module
    reports every such mistake as ``ValueError`` (str/mapping rejections do the
    same). ``str`` is refused too: iterating it yields characters, never the
    intended elements.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field_name} must be an iterable")
    return tuple(value)


class ActionType(StrEnum):
    """The lifecycle actions an outcome rule can select (SF-A-4 §5).

    ``run`` continues the lifecycle (targeting a ``step`` or a ``skill``);
    ``human`` requests a Human Decision; ``complete`` / ``cancel`` are terminal.
    """

    RUN = "run"
    HUMAN = "human"
    COMPLETE = "complete"
    CANCEL = "cancel"


@dataclass(frozen=True, kw_only=True, slots=True)
class ExpectedOutput:
    """A durable output a step is expected to produce (SF-A-5 §4.8 ``outputs:``).

    ``type`` names the artifact kind (e.g. ``"review"``); ``required`` gates
    completion. Checking artifacts against this declaration is SF-017 -- this
    schema only declares it.
    """

    type: str
    required: bool = True

    def __post_init__(self) -> None:
        _clean(self, "ExpectedOutput", "type")
        if not isinstance(self.required, bool):
            raise ValueError("ExpectedOutput.required must be a bool")


@dataclass(frozen=True, kw_only=True, slots=True)
class OutcomeRule:
    """What a named outcome or decision means in this workflow (SF-A-4 §7).

    The rule carries an ``action`` and, for ``run``, exactly one target:

    * ``step`` -- a step id within this same Workflow (checked by
      ``Workflow.__post_init__``).
    * ``skill`` -- a skill name, which need **not** be a step. SF-A-4 §9 maps
      ``fundamental_assumption_wrong`` to ``{action: run, skill: research}`` and
      ``research`` is not a step in the reference workflow, so ``skill`` is
      never validated against the step list.

    ``human``, ``complete`` and ``cancel`` take no target.
    """

    action: ActionType
    step: str | None = None
    skill: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ActionType(self.action))
        _clean_optional(self, "OutcomeRule", "step", "skill")

        if self.action is ActionType.RUN:
            if (self.step is None) == (self.skill is None):
                raise ValueError(
                    "OutcomeRule with action 'run' requires exactly one of "
                    "'step' or 'skill'"
                )
        elif self.step is not None or self.skill is not None:
            raise ValueError(
                f"OutcomeRule with action '{self.action}' must not carry a "
                "'step' or 'skill'"
            )


def _freeze_rules(value: object, field_name: str) -> types.MappingProxyType:
    """Return a read-only snapshot of an outcome/decision mapping.

    Keys are outcome/decision strings (stripped, non-empty); values must already
    be ``OutcomeRule`` instances. Non-mappings are rejected rather than coerced.
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    frozen: dict[str, OutcomeRule] = {}
    for raw_key, rule in value.items():
        key = _require_text(raw_key, f"{field_name} key")
        if key in frozen:
            raise ValueError(f"{field_name} has a duplicate key {key!r}")
        if not isinstance(rule, OutcomeRule):
            raise ValueError(f"{field_name}[{key!r}] must be an OutcomeRule")
        frozen[key] = rule
    return types.MappingProxyType(frozen)


_EMPTY_MAP: Mapping[str, OutcomeRule] = types.MappingProxyType({})


@dataclass(frozen=True, kw_only=True, slots=True)
class WorkflowStep:
    """One step of normal procedure (SF-A-6 §2).

    ``skill`` is required: ``resolve-task`` cannot build a ``RunInput`` without
    it (SF-A-5 §4.8). ``model`` / ``effort`` are free strings, not enums --
    pinning them would couple this schema to Claude Code's model names
    (AGENTS.md principle 3).

    ``outcomes`` maps a Result outcome decision to an ``OutcomeRule``;
    ``decisions`` does the same for Human Decisions (SF-A-5 §7). Both may be
    empty: a step "may [have no] outcome ... for steps that do not require a
    lifecycle outcome" (SF-A-5 §6.5). A ``decisions`` rule with ``action:
    human`` is rejected -- "a human decision must not itself produce another
    ``human`` action" (SF-A-5 §7.7). Conversely, a step with an ``outcomes``
    rule of ``action: human`` must declare at least one ``decisions`` entry:
    otherwise ``/skillflow:decide`` rejects every decision (SF-A-5 §7.5) and the
    Task is stranded in ``waiting_for_human``.
    """

    id: str
    skill: str
    model: str | None = None
    effort: str | None = None
    outputs: tuple[ExpectedOutput, ...] = ()
    outcomes: Mapping[str, OutcomeRule] = _EMPTY_MAP
    decisions: Mapping[str, OutcomeRule] = _EMPTY_MAP

    def __post_init__(self) -> None:
        _clean(self, "WorkflowStep", "id", "skill")
        _clean_optional(self, "WorkflowStep", "model", "effort")

        outputs = _require_sequence(self.outputs, "WorkflowStep.outputs")
        seen_types: set[str] = set()
        for output in outputs:
            if not isinstance(output, ExpectedOutput):
                raise ValueError("WorkflowStep.outputs must contain ExpectedOutput")
            if output.type in seen_types:
                raise ValueError(
                    f"WorkflowStep.outputs has a duplicate type {output.type!r}"
                )
            seen_types.add(output.type)
        object.__setattr__(self, "outputs", outputs)

        object.__setattr__(
            self, "outcomes", _freeze_rules(self.outcomes, "WorkflowStep.outcomes")
        )
        decisions = _freeze_rules(self.decisions, "WorkflowStep.decisions")
        for key, rule in decisions.items():
            if rule.action is ActionType.HUMAN:
                raise ValueError(
                    f"WorkflowStep.decisions[{key!r}] must not map to action 'human'"
                )
        object.__setattr__(self, "decisions", decisions)

        # A 'human' outcome hands the Task to '/skillflow:decide', which
        # validates the decision against this step's 'decisions' (SF-A-5 §7.5).
        # With none declared, every decision is rejected and the Task is stranded
        # in 'waiting_for_human' with no v0 command able to recover it. Reject the
        # workflow here rather than let it deadlock a Task at runtime.
        if not decisions:
            stranding = [
                key
                for key, rule in self.outcomes.items()
                if rule.action is ActionType.HUMAN
            ]
            if stranding:
                raise ValueError(
                    f"WorkflowStep.outcomes[{stranding[0]!r}] maps to action "
                    "'human' but the step declares no 'decisions'"
                )


@dataclass(frozen=True, kw_only=True, slots=True)
class Workflow:
    """A loaded Workflow Definition: a name and an ordered list of steps.

    Distinct from ``domain.WorkflowDefinition`` (the ``(id, name)`` identity row
    a ``Task`` references). Binding the two by id is SF-008 / SF-010's job.

    Cross-step validation runs at construction: step ids are unique, and every
    ``OutcomeRule.step`` (in any step's ``outcomes`` or ``decisions``) names an
    existing step. ``OutcomeRule.skill`` is never checked. Reachability and
    terminal-state analysis are deliberately absent -- a Workflow Definition
    "is not a generic DAG or state-machine runtime" (SF-A-1 §8).
    """

    name: str
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        _clean(self, "Workflow", "name")

        steps = _require_sequence(self.steps, "Workflow.steps")
        if not steps:
            raise ValueError("Workflow.steps must not be empty")
        step_ids: set[str] = set()
        for step in steps:
            if not isinstance(step, WorkflowStep):
                raise ValueError("Workflow.steps must contain WorkflowStep")
            if step.id in step_ids:
                raise ValueError(f"Workflow.steps has a duplicate step id {step.id!r}")
            step_ids.add(step.id)
        object.__setattr__(self, "steps", steps)

        for step in steps:
            for label, rules in (
                ("outcomes", step.outcomes),
                ("decisions", step.decisions),
            ):
                for key, rule in rules.items():
                    if rule.step is not None and rule.step not in step_ids:
                        raise ValueError(
                            f"step {step.id!r} {label}[{key!r}] references unknown "
                            f"step {rule.step!r}"
                        )

    @property
    def initial_step(self) -> WorkflowStep:
        """The normal starting step -- the first one (SF-A-5 §4.5)."""
        return self.steps[0]

    def find_step(self, step_id: str) -> WorkflowStep | None:
        """Return the step with ``step_id``, or ``None`` if there is none."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None
