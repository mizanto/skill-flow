"""Tests for ``skillflow.domain``.

Two kinds of test live here:

* **Change-detectors** for cross-issue contracts (the ``test_workspace.py``
  pattern) -- exact-value assertions on enum membership and the public surface,
  so a later change that adds ``pending`` or a ``Transition`` entity fails loudly.
* **Invariant tests** -- one per rule enforced in ``__post_init__``.
"""

import ast
import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from skillflow import domain
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    Artifact,
    HumanDecision,
    LifecycleEvent,
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    WorkflowDefinition,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
NAIVE = datetime(2026, 9, 6, 12, 0)


# --- construction helpers (test fixtures, not production abstractions) --------


def _task(**over):
    kw = dict(
        id="task-1",
        title="Do the thing",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=EARLIER,
        updated_at=NOW,
    )
    kw.update(over)
    return Task(**kw)


def _run(**over):
    kw = dict(
        id="run-1",
        task_id="task-1",
        status=RunStatus.RUNNING,
        created_at=NOW,
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    kw.update(over)
    return Run(**kw)


def _result(**over):
    kw = dict(
        id="result-1",
        run_id="run-1",
        status=ResultStatus.COMPLETED,
        created_at=NOW,
    )
    kw.update(over)
    return Result(**kw)


def _artifact(**over):
    kw = dict(
        id="artifact-1",
        task_id="task-1",
        run_id="run-1",
        name="plan.md",
        type="plan",
        version=1,
        path="task-1/plan.md",
        created_at=NOW,
    )
    kw.update(over)
    return Artifact(**kw)


def _decision(**over):
    kw = dict(
        id="decision-1",
        task_id="task-1",
        run_id="run-1",
        decision="approved",
        created_at=NOW,
    )
    kw.update(over)
    return HumanDecision(**kw)


def _event(**over):
    kw = dict(
        id="event-1",
        task_id="task-1",
        type=LifecycleEventType.TASK_CREATED,
        created_at=NOW,
    )
    kw.update(over)
    return LifecycleEvent(**kw)


# --- spec-conformance change-detectors --------------------------------------


def test_task_status_values_match_spec():
    assert {s.value for s in TaskStatus} == {
        "active",
        "waiting_for_human",
        "completed",
        "cancelled",
    }


def test_run_status_values_match_spec():
    assert {s.value for s in RunStatus} == {
        "running",
        "completed",
        "failed",
        "waiting_for_human",
        "cancelled",
    }


def test_run_status_has_no_pending_state():
    # SF-2 acceptance criterion, asserted on its own.
    assert "pending" not in {s.value for s in RunStatus}


def test_result_status_values_match_spec():
    assert {s.value for s in ResultStatus} == {"completed", "failed"}


def test_lifecycle_event_type_values_match_spec():
    assert {e.value for e in LifecycleEventType} == {
        "task.created",
        "task.status_changed",
        "run.created",
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "result.created",
        "artifact.created",
        "human.decision_made",
    }


def test_trigger_reason_initial_constant():
    assert TRIGGER_REASON_INITIAL == "initial"


# --- architecture guards ----------------------------------------------------


V0_ENTITIES = {
    "Outcome",
    "Task",
    "Run",
    "Result",
    "Artifact",
    "WorkflowDefinition",
    "HumanDecision",
    "LifecycleEvent",
}


def test_module_defines_exactly_the_v0_dataclasses():
    # Derived from the module namespace, not __all__: a new dataclass that is
    # simply never added to __all__ still fails this. This is the mechanical
    # enforcement of "No extra lifecycle entities".
    defined = {
        name
        for name, value in vars(domain).items()
        if dataclasses.is_dataclass(value) and not name.startswith("_")
    }
    assert defined == V0_ENTITIES


def test_all_exports_match_the_v0_dataclasses():
    exported = {
        name
        for name in domain.__all__
        if dataclasses.is_dataclass(getattr(domain, name))
    }
    assert exported == V0_ENTITIES


def test_no_excluded_lifecycle_entities_present():
    # SF-A-1 §2's exclusion list, verbatim. "Step" is deliberately absent: a
    # Workflow Definition is modelled *as* steps (SF-A-1 §8) and Run.step_id
    # already exists, so banning it would false-fail SF-7.
    #
    # A substring scan, so RunTransition / TaskStage / WorkflowInstance are
    # caught too -- and over every public module-level name, not only
    # dataclasses, so a `class Transition(StrEnum)` or a `resolve_transition()`
    # does not slip past.
    banned_fragments = (
        "transition",
        "router",
        "loop",
        "iteration",
        "rework",
        "handoff",
        "instance",
        "stage",
    )
    offenders = [
        name
        for name in vars(domain)
        if not name.startswith("_")
        and any(frag in name.lower() for frag in banned_fragments)
    ]
    assert not offenders


V0_FIELDS = {
    # Transcribed from SF-A-1 §3-§8 and SF-A-2 §4. SF-4 hard-codes these names
    # into the SQLite schema, so a rename or a dropped field must be a conscious,
    # reviewed change rather than a green test run.
    Outcome: {"type", "decision"},
    Task: {
        "id",
        "title",
        "description",
        "status",
        "created_at",
        "updated_at",
        "workflow_definition_id",
    },
    Run: {
        "id",
        "task_id",
        "status",
        "created_at",
        "workflow_definition_id",
        "step_id",
        "instructions",
        "triggered_by_run_id",
        "trigger_reason",
        "transcript_ref",
        "started_at",
        "completed_at",
    },
    Result: {"id", "run_id", "status", "created_at", "outcome", "metadata"},
    Artifact: {
        "id",
        "task_id",
        "run_id",
        "name",
        "type",
        "version",
        "path",
        "created_at",
        "supersedes_id",
    },
    WorkflowDefinition: {"id", "name"},
    HumanDecision: {
        "id",
        "task_id",
        "run_id",
        "decision",
        "created_at",
        "comment",
    },
    LifecycleEvent: {
        "id",
        "task_id",
        "type",
        "created_at",
        "run_id",
        "payload",
    },
}


def test_field_sets_cover_every_v0_entity():
    assert {entity.__name__ for entity in V0_FIELDS} == V0_ENTITIES


@pytest.mark.parametrize(
    "entity,expected",
    list(V0_FIELDS.items()),
    ids=lambda value: value.__name__ if isinstance(value, type) else "",
)
def test_entity_fields_match_spec(entity, expected):
    assert {f.name for f in dataclasses.fields(entity)} == expected


def test_domain_module_imports_stdlib_only():
    source = Path(domain.__file__).read_text()
    tree = ast.parse(source)
    allowed = {"dataclasses", "datetime", "enum", "types", "typing", "collections"}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= allowed, f"unexpected imports: {modules - allowed}"


# --- structural properties ------------------------------------------------


def test_entities_are_frozen():
    task = _task()
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.status = TaskStatus.COMPLETED


def test_entities_reject_unknown_attributes():
    # frozen + slots: the frozen __setattr__ fires before the slot lookup.
    task = _task()
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.typo = 1


def test_entities_are_keyword_only():
    with pytest.raises(TypeError, match="positional"):
        Task(
            "task-1",
            "title",
            "",
            TaskStatus.ACTIVE,
            EARLIER,
            NOW,
        )


@pytest.mark.parametrize(
    "ctor,field",
    [(_result, "metadata"), (_event, "payload")],
)
def test_mapping_fields_are_read_only_snapshots(ctor, field):
    source = {"key": "value"}
    entity = ctor(**{field: source})
    stored = getattr(entity, field)
    assert stored == {"key": "value"}
    with pytest.raises(TypeError):
        stored["key"] = "changed"
    source["key"] = "mutated"
    assert getattr(entity, field)["key"] == "value"


def test_entities_carrying_a_mapping_are_not_hashable():
    # Hashability is data-dependent: the MappingProxyType is what is unhashable,
    # so a metadata-free Result hashes fine. Identify entities by id, never by
    # hash -- a set of Results would work until the first one carries metadata.
    with pytest.raises(TypeError):
        hash(_result(metadata={"a": "b"}))
    assert hash(_result(metadata=None)) == hash(_result(metadata=None))


# --- happy paths ----------------------------------------------------------


def test_valid_initial_run_constructs():
    run = _run(trigger_reason=TRIGGER_REASON_INITIAL, triggered_by_run_id=None)
    assert run.trigger_reason == "initial"


def test_valid_triggered_run_constructs():
    run = _run(
        id="run-2",
        trigger_reason="review_changes_requested",
        triggered_by_run_id="run-1",
    )
    assert run.triggered_by_run_id == "run-1"


def test_optional_fields_default_to_none():
    run = _run()
    assert run.step_id is None
    assert run.workflow_definition_id is None
    assert run.started_at is None
    assert _task().workflow_definition_id is None
    assert _result().metadata is None


# --- invariants: blank required strings ----------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_task_id_rejected(blank):
    with pytest.raises(ValueError, match="Task.id"):
        _task(id=blank)


def test_blank_task_title_rejected():
    with pytest.raises(ValueError, match="Task.title"):
        _task(title="  ")


def test_blank_run_id_rejected():
    with pytest.raises(ValueError, match="Run.id"):
        _run(id="")


def test_blank_workflow_definition_name_rejected():
    with pytest.raises(ValueError, match="WorkflowDefinition.name"):
        WorkflowDefinition(id="wf-1", name="")


def test_blank_artifact_type_rejected():
    with pytest.raises(ValueError, match="Artifact.type"):
        _artifact(type="")


def test_blank_artifact_path_rejected():
    with pytest.raises(ValueError, match="Artifact.path"):
        _artifact(path="   ")


def test_blank_human_decision_rejected():
    with pytest.raises(ValueError, match="HumanDecision.decision"):
        _decision(decision="")


@pytest.mark.parametrize("field", ["step_id", "trigger_reason", "transcript_ref"])
def test_optional_string_field_rejects_blank_when_present(field):
    with pytest.raises(ValueError, match=f"Run.{field}"):
        _run(**{field: "  "})


def test_identifiers_are_stripped():
    # " t1 " and "t1" must not become two identities once persisted (SF-4).
    assert _task(id="  task-9  ").id == "task-9"
    assert _artifact(type="  plan  ").type == "plan"
    assert _decision(decision=" approved ").decision == "approved"
    assert Outcome(type=" review ", decision=" ok ").decision == "ok"


# --- invariants: timezone-aware datetimes -------------------------------


@pytest.mark.parametrize(
    "ctor,kwargs",
    [
        (_task, {"created_at": NAIVE}),
        (_task, {"updated_at": NAIVE}),
        (_run, {"created_at": NAIVE}),
        (_run, {"started_at": NAIVE}),
        (_run, {"completed_at": NAIVE}),
        (_result, {"created_at": NAIVE}),
        (_artifact, {"created_at": NAIVE}),
        (_decision, {"created_at": NAIVE}),
        (_event, {"created_at": NAIVE}),
    ],
)
def test_naive_datetimes_rejected(ctor, kwargs):
    with pytest.raises(ValueError, match="timezone-aware"):
        ctor(**kwargs)


def test_non_utc_aware_datetime_accepted():
    tz = timezone(timedelta(hours=2))
    task = _task(created_at=EARLIER, updated_at=NOW.astimezone(tz))
    assert task.updated_at.utcoffset() == timedelta(hours=2)


# --- invariants: ordering ----------------------------------------------


def test_task_updated_before_created_rejected():
    with pytest.raises(ValueError, match="updated_at must be >="):
        _task(created_at=NOW, updated_at=EARLIER)


def test_run_completed_before_started_rejected():
    with pytest.raises(ValueError, match="completed_at must be >="):
        _run(started_at=NOW, completed_at=EARLIER)


# --- invariants: Artifact versioning ---------------------------------


@pytest.mark.parametrize("bad_version", [0, -1])
def test_artifact_version_below_one_rejected(bad_version):
    with pytest.raises(ValueError, match="version must be >= 1"):
        _artifact(version=bad_version)


@pytest.mark.parametrize("bad_version", [True, False, 1.0, "1"])
def test_artifact_version_must_be_a_real_int(bad_version):
    # bool is an int subclass; a float/str version is a caller mistake.
    with pytest.raises(ValueError, match="version must be an int"):
        _artifact(version=bad_version)


def test_artifact_cannot_supersede_itself():
    with pytest.raises(ValueError, match="supersedes_id must not be"):
        _artifact(id="a-1", supersedes_id="a-1")


def test_artifact_supersedes_another_ok():
    art = _artifact(id="a-2", version=2, supersedes_id="a-1")
    assert art.supersedes_id == "a-1"


# --- invariants: Run provenance -------------------------------------


def test_run_cannot_trigger_itself():
    with pytest.raises(ValueError, match="triggered_by_run_id must not be"):
        _run(id="run-1", trigger_reason="retry", triggered_by_run_id="run-1")


def test_initial_run_with_trigger_source_rejected():
    with pytest.raises(ValueError, match="initial Run"):
        _run(trigger_reason=TRIGGER_REASON_INITIAL, triggered_by_run_id="run-0")


def test_triggered_run_without_reason_rejected():
    with pytest.raises(ValueError, match="Run.trigger_reason"):
        _run(trigger_reason=None, triggered_by_run_id="run-0")


def test_noninitial_reason_without_trigger_source_rejected():
    # SF-A-5 §4.7 has exactly two provenance shapes; "retry" with no source Run
    # is neither.
    with pytest.raises(ValueError, match="requires a triggered_by_run_id"):
        _run(trigger_reason="retry", triggered_by_run_id=None)


def test_run_with_no_provenance_is_allowed():
    # reason=None + triggered_by=None is the one remaining shape SF-2 does not
    # forbid (whether every Run must carry provenance is SF-5's call).
    run = _run(trigger_reason=None)
    assert run.trigger_reason is None


# --- invariants: Outcome ------------------------------------------


def test_outcome_requires_non_empty_type():
    with pytest.raises(ValueError, match="Outcome.type"):
        Outcome(type="", decision="ready")


def test_outcome_requires_non_empty_decision():
    with pytest.raises(ValueError, match="Outcome.decision"):
        Outcome(type="review", decision="  ")


def test_result_carries_outcome():
    result = _result(outcome=Outcome(type="review", decision="approved"))
    assert result.outcome.decision == "approved"


# --- invariants: status / type / outcome fields --------------------


@pytest.mark.parametrize(
    "ctor,field,enum_name",
    [
        (_task, "status", "TaskStatus"),
        (_run, "status", "RunStatus"),
        (_result, "status", "ResultStatus"),
        (_event, "type", "LifecycleEventType"),
    ],
)
def test_unknown_enum_value_rejected(ctor, field, enum_name):
    with pytest.raises(ValueError, match=enum_name):
        ctor(**{field: "not_a_real_value"})


def test_run_rejects_pending_status():
    # The acceptance criterion "Run has no pending state", defended at the entity
    # and not only at the enum.
    with pytest.raises(ValueError, match="RunStatus"):
        _run(status="pending")


@pytest.mark.parametrize(
    "ctor,field,enum_member",
    [
        (_task, "status", TaskStatus.ACTIVE),
        (_run, "status", RunStatus.RUNNING),
        (_result, "status", ResultStatus.COMPLETED),
        (_event, "type", LifecycleEventType.TASK_CREATED),
    ],
)
def test_bare_string_is_coerced_to_enum(ctor, field, enum_member):
    entity = ctor(**{field: str(enum_member.value)})
    assert getattr(entity, field) is enum_member


# --- invariants: free-text bodies ---------------------------------


@pytest.mark.parametrize("bad", [None, 1, b"bytes"])
def test_task_description_must_be_a_string(bad):
    with pytest.raises(ValueError, match="Task.description must be a string"):
        _task(description=bad)


@pytest.mark.parametrize(
    "ctor,field",
    [(_run, "instructions"), (_decision, "comment")],
)
def test_optional_body_must_be_a_string_when_present(ctor, field):
    with pytest.raises(ValueError, match=f"{field} must be a string"):
        ctor(**{field: 1})
    assert getattr(ctor(**{field: None}), field) is None


def test_bodies_are_stored_verbatim():
    # Content, not identifiers: not stripped, and allowed to be empty.
    assert _task(description="  keep\n  ").description == "  keep\n  "
    assert _run(instructions=" do it ").instructions == " do it "
    assert _decision(comment="").comment == ""


# --- invariants: mapping fields -----------------------------------


@pytest.mark.parametrize(
    "ctor,field",
    [(_result, "metadata"), (_event, "payload")],
)
@pytest.mark.parametrize("bad", [5, ["ab"], "ab", [("a", "b")]])
def test_mapping_field_rejects_non_mappings(ctor, field, bad):
    # dict(["ab"]) would silently invent {"a": "b"}; a caller mistake must not
    # become plausible-looking persisted data.
    with pytest.raises(ValueError, match=f"{field} must be a mapping"):
        ctor(**{field: bad})


def test_result_outcome_must_be_an_outcome_instance():
    with pytest.raises(ValueError, match="Result.outcome must be an Outcome"):
        _result(outcome="approved")
