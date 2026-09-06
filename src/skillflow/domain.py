"""Skill Flow domain types.

Pure, dependency-free value objects for the Skill Flow lifecycle vocabulary
(Domain Model v0, SF-A-1 §3-§8; field lists cross-checked against Persistence
v0, SF-A-2 §4). Every later issue -- SQLite persistence, invariant enforcement,
the workflow schema, lifecycle evaluation, and the four public commands -- talks
in these terms.

This module holds three boundaries deliberately:

* **No persistence.** SQLite is the source of truth (SF-A-2 §1); these objects
  are immutable snapshots. There is no ``to_row`` / ``from_row`` and no
  serialization here (SF-3, SF-4).
* **No Claude Code.** Nothing in this module executes anything; it imports the
  standard library only. Claude Code is the execution layer (Execution Boundary
  v0).
* **No cross-entity lifecycle rules.** ``__post_init__`` enforces intra-entity
  invariants only. "One running Run per Task", "one canonical Result per Run",
  and the legal Run status transitions are SF-5's (SF-A-6 §SF-005). A lifecycle
  change is a *new* instance (``dataclasses.replace`` / ``copy.replace``), never
  a mutation.

Construction is total: an instance cannot exist in an invalid state. Every
required string is non-empty (and stored ``.strip()``-ed), every ``datetime`` is
timezone-aware, and every status / type field is coerced to its enum (an unknown
value raises ``ValueError``). Free-text bodies -- ``Task.description``,
``Run.instructions``, ``HumanDecision.comment`` -- are the exception: they are
content, not identifiers, so they are type-checked but stored verbatim
(unstripped, and permitted to be empty).

Entities are frozen and keyword-only. Their ``metadata`` / ``payload`` mappings
are wrapped in ``types.MappingProxyType``, which is itself unhashable, so an
entity carrying one cannot be hashed. Hashability is therefore data-dependent:
do not rely on it -- identify entities by ``id``, not by ``hash()``.

Ids are plain strings supplied by the caller. This module does not generate ids
and does not read a clock.
"""

import types
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "TRIGGER_REASON_INITIAL",
    "TaskStatus",
    "RunStatus",
    "ResultStatus",
    "LifecycleEventType",
    "Outcome",
    "Task",
    "Run",
    "Result",
    "Artifact",
    "WorkflowDefinition",
    "HumanDecision",
    "LifecycleEvent",
]

#: The one globally meaningful ``trigger_reason``: the first Run of a Task
#: (SF-A-5 §4.7). All other reasons are defined per workflow step, not here.
TRIGGER_REASON_INITIAL = "initial"


class TaskStatus(StrEnum):
    """Task lifecycle states (SF-A-1 §3)."""

    ACTIVE = "active"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """Run lifecycle states (SF-A-1 §4). There is no ``pending`` state in v0."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_HUMAN = "waiting_for_human"
    CANCELLED = "cancelled"


class ResultStatus(StrEnum):
    """Canonical Result states (SF-A-1 §6, SF-A-5 §6.5)."""

    COMPLETED = "completed"
    FAILED = "failed"


class LifecycleEventType(StrEnum):
    """Audit event types, verbatim from Persistence v0 (SF-A-2 §8).

    Lifecycle events are history/debug data, not event sourcing: current state
    is queryable without replaying them (SF-A-2 §8).
    """

    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RESULT_CREATED = "result.created"
    ARTIFACT_CREATED = "artifact.created"
    HUMAN_DECISION_MADE = "human.decision_made"


def _require_text(value: str, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    A required identifier / name / path that is empty or whitespace-only is a
    programming error, not a valid domain state. Leading/trailing whitespace is
    removed so that ``" t1 "`` and ``"t1"`` cannot become two identities that
    print alike once persisted (SF-4 uses these as keys and path components).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_body(value: object, field_name: str, *, optional: bool = False) -> None:
    """Type-check a free-text body without normalising it.

    Bodies are content: they are neither stripped nor required to be non-empty.
    They are still required to be strings -- a required column holding ``None``
    surfaces far from here, as a SQLite ``NOT NULL`` failure (SF-A-2 §4) or as a
    ``None`` rendered into ``RunInput`` (SF-A-5 §4.8).
    """
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")


def _require_aware(value: datetime, field_name: str) -> datetime:
    """Return ``value`` unchanged, or raise ``ValueError`` if it is naive.

    Naive datetimes silently corrupt ordering once persisted, so they are
    rejected at construction.
    """
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _clean_text(
    instance: object,
    prefix: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    """Normalise identifier-like string fields on ``instance`` in place.

    ``required`` fields must be non-empty; ``optional`` fields may be ``None``
    but must be non-empty when present. Each surviving value is stored stripped.
    Free-text bodies (descriptions, comments, instructions) are deliberately not
    passed here -- they are content, not identifiers.
    """
    for name in required:
        cleaned = _require_text(getattr(instance, name), f"{prefix}.{name}")
        object.__setattr__(instance, name, cleaned)
    for name in optional:
        value = getattr(instance, name)
        if value is not None:
            object.__setattr__(instance, name, _require_text(value, f"{prefix}.{name}"))


def _freeze(
    value: Mapping[str, str] | None, field_name: str
) -> types.MappingProxyType | None:
    """Return a read-only, snapshot copy of ``value`` (or ``None``).

    Non-mappings are rejected rather than coerced: ``dict(["ab"])`` would
    otherwise invent ``{"a": "b"}`` and persist a caller mistake as
    plausible-looking data.

    Mapping *values* are expected to be strings: SF-4 serialises these mappings,
    so a non-string value would only fail later. SF-2 does not enforce that.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return types.MappingProxyType(dict(value))


