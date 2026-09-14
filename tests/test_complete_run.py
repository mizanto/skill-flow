"""Tests for ``skillflow.complete_run`` (SF-23, SF-24).

Following ``test_prepare_artifacts.py``'s shape: a ``tmp_path`` workspace
fixture with a ``.git`` marker, ``store.open_store``, and the frozen
``runtime-reference/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, and earlier steps are completed with the real ``complete_run(...)``
itself, so the command is exercised against state the system produces.

* **Contract change-detectors** -- the public surface, ``CompleteRunError``
  (type and ``code`` attribute), the ``RunCompletion`` validation, the AST
  import boundary (no Claude Code launch of any kind), the banned-concept
  scan, and the presence of the ``evaluator`` import (the mechanical form of
  "Lifecycle Evaluation happens here" -- SF-24 flipped the SF-23 absence
  test deliberately).
* **Behaviour tests** -- every rejection path proving the snapshot is
  byte-identical afterwards (the mechanical form of "a rejection leaves the
  Run ``running`` with nothing written"), and every success path proving the
  single atomic write (artifacts registered, exactly one Result, Run
  completed, lifecycle evaluated with its Task consequence applied, no next
  Run).
"""

import ast
import contextlib
import dataclasses
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import complete_run as complete_run_pkg
from skillflow import store, workspace
from skillflow.artifacts import create_artifact, read_content
from skillflow.complete_run import CompleteRunError, RunCompletion
from skillflow.complete_run import complete_run as complete
from skillflow.completion import (
    ArtifactSubmission,
    CompletionError,
    CompletionRequest,
)
from skillflow.domain import (
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import (
    REASON_NO_OUTCOME,
    EvaluationError,
    EvaluationOutput,
)
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

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


def _submit(name, type, content=None):
    return ArtifactSubmission(
        name=name, type=type, content=f"# {name}" if content is None else content
    )


def _drive_to_review(conn, ws, task):
    """Complete requirements/decomposition/implementation as ready.

    Return the running review Run.
    """
    for step, sub in (
        ("requirements", ("requirements.md", "requirements")),
        ("decomposition", ("plan.md", "plan")),
        ("implementation", None),
    ):
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == step
        subs = () if sub is None else (_submit(sub[0], sub[1]),)
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(decision="ready", artifacts=subs),
        )
    review_input = resolve(conn, ws, task_id=task.id)
    assert review_input.step_id == "review"
    return store.get_run(conn, review_input.run_id)


def _terminate(conn, run, status):
    done = replace(
        run, status=status, completed_at=run.started_at + timedelta(seconds=1)
    )
    with conn:
        store.update_run(conn, done)
    return store.get_run(conn, run.id)


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


def _event_types(conn, task_id, count):
    events = store.list_lifecycle_events_for_task(conn, task_id)
    return [event.type for event in events[-count:]]


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(complete_run_pkg.__all__) == {
        "CompleteRunError",
        "RunCompletion",
        "complete_run",
    }


def test_complete_run_error_carries_code():
    err = CompleteRunError("RunNotFound", "no run")
    assert isinstance(err, Exception)
    assert err.code == "RunNotFound"
    assert str(err) == "no run"


def test_run_completion_fields_match_contract():
    assert {f.name for f in dataclasses.fields(RunCompletion)} == {
        "run",
        "result",
        "task",
        "action",
        "artifacts",
    }


