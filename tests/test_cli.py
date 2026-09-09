import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import __version__, store, workspace
from skillflow.artifacts import create_artifact
from skillflow.cli import (
    format_artifact_report,
    format_completion,
    format_run_input,
    main,
)
from skillflow.complete_run import RunCompletion
from skillflow.context import ContextSelection
from skillflow.domain import (
    Artifact,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import EvaluationOutput
from skillflow.outputs import OutputCheck, OutputValidation
from skillflow.prepare_artifacts import ArtifactReport
from skillflow.run_input import RunInput, resolve_run_input
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType, ExpectedOutput, WorkflowStep
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


# --- prepare-artifacts ------------------------------------------------------


def _resolve_cli_task(cli_conn, **kwargs):
    task = _seed_task(cli_conn, workflow_id="software-change", **kwargs)
    assert main(["resolve-task", task.id]) == 0
    runs = store.list_runs_for_task(cli_conn, task.id)
    assert len(runs) == 1
    return task, runs[0]


def test_prepare_artifacts_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["prepare-artifacts"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err


def test_prepare_artifacts_reports_missing_outputs(cli_conn, capsys):
    task, run = _resolve_cli_task(cli_conn)

    assert main(["prepare-artifacts"]) == 0

    out = capsys.readouterr().out
    assert f"Run {run.id} (running)" in out
    assert "step 'requirements'" in out
    assert f"of task {task.id}" in out
    assert "Expected outputs:" in out
    assert "✗ requirements (required)" in out
    assert "Please create the missing artifact:" in out
    assert "- type: requirements" in out
    assert "required: true" in out
    assert "/skillflow:complete-run" in out


def test_prepare_artifacts_missing_outputs_still_exit_zero(cli_conn, capsys):
    # Missing required artifacts are the normal, expected state of this
    # command -- not a rejection. Pinned explicitly so a later change does
    # not "fix" the exit code to 1.
    _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["prepare-artifacts"]) == 0

    captured = capsys.readouterr()
    assert "✗ requirements" in captured.out
    assert captured.err == ""


def test_prepare_artifacts_all_ready_after_registration(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    create_artifact(
        cli_conn,
        cli_ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="the requirements",
    )
    capsys.readouterr()

    assert main(["prepare-artifacts"]) == 0

    out = capsys.readouterr().out
    assert "✓ requirements (requirements.md v1)" in out
    assert "All required artifacts are already prepared." in out
    assert "/skillflow:complete-run" in out


def test_prepare_artifacts_task_selects_the_named_task(cli_conn, capsys):
    task_a, run_a = _resolve_cli_task(cli_conn, title="First")
    task_b, run_b = _resolve_cli_task(cli_conn, title="Second")
    capsys.readouterr()

    assert main(["prepare-artifacts", "--task", task_b.id]) == 0

    out = capsys.readouterr().out
    assert run_b.id in out
    assert run_a.id not in out
    assert task_a.id not in out


def test_prepare_artifacts_two_running_runs_reject_without_task(cli_conn, capsys):
    _resolve_cli_task(cli_conn, title="First")
    _resolve_cli_task(cli_conn, title="Second")
    capsys.readouterr()

    assert main(["prepare-artifacts"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--task" in captured.err


def test_prepare_artifacts_unknown_task_exits_one(cli_conn, capsys):
    assert main(["prepare-artifacts", "--task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "task-nope" in captured.err


# --- format_artifact_report -------------------------------------------------


def _report_artifact(*, name="plan.md", type="plan", version=1):
    now = datetime.now(UTC)
    return Artifact(
        id=f"artifact-{name}-v{version}",
        task_id="task-1",
        run_id="run-1",
        name=name,
        type=type,
        version=version,
        path=f"task-1/{name}",
        created_at=now,
    )


def _report(**over):
    kwargs = dict(
        task_id="task-1",
        run_id="run-1",
        step_id="s",
        validation=OutputValidation(checks=()),
    )
    kwargs.update(over)
    return ArtifactReport(**kwargs)


def test_format_report_all_satisfied():
    artifact = _report_artifact()
    report = _report(
        validation=OutputValidation(
            checks=(OutputCheck(type="plan", required=True, artifacts=(artifact,)),)
        )
    )
    rendered = format_artifact_report(report)

    assert "Run run-1 (running) -- step 's' of task task-1" in rendered
    assert "✓ plan (plan.md v1)" in rendered
    assert "All required artifacts are already prepared." in rendered
    assert "/skillflow:complete-run" in rendered


def test_format_report_lists_every_version_of_one_output():
    report = _report(
        validation=OutputValidation(
            checks=(
                OutputCheck(
                    type="plan",
                    required=True,
                    artifacts=(
                        _report_artifact(version=1),
                        _report_artifact(version=2),
                    ),
                ),
            )
        )
    )
    rendered = format_artifact_report(report)

    assert "✓ plan (plan.md v1, plan.md v2)" in rendered


def test_format_report_single_required_missing():
    report = _report(
        validation=OutputValidation(checks=(OutputCheck(type="review", required=True),))
    )
    rendered = format_artifact_report(report)

    assert "✗ review (required)" in rendered
    assert "Please create the missing artifact:" in rendered
    assert "- type: review" in rendered
    assert "required: true" in rendered
    assert "All required artifacts" not in rendered


def test_format_report_mixed_required_and_optional_missing():
    report = _report(
        validation=OutputValidation(
            checks=(
                OutputCheck(type="review", required=True),
                OutputCheck(type="notes", required=False),
            )
        )
    )
    rendered = format_artifact_report(report)

    assert "✗ review (required)" in rendered
    assert "✗ notes (optional)" in rendered
    assert "- type: review" in rendered
    assert "- type: notes" not in rendered


def test_format_report_pluralises_the_missing_heading():
    report = _report(
        validation=OutputValidation(
            checks=(
                OutputCheck(type="a", required=True),
                OutputCheck(type="b", required=True),
            )
        )
    )
    rendered = format_artifact_report(report)

    assert "Please create the missing artifacts:" in rendered


def test_format_report_no_declared_outputs():
    rendered = format_artifact_report(_report())

    assert "Expected outputs: none declared" in rendered
    assert "All required artifacts are already prepared." in rendered
    assert "/skillflow:complete-run" in rendered


def test_format_report_optional_only_missing_stays_complete():
    report = _report(
        validation=OutputValidation(checks=(OutputCheck(type="notes", required=False),))
    )
    rendered = format_artifact_report(report)

    assert "Expected outputs:" in rendered
    assert "Required artifacts:" not in rendered
    assert "✗ notes (optional)" in rendered
    assert "All required artifacts are already prepared." in rendered
    assert "/skillflow:complete-run" in rendered
    assert "Please create the missing artifact" not in rendered


# --- complete-run -----------------------------------------------------------


def _write_artifact_file(cli_ws, name, content=None):
    cli_ws.root.joinpath(name).write_text(
        f"# {name}" if content is None else content, encoding="utf-8"
    )


def _complete_cli(cli_conn, cli_ws, *, outcome=None, artifacts=()):
    """Complete the current Run through ``main``; return the exit code."""
    args = ["complete-run"]
    if outcome is not None:
        args += ["--outcome", outcome]
    for name, type_ in artifacts:
        _write_artifact_file(cli_ws, name)
        args += ["--artifact", f"{name}:{type_}:{name}"]
    return main(args)


def _drive_cli_to_review(cli_conn, cli_ws, task):
    """Resolve + complete requirements/decomposition/implementation via CLI."""
    steps = (("requirements.md", "requirements"), ("plan.md", "plan"), ())
    for sub in steps:
        assert main(["resolve-task", task.id]) == 0
        subs = (sub,) if sub else ()
        assert _complete_cli(cli_conn, cli_ws, outcome="ready", artifacts=subs) == 0
    assert main(["resolve-task", task.id]) == 0
    return store.list_runs_for_task(cli_conn, task.id)[-1]


def test_complete_run_vertical_slice(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    assert main(["prepare-artifacts"]) == 0
    capsys.readouterr()

    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="ready",
            artifacts=(("requirements.md", "requirements"),),
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert f"Run {run.id} completed." in out
    assert "Next action:" in out
    assert "Run decomposition." in out
    assert f"/skillflow:resolve-task {task.id}" in out
    done = store.get_run(cli_conn, run.id)
    assert done.status is RunStatus.COMPLETED
    assert done.completed_at is not None
    result = store.get_result_for_run(cli_conn, run.id)
    assert result is not None
    assert result.status is ResultStatus.COMPLETED
    assert (result.outcome.type, result.outcome.decision) == (
        "requirements",
        "ready",
    )
    registered = store.list_artifacts_for_run(cli_conn, run.id)
    assert [(a.name, a.type, a.version) for a in registered] == [
        ("requirements.md", "requirements", 1)
    ]
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    assert len(store.list_runs_for_task(cli_conn, task.id)) == 1


def test_complete_run_review_approved_completes_task(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    review = _drive_cli_to_review(cli_conn, cli_ws, task)
    capsys.readouterr()

    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="approved",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )

    out = capsys.readouterr().out
    assert f"Run {review.id} completed." in out
    assert "Task status:" in out
    assert "completed" in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.COMPLETED
    assert main(["resolve-task", task.id]) == 1


def test_complete_run_changes_requested_points_at_implementation(
    cli_conn, cli_ws, capsys
):
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    capsys.readouterr()

    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="changes_requested",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "Run implementation." in out
    assert f"/skillflow:resolve-task {task.id}" in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    # The reported pointer works: the next Run starts in a new resolution.
    assert main(["resolve-task", task.id]) == 0


def test_complete_run_human_required_parks_task(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    capsys.readouterr()

    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="human_required",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "Human decision required." in out
    assert "/skillflow:decide <decision>" in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    capsys.readouterr()
    assert main(["resolve-task", task.id]) == 1
    assert "waiting_for_human" in capsys.readouterr().err


def test_complete_run_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err


def test_complete_run_two_running_runs_reject_without_task(cli_conn, capsys):
    task_a, _ = _resolve_cli_task(cli_conn, title="First")
    task_b, _ = _resolve_cli_task(cli_conn, title="Second")
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert task_a.id in captured.err
    assert task_b.id in captured.err
    assert "--task" in captured.err


def test_complete_run_task_selects_the_named_task(cli_conn, cli_ws, capsys):
    _, run_a = _resolve_cli_task(cli_conn, title="First")
    task_b, run_b = _resolve_cli_task(cli_conn, title="Second")
    _write_artifact_file(cli_ws, "requirements.md")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--task",
                task_b.id,
                "--outcome",
                "ready",
                "--artifact",
                "requirements.md:requirements:requirements.md",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert run_b.id in out
    assert run_a.id not in out
    assert store.get_run(cli_conn, run_a.id).status is RunStatus.RUNNING
    assert store.get_run(cli_conn, run_b.id).status is RunStatus.COMPLETED


def test_complete_run_unknown_task_exits_one(cli_conn, capsys):
    assert main(["complete-run", "--task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "task-nope" in captured.err


def test_complete_run_task_without_runs_exits_one(cli_conn, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")

    assert main(["complete-run", "--task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "has no Runs" in captured.err


def test_complete_run_task_without_running_run_exits_one(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="ready",
            artifacts=(("requirements.md", "requirements"),),
        )
        == 0
    )
    result_before = store.get_result_for_run(cli_conn, run.id)
    capsys.readouterr()

    assert main(["complete-run", "--task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert run.id in captured.err
    assert "completed" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.COMPLETED
    assert store.get_result_for_run(cli_conn, run.id) == result_before


def test_complete_run_missing_artifact_leaves_run_running(cli_conn, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requirements" in captured.err
    assert "/skillflow:prepare-artifacts" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert store.list_artifacts_for_run(cli_conn, run.id) == []
    assert len(store.list_runs_for_task(cli_conn, task.id)) == 1


def test_complete_run_registered_output_needs_no_resubmission(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    create_artifact(
        cli_conn,
        cli_ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="the requirements",
    )
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 0

    out = capsys.readouterr().out
    assert f"Run {run.id} completed." in out
    assert "Run decomposition." in out


def test_complete_run_missing_outcome_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "requirements.md")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--artifact",
                "requirements.md:requirements:requirements.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requires a lifecycle outcome" in captured.err
    assert "'ready'" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert store.list_artifacts_for_run(cli_conn, run.id) == []


def test_complete_run_invalid_outcome_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "requirements.md")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "maybe",
                "--artifact",
                "requirements.md:requirements:requirements.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "has no outcome 'maybe'" in captured.err
    assert "accepted: ['ready']" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert store.list_artifacts_for_run(cli_conn, run.id) == []


def test_complete_run_blank_outcome_rejected(cli_conn, capsys):
    _, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "  "]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "empty --outcome" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_complete_run_second_completion_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="ready",
            artifacts=(("requirements.md", "requirements"),),
        )
        == 0
    )
    result_before = store.get_result_for_run(cli_conn, run.id)
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.COMPLETED
    assert store.get_result_for_run(cli_conn, run.id) == result_before


def test_complete_run_missing_file_rejected(cli_conn, capsys):
    _, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "requirements.md:requirements:does-not-exist.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does-not-exist.md" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_complete_run_directory_as_file_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    cli_ws.root.joinpath("subdir").mkdir()
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "requirements.md:requirements:subdir",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "subdir" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_complete_run_non_utf8_file_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    cli_ws.root.joinpath("blob.md").write_bytes(b"\xff\xfe\x00bad")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "requirements.md:requirements:blob.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid UTF-8" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


@pytest.mark.parametrize(
    "spec", ["justname", "name:type", "name:type:", ":type:path", "name::path", ""]
)
def test_complete_run_malformed_spec_rejected(cli_conn, capsys, spec):
    _, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["complete-run", "--artifact", spec]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "NAME:TYPE:PATH" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_complete_run_artifact_spec_tolerates_surrounding_whitespace(
    cli_conn, cli_ws, capsys
):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "requirements.md")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "  requirements.md : requirements : requirements.md  ",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert f"Run {run.id} completed." in out
    assert store.get_run(cli_conn, run.id).status is RunStatus.COMPLETED


def test_complete_run_artifact_accepts_absolute_path(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "requirements.md")
    absolute = str(cli_ws.root.joinpath("requirements.md"))
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                f"requirements.md:requirements:{absolute}",
            ]
        )
        == 0
    )

    assert store.get_run(cli_conn, run.id).status is RunStatus.COMPLETED


def test_complete_run_extra_colon_belongs_to_path(cli_conn, cli_ws, capsys):
    # maxsplit=2: "requirements.md:extra" is the PATH, so this is a file
    # error naming that path -- not a malformed spec.
    _, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "requirements.md:requirements:requirements.md:extra",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requirements.md:extra" in captured.err
    assert "NAME:TYPE:PATH" not in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_complete_run_traversal_name_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "evil.md")
    files_before = sorted(p for p in cli_ws.artifacts_dir.rglob("*") if p.is_file())
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "../escape.md:requirements:evil.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "plain filename" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert sorted(p for p in cli_ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )
    assert not cli_ws.root.joinpath("escape.md").exists()


def test_complete_run_duplicate_names_rejected(cli_conn, cli_ws, capsys):
    _, run = _resolve_cli_task(cli_conn)
    _write_artifact_file(cli_ws, "first.md")
    _write_artifact_file(cli_ws, "second.md")
    capsys.readouterr()

    assert (
        main(
            [
                "complete-run",
                "--outcome",
                "ready",
                "--artifact",
                "dup.md:requirements:first.md",
                "--artifact",
                "dup.md:requirements:second.md",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "duplicate" in captured.err
    assert "dup.md" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert store.list_artifacts_for_run(cli_conn, run.id) == []


def test_complete_run_contradicting_type_rejected(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    create_artifact(
        cli_conn,
        cli_ws,
        run_id=run.id,
        name="plan.md",
        type="plan",
        content="the plan",
    )
    _write_artifact_file(cli_ws, "plan-new.md")
    capsys.readouterr()

    # The CLI chain check fires before the service's coverage validation,
    # so no covering submission is needed to reach it.
    assert main(["complete-run", "--artifact", "plan.md:requirements:plan-new.md"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is of type 'plan'" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None
    assert len(store.list_artifacts_for_run(cli_conn, run.id)) == 1
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE


def _seed_skill_targeted_run(cli_conn, cli_ws):
    """Resolve + complete the first step, then open a skill-targeted Run."""
    task = _seed_task(cli_conn, workflow_id="software-change")
    assert main(["resolve-task", task.id]) == 0
    first = store.list_runs_for_task(cli_conn, task.id)[-1]
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="ready",
            artifacts=(("requirements.md", "requirements"),),
        )
        == 0
    )
    skill_run = create_run(
        cli_conn,
        task_id=task.id,
        action=EvaluationOutput(
            action=ActionType.RUN, reason="research", skill="research"
        ),
        triggered_by_run_id=first.id,
    )
    return task, skill_run


def test_complete_run_skill_targeted_run_completes(cli_conn, cli_ws, capsys):
    task, skill_run = _seed_skill_targeted_run(cli_conn, cli_ws)
    runs_before = len(store.list_runs_for_task(cli_conn, task.id))
    capsys.readouterr()

    assert main(["complete-run"]) == 0

    out = capsys.readouterr().out
    assert f"Run {skill_run.id} completed." in out
    assert "No lifecycle action applies" in out
    assert "active" in out
    assert store.get_run(cli_conn, skill_run.id).status is RunStatus.COMPLETED
    assert store.get_result_for_run(cli_conn, skill_run.id) is not None
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    assert len(store.list_runs_for_task(cli_conn, task.id)) == runs_before


def test_complete_run_skill_targeted_run_rejects_outcome(cli_conn, cli_ws, capsys):
    _, skill_run = _seed_skill_targeted_run(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not expected" in captured.err
    assert store.get_run(cli_conn, skill_run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, skill_run.id) is None


# --- format_completion ------------------------------------------------------


def _completion(status=TaskStatus.ACTIVE, action=None):
    now = datetime.now(UTC)
    task = Task(
        id="task-1",
        title="Ship it",
        description="",
        status=status,
        created_at=now,
        updated_at=now,
    )
    run = Run(
        id="run-1",
        task_id=task.id,
        status=RunStatus.COMPLETED,
        created_at=now,
        completed_at=now,
    )
    result = Result(
        id="result-1",
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=now,
    )
    return RunCompletion(run=run, result=result, task=task, action=action)


def test_format_completion_run_with_step():
    completion = _completion(
        action=EvaluationOutput(
            action=ActionType.RUN, reason="ready", step="decomposition"
        )
    )

    assert format_completion(completion) == (
        "Run run-1 completed.\n"
        "\n"
        "Next action:\n"
        "Run decomposition.\n"
        "\n"
        "Start the next Run in a new Claude Code session:\n"
        "\n"
        "/skillflow:resolve-task task-1"
    )


def test_format_completion_run_with_skill_has_no_resolve_pointer():
    completion = _completion(
        action=EvaluationOutput(
            action=ActionType.RUN,
            reason="fundamental_assumption_wrong",
            skill="research",
        )
    )

    rendered = format_completion(completion)

    assert rendered == (
        "Run run-1 completed.\n"
        "\n"
        "Next action:\n"
        "Run skill 'research' (reason: fundamental_assumption_wrong).\n"
        "\n"
        "Task status:\n"
        "active"
    )
    # resolve-task rejects skill-only actions, so the formatter must not
    # print a pointer that would fail.
    assert "/skillflow:resolve-task" not in rendered


def test_format_completion_human():
    completion = _completion(
        action=EvaluationOutput(action=ActionType.HUMAN, reason="human_required")
    )

    assert format_completion(completion) == (
        "Run run-1 completed.\n"
        "\n"
        "Next action:\n"
        "Human decision required.\n"
        "\n"
        "Use:\n"
        "\n"
        "/skillflow:decide <decision>"
    )


def test_format_completion_complete():
    completion = _completion(
        status=TaskStatus.COMPLETED,
        action=EvaluationOutput(action=ActionType.COMPLETE, reason="approved"),
    )

    assert format_completion(completion) == (
        "Run run-1 completed.\n\nTask status:\ncompleted"
    )


def test_format_completion_cancel():
    completion = _completion(
        status=TaskStatus.CANCELLED,
        action=EvaluationOutput(action=ActionType.CANCEL, reason="abort"),
    )

    assert format_completion(completion) == (
        "Run run-1 completed.\n\nTask status:\ncancelled"
    )


def test_format_completion_without_action():
    completion = _completion(action=None)

    assert format_completion(completion) == (
        "Run run-1 completed.\n"
        "\n"
        "No lifecycle action applies to this Run (no workflow step).\n"
        "\n"
        "Task status:\n"
        "active"
    )
