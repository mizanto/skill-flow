"""Tests for ``skillflow.service``.

Two kinds of test, matching ``test_store.py`` / ``test_workspace.py``:

* **Contract change-detectors** -- the public surface, the import boundary (the
  service reads no definition files), and the ``UnknownWorkflowError`` type.
* **Behaviour tests** -- Task creation (status, ids, timestamps, round-trip,
  durability, the ``task.created`` event, workflow-reference validation and
  atomicity), ``register_workflow`` idempotence and commit, and the SF-8 -> SF-9
  integration path through the real reference definition.
"""

import ast
import contextlib
import sqlite3
from pathlib import Path

import pytest

from skillflow import service, store, workspace
from skillflow.domain import LifecycleEventType, TaskStatus
from skillflow.service import UnknownWorkflowError, create_task, register_workflow
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
        "register_workflow",
        "create_task",
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