def test_run_completion_is_frozen_slotted_keyword_only():
    from datetime import UTC, datetime

    from skillflow.domain import Result, Run, Task

    params = RunCompletion.__dataclass_params__
    assert params.frozen and params.kw_only
    now = datetime.now(UTC)
    instance = RunCompletion(
        run=Run(
            id="run-1",
            task_id="task-1",
            status=RunStatus.RUNNING,
            created_at=now,
        ),
        result=Result(
            id="result-1",
            run_id="run-1",
            status=ResultStatus.COMPLETED,
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
    )
    assert instance.action is None
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.artifacts = ()


def _valid_run_result_and_task():
    from datetime import UTC, datetime

    from skillflow.domain import Run, Task

    now = datetime.now(UTC)
    run = Run(
        id="run-1",
        task_id="task-1",
        status=RunStatus.RUNNING,
        created_at=now,
    )
    result = Result(
        id="result-1",
        run_id="run-1",
        status=ResultStatus.COMPLETED,
        created_at=now,
    )
    task = Task(
        id="task-1",
        title="Ship it",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    return run, result, task


def test_run_completion_rejects_bad_members():
    run, result, task = _valid_run_result_and_task()
    with pytest.raises(ValueError, match="must be a Run"):
        RunCompletion(run="run-1", result=result, task=task)
    with pytest.raises(ValueError, match="must be a Result"):
        RunCompletion(run=run, result="result-1", task=task)
    with pytest.raises(ValueError, match="must be a Task"):
        RunCompletion(run=run, result=result, task="task-1")
    with pytest.raises(ValueError, match="must be an EvaluationOutput or None"):
        RunCompletion(run=run, result=result, task=task, action="run")
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunCompletion(run=run, result=result, task=task, artifacts="review.md")
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunCompletion(run=run, result=result, task=task, artifacts=123)
    with pytest.raises(ValueError, match="must contain Artifact"):
        RunCompletion(run=run, result=result, task=task, artifacts=(123,))


def test_module_imports_are_within_the_boundary():
    source = Path(complete_run_pkg.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    allowed = {"sqlite3", "dataclasses", "datetime", "uuid", "skillflow"}
    assert modules <= allowed, f"unexpected imports: {modules - allowed}"
    for forbidden in (
        "subprocess",
        "os",
        "shutil",
        "sys",
        "pathlib",
        "yaml",
        "random",
        "time",
    ):
        assert forbidden not in modules


def test_no_excluded_lifecycle_concepts_present():
    banned = (
        "router",
        "transition",
        "loop",
        "iteration",
        "rework",
        "handoff",
        "instance",
        "stage",
    )
    offenders = [
        name
        for name in vars(complete_run_pkg)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_module_evaluates_via_evaluator():
    # SF-24 deliberately flipped the SF-23 absence test: importing the pure
    # evaluator is the mechanical form of "Lifecycle Evaluation happens here".
    source = Path(complete_run_pkg.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "skillflow.evaluator" in imported


# --- rejection paths (nothing written) --------------------------------------


def test_unknown_run_id_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    runs_before = len(store.list_runs_for_task(conn, task.id))
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    with pytest.raises(CompleteRunError) as exc_info:
        complete(conn, ws, run_id="run-missing", request=CompletionRequest())
    assert exc_info.value.code == "RunNotFound"
    assert len(store.list_runs_for_task(conn, task.id)) == runs_before
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == events_before


@pytest.mark.parametrize(
    "status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED]
)
def test_terminal_run_is_never_resumed(conn, ws, workflows, status):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = _terminate(conn, store.get_run(conn, run_input.run_id), status)
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(conn, ws, run_id=run.id, request=CompletionRequest())
    assert exc_info.value.code == "RunNotActive"
    assert "never resumed" in str(exc_info.value)
    assert _snapshot(conn, task.id, run.id) == before


def test_missing_required_artifact_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "requirements"
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(decision="ready"),
        )
    assert exc_info.value.code == "RequiredArtifactsMissing"
    msg = str(exc_info.value)
    assert "'requirements'" in msg
    assert "prepare-artifacts" in msg
    assert _snapshot(conn, task.id, run_input.run_id) == before


def test_invalid_outcome_rejected_without_registering(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(CompletionError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision="maybe_everything_is_fine",
                artifacts=(_submit("requirements.md", "requirements"),),
            ),
        )
    assert exc_info.value.code == "InvalidOutcome"
    assert _snapshot(conn, task.id, run_input.run_id) == before


def test_missing_decision_rejected_without_registering(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(CompletionError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                artifacts=(_submit("requirements.md", "requirements"),)
            ),
        )
    assert exc_info.value.code == "OutcomeRequired"
    assert _snapshot(conn, task.id, run_input.run_id) == before


def _single_step_task(conn, workflows):
    """A Task on a minimal workflow: one step, no outputs, no outcomes.

    No step of the reference workflow declares an empty `outcomes` mapping
    (the reference `implementation` step declares `ready`), so the
    outcome-less paths are exercised here instead.
    """
    (workflows / "single.yaml").write_text(
        "name: single\nsteps:\n  - id: only\n    skill: do-it\n",
        encoding="utf-8",
    )
    workflow = load_workflow(workflows / "single.yaml")
    register_workflow(conn, workflow)
    return create_task(conn, title="One step", workflow_definition_id="single")


def test_decision_on_step_with_no_outcomes_rejected(conn, ws, workflows):
    task = _single_step_task(conn, workflows)
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "only"
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(CompletionError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(decision="ready"),
        )
    assert exc_info.value.code == "OutcomeNotExpected"
    assert _snapshot(conn, task.id, run_input.run_id) == before


def _skill_run_after_review(conn, ws, task):
    """Drive to review, complete it as faw, and create the research Run.

    Returns (review Run, skill Run). Mirrors the real flow: the skill Run is
    created from the review completion's own evaluated action.
    """
    review = _drive_to_review(conn, ws, task)
    done = complete(
        conn,
        ws,
        run_id=review.id,
        request=CompletionRequest(
            decision="fundamental_assumption_wrong",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    assert done.action.action is ActionType.RUN
    assert done.action.skill == "research"
    skill_run = create_run(
        conn,
        task_id=task.id,
        action=done.action,
        triggered_by_run_id=review.id,
    )
    assert skill_run.step_id is None
    return review, skill_run


def test_skill_targeted_run_with_trigger_step_decision_evaluates(conn, ws, workflows):
    # SF-32: the decision is validated against the triggering step's table
    # and the completion evaluates like a step Run's.
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    runs_before = len(store.list_runs_for_task(conn, task.id))
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=skill_run.id,
        request=CompletionRequest(
            decision="replan",
            artifacts=(_submit("research.md", "research"),),
        ),
    )
    assert done.run.status is RunStatus.COMPLETED
    assert done.result.outcome == Outcome(type="review", decision="replan")
    assert done.action.action is ActionType.RUN
    assert (done.action.step, done.action.skill) == ("decomposition", None)
    assert done.action.reason == "replan"
    assert done.task.status is TaskStatus.ACTIVE
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE
    (research,) = store.list_artifacts_for_run(conn, skill_run.id)
    assert (research.name, research.type, research.version) == (
        "research.md",
        "research",
        1,
    )
    assert research.supersedes_id is None
    # The single atomic write: artifact + Result + Run completion -- and no
    # next Run (the evaluated `run` action only keeps the Task active).
    assert len(store.list_runs_for_task(conn, task.id)) == runs_before
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 3
    )
    assert sorted(_event_types(conn, task.id, 3)) == sorted(
        [
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
        ]
    )


def test_skill_targeted_run_with_unknown_decision_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    before = _snapshot(conn, task.id, skill_run.id)
    with pytest.raises(CompletionError) as exc_info:
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(decision="bogus"),
        )
    assert exc_info.value.code == "InvalidOutcome"
    assert "'bogus'" in str(exc_info.value)
    assert "'replan'" in str(exc_info.value)  # the trigger step's keys
    assert _snapshot(conn, task.id, skill_run.id) == before


def test_skill_targeted_run_with_skill_targeting_decision_rejected(conn, ws, workflows):
    # Approver decision 5: research must not resolve to research again.
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    before = _snapshot(conn, task.id, skill_run.id)
    with pytest.raises(EvaluationError, match="must not target another skill"):
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(decision="fundamental_assumption_wrong"),
        )
    assert _snapshot(conn, task.id, skill_run.id) == before


def test_skill_targeted_run_with_human_mapping_decision_parks_task(conn, ws, workflows):
    # The evaluated `human` consequence applies to skill Runs too.
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    done = complete(
        conn,
        ws,
        run_id=skill_run.id,
        request=CompletionRequest(decision="human_required"),
    )
    assert done.run.status is RunStatus.COMPLETED
    assert done.action.action is ActionType.HUMAN
    assert done.task.status is TaskStatus.WAITING_FOR_HUMAN
    assert store.get_task(conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN


def test_skill_targeted_run_without_a_triggering_step_rejected(conn, ws, workflows):
    # A skill Run triggered by another skill Run (hand-built only --
    # uncreatable via commands since skill→skill decisions are rejected).
    _, task = _assigned(conn)
    _, first_skill = _skill_run_after_review(conn, ws, task)
    complete(conn, ws, run_id=first_skill.id, request=CompletionRequest())
    second = create_run(
        conn,
        task_id=task.id,
        action=EvaluationOutput(
            action=ActionType.RUN, reason="research", skill="research"
        ),
        triggered_by_run_id=first_skill.id,
    )
    assert second.step_id is None
    before = _snapshot(conn, task.id, second.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=second.id,
            request=CompletionRequest(decision="replan"),
        )
    assert exc_info.value.code == "StepUnresolved"
    assert "no triggering step" in str(exc_info.value)
    assert _snapshot(conn, task.id, second.id) == before


def test_skill_targeted_run_without_workflow_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    with conn:
        store.update_run(conn, replace(skill_run, workflow_definition_id=None))
    before = _snapshot(conn, task.id, skill_run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(decision="replan"),
        )
    assert exc_info.value.code == "StepUnresolved"
    assert "no Workflow Definition" in str(exc_info.value)
    assert _snapshot(conn, task.id, skill_run.id) == before


def test_skill_targeted_run_without_trigger_rejected(conn, ws, workflows):
    # Provenance is immutable history (`store.update_run` never writes it),
    # so the triggerless Run is inserted directly, `test_decide._stored_run`
    # style, on a fresh task with no running Run.
    _, task = _assigned(conn)
    now = datetime.now(UTC)
    run = Run(
        id="run-no-trigger",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        workflow_definition_id="software-change",
        step_id=None,
    )
    with conn:
        store.insert_run(conn, run)
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run.id,
            request=CompletionRequest(decision="replan"),
        )
    assert exc_info.value.code == "StepUnresolved"
    assert "no triggering step" in str(exc_info.value)
    assert _snapshot(conn, task.id, run.id) == before


def test_skill_targeted_run_trigger_step_absent_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    review, skill_run = _skill_run_after_review(conn, ws, task)
    review = store.get_run(conn, review.id)  # completed, not the stale object
    with conn:
        store.update_run(conn, replace(review, step_id="ghost"))
    before = _snapshot(conn, task.id, skill_run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(decision="replan"),
        )
    assert exc_info.value.code == "WorkflowMismatch"
    assert "'ghost'" in str(exc_info.value)
    assert _snapshot(conn, task.id, skill_run.id) == before


def test_skill_targeted_evaluation_failure_leaves_run_running(
    conn, ws, workflows, monkeypatch
):
    def boom(_evaluation):
        raise EvaluationError("boom")

    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    before = _snapshot(conn, task.id, skill_run.id)
    monkeypatch.setattr(complete_run_pkg, "evaluate", boom)
    with pytest.raises(EvaluationError, match="boom"):
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(
                decision="replan",
                artifacts=(_submit("research.md", "research"),),
            ),
        )
    assert _snapshot(conn, task.id, skill_run.id) == before


def test_skill_targeted_run_without_decision_keeps_action_none(conn, ws, workflows):
    _, task = _assigned(conn)
    _, skill_run = _skill_run_after_review(conn, ws, task)
    task_before = store.get_task(conn, task.id)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(conn, ws, run_id=skill_run.id, request=CompletionRequest())
    assert done.run.status is RunStatus.COMPLETED
    assert done.result.outcome is None
    # A decisionless skill-targeted Run keeps its SF-23 semantics: no
    # evaluation, no Task change -- only the Result and Run completion land.
    assert done.action is None
    assert done.task == task_before
    assert store.get_task(conn, task.id) == task_before
    assert sorted(_event_types(conn, task.id, 2)) == sorted(
        [
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 2
    )


def test_step_absent_from_definition_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    with conn:
        store.update_run(conn, replace(run, step_id="ghost"))
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(conn, ws, run_id=run.id, request=CompletionRequest())
    assert exc_info.value.code == "WorkflowMismatch"
    assert _snapshot(conn, task.id, run.id) == before


def test_traversal_name_rejected_before_the_write(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    files_before = sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(ValueError, match="plain filename"):
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision="ready",
                artifacts=(_submit("../escape.md", "requirements"),),
            ),
        )
    assert _snapshot(conn, task.id, run_input.run_id) == before
    assert sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )
    assert not (ws.root / "escape.md").exists()


