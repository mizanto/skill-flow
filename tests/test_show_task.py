"""Tests for ``skillflow.show_task`` (SF-38).

Following ``test_prepare_artifacts.py``'s shape: a ``tmp_path`` workspace
fixture with a ``.git`` marker, ``store.open_store``, and the **real**
``workflows/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, so the view is exercised against state the system produces;
raw ``store.insert_*`` calls build the states the service layer cannot
(zero events anywhere, failed Results with diagnostics).

* **Contract change-detectors** -- the public surface, ``ShowTaskError``
  (type and ``code`` attribute), the ``TaskView``/``RunView`` validation,
  and the AST import boundary (reads only: no clock, no filesystem, no
  workflow loading).
* **Behaviour tests** -- rejection, empty and populated views, the
  no-event-replay proof, skill-targeted and failed Runs, terminal Tasks,
  and the read-only proof (viewing records nothing).
"""

import ast
import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import show_task as show_task_pkg
from skillflow import store, workspace
from skillflow.domain import (
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
)
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.show_task import RunView, ShowTaskError, TaskView
from skillflow.show_task import show_task as show
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


@pytest.fixture
def workflows(ws):
    directory = ws.root / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / "software-change.yaml").write_text(
        REFERENCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


def _assigned(conn, *, title="Ship it", name="software-change"):
    workflow = load_workflow(REFERENCE)
    register_workflow(conn, workflow)
    task = create_task(conn, title=title, workflow_definition_id=name)
    return workflow, task


def _register(conn):
    # The tasks/runs FK-reference workflow_definitions(id); registering
    # writes no lifecycle event, so zero-event scenarios stay event-free.
    register_workflow(conn, load_workflow(REFERENCE))


def _task_raw(conn, *, id="task-raw", status=TaskStatus.ACTIVE, **over):
    now = datetime.now(UTC)
    kw = dict(
        id=id,
        title="Raw task",
        description="",
        status=status,
        created_at=now,
        updated_at=now,
        workflow_definition_id="software-change",
    )
    task = Task(**(kw | over))
    with conn:
        store.insert_task(conn, task)
    return store.get_task(conn, task.id)


def _handbuilt_run(conn, task_id, **over):
    now = datetime.now(UTC)
    kw = dict(
        id="run-manual",
        task_id=task_id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        workflow_definition_id="software-change",
        step_id="requirements",
        trigger_reason="initial",
    )
    run = Run(**(kw | over))
    with conn:
        store.insert_run(conn, run)
    return store.get_run(conn, run.id)


def _complete(conn, run):
    done = replace(
        run,
        status=RunStatus.COMPLETED,
        completed_at=run.started_at + timedelta(seconds=1),
    )
    with conn:
        store.update_run(conn, done)
    return store.get_run(conn, run.id)


def _result(conn, run, *, decision, type="requirements", **over):
    kw = dict(
        id=f"result-{run.id}",
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=run.completed_at,
        outcome=Outcome(type=type, decision=decision) if decision is not None else None,
    )
    result = Result(**(kw | over))
    with conn:
        store.insert_result(conn, result)
    return result


def _artifact_raw(conn, run, *, name="requirements.md", **over):
    kw = dict(
        id=f"artifact-{run.id}-{name}",
        task_id=run.task_id,
        run_id=run.id,
        name=name,
        type="requirements",
        version=1,
        path=name,
        created_at=datetime.now(UTC),
    )
    artifact = Artifact(**(kw | over))
    with conn:
        store.insert_artifact(conn, artifact)
    return artifact


def _decision_raw(conn, run, *, decision="request_changes", **over):
    kw = dict(
        id=f"decision-{run.id}",
        task_id=run.task_id,
        run_id=run.id,
        decision=decision,
        created_at=datetime.now(UTC),
        comment="Need stronger test coverage",
    )
    made = HumanDecision(**(kw | over))
    with conn:
        store.insert_human_decision(conn, made)
    return made


def _event_raw(
    conn, task_id, id, *, type=LifecycleEventType.TASK_STATUS_CHANGED, **over
):
    kw = dict(
        id=id,
        task_id=task_id,
        type=type,
        created_at=datetime.now(UTC),
    )
    event = LifecycleEvent(**(kw | over))
    with conn:
        store.insert_lifecycle_event(conn, event)
    return event


def _snapshot(conn, task_id):
    return (
        store.get_task(conn, task_id),
        [store.get_run(conn, r.id) for r in store.list_runs_for_task(conn, task_id)],
        [(a.id, a.version) for a in store.list_artifacts_for_task(conn, task_id)],
        [d.id for d in store.list_human_decisions_for_task(conn, task_id)],
        [(e.id, e.type) for e in store.list_lifecycle_events_for_task(conn, task_id)],
    )


def _table_counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "tasks",
            "runs",
            "results",
            "artifacts",
            "workflow_definitions",
            "human_decisions",
            "lifecycle_events",
        )
    }


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(show_task_pkg.__all__) == {
        "RunView",
        "ShowTaskError",
        "TaskView",
        "show_task",
    }


