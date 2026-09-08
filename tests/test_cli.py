import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import __version__, store, workspace
from skillflow.cli import format_run_input, main
from skillflow.context import ContextSelection
from skillflow.domain import Run, RunStatus, Task, TaskStatus
from skillflow.run_input import RunInput, resolve_run_input
from skillflow.service import create_task, register_workflow
from skillflow.workflow import ExpectedOutput, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"


def test_version_exits_zero_and_prints_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert f"skillflow {__version__}" in capsys.readouterr().out


def test_no_args_returns_zero_and_prints_usage(capsys):
    assert main([]) == 0
    assert "usage: skillflow" in capsys.readouterr().out


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as exc_info:
        main(["definitely-not-a-command"])
    assert exc_info.value.code == 2


# --- resolve-task ---------------------------------------------------------


@pytest.fixture
def cli_ws(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    ws = workspace.init_workspace(tmp_path)
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "software-change.yaml").write_text(
        REFERENCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return ws


@pytest.fixture
def cli_conn(cli_ws):
    with contextlib.closing(store.open_store(cli_ws)) as connection:
        yield connection


def _seed_task(cli_conn, *, title="Ship it", description="", workflow_id=None):
    if workflow_id is not None:
        register_workflow(cli_conn, load_workflow(REFERENCE))
    return create_task(
        cli_conn, title=title, description=description,
        workflow_definition_id=workflow_id,
    )


def test_resolve_task_without_id_exits_two():
    with pytest.raises(SystemExit) as exc_info:
        main(["resolve-task"])
    assert exc_info.value.code == 2


def test_resolve_task_success_prints_run_input(cli_conn, capsys):
    task = _seed_task(
        cli_conn,
        workflow_id="software-change",
        description="Users need CSV export from the reports page.",
    )

    assert main(["resolve-task", task.id]) == 0

    out = capsys.readouterr().out
    runs = store.list_runs_for_task(cli_conn, task.id)
    assert len(runs) == 1
    assert task.id in out
    assert runs[0].id in out
    assert "Users need CSV export from the reports page." in out
    assert "requirements" in out
    assert "requirements-analysis" in out
    assert "requirements (required)" in out
    assert "/skillflow:prepare-artifacts" in out


def test_resolve_task_with_workflow_assigns_and_resolves(cli_conn, capsys):
    task = _seed_task(cli_conn)

    assert main(["resolve-task", task.id, "--workflow", "software-change"]) == 0

    assert "requirements" in capsys.readouterr().out
    assert (
        store.get_task(cli_conn, task.id).workflow_definition_id == "software-change"
    )


def test_resolve_task_unknown_task_exits_one_with_stderr_only(cli_conn, capsys):
    assert main(["resolve-task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no task" in captured.err


def test_resolve_task_selection_error_lists_definitions(cli_conn, capsys):
    task = _seed_task(cli_conn)

    assert main(["resolve-task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "software-change" in captured.err


# --- format_run_input -----------------------------------------------------


def _task_and_run(*, step_id="implementation"):
    now = datetime.now(UTC)
    task = Task(
        id="task-1",
        title="Ship it",
        description="Do the thing.",
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
        workflow_definition_id="software-change",
        step_id=step_id,
        trigger_reason="ready",
        triggered_by_run_id="run-0",
    )
    return task, run


def test_format_renders_unresolved_context_as_informational():
    task, run = _task_and_run()
    step = load_workflow(REFERENCE).find_step("implementation")
    assert step is not None
    rendered = format_run_input(
        resolve_run_input(task=task, run=run, step=step, artifacts=[])
    )

    assert "Description: Do the thing." in rendered
    assert "Context: none selected" in rendered
    assert "Unresolved context types: requirements, plan, review" in rendered


def test_format_omits_empty_description():
    now = datetime.now(UTC)
    task = Task(
        id="task-1",
        title="Ship it",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    _, run = _task_and_run()
    run = Run(
        id=run.id,
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        step_id="s",
        trigger_reason="initial",
    )
    step = WorkflowStep(id="s", skill="do-it")
    rendered = format_run_input(
        resolve_run_input(task=task, run=run, step=step, artifacts=[])
    )

    assert "Description:" not in rendered


def test_format_marks_optional_outputs():
    task, run = _task_and_run(step_id="s")
    step = WorkflowStep(
        id="s",
        skill="do-it",
        outputs=(
            ExpectedOutput(type="a", required=True),
            ExpectedOutput(type="b", required=False),
        ),
    )
    rendered = format_run_input(
        RunInput(
            task_id=task.id,
            task_title=task.title,
            task_description=task.description,
            run_id=run.id,
            step_id=step.id,
            skill=step.skill,
            outputs=step.outputs,
            context=ContextSelection(),
        )
    )

    assert "a (required)" in rendered
    assert "b (optional)" in rendered


def test_format_omits_absent_model_and_effort():
    task, run = _task_and_run(step_id="s")
    step = WorkflowStep(id="s", skill="do-it")
    rendered = format_run_input(
        resolve_run_input(task=task, run=run, step=step, artifacts=[])
    )

    assert "Execution:" not in rendered
    assert "Expected outputs: none declared" in rendered