# --- failures inside the write block (rollback proof) -------------------------


def test_failure_after_a_write_rolls_back_and_leaves_no_orphan(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="notes.md",
        type="notes",
        content="v1",
    )
    files_before = sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(ValueError, match="refusing to create version 2"):
        complete(
            conn,
            ws,
            run_id=run.id,
            request=CompletionRequest(
                decision="ready",
                artifacts=(
                    _submit("requirements.md", "requirements"),
                    _submit("notes.md", "WRONGTYPE", content="v2"),
                ),
            ),
        )
    assert _snapshot(conn, task.id, run.id) == before
    assert sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="ready",
            artifacts=(_submit("requirements.md", "requirements"),),
        ),
    )
    assert done.run.status is RunStatus.COMPLETED


def test_preexisting_result_rolls_back_written_rows(conn, ws, workflows):
    from datetime import UTC, datetime

    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    seeded = Result(
        id="result-seeded",
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=datetime.now(UTC),
        outcome=Outcome(type="requirements", decision="ready"),
    )
    with conn:
        store.insert_result(conn, seeded)
    files_before = sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(store.InvariantViolationError, match="already has"):
        complete(
            conn,
            ws,
            run_id=run.id,
            request=CompletionRequest(
                decision="ready",
                artifacts=(_submit("requirements.md", "requirements"),),
            ),
        )
    assert _snapshot(conn, task.id, run.id) == before
    assert sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )


