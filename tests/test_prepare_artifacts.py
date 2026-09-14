"""Tests for ``skillflow.prepare_artifacts`` (SF-21).

Following ``test_resolve_task.py``'s shape: a ``tmp_path`` workspace fixture
with a ``.git`` marker, ``store.open_store``, and the frozen
``runtime-reference/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, so the command is exercised against state the system produces.

* **Contract change-detectors** -- the public surface, ``PrepareArtifactsError``
  (type and ``code`` attribute), the ``ArtifactReport`` validation, and the AST
  import boundary (read-only: no Claude Code launch, no clock, no filesystem).
* **Behaviour tests** -- current-Run resolution, step resolution, report
  content, determinism, and the read-only proof (no lifecycle mutation).
"""

import ast
import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import prepare_artifacts as prepare_artifacts_pkg
from skillflow import store, workspace
from skillflow.artifacts import create_artifact
from skillflow.domain import (
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import resolve_initial_action
from skillflow.outputs import OutputValidation
from skillflow.prepare_artifacts import ArtifactReport, PrepareArtifactsError
from skillflow.prepare_artifacts import prepare_artifacts as prepare
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow_loader import WorkflowLoadError, load_workflow

REFERENCE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "workflows"
    / "runtime-reference"
    / "software-change.yaml"
)


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


def _complete(conn, run):
    done = replace(
        run,
        status=RunStatus.COMPLETED,
        completed_at=run.started_at + timedelta(seconds=1),
    )
    with conn:
        store.update_run(conn, done)
    return store.get_run(conn, run.id)


def _result(conn, run, *, decision, type="requirements"):
    result = Result(
        id=f"result-{run.id}",
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=run.completed_at,
        outcome=Outcome(type=type, decision=decision) if decision is not None else None,
    )
    with conn:
        store.insert_result(conn, result)
    return result


def _run_row(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _snapshot(conn, task_id, run_id):
    task = store.get_task(conn, task_id)
    run = store.get_run(conn, run_id)
    return (
        task.status,
        task.updated_at,
        run.status,
        run.completed_at,
        store.get_result_for_run(conn, run_id),
        len(store.list_artifacts_for_run(conn, run_id)),
        len(store.list_lifecycle_events_for_task(conn, task_id)),
        len(store.list_runs_for_task(conn, task_id)),
    )


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(prepare_artifacts_pkg.__all__) == {
        "ArtifactReport",
        "PrepareArtifactsError",
        "prepare_artifacts",
    }


def test_prepare_artifacts_error_carries_code():
    err = PrepareArtifactsError("RunNotFound", "no Run")
    assert isinstance(err, Exception)
    assert err.code == "RunNotFound"
    assert str(err) == "no Run"


def test_module_imports_are_within_the_boundary():
    # prepare-artifacts inspects but never launches: no subprocess, no shell,
    # no filesystem writes of its own, no clock -- the same mechanical
    # guarantee the context/outputs/artifacts modules pin.
    source = Path(prepare_artifacts_pkg.__file__).read_text()
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


def test_report_rejects_blank_ids():
    validation = OutputValidation(checks=())
    for field in ("task_id", "run_id", "step_id"):
        kwargs = dict(
            task_id="task-1", run_id="run-1", step_id="s", validation=validation
        )
        kwargs[field] = "  "
        with pytest.raises(ValueError):
            ArtifactReport(**kwargs)


def test_report_rejects_non_validation():
    with pytest.raises(ValueError):
        ArtifactReport(
            task_id="task-1",
            run_id="run-1",
            step_id="s",
            validation="not-a-validation",
        )


def test_report_holds_validation_whole():
    validation = OutputValidation(checks=())
    report = ArtifactReport(
        task_id="task-1", run_id="run-1", step_id="s", validation=validation
    )
    assert report.validation is validation


# --- current-Run resolution -------------------------------------------------


def test_empty_workspace_rejects_with_run_not_found(conn, ws, workflows):
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws)
    assert exc_info.value.code == "RunNotFound"
    assert "resolve-task" in str(exc_info.value)


def test_single_running_run_resolves(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    report = prepare(conn, ws)
    assert report.task_id == task.id
    assert report.run_id == run_input.run_id
    assert report.step_id == "requirements"


def test_two_running_runs_reject_as_ambiguous(conn, ws, workflows):
    _, task_a = _assigned(conn, title="First")
    workflow, task_b = _assigned(conn, title="Second")
    run_a = store.get_run(conn, resolve(conn, ws, task_id=task_a.id).run_id)
    # A second running Run is unreachable through resolve-task since SF-43;
    # seeded through the service to keep the AmbiguousCurrentRun path covered.
    run_b = create_run(
        conn, task_id=task_b.id, action=resolve_initial_action(task_b, workflow)
    )
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws)
    assert exc_info.value.code == "AmbiguousCurrentRun"
    message = str(exc_info.value)
    assert task_a.id in message and run_a.id in message
    assert task_b.id in message and run_b.id in message
    assert "--task" in message