@dataclass(frozen=True, kw_only=True, slots=True)
class Outcome:
    """The structured result of a Run, as observed by ``complete-run``.

    Deterministic Lifecycle Evaluation (SF-A-4 §7) keys an outcome-to-action
    rule on ``decision``; ``type`` names the kind of outcome (e.g. ``"review"``).
    """

    type: str
    decision: str

    def __post_init__(self) -> None:
        _clean_text(self, "Outcome", ("type", "decision"))


@dataclass(frozen=True, kw_only=True, slots=True)
class Task:
    """Work whose lifecycle can span multiple independent Runs (SF-A-1 §3)."""

    id: str
    title: str
    description: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    workflow_definition_id: str | None = None

    def __post_init__(self) -> None:
        _clean_text(self, "Task", ("id", "title"), ("workflow_definition_id",))
        _require_body(self.description, "Task.description")
        object.__setattr__(self, "status", TaskStatus(self.status))
        _require_aware(self.created_at, "Task.created_at")
        _require_aware(self.updated_at, "Task.updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("Task.updated_at must be >= Task.created_at")


@dataclass(frozen=True, kw_only=True, slots=True)
class Run:
    """A bounded execution intended to advance a Task (SF-A-1 §4).

    ``workflow_definition_id`` / ``step_id`` mirror SF-A-1 §4's ``workflow: {id,
    step}``; ``prepare-artifacts`` reads the current step from the Run
    (SF-A-5 §5.1). Both are optional: a skill-targeted action (SF-A-4 §9) has no
    workflow step. Provenance is ``triggered_by_run_id`` + ``trigger_reason``;
    SF-A-5 §4.7 defines exactly two shapes -- an initial Run
    (``trigger_reason == "initial"``, no ``triggered_by_run_id``) and a triggered
    Run (both populated) -- and ``__post_init__`` rejects anything else.

    ``instructions`` is a free-text body (like ``Task.description``), stored
    verbatim. The durable context selected for the Run (SF-A-4 Context
    Selection) is **not** a field here: SF-A-2 §4 does not persist it on the Run
    in v0, and ``RunInput`` is the ``resolve-task`` issue's concern.
    """

    id: str
    task_id: str
    status: RunStatus
    created_at: datetime
    workflow_definition_id: str | None = None
    step_id: str | None = None
    instructions: str | None = None
    triggered_by_run_id: str | None = None
    trigger_reason: str | None = None
    transcript_ref: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _clean_text(
            self,
            "Run",
            ("id", "task_id"),
            (
                "workflow_definition_id",
                "step_id",
                "triggered_by_run_id",
                "trigger_reason",
                "transcript_ref",
            ),
        )
        object.__setattr__(self, "status", RunStatus(self.status))
        _require_body(self.instructions, "Run.instructions", optional=True)
        _require_aware(self.created_at, "Run.created_at")
        if self.started_at is not None:
            _require_aware(self.started_at, "Run.started_at")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "Run.completed_at")

        if self.triggered_by_run_id == self.id:
            raise ValueError("Run.triggered_by_run_id must not be Run.id")

        is_initial = self.trigger_reason == TRIGGER_REASON_INITIAL
        if is_initial and self.triggered_by_run_id is not None:
            raise ValueError("an initial Run must not have a triggered_by_run_id")
        if self.triggered_by_run_id is not None and self.trigger_reason is None:
            raise ValueError(
                "Run.trigger_reason is required when triggered_by_run_id is set"
            )
        if (
            self.trigger_reason is not None
            and not is_initial
            and self.triggered_by_run_id is None
        ):
            raise ValueError(
                "a non-initial Run.trigger_reason requires a triggered_by_run_id"
            )

        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("Run.completed_at must be >= Run.started_at")