def test_retry_after_crash_reuses_orphan_artifact(conn, ws, workflows):
    # A kill between the content-file write and the commit leaves an orphan
    # file and no row; the retry reuses it when the bytes are identical and
    # the Run completes normally.
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    submission = _submit("requirements.md", "requirements")
    orphan = ws.artifacts_dir / task.id / "requirements-v1.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(submission.content, encoding="utf-8")

    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(decision="ready", artifacts=(submission,)),
    )

    assert done.run.status is RunStatus.COMPLETED
    assert store.get_result_for_run(conn, run.id).id == done.result.id
    (artifact,) = store.list_artifacts_for_run(conn, run.id)
    assert artifact.version == 1
    assert artifact.path == f"{task.id}/requirements-v1.md"
    assert read_content(ws, artifact) == submission.content


class _CommitFails(sqlite3.Connection):
    """A connection whose transaction block fails at commit time.

    Duplicated from ``test_artifacts.py``: ``sqlite3.Connection.commit`` is
    a read-only C attribute and cannot be monkeypatched, and ``__exit__``
    is where ``with conn:`` commits -- exactly the moment this test needs
    to fail, after every file has been written inside the block. An exception
    already in flight from the body is left alone, so only a body that ran
    clean is failed at commit.
    """

    def __exit__(self, *exc_info):
        if exc_info[0] is None:
            raise sqlite3.OperationalError("commit failed")
        return False