def test_task_disambiguator_selects_the_named_task(conn, ws, workflows):
    _, task_a = _assigned(conn, title="First")
    workflow, task_b = _assigned(conn, title="Second")
    run_a_id = resolve(conn, ws, task_id=task_a.id).run_id
    # A second running Run is unreachable through resolve-task since SF-43;
    # seeded through the service to keep the AmbiguousCurrentRun path covered.
    run_b_id = create_run(
        conn, task_id=task_b.id, action=resolve_initial_action(task_b, workflow)
    ).id
    report = prepare(conn, ws, task_id=task_b.id)
    assert (report.task_id, report.run_id) == (task_b.id, run_b_id)
    other = prepare(conn, ws, task_id=task_a.id)
    assert (other.task_id, other.run_id) == (task_a.id, run_a_id)


def test_unknown_task_rejects_with_task_not_found(conn, ws, workflows):
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id="task-nope")
    assert exc_info.value.code == "TaskNotFound"
    assert "task-nope" in str(exc_info.value)


def test_task_with_no_runs_rejects_with_run_not_found(conn, ws, workflows):
    _, task = _assigned(conn)
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id=task.id)
    assert exc_info.value.code == "RunNotFound"
    assert task.id in str(exc_info.value)
    assert "resolve-task" in str(exc_info.value)


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.WAITING_FOR_HUMAN,
    ],
)
def test_task_with_no_running_run_rejects_with_run_not_active(
    conn, ws, workflows, status
):
    _, task = _assigned(conn)
    run = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    done = replace(
        run,
        status=status,
        completed_at=run.started_at + timedelta(seconds=1),
    )
    with conn:
        store.update_run(conn, done)
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id=task.id)
    assert exc_info.value.code == "RunNotActive"
    message = str(exc_info.value)
    assert run.id in message
    assert status.value in message
    assert "resolve-task" in message


def test_completed_runs_are_ignored_without_task(conn, ws, workflows):
    _, task_a = _assigned(conn, title="First")
    _, task_b = _assigned(conn, title="Second")
    run_a = store.get_run(conn, resolve(conn, ws, task_id=task_a.id).run_id)
    _result(conn, _complete(conn, run_a), decision="ready", type="requirements")
    run_b_id = resolve(conn, ws, task_id=task_b.id).run_id
    report = prepare(conn, ws)
    assert (report.task_id, report.run_id) == (task_b.id, run_b_id)


# --- step resolution --------------------------------------------------------


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


def test_run_without_step_rejects_with_step_unresolved(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-skill", step_id=None)
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id=task.id)
    assert exc_info.value.code == "StepUnresolved"
    assert run.id in str(exc_info.value)


def test_run_without_workflow_rejects_with_step_unresolved(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-nowf", workflow_definition_id=None)
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id=task.id)
    assert exc_info.value.code == "StepUnresolved"
    assert run.id in str(exc_info.value)


def test_deleted_definition_propagates_workflow_load_error(conn, ws, workflows):
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)
    (ws.root / "workflows" / "software-change.yaml").unlink()
    with pytest.raises(WorkflowLoadError):
        prepare(conn, ws, task_id=task.id)


def test_unknown_step_rejects_with_workflow_mismatch(conn, ws, workflows):
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)
    (ws.root / "workflows" / "software-change.yaml").write_text(
        "name: software-change\nsteps:\n  - id: other\n    skill: something-else\n",
        encoding="utf-8",
    )
    with pytest.raises(PrepareArtifactsError) as exc_info:
        prepare(conn, ws, task_id=task.id)
    assert exc_info.value.code == "WorkflowMismatch"
    message = str(exc_info.value)
    assert "requirements" in message
    assert "software-change" in message


# --- report content ---------------------------------------------------------


def test_fresh_run_reports_missing_required_output(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    report = prepare(conn, ws, task_id=task.id)
    assert report.run_id == run_input.run_id
    (check,) = report.validation.checks
    assert check.type == "requirements"
    assert check.required is True
    assert check.satisfied is False
    assert report.validation.is_complete is False
    assert len(report.validation.missing_required) == 1


def test_registered_artifact_satisfies_the_output(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    artifact = create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="the requirements",
    )
    report = prepare(conn, ws, task_id=task.id)
    (check,) = report.validation.checks
    assert check.satisfied is True
    assert check.artifacts == (artifact,)
    assert report.validation.is_complete is True


def test_two_versions_both_appear_in_order(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    first = create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="v1",
    )
    second = create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="v2",
    )
    report = prepare(conn, ws, task_id=task.id)
    (check,) = report.validation.checks
    assert check.artifacts == (first, second)