@dataclass(frozen=True, kw_only=True, slots=True)
class Result:
    """The one canonical, immutable observation of a Run (SF-A-1 §6).

    Human-readable output belongs in Artifacts, not here. The Run -> Artifact
    relation is recoverable from ``Artifact.run_id``, so SF-A-1 §6's
    ``artifacts`` list is not duplicated as a field (SF-A-2 §4 does not persist
    it on the Result either).
    """

    id: str
    run_id: str
    status: ResultStatus
    created_at: datetime
    outcome: Outcome | None = None
    metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _clean_text(self, "Result", ("id", "run_id"))
        object.__setattr__(self, "status", ResultStatus(self.status))
        _require_aware(self.created_at, "Result.created_at")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise ValueError("Result.outcome must be an Outcome")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "Result.metadata"))


@dataclass(frozen=True, kw_only=True, slots=True)
class Artifact:
    """Durable context produced by a Run (SF-A-1 §5).

    Logically immutable and versioned: new content is a new Artifact with
    ``version`` incremented and ``supersedes_id`` pointing at the prior one.
    ``path`` is interpreted relative to the artifact store; the concrete
    ``.skillflow/artifacts/`` layout is SF-3/SF-4's decision.
    """

    id: str
    task_id: str
    run_id: str
    name: str
    type: str
    version: int
    path: str
    created_at: datetime
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        _clean_text(
            self,
            "Artifact",
            ("id", "task_id", "run_id", "name", "type", "path"),
            ("supersedes_id",),
        )
        _require_aware(self.created_at, "Artifact.created_at")
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("Artifact.version must be an int")
        if self.version < 1:
            raise ValueError("Artifact.version must be >= 1")
        if self.supersedes_id is not None and self.supersedes_id == self.id:
            raise ValueError("Artifact.supersedes_id must not be Artifact.id")


@dataclass(frozen=True, kw_only=True, slots=True)
class WorkflowDefinition:
    """The normal procedure for a type of Task -- identity only in v0.

    Steps, skills, models, effort, expected outputs and outcome-to-action rules
    are SF-7's deliverable ("Define Workflow Definition schema"). Adding them
    here would preempt that issue and drift toward a generic workflow DSL.
    A Workflow Definition is a definition, never a runtime instance.
    """

    id: str
    name: str

    def __post_init__(self) -> None:
        _clean_text(self, "WorkflowDefinition", ("id", "name"))


@dataclass(frozen=True, kw_only=True, slots=True)
class HumanDecision:
    """Optional human input recorded against a Run (SF-A-1 §8, SF-A-5 §7).

    ``decision`` is a plain string: its allowed values are defined per workflow
    step, not globally (SF-A-5 §7.3). ``comment`` is a free-text body, stored
    verbatim.
    """

    id: str
    task_id: str
    run_id: str
    decision: str
    created_at: datetime
    comment: str | None = None

    def __post_init__(self) -> None:
        _clean_text(self, "HumanDecision", ("id", "task_id", "run_id", "decision"))
        _require_body(self.comment, "HumanDecision.comment", optional=True)
        _require_aware(self.created_at, "HumanDecision.created_at")


@dataclass(frozen=True, kw_only=True, slots=True)
class LifecycleEvent:
    """An audit/history record (SF-A-2 §8). Not event sourcing."""

    id: str
    task_id: str
    type: LifecycleEventType
    created_at: datetime
    run_id: str | None = None
    payload: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _clean_text(self, "LifecycleEvent", ("id", "task_id"), ("run_id",))
        object.__setattr__(self, "type", LifecycleEventType(self.type))
        _require_aware(self.created_at, "LifecycleEvent.created_at")
        object.__setattr__(
            self, "payload", _freeze(self.payload, "LifecycleEvent.payload")
        )