def test_commit_failure_helper_leaves_body_exceptions_alone(ws):
    # The helper fails the commit, not the body: an exception already in
    # flight must propagate unmasked, or a test could pass vacuously.
    failing = sqlite3.connect(ws.db_path, factory=_CommitFails)
    try:
        with pytest.raises(RuntimeError, match="body"):
            with failing:
                raise RuntimeError("body failed")
    finally:
        failing.close()


def test_commit_failure_with_multiple_artifacts_rolls_back_and_unlinks(
    conn, ws, workflows
):
    # The write block is one unit: a commit failure rolls back the artifact
    # rows, the Result, the Run completion and the events -- and unlinks every
    # file this attempt created.
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    failing = sqlite3.connect(ws.db_path, factory=_CommitFails)
    failing.row_factory = sqlite3.Row
    failing.execute("PRAGMA foreign_keys = ON")
    files_before = sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())
    before = _snapshot(conn, task.id, run.id)
    try:
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            complete(
                failing,
                ws,
                run_id=run.id,
                request=CompletionRequest(
                    decision="ready",
                    artifacts=(
                        _submit("requirements.md", "requirements"),
                        _submit("notes.md", "notes"),
                    ),
                ),
            )
    finally:
        # Closing rolls the still-open transaction back and releases the write
        # lock, so the fixture connection can read below.
        failing.close()
    assert _snapshot(conn, task.id, run.id) == before
    assert sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )


def test_non_completion_request_rejected(conn, ws):
    with pytest.raises(ValueError, match="must be a CompletionRequest"):
        complete(conn, ws, run_id="run-1", request={})


# --- success paths (one atomic write) ---------------------------------------


def test_review_completion_registers_result_and_artifact(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="approved",
            artifacts=(_submit("review.md", "review", content="# Review"),),
        ),
    )
    assert done.run.status is RunStatus.COMPLETED
    assert done.run.completed_at is not None
    assert done.result.status is ResultStatus.COMPLETED
    assert done.result.run_id == run.id
    assert (done.result.outcome.type, done.result.outcome.decision) == (
        "review",
        "approved",
    )
    assert len(done.artifacts) == 1
    (artifact,) = done.artifacts
    assert artifact.version == 1
    assert artifact.path == f"{task.id}/review-v1.md"
    assert read_content(ws, artifact) == "# Review"
    # `approved` evaluates to `complete`, so the Task consequence lands in the
    # same write: one more event than the SF-23 three. A multiset comparison:
    # the four rows share one `created_at`, so the store's (created_at, id)
    # order between them is uuid noise.
    assert sorted(_event_types(conn, task.id, 4)) == sorted(
        [
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
            LifecycleEventType.TASK_STATUS_CHANGED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 4
    )
    assert done.action.action is ActionType.COMPLETE
    assert done.action.reason == "approved"
    assert done.task.status is TaskStatus.COMPLETED
    assert store.get_task(conn, task.id) == done.task
    persisted_run = store.get_run(conn, run.id)
    assert persisted_run.status is RunStatus.COMPLETED
    assert store.get_result_for_run(conn, run.id).id == done.result.id
    assert [a.id for a in store.list_artifacts_for_run(conn, run.id)] == [artifact.id]


def test_step_with_no_outputs_or_outcomes_completes(conn, ws, workflows):
    task = _single_step_task(conn, workflows)
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "only"
    done = complete(conn, ws, run_id=run_input.run_id, request=CompletionRequest())
    assert done.run.status is RunStatus.COMPLETED
    assert done.result.outcome is None
    assert done.artifacts == ()
    # The outcome-less step keeps its REASON_NO_OUTCOME meaning: the Task ends.
    assert done.action.action is ActionType.COMPLETE
    assert done.action.reason == REASON_NO_OUTCOME
    assert done.task.status is TaskStatus.COMPLETED
    assert store.get_task(conn, task.id).status is TaskStatus.COMPLETED


def test_already_registered_output_needs_no_resubmission(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="# Requirements",
    )
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(decision="ready"),
    )
    assert done.run.status is RunStatus.COMPLETED
    assert len(store.list_artifacts_for_run(conn, run.id)) == 1


def test_resubmitted_name_becomes_version_two(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    run = store.get_run(conn, run_input.run_id)
    first = create_artifact(
        conn,
        ws,
        run_id=run.id,
        name="requirements.md",
        type="requirements",
        content="# Requirements v1",
    )
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="ready",
            artifacts=(
                _submit("requirements.md", "requirements", content="# Requirements v2"),
            ),
        ),
    )
    assert len(done.artifacts) == 1
    (second,) = done.artifacts
    assert second.version == 2
    assert second.supersedes_id == first.id
    assert read_content(ws, second) == "# Requirements v2"