def test_undeclared_type_satisfies_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    create_artifact(
        conn, ws, run_id=run.id, name="notes.md", type="notes", content="notes"
    )
    report = prepare(conn, ws, task_id=task.id)
    (check,) = report.validation.checks
    assert check.satisfied is False
    assert report.validation.is_complete is False


def _drive_to_step(conn, ws, task, step_id):
    """Complete each step as ``ready`` until a Run for ``step_id`` exists."""
    for step in ("requirements", "decomposition", "implementation", "review"):
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == step
        run = store.get_run(conn, run_input.run_id)
        if step == step_id:
            return run
        _result(conn, _complete(conn, run), decision="ready", type=step)
    raise AssertionError(f"step {step_id!r} not reached")


def test_step_without_outputs_is_complete(conn, ws, workflows):
    _, task = _assigned(conn)
    _drive_to_step(conn, ws, task, "implementation")
    report = prepare(conn, ws, task_id=task.id)
    assert report.step_id == "implementation"
    assert report.validation.checks == ()
    assert report.validation.is_complete is True


def test_optional_output_missing_stays_complete(conn, ws, workflows):
    (ws.root / "workflows" / "maybe.yaml").write_text(
        "name: maybe\n"
        "steps:\n"
        "  - id: draft\n"
        "    skill: drafting\n"
        "    outputs:\n"
        "      - type: notes\n"
        "        required: false\n",
        encoding="utf-8",
    )
    workflow = load_workflow(ws.root / "workflows" / "maybe.yaml")
    register_workflow(conn, workflow)
    task = create_task(conn, title="Maybe", workflow_definition_id="maybe")
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "draft"
    report = prepare(conn, ws, task_id=task.id)
    (check,) = report.validation.checks
    assert check.type == "notes"
    assert check.required is False
    assert check.satisfied is False
    assert report.validation.is_complete is True
    assert report.validation.missing_required == ()


def test_earlier_run_artifact_does_not_satisfy_current_run(conn, ws, workflows):
    _, task = _assigned(conn)
    first_input = resolve(conn, ws, task_id=task.id)
    first = store.get_run(conn, first_input.run_id)
    create_artifact(
        conn,
        ws,
        run_id=first.id,
        name="plan.md",
        type="plan",
        content="early plan",
    )
    _result(conn, _complete(conn, first), decision="ready", type="requirements")
    second_input = resolve(conn, ws, task_id=task.id)
    assert second_input.step_id == "decomposition"
    report = prepare(conn, ws, task_id=task.id)
    assert report.run_id == second_input.run_id
    (check,) = report.validation.checks
    assert check.type == "plan"
    assert check.satisfied is False
    assert report.validation.is_complete is False


def test_report_is_deterministic(conn, ws, workflows):
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)
    assert prepare(conn, ws, task_id=task.id) == prepare(conn, ws, task_id=task.id)


# --- read-only proof ----------------------------------------------------------


def test_successful_report_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="the requirements",
    )
    before = _snapshot(conn, task.id, run.id)
    prepare(conn, ws, task_id=task.id)
    assert _snapshot(conn, task.id, run.id) == before


def test_run_not_found_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    before_tasks = conn.execute("SELECT * FROM tasks").fetchall()
    before_runs = conn.execute("SELECT * FROM runs").fetchall()
    with pytest.raises(PrepareArtifactsError):
        prepare(conn, ws, task_id=task.id)
    assert conn.execute("SELECT * FROM tasks").fetchall() == before_tasks
    assert conn.execute("SELECT * FROM runs").fetchall() == before_runs


def test_run_not_active_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _result(conn, _complete(conn, run), decision="ready", type="requirements")
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(PrepareArtifactsError):
        prepare(conn, ws, task_id=task.id)
    assert _snapshot(conn, task.id, run.id) == before


def test_step_unresolved_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-manual", step_id=None)
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(PrepareArtifactsError):
        prepare(conn, ws, task_id=task.id)
    assert _snapshot(conn, task.id, run.id) == before


def test_task_status_is_not_checked(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    updated = replace(task, status=TaskStatus.WAITING_FOR_HUMAN)
    with conn:
        store.update_task(conn, updated)
    report = prepare(conn, ws, task_id=task.id)
    assert report.run_id == run_input.run_id
