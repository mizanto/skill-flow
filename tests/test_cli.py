import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

import skillflow
from skillflow import __version__, store, workspace
from skillflow.artifacts import create_artifact
from skillflow.cli import (
    _bundled_workflows_dir,
    format_artifact_report,
    format_completion,
    format_decision,
    format_error,
    format_failure,
    format_run_input,
    main,
)
from skillflow.complete_run import RunCompletion
from skillflow.context import ContextSelection
from skillflow.decide import DecisionRecord
from skillflow.domain import (
    Artifact,
    HumanDecision,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import (
    EvaluationError,
    EvaluationOutput,
    WorkflowSelectionRequiredError,
    resolve_initial_action,
)
from skillflow.fail_run import RunFailure
from skillflow.outputs import OutputCheck, OutputValidation
from skillflow.prepare_artifacts import ArtifactReport
from skillflow.resolve_task import ResolveTaskError
from skillflow.run_input import (
    RunInput,
    resolve_run_input,
    resolve_skill_run_input,
)
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType, ExpectedOutput, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "workflows"
    / "runtime-reference"
    / "software-change.yaml"
)


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
        cli_conn,
        title=title,
        description=description,
        workflow_definition_id=workflow_id,
    )


def test_resolve_task_without_id_resolves_the_single_task(cli_conn, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")

    assert main(["resolve-task"]) == 0

    captured = capsys.readouterr()
    assert task.id in captured.out
    assert captured.err == ""
    assert len(store.list_runs_for_task(cli_conn, task.id)) == 1


def test_resolve_task_without_id_and_no_task_exits_one(cli_conn, capsys):
    assert main(["resolve-task"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow resolve-task: TaskNotFound: ")
    assert "skillflow start" in captured.err
    assert lines[-1] == "No Run was created."


def test_resolve_task_without_id_and_several_tasks_is_ambiguous(cli_conn, capsys):
    first = _seed_task(cli_conn, title="First", workflow_id="software-change")
    second = _seed_task(cli_conn, title="Second", workflow_id="software-change")

    assert main(["resolve-task"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("skillflow resolve-task: AmbiguousCurrentTask: ")
    assert first.id in captured.err
    assert second.id in captured.err
    assert cli_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_resolve_task_rejects_while_another_tasks_run_is_running(cli_conn, capsys):
    task_a, run_a = _resolve_cli_task(cli_conn, title="First")
    task_b = _seed_task(cli_conn, title="Second", workflow_id="software-change")
    capsys.readouterr()

    assert main(["resolve-task", task_b.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("skillflow resolve-task: ActiveRunExists: ")
    assert task_a.id in captured.err
    assert run_a.id in captured.err
    assert store.list_runs_for_task(cli_conn, task_b.id) == []


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
    assert "Workflow: software-change" in out
    assert "requirements (required)" in out
    assert "/skillflow:prepare-artifacts" in out


def test_resolve_task_with_workflow_assigns_and_resolves(cli_conn, capsys):
    task = _seed_task(cli_conn)

    assert main(["resolve-task", task.id, "--workflow", "software-change"]) == 0

    assert "requirements" in capsys.readouterr().out
    assert store.get_task(cli_conn, task.id).workflow_definition_id == "software-change"


def test_resolve_task_unknown_task_exits_one_with_stderr_only(cli_conn, capsys):
    assert main(["resolve-task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no task" in captured.err
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow resolve-task: TaskNotFound: ")
    assert lines[-1] == "No Run was created."
    assert store.list_runs_for_task(cli_conn, "task-nope") == []


def test_resolve_task_selection_error_lists_definitions(cli_conn, capsys):
    task = _seed_task(cli_conn)

    assert main(["resolve-task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "software-change" in captured.err


def test_resolve_task_workflow_reassignment_suggests_omitting_workflow(
    cli_conn, cli_ws, capsys
):
    task = _seed_task(cli_conn, workflow_id="software-change")
    (cli_ws.workflows_dir / "other.yaml").write_text(
        "name: other\nsteps:\n  - id: only\n    skill: do-it\n", encoding="utf-8"
    )

    assert main(["resolve-task", task.id, "--workflow", "other"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skillflow resolve-task: WorkflowAssignmentError: " in captured.err
    assert "re-run without `--workflow`" in captured.err
    assert captured.err.splitlines()[-1] == "No Run was created."
    assert store.get_task(cli_conn, task.id).workflow_definition_id == "software-change"
    assert store.list_runs_for_task(cli_conn, task.id) == []


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


def test_format_renders_unresolved_context_as_informational(tmp_path):
    task, run = _task_and_run()
    step = load_workflow(REFERENCE).find_step("implementation")
    assert step is not None
    rendered = format_run_input(
        resolve_run_input(task=task, run=run, step=step, artifacts=[]),
        workspace.Workspace(root=tmp_path),
        "software-change",
    )

    assert "Description: Do the thing." in rendered
    assert "Workflow: software-change" in rendered
    assert "Context: none selected" in rendered
    assert "Unresolved context types: requirements, plan, review" in rendered


def test_format_omits_empty_description(tmp_path):
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
        resolve_run_input(task=task, run=run, step=step, artifacts=[]),
        workspace.Workspace(root=tmp_path),
        "software-change",
    )

    assert "Description:" not in rendered


def test_format_marks_optional_outputs(tmp_path):
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
        ),
        workspace.Workspace(root=tmp_path),
        "software-change",
    )

    assert "a (required)" in rendered
    assert "b (optional)" in rendered


def test_format_omits_absent_model_and_effort(tmp_path):
    task, run = _task_and_run(step_id="s")
    step = WorkflowStep(id="s", skill="do-it")
    rendered = format_run_input(
        resolve_run_input(task=task, run=run, step=step, artifacts=[]),
        workspace.Workspace(root=tmp_path),
        "software-change",
    )

    assert "Execution:" not in rendered
    assert "Expected outputs: none declared" in rendered


def test_format_renders_skill_run_without_a_step_line(tmp_path):
    task, run = _task_and_run(step_id=None)
    rendered = format_run_input(
        resolve_skill_run_input(task=task, run=run, skill="research", artifacts=[]),
        workspace.Workspace(root=tmp_path),
        "software-change",
    )

    assert "Run run-1 (running) -- skill 'research' (no workflow step)" in rendered
    assert "-- step" not in rendered
    assert "via skill" not in rendered
    assert "Expected outputs: none declared" in rendered
    assert "`/skillflow:complete-run`" in rendered
    assert "/skillflow:prepare-artifacts" not in rendered


# --- prepare-artifacts ------------------------------------------------------


def _resolve_cli_task(cli_conn, **kwargs):
    task = _seed_task(cli_conn, workflow_id="software-change", **kwargs)
    assert main(["resolve-task", task.id]) == 0
    runs = store.list_runs_for_task(cli_conn, task.id)
    assert len(runs) == 1
    return task, runs[0]


def _seed_parallel_running_run(cli_conn, **kwargs):
    # A second running Run is unreachable through resolve-task since SF-43
    # (one running Run per workspace); seed it through the service so the
    # AmbiguousCurrentRun / --task paths (pre-SF-43 workspaces, the accepted
    # concurrent race) stay covered.
    task = _seed_task(cli_conn, workflow_id="software-change", **kwargs)
    run = create_run(
        cli_conn,
        task_id=task.id,
        action=resolve_initial_action(task, load_workflow(REFERENCE)),
    )
    return task, run


def test_prepare_artifacts_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["prepare-artifacts"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow prepare-artifacts: RunNotFound: ")
    assert lines[-1] == "Nothing was changed: this command only inspects state."


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
    task_b, run_b = _seed_parallel_running_run(cli_conn, title="Second")
    capsys.readouterr()

    assert main(["prepare-artifacts", "--task", task_b.id]) == 0

    out = capsys.readouterr().out
    assert run_b.id in out
    assert run_a.id not in out
    assert task_a.id not in out


def test_prepare_artifacts_two_running_runs_reject_without_task(cli_conn, capsys):
    _resolve_cli_task(cli_conn, title="First")
    _seed_parallel_running_run(cli_conn, title="Second")
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
    assert "Next: /skillflow:work" in out
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
    assert "Next: /skillflow:work" in out
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


def test_complete_run_skill_targeting_skill_exits_one(cli_conn, cli_ws, capsys):
    # Approver decision 5, through the real CLI: research reporting
    # fundamental_assumption_wrong is rejected at evaluation, and the CLI
    # maps EvaluationError to exit 1 with stderr only.
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="fundamental_assumption_wrong",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )
    assert main(["resolve-task", task.id]) == 0
    skill_run = store.list_runs_for_task(cli_conn, task.id)[-1]
    assert skill_run.step_id is None
    capsys.readouterr()

    assert _complete_cli(cli_conn, cli_ws, outcome="fundamental_assumption_wrong") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must not target another skill" in captured.err
    assert "skillflow complete-run: EvaluationError: " in captured.err
    assert "fix the rule in the definition file" in captured.err
    assert captured.err.splitlines()[-1] == (
        "No Result was created; no Run status was changed."
    )
    assert store.get_run(cli_conn, skill_run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, skill_run.id) is None


def test_complete_run_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err


def test_complete_run_two_running_runs_reject_without_task(cli_conn, capsys):
    task_a, _ = _resolve_cli_task(cli_conn, title="First")
    task_b, _ = _seed_parallel_running_run(cli_conn, title="Second")
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert task_a.id in captured.err
    assert task_b.id in captured.err
    assert "--task" in captured.err


def test_complete_run_task_selects_the_named_task(cli_conn, cli_ws, capsys):
    _, run_a = _resolve_cli_task(cli_conn, title="First")
    task_b, run_b = _seed_parallel_running_run(cli_conn, title="Second")
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
    assert "skillflow prepare-artifacts" in captured.err
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow complete-run: RequiredArtifactsMissing: ")
    assert lines[-1] == "No Result was created; no Run status was changed."
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


def test_complete_run_skill_targeted_run_reports_trigger_step_outcome(
    cli_conn, cli_ws, capsys
):
    # SF-32: the decision validates against the triggering step's table --
    # here `ready` on requirements -- and evaluates like a step Run's.
    task, skill_run = _seed_skill_targeted_run(cli_conn, cli_ws)
    runs_before = len(store.list_runs_for_task(cli_conn, task.id))
    capsys.readouterr()

    assert main(["complete-run", "--outcome", "ready"]) == 0

    out = capsys.readouterr().out
    assert f"Run {skill_run.id} completed." in out
    assert "Run decomposition." in out
    assert "Next: /skillflow:work" in out
    assert store.get_run(cli_conn, skill_run.id).status is RunStatus.COMPLETED
    result = store.get_result_for_run(cli_conn, skill_run.id)
    assert (result.outcome.type, result.outcome.decision) == (
        "requirements",
        "ready",
    )
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    assert len(store.list_runs_for_task(cli_conn, task.id)) == runs_before


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
        "Next: /skillflow:work"
    )


def test_format_completion_run_with_skill_points_at_work():
    # SF-32 flips the SF-29 rationale: skill-targeted Runs resolve, so the
    # pointer is printed. SF-53 points the continuation at the driver.
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
        "Next: /skillflow:work"
    )


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


# --- decide -----------------------------------------------------------------


def _park_cli_task(cli_conn, cli_ws):
    """Seed a Task and park it via the real CLI flow; return the Task."""
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="human_required",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )
    assert store.get_task(cli_conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    return task


def test_decide_without_decision_exits_two():
    with pytest.raises(SystemExit) as exc_info:
        main(["decide"])
    assert exc_info.value.code == 2


def test_decide_request_changes_points_at_implementation(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "request_changes"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert "Human decision recorded: request_changes." in out
    assert "Next action:" in out
    assert "Run implementation." in out
    assert "Next: /skillflow:work" in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    (decision,) = store.list_human_decisions_for_task(cli_conn, task.id)
    assert decision.decision == "request_changes"
    assert decision.comment is None
    # The reported pointer works: the next Run starts in a new resolution.
    assert main(["resolve-task", task.id]) == 0


def test_decide_approve_completes_task(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "approve"]) == 0

    out = capsys.readouterr().out
    assert "Human decision recorded: approve." in out
    assert "Task completed." in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.COMPLETED


def test_decide_cancel_cancels_task(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "cancel"]) == 0

    out = capsys.readouterr().out
    assert "Human decision recorded: cancel." in out
    assert "Task cancelled." in out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.CANCELLED


def test_decide_comment_is_stored(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "request_changes", "--comment", "Need more tests"]) == 0

    assert "Human decision recorded: request_changes." in capsys.readouterr().out
    (decision,) = store.list_human_decisions_for_task(cli_conn, task.id)
    assert decision.comment == "Need more tests"


def test_decide_task_selects_the_named_task(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    other = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "approve", "--task", task.id]) == 0

    assert "Human decision recorded: approve." in capsys.readouterr().out
    assert store.get_task(cli_conn, task.id).status is TaskStatus.COMPLETED
    assert store.get_task(cli_conn, other.id).status is TaskStatus.WAITING_FOR_HUMAN
    assert store.list_human_decisions_for_task(cli_conn, other.id) == []


def test_decide_two_waiting_without_task_exits_one(cli_conn, cli_ws, capsys):
    first = _park_cli_task(cli_conn, cli_ws)
    second = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "approve"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert first.id in captured.err
    assert second.id in captured.err
    assert store.get_task(cli_conn, first.id).status is TaskStatus.WAITING_FOR_HUMAN
    assert store.get_task(cli_conn, second.id).status is TaskStatus.WAITING_FOR_HUMAN


def test_decide_no_waiting_task_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["decide", "approve"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "waiting_for_human" in captured.err


def test_decide_unknown_task_exits_one(cli_conn, capsys):
    assert main(["decide", "approve", "--task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "task-nope" in captured.err


def test_decide_active_task_exits_one(cli_conn, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")

    assert main(["decide", "approve", "--task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "active" in captured.err
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow decide: HumanDecisionNotExpected: ")
    assert lines[-1] == "No decision was recorded; no Task status was changed."
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    assert store.list_human_decisions_for_task(cli_conn, task.id) == []


def test_decide_invalid_decision_exits_one(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "maybe"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "maybe" in captured.err
    assert store.get_task(cli_conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    assert store.list_human_decisions_for_task(cli_conn, task.id) == []


def test_decide_blank_decision_exits_one(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    capsys.readouterr()

    assert main(["decide", "  "]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "empty decision" in captured.err
    assert store.get_task(cli_conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    assert store.list_human_decisions_for_task(cli_conn, task.id) == []


def test_decide_completed_task_exits_one(cli_conn, cli_ws, capsys):
    task = _park_cli_task(cli_conn, cli_ws)
    assert main(["decide", "approve"]) == 0
    capsys.readouterr()

    assert main(["decide", "cancel", "--task", task.id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already 'completed'" in captured.err
    assert len(store.list_human_decisions_for_task(cli_conn, task.id)) == 1


# --- format_decision ----------------------------------------------------------


def _record(status=TaskStatus.ACTIVE, action=None, decision="request_changes"):
    now = datetime.now(UTC)
    return DecisionRecord(
        decision=HumanDecision(
            id="decision-1",
            task_id="task-1",
            run_id="run-1",
            decision=decision,
            created_at=now,
        ),
        task=Task(
            id="task-1",
            title="Ship it",
            description="",
            status=status,
            created_at=now,
            updated_at=now,
        ),
        action=action,
    )


def test_format_decision_run_with_step():
    record = _record(
        action=EvaluationOutput(
            action=ActionType.RUN, reason="request_changes", step="implementation"
        )
    )

    assert format_decision(record) == (
        "Human decision recorded: request_changes.\n"
        "\n"
        "Next action:\n"
        "Run implementation.\n"
        "\n"
        "Next: /skillflow:work"
    )


def test_format_decision_run_with_skill_points_at_work():
    record = _record(
        action=EvaluationOutput(
            action=ActionType.RUN, reason="research", skill="research"
        )
    )

    assert format_decision(record) == (
        "Human decision recorded: request_changes.\n"
        "\n"
        "Next action:\n"
        "Run skill 'research' (reason: research).\n"
        "\n"
        "Next: /skillflow:work"
    )


def test_format_decision_complete():
    record = _record(
        status=TaskStatus.COMPLETED,
        action=EvaluationOutput(action=ActionType.COMPLETE, reason="approve"),
        decision="approve",
    )

    assert format_decision(record) == (
        "Human decision recorded: approve.\n\nTask completed."
    )


def test_format_decision_cancel():
    record = _record(
        status=TaskStatus.CANCELLED,
        action=EvaluationOutput(action=ActionType.CANCEL, reason="cancel"),
        decision="cancel",
    )

    assert format_decision(record) == (
        "Human decision recorded: cancel.\n\nTask cancelled."
    )


def test_format_decision_human_action_raises():
    record = _record(
        status=TaskStatus.WAITING_FOR_HUMAN,
        action=EvaluationOutput(action=ActionType.HUMAN, reason="human_required"),
    )

    with pytest.raises(ValueError, match="must not itself produce"):
        format_decision(record)


# --- fail-run ---------------------------------------------------------------


def _fail_cli(cli_conn, cli_ws, *, message=None, diagnostics_file=None, artifacts=()):
    """Fail the current Run through ``main``; return the exit code."""
    args = ["fail-run"]
    if message is not None:
        args += ["--message", message]
    if diagnostics_file is not None:
        args += ["--diagnostics-file", diagnostics_file]
    for name, type_ in artifacts:
        _write_artifact_file(cli_ws, name)
        args += ["--artifact", f"{name}:{type_}:{name}"]
    return main(args)


def test_fail_run_vertical_slice(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert _fail_cli(cli_conn, cli_ws, message="boom: OOM") == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert f"Run {run.id} failed." in out
    assert "Task status:\nactive" in out
    assert f"Diagnostics: runs/{run.id}/output.log" in out
    assert "Next action:" in out
    assert "Run requirements." in out
    assert "Next: /skillflow:work" in out
    done = store.get_run(cli_conn, run.id)
    assert done.status is RunStatus.FAILED
    assert done.completed_at is not None
    result = store.get_result_for_run(cli_conn, run.id)
    assert result is not None
    assert result.status is ResultStatus.FAILED
    assert result.outcome is None
    log = cli_ws.run_dir(run.id) / "output.log"
    assert log.read_text(encoding="utf-8") == "boom: OOM"
    assert store.get_task(cli_conn, task.id).status is TaskStatus.ACTIVE
    assert len(store.list_runs_for_task(cli_conn, task.id)) == 1


def test_fail_run_with_diagnostics_file(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    cli_ws.root.joinpath("crash.txt").write_text("raw\noutput\n", encoding="utf-8")
    capsys.readouterr()

    assert _fail_cli(cli_conn, cli_ws, diagnostics_file="crash.txt") == 0

    assert f"Diagnostics: runs/{run.id}/output.log" in capsys.readouterr().out
    log = cli_ws.run_dir(run.id) / "output.log"
    assert log.read_text(encoding="utf-8") == "raw\noutput\n"
    assert store.get_run(cli_conn, run.id).status is RunStatus.FAILED


def test_fail_run_without_diagnostics_writes_no_file(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert _fail_cli(cli_conn, cli_ws) == 0

    out = capsys.readouterr().out
    assert f"Run {run.id} failed." in out
    assert "Diagnostics:" not in out
    assert not (cli_ws.runs_dir / run.id).exists()


def test_fail_run_with_artifact_registers_partial(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)

    assert (
        _fail_cli(
            cli_conn,
            cli_ws,
            message="boom",
            artifacts=(("notes.md", "notes"),),
        )
        == 0
    )

    registered = store.list_artifacts_for_run(cli_conn, run.id)
    assert [(a.name, a.type, a.version) for a in registered] == [
        ("notes.md", "notes", 1)
    ]


def test_fail_run_message_and_file_rejected(cli_conn, cli_ws):
    _resolve_cli_task(cli_conn)
    with pytest.raises(SystemExit) as exc_info:
        main(["fail-run", "--message", "x", "--diagnostics-file", "y"])
    assert exc_info.value.code == 2


def test_fail_run_blank_message_exits_one(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert _fail_cli(cli_conn, cli_ws, message="   ") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "empty --message" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_fail_run_missing_diagnostics_file_exits_one(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert _fail_cli(cli_conn, cli_ws, diagnostics_file="nope.txt") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read --diagnostics-file" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING


def test_fail_run_non_utf8_diagnostics_file_exits_one(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()
    cli_ws.root.joinpath("binary.txt").write_bytes(b"\xff\xfe\x00invalid")

    assert _fail_cli(cli_conn, cli_ws, diagnostics_file="binary.txt") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid UTF-8" in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING


def test_fail_run_bad_artifact_mentions_fail_run_rerun(cli_conn, cli_ws, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["fail-run", "--artifact", "malformed"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "malformed --artifact" in captured.err
    assert "skillflow fail-run" in captured.err
    assert "complete-run" not in captured.err
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING


def test_fail_run_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["fail-run", "--message", "boom"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "resolve-task" in captured.err


def test_fail_run_blank_message_reports_envelope(cli_conn, capsys):
    _, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["fail-run", "--message", "  "]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow fail-run: InvalidDiagnostics: ")
    assert lines[-1] == "No failure was recorded; no Run status was changed."
    assert store.get_run(cli_conn, run.id).status is RunStatus.RUNNING
    assert store.get_result_for_run(cli_conn, run.id) is None


def test_fail_run_task_selects_the_named_task(cli_conn, cli_ws, capsys):
    task_a, run_a = _resolve_cli_task(cli_conn, title="First")
    task_b, run_b = _seed_parallel_running_run(cli_conn, title="Second")

    assert main(["fail-run", "--task", task_a.id, "--message", "boom"]) == 0

    assert store.get_run(cli_conn, run_a.id).status is RunStatus.FAILED
    assert store.get_run(cli_conn, run_b.id).status is RunStatus.RUNNING


def _failure(
    *, step="requirements", skill=None, diagnostics_path="runs/run-1/output.log"
):
    now = datetime.now(UTC)
    return RunFailure(
        run=Run(
            id="run-1",
            task_id="task-1",
            status=RunStatus.FAILED,
            created_at=now,
            completed_at=now,
        ),
        result=Result(
            id="result-1",
            run_id="run-1",
            status=ResultStatus.FAILED,
            created_at=now,
        ),
        task=Task(
            id="task-1",
            title="Ship it",
            description="",
            status=TaskStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        ),
        action=EvaluationOutput(
            action=ActionType.RUN, reason="run_failed", step=step, skill=skill
        ),
        diagnostics_path=diagnostics_path,
    )


def test_format_failure_step_run():
    assert format_failure(_failure()) == (
        "Run run-1 failed.\n"
        "\n"
        "Task status:\n"
        "active\n"
        "\n"
        "Diagnostics: runs/run-1/output.log\n"
        "\n"
        "Next action:\n"
        "Run requirements.\n"
        "\n"
        "Next: /skillflow:work"
    )


def test_format_failure_without_diagnostics():
    rendered = format_failure(_failure(diagnostics_path=None))

    assert "Diagnostics:" not in rendered
    assert "Run run-1 failed." in rendered
    assert "Run requirements." in rendered


def test_format_failure_skill_run():
    rendered = format_failure(_failure(step=None, skill="research"))

    assert "Run skill 'research' (reason: run_failed)." in rendered
    assert "Next: /skillflow:work" in rendered


def test_format_failure_non_run_action_raises():
    now = datetime.now(UTC)
    failure = RunFailure(
        run=Run(
            id="run-1",
            task_id="task-1",
            status=RunStatus.FAILED,
            created_at=now,
        ),
        result=Result(
            id="result-1",
            run_id="run-1",
            status=ResultStatus.FAILED,
            created_at=now,
        ),
        task=Task(
            id="task-1",
            title="Ship it",
            description="",
            status=TaskStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        ),
        action=EvaluationOutput(action=ActionType.COMPLETE, reason="approved"),
    )

    with pytest.raises(ValueError, match="always retries as 'run'"):
        format_failure(failure)


# --- format_error (SF-37) --------------------------------------------------


@pytest.mark.parametrize(
    ("command", "unchanged"),
    [
        ("resolve-task", "No Run was created."),
        (
            "prepare-artifacts",
            "Nothing was changed: this command only inspects state.",
        ),
        ("complete-run", "No Result was created; no Run status was changed."),
        ("decide", "No decision was recorded; no Task status was changed."),
        ("fail-run", "No failure was recorded; no Run status was changed."),
    ],
)
def test_format_error_envelope_per_command(command, unchanged):
    err = ResolveTaskError("TaskNotFound", "no task with id 'task-nope'")

    assert format_error(command, err) == (
        f"skillflow {command}: TaskNotFound: no task with id 'task-nope'\n{unchanged}"
    )


def test_format_error_uses_class_name_when_no_code():
    err = EvaluationError("step 'review' has no rule for outcome 'bogus'")

    assert format_error("complete-run", err) == (
        "skillflow complete-run: EvaluationError: "
        "step 'review' has no rule for outcome 'bogus'\n"
        "No Result was created; no Run status was changed."
    )


def test_format_error_reports_workflow_selection_required_code():
    err = WorkflowSelectionRequiredError("task 'task-1' has no Workflow Definition")

    assert format_error("resolve-task", err) == (
        "skillflow resolve-task: WorkflowSelectionRequired: "
        "task 'task-1' has no Workflow Definition\n"
        "No Run was created."
    )


def test_command_outside_repository_names_recovery(tmp_path, monkeypatch, capsys):
    # tmp_path lives under the system temp root, which has no .git/.skillflow
    # above it (see test_workspace.py) -- so repo-root discovery fails.
    monkeypatch.chdir(tmp_path)

    assert main(["resolve-task", "task-1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skillflow resolve-task: RepositoryRootNotFoundError: " in captured.err
    assert "from inside the target repository" in captured.err
    assert captured.err.splitlines()[-1] == "No Run was created."


def test_command_in_uninitialised_repo_points_at_init_workspace(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    assert main(["resolve-task", "task-1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skillflow resolve-task: WorkspaceError: " in captured.err
    assert "init_workspace" in captured.err
    assert "run init_workspace first" not in captured.err
    assert captured.err.splitlines()[-1] == "No Run was created."


# --- show-task (SF-38) -----------------------------------------------------


def test_show_task_renders_running_lifecycle_view(cli_conn, capsys):
    task, run = _resolve_cli_task(cli_conn)
    capsys.readouterr()

    assert main(["show-task", task.id]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert f"Task {task.id}: Ship it (active)" in out
    assert "Workflow: software-change" in out
    assert "Runs (1):" in out
    assert f"[1] {run.id} (running)" in out
    assert "step 'requirements'" in out
    assert "trigger: initial" in out
    assert "Result: none" in out
    assert "Artifacts: none" in out
    assert "Decisions: none" in out
    assert "Events (2):" in out
    assert "task.created" in out
    assert "run.created" in out


def test_show_task_unknown_task_exits_one_with_envelope(cli_conn, capsys):
    assert main(["show-task", "task-nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "task-nope" in captured.err
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow show-task: TaskNotFound: ")
    assert lines[-1] == "Nothing was changed: this command only inspects state."


def test_show_task_completed_lifecycle_without_workflow_files(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    review = _drive_cli_to_review(cli_conn, cli_ws, task)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="approved",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )
    assert store.get_task(cli_conn, task.id).status is TaskStatus.COMPLETED
    # The view reads stored rows only: deleting every definition file must
    # not change a byte of it.
    for child in cli_ws.workflows_dir.iterdir():
        child.unlink()
    cli_ws.workflows_dir.rmdir()
    capsys.readouterr()

    assert main(["show-task", task.id]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out
    assert f"Task {task.id}:" in out
    assert "(completed)" in out
    assert "Runs (4):" in out
    assert review.id in out
    assert "review/approved" in out
    assert "requirements.md (requirements v1)" in out
    assert "plan.md (plan v1)" in out
    assert "review.md (review v1)" in out
    assert "Artifacts: none" in out
    assert "run.created" in out
    assert "result.created" in out
    assert "task.status_changed" in out


def test_show_task_failed_run_shows_diagnostics(cli_conn, capsys):
    task, run = _resolve_cli_task(cli_conn)
    assert main(["fail-run", "--message", "boom"]) == 0
    capsys.readouterr()

    assert main(["show-task", task.id]) == 0

    out = capsys.readouterr().out
    assert f"{run.id} (failed)" in out
    assert f"diagnostics: runs/{run.id}/output.log" in out


def test_show_task_skill_targeted_run_renders_without_step(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="fundamental_assumption_wrong",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )
    assert main(["resolve-task", task.id]) == 0
    skill_run = store.list_runs_for_task(cli_conn, task.id)[-1]
    assert skill_run.step_id is None
    capsys.readouterr()

    assert main(["show-task", task.id]) == 0

    out = capsys.readouterr().out
    assert skill_run.id in out
    assert "step none" in out
    assert "Result: none" in out


def test_show_task_task_without_events_renders_empty_sections(cli_conn, capsys):
    now = datetime.now(UTC)
    task = Task(
        id="task-raw",
        title="Raw",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    with cli_conn:
        store.insert_task(cli_conn, task)

    assert main(["show-task", task.id]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Workflow: none assigned" in captured.out
    assert "Runs: none" in captured.out
    assert "Events: none recorded" in captured.out


# --- assignment (SF-44) ---------------------------------------------------------


def test_assignment_after_resolve_prints_identical_output(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    assert main(["resolve-task", task.id]) == 0
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
    capsys.readouterr()

    assert main(["resolve-task", task.id]) == 0
    resolved = capsys.readouterr().out

    assert main(["assignment"]) == 0
    assigned = capsys.readouterr().out

    assert "requirements.md (requirements v1): " in resolved
    assert assigned == resolved


def test_assignment_prints_paths_holding_version_content(cli_conn, cli_ws, capsys):
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
    create_artifact(
        cli_conn,
        cli_ws,
        run_id=first.id,
        name="requirements.md",
        type="requirements",
        content="the v2 requirements",
    )
    assert main(["resolve-task", task.id]) == 0
    capsys.readouterr()

    assert main(["assignment"]) == 0

    out = capsys.readouterr().out
    expected = f".skillflow/artifacts/{task.id}/requirements-v2.md"
    assert f"  - requirements.md (requirements v2): {expected}" in out
    v2_file = cli_ws.root / expected
    assert v2_file.read_text(encoding="utf-8") == "the v2 requirements"
    v1_file = cli_ws.root / f".skillflow/artifacts/{task.id}/requirements-v1.md"
    assert v1_file.read_text(encoding="utf-8") == "# requirements.md"


def test_assignment_skill_match_exits_zero(cli_conn, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    assert main(["resolve-task", task.id]) == 0
    capsys.readouterr()

    assert main(["assignment", "--skill", "requirements-analysis"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "step 'requirements' via skill 'requirements-analysis'" in captured.out


def test_assignment_skill_mismatch_exits_one_without_writes(cli_conn, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    assert main(["resolve-task", task.id]) == 0
    run = store.list_runs_for_task(cli_conn, task.id)[-1]
    capsys.readouterr()
    before = (
        store.get_task(cli_conn, task.id),
        store.get_run(cli_conn, run.id),
        store.get_result_for_run(cli_conn, run.id),
        store.list_artifacts_for_task(cli_conn, task.id),
        store.list_lifecycle_events_for_task(cli_conn, task.id),
    )

    assert main(["assignment", "--skill", "nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[0].startswith(
        "skillflow assignment: AssignmentMismatch: "
    )
    assert "'nope'" in captured.err
    assert "'requirements-analysis'" in captured.err
    assert captured.err.splitlines()[-1] == (
        "Nothing was changed: this command only inspects state."
    )
    assert (
        store.get_task(cli_conn, task.id),
        store.get_run(cli_conn, run.id),
        store.get_result_for_run(cli_conn, run.id),
        store.list_artifacts_for_task(cli_conn, task.id),
        store.list_lifecycle_events_for_task(cli_conn, task.id),
    ) == before


def test_assignment_without_running_run_exits_one(cli_conn, capsys):
    _seed_task(cli_conn, workflow_id="software-change")

    assert main(["assignment"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[0].startswith(
        "skillflow assignment: RunNotFound: "
    )
    assert captured.err.splitlines()[-1] == (
        "Nothing was changed: this command only inspects state."
    )


def test_assignment_two_running_runs_reject(cli_conn, capsys):
    task, _ = _resolve_cli_task(cli_conn)
    other, _ = _seed_parallel_running_run(cli_conn)
    capsys.readouterr()

    assert main(["assignment"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skillflow assignment: AmbiguousCurrentRun: " in captured.err
    assert f"task {task.id!r}" in captured.err
    assert f"task {other.id!r}" in captured.err


def test_assignment_verifies_a_skill_targeted_run(cli_conn, cli_ws, capsys):
    task = _seed_task(cli_conn, workflow_id="software-change")
    _drive_cli_to_review(cli_conn, cli_ws, task)
    assert (
        _complete_cli(
            cli_conn,
            cli_ws,
            outcome="fundamental_assumption_wrong",
            artifacts=(("review.md", "review"),),
        )
        == 0
    )
    capsys.readouterr()
    assert main(["resolve-task", task.id]) == 0
    skill_run = store.list_runs_for_task(cli_conn, task.id)[-1]
    assert skill_run.step_id is None
    created = capsys.readouterr().out

    assert main(["assignment", "--skill", "research"]) == 0
    matched = capsys.readouterr()
    assert matched.err == ""
    assert matched.out == created
    assert "skill 'research' (no workflow step)" in matched.out

    assert main(["assignment", "--skill", "code-review"]) == 1
    mismatched = capsys.readouterr()
    assert mismatched.out == ""
    assert "skillflow assignment: AssignmentMismatch: " in mismatched.err
    assert store.get_run(cli_conn, skill_run.id).status is RunStatus.RUNNING


def _insert_escaping_artifact(cli_conn, task_id, run_id):
    now = datetime.now(UTC)
    escaping = Artifact(
        id="artifact-escape",
        task_id=task_id,
        run_id=run_id,
        name="requirements.md",
        type="requirements",
        version=99,
        path="../escape.md",
        created_at=now,
    )
    with cli_conn:
        store.insert_artifact(cli_conn, escaping)
    return escaping


def test_assignment_stored_path_escape_rejected(cli_conn, cli_ws, capsys):
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
    assert main(["resolve-task", task.id]) == 0
    second = store.list_runs_for_task(cli_conn, task.id)[-1]
    # Corrupt the store only after the clean resolve: the escaping row is the
    # new chain head, so the rebuild selects it and the print rejects.
    _insert_escaping_artifact(cli_conn, task.id, first.id)
    runs_before = store.list_runs_for_task(cli_conn, task.id)
    capsys.readouterr()

    assert main(["assignment"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "skillflow assignment: ArtifactStorageError: " in captured.err
    assert captured.err.splitlines()[-1] == (
        "Nothing was changed: this command only inspects state."
    )
    assert store.list_runs_for_task(cli_conn, task.id) == runs_before
    assert store.get_run(cli_conn, second.id).status is RunStatus.RUNNING


def test_resolve_task_stored_path_escape_keeps_a_truthful_sentence(
    cli_conn, cli_ws, capsys
):
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
    _insert_escaping_artifact(cli_conn, task.id, first.id)
    capsys.readouterr()

    assert main(["resolve-task", task.id]) == 1

    runs = store.list_runs_for_task(cli_conn, task.id)
    assert len(runs) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow resolve-task: ArtifactStorageError: ")
    assert f"Run {runs[-1].id!r} was created" in captured.err
    assert "No Run was created." not in captured.err
    assert f"`skillflow show-task {task.id}`" in captured.err


# --- start (SF-45) ----------------------------------------------------------


@pytest.fixture
def start_repo(tmp_path, monkeypatch):
    # Unlike cli_ws, the workspace must NOT be pre-initialised: start
    # owns init_workspace itself.
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def bundled_defs(monkeypatch):
    # Hermetic: never depend on the checkout's root workflows/ or install
    # layout.
    monkeypatch.setenv("SKILLFLOW_BUNDLED_WORKFLOWS", str(REFERENCE.parent))


def test_start_in_empty_git_repo_creates_active_task(start_repo, bundled_defs, capsys):
    assert main(["start", "--title", "Ship it"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    task_id = lines[0]

    ws = workspace.Workspace(root=start_repo)
    assert ws.db_path.is_file()
    with contextlib.closing(store.open_store(ws)) as conn:
        task = store.get_task(conn, task_id)
        assert task is not None
        assert task.status is TaskStatus.ACTIVE
        assert task.title == "Ship it"
        assert task.description == ""
        assert task.workflow_definition_id == "software-change"
        assert store.get_workflow_definition(conn, "software-change") is not None
    assert (start_repo / "workflows" / "software-change.yaml").read_bytes() == (
        REFERENCE.read_bytes()
    )


def test_start_twice_creates_second_task_and_leaves_yaml_untouched(
    start_repo, bundled_defs, capsys
):
    assert main(["start", "--title", "First"]) == 0
    first_id = capsys.readouterr().out.splitlines()[0]

    pinned = start_repo / "workflows" / "software-change.yaml"
    with pinned.open("a", encoding="utf-8") as handle:
        handle.write("# pinned\n")

    assert main(["start", "--title", "Second"]) == 0
    second_id = capsys.readouterr().out.splitlines()[0]

    assert first_id != second_id
    assert "# pinned" in pinned.read_text(encoding="utf-8")

    ws = workspace.Workspace(root=start_repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        assert len(store.list_tasks_by_status(conn, TaskStatus.ACTIVE)) == 2


def test_start_unknown_workflow_rejects_without_task(start_repo, bundled_defs, capsys):
    assert main(["start", "--title", "T", "--workflow", "nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow start: WorkflowLoadError: ")
    assert lines[-1] == "No Task was created."
    assert not (start_repo / "workflows" / "nope.yaml").exists()
    assert (start_repo / ".skillflow").is_dir()

    ws = workspace.Workspace(root=start_repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        assert store.list_tasks_by_status(conn, TaskStatus.ACTIVE) == []


def test_start_missing_title_exits_two(start_repo, bundled_defs):
    with pytest.raises(SystemExit) as exc_info:
        main(["start"])
    assert exc_info.value.code == 2


def test_start_blank_title_exits_two(start_repo, bundled_defs):
    with pytest.raises(SystemExit) as exc_info:
        main(["start", "--title", "  "])
    assert exc_info.value.code == 2


def test_start_respects_prepinned_yaml_without_bundled(
    start_repo, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("SKILLFLOW_BUNDLED_WORKFLOWS", str(tmp_path / "empty-bundled"))
    pinned = start_repo / "workflows"
    pinned.mkdir()
    (pinned / "software-change.yaml").write_bytes(REFERENCE.read_bytes())

    assert main(["start", "--title", "Pinned"]) == 0

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert (pinned / "software-change.yaml").read_bytes() == REFERENCE.read_bytes()


def test_start_with_description_and_workflow_flag(start_repo, bundled_defs, capsys):
    assert (
        main(
            [
                "start",
                "--title",
                "Described",
                "--description",
                "Why this exists",
                "--workflow",
                "software-change",
            ]
        )
        == 0
    )

    task_id = capsys.readouterr().out.splitlines()[0]
    ws = workspace.Workspace(root=start_repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        task = store.get_task(conn, task_id)
        assert task is not None
        assert task.description == "Why this exists"
        assert task.workflow_definition_id == "software-change"


def test_bundled_dir_prefers_env_then_package_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILLFLOW_BUNDLED_WORKFLOWS", raising=False)
    fallback = Path(skillflow.__file__).resolve().parents[2] / "workflows"
    assert _bundled_workflows_dir() == fallback
    monkeypatch.setenv("SKILLFLOW_BUNDLED_WORKFLOWS", "")
    assert _bundled_workflows_dir() == fallback
    monkeypatch.setenv("SKILLFLOW_BUNDLED_WORKFLOWS", str(tmp_path))
    assert _bundled_workflows_dir() == tmp_path


def test_start_outside_repo_rejects(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    assert main(["start", "--title", "Lost"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[0].startswith(
        "skillflow start: RepositoryRootNotFoundError: "
    )


@pytest.mark.parametrize("workflow_id", ["../evil", ""])
def test_start_hostile_workflow_id_rejects_without_side_effects(
    start_repo, tmp_path_factory, monkeypatch, capsys, workflow_id
):
    # The escape target lives outside the repo on purpose and exists:
    # a naive path-join staging would copy it to the repo root before
    # the loader rejects the id, so only the side-effect assertions
    # below discriminate the workflow_path guard (exit code and
    # envelope are identical either way).
    outside = tmp_path_factory.mktemp("bundled-outer")
    bundled = outside / "bundled"
    bundled.mkdir()
    (outside / "evil.yaml").write_text("name: evil\n", encoding="utf-8")
    monkeypatch.setenv("SKILLFLOW_BUNDLED_WORKFLOWS", str(bundled))

    assert main(["start", "--title", "T", "--workflow", workflow_id]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0].startswith("skillflow start: WorkflowLoadError: ")
    assert lines[-1] == "No Task was created."
    assert list(start_repo.glob("*.yaml")) == []
    assert not (start_repo / "workflows").exists()

    ws = workspace.Workspace(root=start_repo)
    with contextlib.closing(store.open_store(ws)) as conn:
        assert store.list_tasks_by_status(conn, TaskStatus.ACTIVE) == []