def test_two_submissions_register_in_one_write(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    artifacts_before = len(store.list_artifacts_for_run(conn, run_input.run_id))
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run_input.run_id,
        request=CompletionRequest(
            decision="ready",
            artifacts=(
                _submit("requirements.md", "requirements"),
                _submit("notes.md", "notes"),
            ),
        ),
    )
    assert [a.name for a in done.artifacts] == ["requirements.md", "notes.md"]
    assert len(store.list_artifacts_for_run(conn, run_input.run_id)) == (
        artifacts_before + 2
    )
    assert sorted(_event_types(conn, task.id, 4)) == sorted(
        [
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 4
    )


def test_duplicate_names_rejected_before_any_write(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    before = _snapshot(conn, task.id, run_input.run_id)
    with pytest.raises(ValueError, match="duplicate name"):
        CompletionRequest(
            decision="ready",
            artifacts=(
                _submit("requirements.md", "requirements"),
                _submit("requirements.md", "requirements"),
            ),
        )
    assert _snapshot(conn, task.id, run_input.run_id) == before


def test_second_completion_is_rejected_with_one_result(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="approved",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run.id,
            request=CompletionRequest(decision="approved"),
        )
    assert exc_info.value.code == "RunNotActive"
    assert (
        len(
            conn.execute("SELECT * FROM results WHERE run_id = ?", (run.id,)).fetchall()
        )
        == 1
    )


def test_run_action_leaves_task_active_with_no_status_event(conn, ws, workflows):
    # `changes_requested` evaluates to `run`, whose Task consequence (ACTIVE)
    # is a no-op on an active Task: no write, no status-change event -- but the
    # evaluated action is still returned, and no next Run is created here.
    _, task = _assigned(conn)
    updated_before = store.get_task(conn, task.id).updated_at
    run = _drive_to_review(conn, ws, task)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="changes_requested",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    assert done.action.action is ActionType.RUN
    assert done.action.step == "implementation"
    assert done.action.reason == "changes_requested"
    assert done.task.status is TaskStatus.ACTIVE
    after = store.get_task(conn, task.id)
    assert after.status is TaskStatus.ACTIVE
    assert after.updated_at == updated_before
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 3  # artifact.created, result.created, run.completed
    )
    assert LifecycleEventType.TASK_STATUS_CHANGED not in _event_types(conn, task.id, 3)
    assert len(store.list_runs_for_task(conn, task.id)) == 4


def test_rejected_then_corrected_succeeds(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(decision="ready"),
        )
    assert exc_info.value.code == "RequiredArtifactsMissing"
    done = complete(
        conn,
        ws,
        run_id=run_input.run_id,
        request=CompletionRequest(
            decision="ready",
            artifacts=(_submit("requirements.md", "requirements"),),
        ),
    )
    assert done.run.status is RunStatus.COMPLETED
    assert done.result.outcome.decision == "ready"


# --- lifecycle consequences (SF-24) ------------------------------------------


def test_human_action_parks_task_and_blocks_resolve(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="human_required",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    assert done.action.action is ActionType.HUMAN
    assert done.action.reason == "human_required"
    assert done.task.status is TaskStatus.WAITING_FOR_HUMAN
    after = store.get_task(conn, task.id)
    assert after.status is TaskStatus.WAITING_FOR_HUMAN
    assert after.updated_at > task.updated_at
    assert sorted(_event_types(conn, task.id, 4)) == sorted(
        [
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
            LifecycleEventType.TASK_STATUS_CHANGED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 4
    )
    (status_event,) = [
        e
        for e in store.list_lifecycle_events_for_task(conn, task.id)
        if e.type is LifecycleEventType.TASK_STATUS_CHANGED
    ]
    assert status_event.run_id == run.id
    assert dict(status_event.payload) == {
        "from": "active",
        "to": "waiting_for_human",
        "action": "human",
        "reason": "human_required",
    }
    assert len(store.list_runs_for_task(conn, task.id)) == 4
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "HumanDecisionRequired"


def test_complete_action_finishes_task_and_blocks_resolve(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="approved",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    assert done.action.action is ActionType.COMPLETE
    assert done.task.status is TaskStatus.COMPLETED
    assert store.get_task(conn, task.id).status is TaskStatus.COMPLETED
    assert LifecycleEventType.TASK_STATUS_CHANGED in _event_types(conn, task.id, 4)
    assert len(store.list_runs_for_task(conn, task.id)) == 4
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "TaskAlreadyCompleted"


def test_skill_targeted_action_leaves_task_active(conn, ws, workflows):
    # `fundamental_assumption_wrong` evaluates to a skill-targeted `run`: the
    # skill rides on the returned action, and the Task consequence is silent.
    _, task = _assigned(conn)
    updated_before = store.get_task(conn, task.id).updated_at
    run = _drive_to_review(conn, ws, task)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="fundamental_assumption_wrong",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    assert done.action.action is ActionType.RUN
    assert done.action.skill == "research"
    assert done.action.step is None
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE
    assert store.get_task(conn, task.id).updated_at == updated_before
    assert LifecycleEventType.TASK_STATUS_CHANGED not in _event_types(conn, task.id, 3)
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 3
    )
    assert len(store.list_runs_for_task(conn, task.id)) == 4


def _abortable_task(conn, workflows):
    """A Task on a minimal workflow whose only outcome cancels the Task.

    No step of the reference workflow maps an outcome to `cancel` (it appears
    only in `decisions`), so the cancel consequence is exercised here instead.
    """
    (workflows / "abortable.yaml").write_text(
        "name: abortable\n"
        "steps:\n"
        "  - id: only\n"
        "    skill: do-it\n"
        "    outcomes:\n"
        "      abort: { action: cancel }\n",
        encoding="utf-8",
    )
    workflow = load_workflow(workflows / "abortable.yaml")
    register_workflow(conn, workflow)
    return create_task(conn, title="Abortable", workflow_definition_id="abortable")


def test_cancel_action_cancels_task(conn, ws, workflows):
    task = _abortable_task(conn, workflows)
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "only"
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    done = complete(
        conn,
        ws,
        run_id=run_input.run_id,
        request=CompletionRequest(decision="abort"),
    )
    assert done.action.action is ActionType.CANCEL
    assert done.action.reason == "abort"
    assert done.task.status is TaskStatus.CANCELLED
    assert store.get_task(conn, task.id).status is TaskStatus.CANCELLED
    assert sorted(_event_types(conn, task.id, 3)) == sorted(
        [
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
            LifecycleEventType.TASK_STATUS_CHANGED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 3
    )
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_evaluate_is_called_exactly_once_with_completed_snapshots(
    conn, ws, workflows, monkeypatch
):
    # Patched where `complete_run` looks it up: its local `evaluate` binding.
    calls = []
    real_evaluate = complete_run_pkg.evaluate

    def counting(evaluation):
        calls.append(evaluation)
        return real_evaluate(evaluation)

    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    monkeypatch.setattr(complete_run_pkg, "evaluate", counting)
    complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="approved",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    (evaluation,) = calls
    assert evaluation.task.id == task.id
    assert evaluation.current_run.status is RunStatus.COMPLETED
    assert evaluation.result.run_id == run.id
    assert evaluation.result.outcome.decision == "approved"


def test_skill_targeted_completion_never_evaluates(conn, ws, workflows, monkeypatch):
    def boom(_evaluation):
        raise AssertionError("evaluate() must not run for a skill-targeted Run")

    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)
    first = store.get_run(conn, run_input.run_id)
    complete(
        conn,
        ws,
        run_id=first.id,
        request=CompletionRequest(
            decision="ready",
            artifacts=(_submit("requirements.md", "requirements"),),
        ),
    )
    skill_run = create_run(
        conn,
        task_id=task.id,
        action=EvaluationOutput(
            action=ActionType.RUN, reason="research", skill="research"
        ),
        triggered_by_run_id=first.id,
    )
    monkeypatch.setattr(complete_run_pkg, "evaluate", boom)
    done = complete(conn, ws, run_id=skill_run.id, request=CompletionRequest())
    assert done.action is None


def test_task_consequence_failure_rolls_everything_back(
    conn, ws, workflows, monkeypatch
):
    # The Task consequence joins the single write block: a failure there rolls
    # back the Result, the Run completion, the Task update, the events, the
    # artifact rows -- and unlinks the artifact files this attempt wrote.
    real_apply = complete_run_pkg.apply_lifecycle_action

    def boom(conn, **kwargs):
        real_apply(conn, **kwargs)  # the Task write really happens first
        raise RuntimeError("consequence write failed")

    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    monkeypatch.setattr(complete_run_pkg, "apply_lifecycle_action", boom)
    files_before = sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(RuntimeError, match="consequence write failed"):
        complete(
            conn,
            ws,
            run_id=run.id,
            request=CompletionRequest(
                decision="approved",
                artifacts=(_submit("review.md", "review"),),
            ),
        )
    assert _snapshot(conn, task.id, run.id) == before
    assert sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file()) == (
        files_before
    )
