"""Tests for ``skillflow.service``.

Two kinds of test, matching ``test_store.py`` / ``test_workspace.py``:

* **Contract change-detectors** -- the public surface, the import boundary (the
  service reads no definition files), and the ``UnknownWorkflowError`` /
  ``WorkflowAssignmentError`` types.
* **Behaviour tests** -- Task creation (status, ids, timestamps, round-trip,
  durability, the ``task.created`` event, workflow-reference validation and
  atomicity), ``register_workflow`` idempotence and commit, ``assign_workflow``
  (persistence, immutable-field preservation, durability, the
  ``task.workflow_assigned`` event, ``waiting_for_human`` acceptance,
  idempotent no-op / normalisation, every rejection path proving no write,
  error precedence, and transaction atomicity), and the SF-8 -> SF-9
  integration path through the real reference definition.
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
from skillflow.domain import LifecycleEventType, TaskStatus
from skillflow.service import (
    UnknownWorkflowError,
    WorkflowAssignmentError,
    assign_workflow,
    create_task,
    register_workflow,
)
from skillflow.workflow import Workflow, WorkflowStep
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
        "register_workflow",
        "create_task",
        "assign_workflow",
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
