"""Tests for ``skillflow.service``.

Two kinds of test, matching ``test_store.py`` / ``test_workspace.py``:

* **Contract change-detectors** -- the public surface, the import boundary (the
  service reads no definition files), and the ``UnknownWorkflowError`` /
  ``WorkflowAssignmentError`` / ``RunCreationError`` types.
* **Behaviour tests** -- Task creation (status, ids, timestamps, round-trip,
  durability, the ``task.created`` event, workflow-reference validation and
  atomicity), ``register_workflow`` idempotence and commit, ``assign_workflow``
  (persistence, immutable-field preservation, durability, the
  ``task.workflow_assigned`` event, ``waiting_for_human`` acceptance,
  idempotent no-op / normalisation, every rejection path proving no write,
  error precedence, and transaction atomicity), ``create_run`` (initial and
  subsequent provenance, the ``running`` status with no ``pending`` state, the
  ``run.created`` event with no ``run.started``, every rejection path proving no
  write, error precedence, atomicity, and the wave-5 vertical slice over the
  real reference definition), ``apply_lifecycle_action`` (the action -> status
  mapping for every ``ActionType``, the status no-op, transaction neutrality,
  and every rejection path), and the SF-8 -> SF-9 integration path through the
  real reference definition.
"""

import ast
import contextlib
import inspect
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from skillflow import service, store, workspace
from skillflow.domain import (
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import (
    EvaluationInput,
    EvaluationOutput,
    evaluate,
    resolve_initial_action,
)
from skillflow.service import (
    RunCreationError,
    UnknownWorkflowError,
    WorkflowAssignmentError,
    apply_lifecycle_action,
    assign_workflow,
    create_run,
    create_task,
    register_workflow,
)
from skillflow.workflow import ActionType, Workflow, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    with contextlib.closing(store.open_store(ws)) as connection:
        yield connection


def _workflow(name="demo"):
    return Workflow(name=name, steps=(WorkflowStep(id="only", skill="do-it"),))


def _rows(conn, table):
    return conn.execute(f"SELECT * FROM {table}").fetchall()


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(service.__all__) == {
        "UnknownWorkflowError",
        "WorkflowAssignmentError",
        "RunCreationError",
        "register_workflow",
        "create_task",
        "assign_workflow",
        "create_run",
        "apply_lifecycle_action",
    }


def test_service_module_reads_no_definition_files():
    # The service takes an already-loaded Workflow; discovery and YAML parsing
    # stay in workflow_loader. Mechanically checkable, like the store/workspace
    # import-boundary tests.
    tree = ast.parse(Path(service.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert "yaml" not in modules
    assert "skillflow.workflow_loader" not in modules


def test_unknown_workflow_error_is_a_distinct_type():
    assert not issubclass(UnknownWorkflowError, ValueError)
    assert not issubclass(UnknownWorkflowError, sqlite3.Error)


def test_workflow_assignment_error_is_a_distinct_type():
    assert not issubclass(WorkflowAssignmentError, ValueError)
    assert not issubclass(WorkflowAssignmentError, sqlite3.Error)


def test_assign_workflow_never_infers_a_definition():
    # workflow_definition_id is keyword-only with no default: the caller must
    # name the Workflow, so nothing here can select one.
    sig = inspect.signature(assign_workflow)
    param = sig.parameters["workflow_definition_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


# --- create_task: shape -------------------------------------------------


def test_create_task_without_workflow_is_active(conn):
    task = create_task(conn, title="Do the thing")
    assert task.status is TaskStatus.ACTIVE
    assert task.workflow_definition_id is None
    assert task.id.startswith("task-")
    assert task.created_at == task.updated_at
    assert task.created_at.utcoffset() is not None


def test_create_task_round_trips_by_full_equality(conn):
    task = create_task(conn, title="Round trip", description="body")
    assert store.get_task(conn, task.id) == task


def test_create_task_is_committed(conn, ws):
    task = create_task(conn, title="Durable")
    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_task(reopened, task.id) == task


def test_two_creations_get_different_ids(conn):
    a = create_task(conn, title="One")
    b = create_task(conn, title="Two")
    assert a.id != b.id


def test_empty_description_is_accepted(conn):
    task = create_task(conn, title="No body")
    assert task.description == ""


# --- create_task: the task.created event ------------------------------


def test_create_task_writes_exactly_one_task_created_event(conn):
    task = create_task(conn, title="Eventful")
    events = store.list_lifecycle_events_for_task(conn, task.id)
    assert len(events) == 1
    (event,) = events
    assert event.type is LifecycleEventType.TASK_CREATED
    assert event.run_id is None
    assert dict(event.payload) == {"status": "active"}
    # The clock is read once: the event shares the Task's timestamp. Its id is
    # its own.
    assert event.created_at == task.created_at
    assert event.id != task.id


def test_task_created_event_carries_the_workflow_reference(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="With workflow", workflow_definition_id="demo")
    (event,) = store.list_lifecycle_events_for_task(conn, task.id)
    assert dict(event.payload) == {
        "status": "active",
        "workflow_definition_id": "demo",
    }


# --- create_task: workflow-reference validation ----------------------


def test_registered_workflow_reference_persists(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Bound", workflow_definition_id="demo")
    assert store.get_task(conn, task.id).workflow_definition_id == "demo"


def test_workflow_reference_is_stripped_before_lookup_and_storage(conn):
    register_workflow(conn, _workflow("software-change"))
    task = create_task(
        conn, title="Whitespace", workflow_definition_id=" software-change "
    )
    assert task.workflow_definition_id == "software-change"
    assert store.get_task(conn, task.id).workflow_definition_id == "software-change"


def test_unknown_workflow_reference_is_rejected_with_no_write(conn):
    with pytest.raises(UnknownWorkflowError, match="nope"):
        create_task(conn, title="Doomed", workflow_definition_id="nope")
    assert _rows(conn, "tasks") == []
    assert _rows(conn, "lifecycle_events") == []


def test_blank_title_raises_value_error_with_no_write(conn):
    with pytest.raises(ValueError):
        create_task(conn, title="   ")
    assert _rows(conn, "tasks") == []
    assert _rows(conn, "lifecycle_events") == []


def test_empty_workflow_reference_is_a_domain_value_error_not_unknown_workflow(conn):
    # "" is not treated as None: domain.Task rejects a blank optional identifier
    # before the registration lookup is ever reached.
    with pytest.raises(ValueError, match="workflow_definition_id"):
        create_task(conn, title="Empty ref", workflow_definition_id="")
    assert _rows(conn, "tasks") == []
    assert _rows(conn, "lifecycle_events") == []


def test_non_string_description_raises_value_error_with_no_write(conn):
    with pytest.raises(ValueError, match="Task.description"):
        create_task(conn, title="Bad body", description=None)
    assert _rows(conn, "tasks") == []
    assert _rows(conn, "lifecycle_events") == []


def test_task_and_event_are_one_transaction(conn, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(service.store, "insert_lifecycle_event", boom)
    with pytest.raises(RuntimeError, match="event write failed"):
        create_task(conn, title="Half-written")
    assert _rows(conn, "tasks") == []


# --- register_workflow ------------------------------------------------


def test_register_workflow_persists_id_equal_to_name(conn):
    definition = register_workflow(conn, _workflow("demo"))
    assert (definition.id, definition.name) == ("demo", "demo")
    assert store.get_workflow_definition(conn, "demo") == definition


def test_register_workflow_is_idempotent(conn):
    first = register_workflow(conn, _workflow("demo"))
    second = register_workflow(conn, _workflow("demo"))
    assert first == second
    assert len(_rows(conn, "workflow_definitions")) == 1


def test_register_workflow_is_committed(conn, ws):
    register_workflow(conn, _workflow("demo"))
    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_workflow_definition(reopened, "demo") is not None


# --- integration: SF-8 reference definition -> SF-9 -----------------


def test_reference_workflow_can_be_registered_and_referenced(conn):
    workflow = load_workflow(REFERENCE)
    definition = register_workflow(conn, workflow)
    assert definition.id == "software-change"

    task = create_task(conn, title="Ship it", workflow_definition_id="software-change")
    assert store.get_task(conn, task.id).workflow_definition_id == "software-change"
    (event,) = store.list_lifecycle_events_for_task(conn, task.id)
    assert event.type is LifecycleEventType.TASK_CREATED


# --- assign_workflow ------------------------------------------------


def _force_status(conn, task, status):
    """Move a Task to ``status`` directly (``update_task`` does no validation)."""
    changed = replace(
        task, status=status, updated_at=task.updated_at + timedelta(seconds=1)
    )
    with conn:
        store.update_task(conn, changed)
    return store.get_task(conn, task.id)


def test_assign_workflow_is_not_inferred_even_with_one_definition(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Unbound")
    assert task.workflow_definition_id is None
    assert store.get_task(conn, task.id).workflow_definition_id is None


def test_assign_workflow_persists(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Unbound")

    updated = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    assert updated.workflow_definition_id == "demo"
    stored = store.get_task(conn, task.id)
    assert stored == updated
    assert stored.workflow_definition_id == "demo"


def test_assign_workflow_preserves_immutable_fields(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Keep me", description="body")

    updated = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    assert (updated.id, updated.title, updated.description, updated.status) == (
        task.id,
        task.title,
        task.description,
        task.status,
    )
    assert updated.created_at == task.created_at
    assert updated.updated_at > task.created_at


def test_assign_workflow_survives_reopening_the_store(conn, ws):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Durable")
    assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_task(reopened, task.id).workflow_definition_id == "demo"


def test_assign_workflow_writes_exactly_one_assignment_event(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Eventful")

    updated = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    events = store.list_lifecycle_events_for_task(conn, task.id)
    assert [e.type for e in events] == [
        LifecycleEventType.TASK_CREATED,
        LifecycleEventType.TASK_WORKFLOW_ASSIGNED,
    ]
    assigned = events[-1]
    assert assigned.run_id is None
    assert dict(assigned.payload) == {"workflow_definition_id": "demo"}
    assert assigned.created_at == updated.updated_at


def test_assign_workflow_accepts_a_waiting_for_human_task(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Waiting")
    task = _force_status(conn, task, TaskStatus.WAITING_FOR_HUMAN)

    updated = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    assert updated.workflow_definition_id == "demo"
    assert updated.status is TaskStatus.WAITING_FOR_HUMAN


def test_assign_workflow_integration_with_reference_definition(conn):
    register_workflow(conn, load_workflow(REFERENCE))
    task = create_task(conn, title="Ship it")

    assign_workflow(conn, task_id=task.id, workflow_definition_id="software-change")

    assert store.get_task(conn, task.id).workflow_definition_id == "software-change"


# assign_workflow: idempotence / normalisation


def test_assign_workflow_same_definition_twice_is_a_no_op(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Retry")
    first = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    second = assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    assert second == first
    assert second.updated_at == first.updated_at
    events = store.list_lifecycle_events_for_task(conn, task.id)
    assert [e.type for e in events] == [
        LifecycleEventType.TASK_CREATED,
        LifecycleEventType.TASK_WORKFLOW_ASSIGNED,
    ]


def test_assign_workflow_strips_whitespace_then_treats_it_as_the_no_op(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Whitespace")

    assigned = assign_workflow(conn, task_id=task.id, workflow_definition_id=" demo ")
    assert assigned.workflow_definition_id == "demo"
    assert store.get_task(conn, task.id).workflow_definition_id == "demo"

    again = assign_workflow(conn, task_id=task.id, workflow_definition_id=" demo ")
    assert again.updated_at == assigned.updated_at
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == 2


# assign_workflow: invalid cases -- each writes nothing


def _assert_task_and_events_unchanged(conn, task_id, snapshot_task, event_count):
    assert store.get_task(conn, task_id) == snapshot_task
    assert len(store.list_lifecycle_events_for_task(conn, task_id)) == event_count


def test_assign_workflow_missing_task_raises_lookup_error(conn):
    register_workflow(conn, _workflow("demo"))
    with pytest.raises(LookupError, match="no task"):
        assign_workflow(conn, task_id="task-missing", workflow_definition_id="demo")


def test_assign_workflow_unregistered_definition_raises_and_writes_nothing(conn):
    task = create_task(conn, title="Doomed")
    before = store.get_task(conn, task.id)

    with pytest.raises(UnknownWorkflowError, match="nope"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="nope")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_rejects_reassignment_to_a_different_definition(conn):
    register_workflow(conn, _workflow("demo"))
    register_workflow(conn, _workflow("other"))
    task = create_task(conn, title="Bound", workflow_definition_id="demo")
    before = store.get_task(conn, task.id)

    with pytest.raises(WorkflowAssignmentError, match="already assigned"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="other")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_rejects_a_completed_task(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Done")
    task = _force_status(conn, task, TaskStatus.COMPLETED)
    before = store.get_task(conn, task.id)

    with pytest.raises(WorkflowAssignmentError, match="completed"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_rejects_a_cancelled_task(conn):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Gone")
    task = _force_status(conn, task, TaskStatus.CANCELLED)
    before = store.get_task(conn, task.id)

    with pytest.raises(WorkflowAssignmentError, match="cancelled"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_empty_id_is_a_domain_value_error(conn):
    task = create_task(conn, title="Empty")
    before = store.get_task(conn, task.id)

    with pytest.raises(ValueError, match="workflow_definition_id"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_none_id_is_a_value_error(conn):
    task = create_task(conn, title="None")
    before = store.get_task(conn, task.id)

    with pytest.raises(ValueError, match="does not unassign"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id=None)

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_terminal_task_precedes_unregistered_definition(conn):
    task = create_task(conn, title="Precedence")
    task = _force_status(conn, task, TaskStatus.COMPLETED)
    before = store.get_task(conn, task.id)

    with pytest.raises(WorkflowAssignmentError):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="nope")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_assign_workflow_and_event_are_one_transaction(conn, monkeypatch):
    register_workflow(conn, _workflow("demo"))
    task = create_task(conn, title="Half-written")
    before = store.get_task(conn, task.id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(service.store, "insert_lifecycle_event", boom)
    with pytest.raises(RuntimeError, match="event write failed"):
        assign_workflow(conn, task_id=task.id, workflow_definition_id="demo")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


# --- create_run ------------------------------------------------------


def _flow(name="flow"):
    return Workflow(
        name=name,
        steps=(
            WorkflowStep(id="first", skill="s1"),
            WorkflowStep(id="second", skill="s2"),
        ),
    )


def _bound_task(conn, *, name="flow", title="Runnable"):
    workflow = _flow(name)
    register_workflow(conn, workflow)
    task = create_task(conn, title=title, workflow_definition_id=name)
    return workflow, task


def _initial(workflow, task):
    return resolve_initial_action(task, workflow)


def _run_events(conn, task_id, run_id):
    return [
        e
        for e in store.list_lifecycle_events_for_task(conn, task_id)
        if e.run_id == run_id
    ]


def _task_events(conn, task_id):
    return [e for e in _rows(conn, "lifecycle_events") if e["task_id"] == task_id]


def _assert_nothing_written(conn, *, events):
    """No Run row, and the lifecycle_events count is still ``events`` (plan §8)."""
    assert _rows(conn, "runs") == []
    assert len(_rows(conn, "lifecycle_events")) == events


def _complete(conn, run):
    done = replace(
        run,
        status=RunStatus.COMPLETED,
        completed_at=run.started_at + timedelta(seconds=1),
    )
    with conn:
        store.update_run(conn, done)
    return store.get_run(conn, run.id)


# create_run: contract change-detectors


def test_run_creation_error_is_a_distinct_type():
    assert not issubclass(RunCreationError, ValueError)
    assert not issubclass(RunCreationError, sqlite3.Error)


def test_create_run_never_infers_an_action():
    # action is keyword-only with no default: the caller must supply the
    # resolved lifecycle action, so nothing here can select one.
    param = inspect.signature(create_run).parameters["action"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_create_run_rejects_a_non_evaluation_output(conn):
    _, task = _bound_task(conn)
    with pytest.raises(ValueError, match="EvaluationOutput"):
        create_run(conn, task_id=task.id, action="run")
    assert _rows(conn, "runs") == []
    assert _rows(conn, "lifecycle_events") == _task_events(conn, task.id)


# create_run: shape / initial provenance


def test_create_run_from_initial_action_is_running_with_initial_provenance(conn):
    workflow, task = _bound_task(conn)
    action = _initial(workflow, task)
    task_before = store.get_task(conn, task.id)

    run = create_run(conn, task_id=task.id, action=action)

    assert run.status is RunStatus.RUNNING
    assert run.created_at == run.started_at
    assert run.completed_at is None
    assert run.trigger_reason == "initial"
    assert run.triggered_by_run_id is None
    assert run.step_id == action.step == "first"
    assert run.workflow_definition_id == task.workflow_definition_id == "flow"
    assert run.id.startswith("run-")
    # create_run never touches the tasks row: the Task stays active, unbumped
    # (SF-A-5 §4.9). assign_workflow, next door, does bump updated_at.
    assert store.get_task(conn, task.id) == task_before


def test_create_run_round_trips_by_full_equality(conn):
    workflow, task = _bound_task(conn)
    run = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    assert store.get_run(conn, run.id) == run


def test_create_run_is_committed(conn, ws):
    workflow, task = _bound_task(conn)
    run = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_run(reopened, run.id) == run


def test_two_runs_on_different_tasks_get_different_ids(conn):
    workflow, task_a = _bound_task(conn, title="A")
    task_b = create_task(conn, title="B", workflow_definition_id="flow")
    a = create_run(conn, task_id=task_a.id, action=_initial(workflow, task_a))
    b = create_run(conn, task_id=task_b.id, action=_initial(workflow, task_b))
    assert a.id != b.id


@pytest.mark.parametrize("instructions", ["do this", "", None])
def test_create_run_passes_instructions_through(conn, instructions):
    workflow, task = _bound_task(conn)
    run = create_run(
        conn,
        task_id=task.id,
        action=_initial(workflow, task),
        instructions=instructions,
    )
    assert run.instructions == instructions
    assert store.get_run(conn, run.id).instructions == instructions


def test_create_run_rejects_non_string_instructions_with_no_write(conn):
    workflow, task = _bound_task(conn)
    with pytest.raises(ValueError, match="Run.instructions"):
        create_run(
            conn,
            task_id=task.id,
            action=_initial(workflow, task),
            instructions=123,
        )
    assert _rows(conn, "runs") == []
    assert [e.type for e in store.list_lifecycle_events_for_task(conn, task.id)] == [
        LifecycleEventType.TASK_CREATED
    ]


# create_run: the run.created event


def test_create_run_writes_exactly_one_run_created_event(conn):
    workflow, task = _bound_task(conn)
    run = create_run(conn, task_id=task.id, action=_initial(workflow, task))

    events = _run_events(conn, task.id, run.id)
    assert len(events) == 1
    (event,) = events
    assert event.type is LifecycleEventType.RUN_CREATED
    assert event.run_id == run.id
    assert event.created_at == run.created_at
    assert dict(event.payload) == {
        "status": "running",
        "trigger_reason": "initial",
        "workflow_definition_id": "flow",
        "step_id": "first",
    }


def test_create_run_emits_no_run_started_event(conn):
    workflow, task = _bound_task(conn)
    run = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    types_ = [e.type for e in _run_events(conn, task.id, run.id)]
    assert LifecycleEventType.RUN_STARTED not in types_


# create_run: subsequent provenance


def test_subsequent_run_references_the_triggering_run_and_reason(conn):
    workflow, task = _bound_task(conn)
    first = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    _complete(conn, first)

    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="second")
    second = create_run(
        conn, task_id=task.id, action=action, triggered_by_run_id=first.id
    )

    assert second.triggered_by_run_id == first.id
    assert second.trigger_reason == "ready"
    assert second.step_id == "second"
    (event,) = _run_events(conn, task.id, second.id)
    assert dict(event.payload) == {
        "status": "running",
        "trigger_reason": "ready",
        "triggered_by_run_id": first.id,
        "workflow_definition_id": "flow",
        "step_id": "second",
    }


def test_skill_targeted_action_records_the_skill_and_no_step(conn):
    workflow, task = _bound_task(conn)
    first = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    _complete(conn, first)

    action = EvaluationOutput(
        action=ActionType.RUN, reason="fundamental_assumption_wrong", skill="research"
    )
    run = create_run(conn, task_id=task.id, action=action, triggered_by_run_id=first.id)

    assert run.step_id is None
    assert run.workflow_definition_id == "flow"
    (event,) = _run_events(conn, task.id, run.id)
    assert event.payload["skill"] == "research"
    assert "step_id" not in event.payload


# create_run: rejections -- each proves nothing was written


def test_create_run_rejects_a_duplicate_running_run(conn):
    workflow, task = _bound_task(conn)
    create_run(conn, task_id=task.id, action=_initial(workflow, task))
    events_before = len(_rows(conn, "lifecycle_events"))

    with pytest.raises(store.InvariantViolationError, match="already has a running"):
        create_run(conn, task_id=task.id, action=_initial(workflow, task))

    assert len(_rows(conn, "runs")) == 1
    assert len(_rows(conn, "lifecycle_events")) == events_before


def test_create_run_unknown_task_raises_lookup_error(conn):
    with pytest.raises(LookupError, match="no task"):
        create_run(
            conn,
            task_id="task-missing",
            action=EvaluationOutput(
                action=ActionType.RUN, reason="initial", step="first"
            ),
        )
    assert _rows(conn, "runs") == []
    assert _rows(conn, "lifecycle_events") == []


@pytest.mark.parametrize(
    "status",
    [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.WAITING_FOR_HUMAN],
)
def test_create_run_rejects_a_non_active_task(conn, status):
    workflow, task = _bound_task(conn)
    action = _initial(workflow, task)
    task = _force_status(conn, task, status)
    events_before = len(_rows(conn, "lifecycle_events"))

    with pytest.raises(RunCreationError, match=status.value):
        create_run(conn, task_id=task.id, action=action)

    assert _rows(conn, "runs") == []
    assert len(_rows(conn, "lifecycle_events")) == events_before


def test_create_run_rejects_an_unknown_triggering_run(conn):
    _, task = _bound_task(conn)
    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="second")
    with pytest.raises(RunCreationError, match="names no Run"):
        create_run(
            conn, task_id=task.id, action=action, triggered_by_run_id="run-missing"
        )
    assert _rows(conn, "runs") == []
    assert _rows(conn, "lifecycle_events") == _task_events(conn, task.id)


def test_create_run_rejects_a_triggering_run_from_another_task(conn):
    workflow, task_a = _bound_task(conn, title="A")
    task_b = create_task(conn, title="B", workflow_definition_id="flow")
    foreign = create_run(conn, task_id=task_a.id, action=_initial(workflow, task_a))

    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="second")
    events_before = len(_rows(conn, "lifecycle_events"))
    with pytest.raises(RunCreationError, match="belongs to task"):
        create_run(
            conn, task_id=task_b.id, action=action, triggered_by_run_id=foreign.id
        )

    assert len(_rows(conn, "runs")) == 1  # only task_a's run
    assert len(_rows(conn, "lifecycle_events")) == events_before


@pytest.mark.parametrize(
    "bad_action",
    [
        EvaluationOutput(action=ActionType.HUMAN, reason="human_required"),
        EvaluationOutput(action=ActionType.COMPLETE, reason="approved"),
        EvaluationOutput(action=ActionType.CANCEL, reason="cancel"),
    ],
)
def test_create_run_rejects_a_non_run_action(conn, bad_action):
    _, task = _bound_task(conn)
    events_before = len(_rows(conn, "lifecycle_events"))
    with pytest.raises(ValueError, match="must be 'run'"):
        create_run(conn, task_id=task.id, action=bad_action)
    _assert_nothing_written(conn, events=events_before)


def test_create_run_rejects_initial_reason_with_a_triggering_run(conn):
    workflow, task = _bound_task(conn)
    first = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    _complete(conn, first)

    with pytest.raises(ValueError, match="initial Run must not"):
        create_run(
            conn,
            task_id=task.id,
            action=_initial(workflow, task),
            triggered_by_run_id=first.id,
        )
    assert len(_rows(conn, "runs")) == 1


def test_create_run_rejects_non_initial_reason_without_a_triggering_run(conn):
    _, task = _bound_task(conn)
    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="second")
    events_before = len(_rows(conn, "lifecycle_events"))
    with pytest.raises(ValueError, match="non-initial"):
        create_run(conn, task_id=task.id, action=action)
    _assert_nothing_written(conn, events=events_before)


def test_create_run_rejects_a_step_action_when_task_has_no_workflow(conn):
    task = create_task(conn, title="Unbound")
    action = EvaluationOutput(action=ActionType.RUN, reason="initial", step="first")
    events_before = len(_rows(conn, "lifecycle_events"))
    with pytest.raises(ValueError, match="no workflow definition"):
        create_run(conn, task_id=task.id, action=action)
    _assert_nothing_written(conn, events=events_before)


def test_create_run_unknown_task_precedes_the_triggering_run_check(conn):
    with pytest.raises(LookupError):
        create_run(
            conn,
            task_id="task-missing",
            action=EvaluationOutput(
                action=ActionType.RUN, reason="ready", step="second"
            ),
            triggered_by_run_id="run-also-missing",
        )


def test_create_run_non_active_task_precedes_the_triggering_run_check(conn):
    _, task = _bound_task(conn)
    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="second")
    task = _force_status(conn, task, TaskStatus.COMPLETED)
    with pytest.raises(RunCreationError, match="completed"):
        create_run(
            conn, task_id=task.id, action=action, triggered_by_run_id="run-missing"
        )


def test_create_run_action_shape_precedes_the_unknown_task_check(conn):
    # rules 1-2 (action must be a run EvaluationOutput) beat rule 3 (Task exists).
    with pytest.raises(ValueError, match="must be 'run'"):
        create_run(
            conn,
            task_id="task-missing",
            action=EvaluationOutput(action=ActionType.HUMAN, reason="human_required"),
        )


def test_create_run_triggering_run_check_precedes_the_missing_workflow_check(conn):
    # rule 5 (triggering Run must exist and match) beats rule 6 (step needs a
    # Workflow): an unbound Task with a step action and a bad triggering Run
    # surfaces the RunCreationError, not the ValueError.
    task = create_task(conn, title="Unbound")
    action = EvaluationOutput(action=ActionType.RUN, reason="ready", step="first")
    with pytest.raises(RunCreationError, match="names no Run"):
        create_run(
            conn, task_id=task.id, action=action, triggered_by_run_id="run-missing"
        )


# create_run: atomicity


def test_create_run_and_event_are_one_transaction(conn, monkeypatch):
    workflow, task = _bound_task(conn)

    def boom(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(service.store, "insert_lifecycle_event", boom)
    with pytest.raises(RuntimeError, match="event write failed"):
        create_run(conn, task_id=task.id, action=_initial(workflow, task))

    assert _rows(conn, "runs") == []


# create_run: vertical slice over the real reference workflow


def test_vertical_slice_initial_then_subsequent_run(conn):
    workflow = load_workflow(REFERENCE)
    register_workflow(conn, workflow)
    task = create_task(conn, title="Ship it", workflow_definition_id="software-change")

    first_action = resolve_initial_action(task, workflow)
    first = create_run(conn, task_id=task.id, action=first_action)
    assert first.step_id == "requirements"
    assert first.trigger_reason == "initial"
    assert first.triggered_by_run_id is None

    first = _complete(conn, first)
    result = Result(
        id="result-1",
        run_id=first.id,
        status=ResultStatus.COMPLETED,
        created_at=first.completed_at,
        outcome=Outcome(type="requirements", decision="ready"),
    )
    with conn:
        store.insert_result(conn, result)

    second_action = evaluate(
        EvaluationInput(
            task=task,
            workflow=workflow,
            current_run=first,
            result=result,
        )
    )
    second = create_run(
        conn, task_id=task.id, action=second_action, triggered_by_run_id=first.id
    )
    assert second.step_id == "decomposition"
    assert second.trigger_reason == "ready"
    assert second.triggered_by_run_id == first.id


# --- apply_lifecycle_action --------------------------------------------


def _action(action, reason, **target):
    return EvaluationOutput(action=action, reason=reason, **target)


def test_apply_lifecycle_action_takes_task_and_action_as_keywords(conn):
    # Both are keyword-only with no default: the caller must supply the stored
    # Task and the resolved action, so nothing here can select either.
    sig = inspect.signature(apply_lifecycle_action)
    for name in ("task", "action"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("action", "from_status", "to_status"),
    [
        (
            _action(ActionType.RUN, "request_changes", step="implementation"),
            TaskStatus.WAITING_FOR_HUMAN,
            TaskStatus.ACTIVE,
        ),
        (
            _action(ActionType.HUMAN, "human_required"),
            TaskStatus.ACTIVE,
            TaskStatus.WAITING_FOR_HUMAN,
        ),
        (
            _action(ActionType.COMPLETE, "approved"),
            TaskStatus.ACTIVE,
            TaskStatus.COMPLETED,
        ),
        (
            _action(ActionType.CANCEL, "cancel"),
            TaskStatus.ACTIVE,
            TaskStatus.CANCELLED,
        ),
    ],
)
def test_apply_lifecycle_action_mapping(conn, action, from_status, to_status):
    task = create_task(conn, title="Consequential")
    task = _force_status(conn, task, from_status)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))

    with conn:
        updated = apply_lifecycle_action(conn, task=task, action=action)

    assert updated.status is to_status
    assert store.get_task(conn, task.id) == updated
    events = store.list_lifecycle_events_for_task(conn, task.id)
    assert len(events) == events_before + 1
    (event,) = events[events_before:]
    assert event.type is LifecycleEventType.TASK_STATUS_CHANGED
    # The clock is read once: the event shares the Task's new stamp (which may
    # predate the +1s `_force_status` stamp above -- the direction is not the
    # contract, the single stamp is).
    assert event.created_at == updated.updated_at
    assert updated.updated_at != task.updated_at
    assert dict(event.payload) == {
        "from": from_status.value,
        "to": to_status.value,
        "action": action.action.value,
        "reason": action.reason,
    }


def test_apply_lifecycle_action_covers_every_action_type():
    # A new ActionType member without a mapping row is a contract change, not
    # a silent KeyError at runtime.
    assert set(service._ACTION_TASK_STATUS) == set(ActionType)


def test_apply_lifecycle_action_carries_run_id_and_reuses_now(conn):
    from datetime import UTC, datetime

    workflow, task = _bound_task(conn, title="Stamped")
    run = create_run(conn, task_id=task.id, action=_initial(workflow, task))
    now = datetime.now(UTC) + timedelta(seconds=30)
    action = _action(ActionType.COMPLETE, "approved")

    with conn:
        updated = apply_lifecycle_action(
            conn, task=task, action=action, run_id=run.id, now=now
        )

    assert updated.updated_at == now
    (event,) = store.list_lifecycle_events_for_task(conn, task.id)[-1:]
    assert event.run_id == run.id
    assert event.created_at == now


def test_apply_lifecycle_action_no_op_writes_nothing(conn):
    task = create_task(conn, title="Steady")
    before = store.get_task(conn, task.id)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    action = _action(ActionType.RUN, "ready", step="decomposition")

    with conn:
        updated = apply_lifecycle_action(conn, task=task, action=action)

    assert updated == before
    assert updated.updated_at == before.updated_at
    assert store.get_task(conn, task.id) == before
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == events_before


def test_apply_lifecycle_action_does_not_commit(conn, ws):
    task = create_task(conn, title="Uncommitted")
    action = _action(ActionType.HUMAN, "human_required")

    apply_lifecycle_action(conn, task=task, action=action)
    assert conn.in_transaction  # the write is still pending: no commit happened
    conn.rollback()

    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_task(reopened, task.id).status is TaskStatus.ACTIVE
        assert len(store.list_lifecycle_events_for_task(reopened, task.id)) == 1


def test_apply_lifecycle_action_missing_task_raises_lookup_error(conn):
    task = create_task(conn, title="Gone")
    ghost = replace(task, id="task-missing")
    action = _action(ActionType.COMPLETE, "approved")

    with pytest.raises(LookupError, match="no task"):
        apply_lifecycle_action(conn, task=ghost, action=action)

    assert _rows(conn, "tasks") != []
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE


def test_apply_lifecycle_action_rejects_a_non_evaluation_output(conn):
    task = create_task(conn, title="Bad action")
    before = store.get_task(conn, task.id)

    with pytest.raises(ValueError, match="EvaluationOutput"):
        apply_lifecycle_action(conn, task=task, action="complete")

    _assert_task_and_events_unchanged(conn, task.id, before, 1)


def test_apply_lifecycle_action_rejects_a_non_task(conn):
    action = _action(ActionType.COMPLETE, "approved")

    with pytest.raises(ValueError, match="must be a Task"):
        apply_lifecycle_action(conn, task="task-1", action=action)

    assert _rows(conn, "tasks") == []
    assert _rows(conn, "lifecycle_events") == []


def test_apply_lifecycle_action_shape_precedes_the_missing_task_check(conn):
    # Rule 1 (action must be an EvaluationOutput) beats rule 3 (Task exists).
    ghost = replace(create_task(conn, title="Gone"), id="task-missing")

    with pytest.raises(ValueError, match="EvaluationOutput"):
        apply_lifecycle_action(conn, task=ghost, action="complete")


def test_apply_lifecycle_action_rejects_a_naive_now(conn):
    from datetime import datetime

    task = create_task(conn, title="Naive")
    before = store.get_task(conn, task.id)

    with pytest.raises(ValueError, match="timezone-aware"):
        with conn:
            apply_lifecycle_action(
                conn,
                task=task,
                action=_action(ActionType.COMPLETE, "approved"),
                now=datetime.now(),
            )

    _assert_task_and_events_unchanged(conn, task.id, before, 1)