def test_show_task_error_carries_code():
    err = ShowTaskError("TaskNotFound", "no task")
    assert isinstance(err, Exception)
    assert err.code == "TaskNotFound"
    assert str(err) == "no task"


def test_module_imports_are_within_the_boundary():
    # show-task inspects but never launches, loads, or writes: no workflow
    # loader, no subprocess, no shell, no filesystem, no clock -- the same
    # mechanical guarantee the sibling inspection module pins.
    source = Path(show_task_pkg.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {"sqlite3", "dataclasses", "skillflow"}, (
        f"unexpected imports: {modules}"
    )
    for forbidden in (
        "subprocess",
        "os",
        "shutil",
        "sys",
        "pathlib",
        "yaml",
        "datetime",
        "random",
        "time",
    ):
        assert forbidden not in modules


def _view_parts():
    now = datetime.now(UTC)
    task = Task(
        id="task-1",
        title="Ship it",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    run = Run(
        id="run-1",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        trigger_reason="initial",
    )
    return task, run


def test_run_view_rejects_non_run():
    _, run = _view_parts()
    with pytest.raises(ValueError):
        RunView(run="not-a-run", result=None)
    with pytest.raises(ValueError):
        RunView(run=run, result="not-a-result")
    with pytest.raises(ValueError):
        RunView(run=run, result=None, artifacts="not-artifacts")
    with pytest.raises(ValueError):
        RunView(run=run, result=None, decisions=(run,))


def test_task_view_rejects_non_task():
    task, run = _view_parts()
    with pytest.raises(ValueError):
        TaskView(task="not-a-task")
    with pytest.raises(ValueError):
        TaskView(task=task, runs="not-runs")
    with pytest.raises(ValueError):
        TaskView(task=task, runs=(run,))
    with pytest.raises(ValueError):
        TaskView(task=task, events=(task,))


# --- rejection ------------------------------------------------------------


def test_unknown_task_rejected(conn):
    with pytest.raises(ShowTaskError) as exc_info:
        show(conn, task_id="task-nope")
    assert exc_info.value.code == "TaskNotFound"
    message = str(exc_info.value)
    assert "task-nope" in message
    assert "skillflow show-task" in message


def test_blank_task_id_rejected(conn):
    with pytest.raises(ShowTaskError) as exc_info:
        show(conn, task_id="  ")
    assert exc_info.value.code == "TaskNotFound"


def test_rejection_writes_nothing(conn):
    _, task = _assigned(conn)
    before = _table_counts(conn)
    with pytest.raises(ShowTaskError):
        show(conn, task_id="task-nope")
    assert _table_counts(conn) == before
    assert store.get_task(conn, task.id) is not None


# --- views ----------------------------------------------------------------


def test_empty_task_shows_no_runs_or_events(conn):
    _register(conn)
    task = _task_raw(conn)
    assert store.list_lifecycle_events_for_task(conn, task.id) == []

    view = show(conn, task_id=task.id)

    assert view.task.id == task.id
    assert view.task.status is TaskStatus.ACTIVE
    assert view.runs == ()
    assert view.events == ()


def test_view_shows_runs_results_artifacts_decisions_events(conn, ws, workflows):
    _, task = _assigned(conn)
    first = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _result(conn, _complete(conn, first), decision="ready", type="requirements")
    artifact = _artifact_raw(conn, first)
    second = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    made = _decision_raw(conn, first, comment="keep going")
    later = datetime.now(UTC) + timedelta(seconds=30)
    _event_raw(
        conn,
        task.id,
        "event-status",
        type=LifecycleEventType.TASK_STATUS_CHANGED,
        payload={"from": "active", "to": "waiting_for_human"},
        created_at=later,
    )
    _event_raw(
        conn,
        task.id,
        "event-decision",
        type=LifecycleEventType.HUMAN_DECISION_MADE,
        run_id=first.id,
        payload={"decision": "request_changes"},
        created_at=later + timedelta(seconds=1),
    )

    view = show(conn, task_id=task.id)

    assert view.task.id == task.id
    assert [run_view.run.id for run_view in view.runs] == [first.id, second.id]
    assert [run_view.run.status for run_view in view.runs] == [
        RunStatus.COMPLETED,
        RunStatus.RUNNING,
    ]
    assert view.runs[0].run.trigger_reason == "initial"
    assert view.runs[0].run.triggered_by_run_id is None
    assert view.runs[1].run.triggered_by_run_id == first.id
    assert view.runs[1].run.trigger_reason == "ready"
    assert view.runs[1].run.step_id == "decomposition"
    assert view.runs[0].result.outcome.decision == "ready"
    assert view.runs[0].result.outcome.type == "requirements"
    assert view.runs[1].result is None
    assert [a.id for a in view.runs[0].artifacts] == [artifact.id]
    assert view.runs[1].artifacts == ()
    assert [d.id for d in view.runs[0].decisions] == [made.id]
    assert view.runs[0].decisions[0].comment == "keep going"
    assert view.runs[1].decisions == ()
    types = [event.type for event in view.events]
    assert types[0] is LifecycleEventType.TASK_CREATED
    assert types.count(LifecycleEventType.RUN_CREATED) == 2
    assert types[-2:] == [
        LifecycleEventType.TASK_STATUS_CHANGED,
        LifecycleEventType.HUMAN_DECISION_MADE,
    ]
    assert view.events[-1].run_id == first.id
    assert view.events[-1].payload["decision"] == "request_changes"
    stamps = [event.created_at for event in view.events]
    assert stamps == sorted(stamps)


def test_view_without_events_shows_current_state(conn):
    # The no-event-replay proof (SF-38 criterion 3): every row below goes
    # through raw store inserts, so the Task has no events at all -- the
    # view must still be complete and correct.
    _register(conn)
    task = _task_raw(conn)
    first = _complete(conn, _handbuilt_run(conn, task.id, id="run-1"))
    second = _handbuilt_run(
        conn,
        task.id,
        id="run-2",
        step_id="decomposition",
        triggered_by_run_id="run-1",
        trigger_reason="ready",
    )
    result = _result(conn, first, decision="ready", type="requirements")
    artifact = _artifact_raw(conn, first)
    made = _decision_raw(conn, first)
    assert store.list_lifecycle_events_for_task(conn, task.id) == []

    view = show(conn, task_id=task.id)

    assert view.task.status is TaskStatus.ACTIVE
    assert [run_view.run.id for run_view in view.runs] == ["run-1", "run-2"]
    assert view.runs[1].run.triggered_by_run_id == "run-1"
    assert view.runs[0].result.id == result.id
    assert view.runs[0].result.outcome.decision == "ready"
    assert [a.id for a in view.runs[0].artifacts] == [artifact.id]
    assert [d.id for d in view.runs[0].decisions] == [made.id]
    assert view.events == ()
    assert second.id == "run-2"


def test_show_task_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _artifact_raw(conn, run)
    before = _snapshot(conn, task.id)

    show(conn, task_id=task.id)

    assert _snapshot(conn, task.id) == before


def test_skill_targeted_run_shows_stored_fields(conn):
    _register(conn)
    task = _task_raw(conn)
    run = _handbuilt_run(conn, task.id, step_id=None)

    view = show(conn, task_id=task.id)

    assert len(view.runs) == 1
    assert view.runs[0].run.id == run.id
    assert view.runs[0].run.step_id is None
    assert view.runs[0].run.workflow_definition_id == "software-change"
    assert view.runs[0].result is None


def test_failed_run_carries_diagnostics_reference(conn):
    _register(conn)
    task = _task_raw(conn)
    run = _handbuilt_run(conn, task.id, id="run-failed")
    failed = replace(
        run,
        status=RunStatus.FAILED,
        completed_at=run.started_at + timedelta(seconds=1),
    )
    with conn:
        store.update_run(conn, failed)
    stored = store.get_run(conn, run.id)
    _result(
        conn,
        stored,
        decision=None,
        status=ResultStatus.FAILED,
        metadata={"diagnostics": "runs/run-failed/output.log"},
    )

    view = show(conn, task_id=task.id)

    assert view.runs[0].run.status is RunStatus.FAILED
    assert view.runs[0].result.status is ResultStatus.FAILED
    assert view.runs[0].result.outcome is None
    assert view.runs[0].result.metadata["diagnostics"] == "runs/run-failed/output.log"


def test_terminal_task_is_viewable(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    done = replace(
        store.get_task(conn, task.id),
        status=TaskStatus.COMPLETED,
        updated_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    with conn:
        store.update_task(conn, done)

    view = show(conn, task_id=task.id)

    assert view.task.status is TaskStatus.COMPLETED
    assert [run_view.run.id for run_view in view.runs] == [run_id]
