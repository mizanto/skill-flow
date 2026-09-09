"""Tests for ``skillflow.resolve_task`` (SF-20).

Following ``test_service.py``'s shape: a ``tmp_path`` workspace fixture with
a ``.git`` marker, ``store.open_store``, and the **real**
``workflows/software-change.yaml`` copied into ``<root>/workflows/``.

* **Contract change-detectors** -- the public surface, ``ResolveTaskError``
  (type and ``code`` attribute), and the AST import boundary (no Claude Code
  launch of any kind).
* **Behaviour tests** -- preconditions (each proving the rejection code and
  that no Run was written), Workflow selection, initial resolution,
  subsequent resolution through ``evaluate``, and Workflow identity.
"""

import ast
import contextlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from skillflow import resolve_task, store, workspace
from skillflow.artifacts import create_artifact
from skillflow.domain import (
    HumanDecision,
    Outcome,
    Result,
    ResultStatus,
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import (
    EvaluationError,
    EvaluationOutput,
    WorkflowSelectionRequiredError,
    resolve_initial_action,
)
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import (
    WorkflowAssignmentError,
    create_run,
    create_task,
    register_workflow,
)
from skillflow.workflow import ActionType, Workflow, WorkflowStep
from skillflow.workflow_loader import WorkflowLoadError, load_workflow

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


def _artifact(conn, ws, run, *, name, type):
    return create_artifact(
        conn, ws, run_id=run.id, name=name, type=type, content=f"{name} content"
    )


def _set_status(conn, task, status):
    updated = replace(task, status=status, updated_at=task.updated_at)
    with conn:
        store.update_task(conn, updated)
    return store.get_task(conn, task.id)


def _drive_to_review(conn, ws, task):
    """Complete requirements/decomposition/implementation as ready.

    Return the completed review Run, ready for its Result to be seeded.
    """
    for step in ("requirements", "decomposition", "implementation"):
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == step
        run = store.get_run(conn, run_input.run_id)
        _result(conn, _complete(conn, run), decision="ready", type=step)
    review_input = resolve(conn, ws, task_id=task.id)
    assert review_input.step_id == "review"
    return _complete(conn, store.get_run(conn, review_input.run_id))


def _runs(conn):
    return conn.execute("SELECT * FROM runs").fetchall()


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(resolve_task.__all__) == {"ResolveTaskError", "resolve_task"}


def test_resolve_task_error_carries_code():
    err = ResolveTaskError("TaskNotFound", "no task")
    assert isinstance(err, Exception)
    assert err.code == "TaskNotFound"
    assert str(err) == "no task"


def test_module_imports_are_within_the_boundary():
    # resolve-task orchestrates but never launches: no subprocess, no shell,
    # no filesystem writes of its own -- the same mechanical guarantee the
    # context/outputs/artifacts modules pin.
    source = Path(resolve_task.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {"sqlite3", "skillflow"}, f"unexpected imports: {modules}"
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


# --- preconditions --------------------------------------------------------


def test_missing_task_rejects_with_task_not_found(conn, ws, workflows):
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id="task-nope")
    assert exc_info.value.code == "TaskNotFound"
    assert _runs(conn) == []


def test_completed_task_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    task = _set_status(conn, task, TaskStatus.COMPLETED)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "TaskAlreadyCompleted"
    assert _runs(conn) == []


def test_cancelled_task_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    task = _set_status(conn, task, TaskStatus.CANCELLED)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "TaskCancelled"
    assert _runs(conn) == []


def test_waiting_for_human_task_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    task = _set_status(conn, task, TaskStatus.WAITING_FOR_HUMAN)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "HumanDecisionRequired"
    assert "/skillflow:decide" in str(exc_info.value)
    assert _runs(conn) == []


def test_running_run_is_rejected(conn, ws, workflows):
    workflow, task = _assigned(conn)
    first = create_run(
        conn, task_id=task.id, action=resolve_initial_action(task, workflow)
    )
    assert first.status is RunStatus.RUNNING
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "ActiveRunExists"
    assert [r["id"] for r in _runs(conn)] == [first.id]


def test_task_status_takes_precedence_over_active_run(conn, ws, workflows):
    # A cancelled Task that also has a running Run reports the Task status
    # first (pipeline order §3: step 2 before step 3).
    workflow, task = _assigned(conn)
    create_run(
        conn, task_id=task.id, action=resolve_initial_action(task, workflow)
    )
    _set_status(conn, task, TaskStatus.CANCELLED)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "TaskCancelled"


# --- Workflow selection ---------------------------------------------------


def test_unassigned_without_workflow_lists_available(conn, ws, workflows):
    task = create_task(conn, title="Unscoped")
    with pytest.raises(WorkflowSelectionRequiredError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert "software-change" in str(exc_info.value)
    # No Run written, no assignment made.
    assert _runs(conn) == []
    assert store.get_task(conn, task.id).workflow_definition_id is None


def test_unassigned_without_workflow_and_empty_directory(conn, ws):
    task = create_task(conn, title="Unscoped")
    with pytest.raises(WorkflowSelectionRequiredError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert "no Workflow Definitions found" in str(exc_info.value)
    assert "workflows" in str(exc_info.value)
    assert _runs(conn) == []


def test_unassigned_with_workflow_assigns_and_resolves(conn, ws, workflows):
    task = create_task(conn, title="Unscoped")
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))

    run_input = resolve(conn, ws, task_id=task.id, workflow="software-change")

    assigned = store.get_task(conn, task.id)
    assert assigned.workflow_definition_id == "software-change"
    assert (
        len(store.list_lifecycle_events_for_task(conn, task.id)) == events_before + 2
    )  # assigned + run.created
    assert run_input.step_id == "requirements"
    assert run_input.run_id.startswith("run-")


def test_unknown_workflow_file_is_rejected_before_any_write(conn, ws, workflows):
    task = create_task(conn, title="Unscoped")
    with pytest.raises(WorkflowLoadError):
        resolve(conn, ws, task_id=task.id, workflow="no-such-definition")
    assert _runs(conn) == []
    assert store.get_task(conn, task.id).workflow_definition_id is None


def test_workflow_differing_from_assignment_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    (workflows / "other.yaml").write_text(
        "name: other\nsteps:\n  - id: only\n    skill: do-it\n", encoding="utf-8"
    )
    with pytest.raises(WorkflowAssignmentError):
        resolve(conn, ws, task_id=task.id, workflow="other")
    # A rejected --workflow writes nothing: no Run, no reassignment, and
    # not even the definition row.
    assert _runs(conn) == []
    assert store.get_task(conn, task.id).workflow_definition_id == "software-change"
    definition_ids = {
        row["id"]
        for row in conn.execute("SELECT id FROM workflow_definitions").fetchall()
    }
    assert definition_ids == {"software-change"}


def test_workflow_equal_to_assignment_is_idempotent(conn, ws, workflows):
    _, task = _assigned(conn)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    run_input = resolve(conn, ws, task_id=task.id, workflow="software-change")
    # No second assignment event: only the run.created event was added.
    assert (
        len(store.list_lifecycle_events_for_task(conn, task.id)) == events_before + 1
    )
    assert run_input.step_id == "requirements"


def test_definition_name_mismatch_names_both(conn, ws, workflows):
    task = create_task(conn, title="Unscoped")
    (workflows / "foo.yaml").write_text(
        "name: bar\nsteps:\n  - id: only\n    skill: do-it\n", encoding="utf-8"
    )
    with pytest.raises(WorkflowLoadError) as exc_info:
        resolve(conn, ws, task_id=task.id, workflow="foo")
    assert "foo" in str(exc_info.value)
    assert "bar" in str(exc_info.value)
    assert _runs(conn) == []


@pytest.mark.parametrize("definition_id", ["../escape", "a/b", "", "   "])
def test_unsafe_definition_ids_are_rejected(conn, ws, workflows, definition_id):
    task = create_task(conn, title="Unscoped")
    with pytest.raises(WorkflowLoadError):
        resolve(conn, ws, task_id=task.id, workflow=definition_id)
    assert _runs(conn) == []
    assert store.get_task(conn, task.id).workflow_definition_id is None


# --- initial resolution ---------------------------------------------------


def test_initial_resolution_creates_exactly_one_running_run(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)

    runs = store.list_runs_for_task(conn, task.id)
    assert len(runs) == 1
    run = runs[0]
    assert run.id == run_input.run_id
    assert run.status is RunStatus.RUNNING
    assert run.trigger_reason == "initial"
    assert run.triggered_by_run_id is None
    assert run.step_id == "requirements"
    assert run.workflow_definition_id == "software-change"


def test_initial_run_input_projection(conn, ws, workflows):
    _, task = _assigned(conn)
    run_input = resolve(conn, ws, task_id=task.id)

    assert run_input.task_id == task.id
    assert run_input.task_title == task.title
    assert run_input.task_description == task.description
    assert run_input.step_id == "requirements"
    assert run_input.skill == "requirements-analysis"
    assert run_input.model == "opus"
    assert run_input.effort == "high"
    assert [(o.type, o.required) for o in run_input.outputs] == [
        ("requirements", True)
    ]
    assert run_input.context.artifacts == ()
    assert run_input.instructions is None


def test_second_resolution_reports_active_run(conn, ws, workflows):
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "ActiveRunExists"
    assert len(store.list_runs_for_task(conn, task.id)) == 1


# --- subsequent resolution ------------------------------------------------


def test_ready_outcome_advances_with_context(conn, ws, workflows):
    _, task = _assigned(conn)
    first = resolve(conn, ws, task_id=task.id).run_id
    first_run = store.get_run(conn, first)
    _artifact(conn, ws, first_run, name="requirements.md", type="requirements")
    _result(conn, _complete(conn, first_run), decision="ready")

    run_input = resolve(conn, ws, task_id=task.id)

    assert run_input.step_id == "decomposition"
    assert run_input.skill == "decomposition"
    second = store.get_run(conn, run_input.run_id)
    assert second.trigger_reason == "ready"
    assert second.triggered_by_run_id == first
    assert [(a.name, a.type) for a in run_input.context.artifacts] == [
        ("requirements.md", "requirements")
    ]


def test_rework_path_selects_all_prior_context(conn, ws, workflows):
    # Drive requirements -> decomposition -> implementation -> review, then a
    # changes_requested Result routes back to implementation carrying every
    # prior artifact as context.
    _, task = _assigned(conn)
    previous_id = None
    for step, outcome in (
        ("requirements", "ready"),
        ("decomposition", "ready"),
        ("implementation", "ready"),
    ):
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == step
        run = store.get_run(conn, run_input.run_id)
        if previous_id is not None:
            assert run.triggered_by_run_id == previous_id
        _artifact(conn, ws, run, name=f"{step}.md", type=step)
        if step == "decomposition":
            _artifact(conn, ws, run, name="plan.md", type="plan")
        _result(conn, _complete(conn, run), decision=outcome, type=step)
        previous_id = run.id

    review_input = resolve(conn, ws, task_id=task.id)
    assert review_input.step_id == "review"
    review = store.get_run(conn, review_input.run_id)
    _artifact(conn, ws, review, name="review.md", type="review")
    _result(conn, _complete(conn, review), decision="changes_requested", type="review")

    rework = resolve(conn, ws, task_id=task.id)
    assert rework.step_id == "implementation"
    run = store.get_run(conn, rework.run_id)
    assert run.trigger_reason == "changes_requested"
    assert run.triggered_by_run_id == review.id
    # The rework Run sees requirements, plan *and* the review artifact.
    assert [(a.name, a.type) for a in rework.context.artifacts] == [
        ("requirements.md", "requirements"),
        ("plan.md", "plan"),
        ("review.md", "review"),
    ]


def test_artifact_versioning_selects_the_chain_head(conn, ws, workflows):
    # Two versions of plan.md: the implementation step declares the plan
    # type, and only the chain head is selected.
    _, task = _assigned(conn)
    first = resolve(conn, ws, task_id=task.id).run_id
    first_run = store.get_run(conn, first)
    _artifact(conn, ws, first_run, name="requirements.md", type="requirements")
    _result(conn, _complete(conn, first_run), decision="ready")
    second = resolve(conn, ws, task_id=task.id)
    assert second.step_id == "decomposition"
    second_run = store.get_run(conn, second.run_id)
    _artifact(conn, ws, second_run, name="plan.md", type="plan")
    _artifact(conn, ws, second_run, name="plan.md", type="plan")
    _result(conn, _complete(conn, second_run), decision="ready", type="plan")

    run_input = resolve(conn, ws, task_id=task.id)

    assert run_input.step_id == "implementation"
    assert [(a.name, a.version) for a in run_input.context.artifacts] == [
        ("requirements.md", 1),
        ("plan.md", 2),
    ]


def test_human_decision_uses_the_decisions_table(conn, ws, workflows):
    # A request_changes decision on the review Run routes via decisions, not
    # outcomes, with the decision key as the trigger reason.
    _, task = _assigned(conn)
    for step, outcome in (
        ("requirements", "ready"),
        ("decomposition", "ready"),
        ("implementation", "ready"),
    ):
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == step
        run = store.get_run(conn, run_input.run_id)
        _artifact(conn, ws, run, name=f"{step}.md", type=step)
        _result(conn, _complete(conn, run), decision=outcome, type=step)
    review_input = resolve(conn, ws, task_id=task.id)
    assert review_input.step_id == "review"
    review_run = _complete(conn, store.get_run(conn, review_input.run_id))
    _result(conn, review_run, decision="changes_requested", type="review")
    decision = HumanDecision(
        id="decision-1",
        task_id=task.id,
        run_id=review_run.id,
        decision="request_changes",
        created_at=review_run.completed_at,
    )
    with conn:
        store.insert_human_decision(conn, decision)

    rerun = resolve(conn, ws, task_id=task.id)

    assert rerun.step_id == "implementation"
    run = store.get_run(conn, rerun.run_id)
    assert run.trigger_reason == "request_changes"
    assert run.triggered_by_run_id == review_run.id


def test_terminal_action_is_rejected_without_a_write(conn, ws, workflows):
    # review/approved maps to complete: while the Task is still active there
    # is no Run to create.
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _result(conn, review, decision="approved", type="review")

    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "NoLifecycleAction"
    assert len(store.list_runs_for_task(conn, task.id)) == 4


def test_outcome_less_step_completion_is_rejected_without_a_write(
    conn, ws, workflows
):
    # SF-22: a step declaring no outcome rules is terminal -- completing its
    # Run without an outcome resolves to `complete`, so resolve-task raises
    # NoLifecycleAction (previously EvaluationError propagated).
    (workflows / "single.yaml").write_text(
        "name: single\nsteps:\n  - id: only\n    skill: do-it\n",
        encoding="utf-8",
    )
    workflow = load_workflow(workflows / "single.yaml")
    register_workflow(conn, workflow)
    task = create_task(conn, title="One step", workflow_definition_id="single")

    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "only"
    run = store.get_run(conn, run_input.run_id)
    _result(conn, _complete(conn, run), decision=None)

    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "NoLifecycleAction"
    assert "'complete'" in str(exc_info.value)
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_human_action_points_at_decide(conn, ws, workflows):
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _result(conn, review, decision="human_required", type="review")

    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "NoLifecycleAction"
    assert "/skillflow:decide" in str(exc_info.value)
    assert len(store.list_runs_for_task(conn, task.id)) == 4


def test_skill_targeted_action_creates_a_skill_run(conn, ws, workflows):
    # SF-32: review/fundamental_assumption_wrong targets a skill, and the
    # research Run is now created with the review's artifacts as context.
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _artifact(conn, ws, review, name="review.md", type="review")
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")

    run_input = resolve(conn, ws, task_id=task.id)

    assert run_input.step_id is None
    assert run_input.skill == "research"
    assert run_input.model is None
    assert run_input.effort is None
    assert run_input.outputs == ()
    assert [a.name for a in run_input.context.artifacts] == ["review.md"]
    assert run_input.context.unresolved == ()
    run = store.get_run(conn, run_input.run_id)
    assert run.step_id is None
    assert run.status is RunStatus.RUNNING
    assert run.triggered_by_run_id == review.id
    assert run.trigger_reason == "fundamental_assumption_wrong"
    assert len(store.list_runs_for_task(conn, task.id)) == 5


def test_resolve_after_skill_run_with_outcome_routes_to_step(conn, ws, workflows):
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")
    skill_input = resolve(conn, ws, task_id=task.id)
    assert skill_input.step_id is None
    skill_run = store.get_run(conn, skill_input.run_id)
    _result(conn, _complete(conn, skill_run), decision="replan", type="review")

    run_input = resolve(conn, ws, task_id=task.id)

    assert run_input.step_id == "decomposition"
    run = store.get_run(conn, run_input.run_id)
    assert run.triggered_by_run_id == skill_run.id
    assert run.trigger_reason == "replan"


def test_resolve_after_decisionless_skill_run_rejected_without_a_write(
    conn, ws, workflows
):
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")
    skill_input = resolve(conn, ws, task_id=task.id)
    skill_run = store.get_run(conn, skill_input.run_id)
    _result(conn, _complete(conn, skill_run), decision=None)
    with pytest.raises(EvaluationError):
        resolve(conn, ws, task_id=task.id)
    assert len(store.list_runs_for_task(conn, task.id)) == 5


def test_resolve_after_skill_targeting_skill_outcome_rejected(conn, ws, workflows):
    # Hand-seeded only: complete-run rejects skill-targeting decisions, so
    # resolve-task agrees by re-deriving the same EvaluationError.
    _, task = _assigned(conn)
    review = _drive_to_review(conn, ws, task)
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")
    skill_input = resolve(conn, ws, task_id=task.id)
    skill_run = store.get_run(conn, skill_input.run_id)
    _result(
        conn,
        _complete(conn, skill_run),
        decision="fundamental_assumption_wrong",
        type="review",
    )
    with pytest.raises(EvaluationError, match="must not target another skill"):
        resolve(conn, ws, task_id=task.id)
    assert len(store.list_runs_for_task(conn, task.id)) == 5


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED])
def test_non_completed_latest_run_is_rejected(conn, ws, workflows, status):
    _, task = _assigned(conn)
    first = resolve(conn, ws, task_id=task.id).run_id
    run = store.get_run(conn, first)
    done = replace(
        run, status=status, completed_at=run.started_at + timedelta(seconds=1)
    )
    with conn:
        store.update_run(conn, done)
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "RunNotCompleted"
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_completed_run_without_result_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    first = resolve(conn, ws, task_id=task.id).run_id
    _complete(conn, store.get_run(conn, first))
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "ResultMissing"
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_unknown_outcome_propagates_without_a_write(conn, ws, workflows):
    _, task = _assigned(conn)
    first = resolve(conn, ws, task_id=task.id).run_id
    _result(conn, _complete(conn, store.get_run(conn, first)), decision="bogus")
    with pytest.raises(EvaluationError):
        resolve(conn, ws, task_id=task.id)
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_run_naming_an_absent_step_is_rejected(conn, ws, workflows):
    # The definition changed under a live Task: the latest Run's step is
    # gone, so evaluate (rule 3) rejects rather than inventing a transition.
    _, task = _assigned(conn)
    stray = create_run(
        conn,
        task_id=task.id,
        action=EvaluationOutput(action=ActionType.RUN, reason="initial", step="gone"),
    )
    _result(conn, _complete(conn, stray), decision="ready")
    with pytest.raises(EvaluationError):
        resolve(conn, ws, task_id=task.id)
    assert len(store.list_runs_for_task(conn, task.id)) == 1


# --- Workflow identity ----------------------------------------------------


def test_tampered_workflow_id_fails_before_evaluation(conn, ws, workflows):
    _, task = _assigned(conn)
    register_workflow(
        conn, Workflow(name="other-id", steps=(WorkflowStep(id="only", skill="s"),))
    )
    with conn:
        store.update_task(
            conn, replace(task, workflow_definition_id="other-id")
        )
    with pytest.raises(WorkflowLoadError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert "other-id" in str(exc_info.value)
    # The Task row is untouched by the failed resolution and no Run exists.
    assert store.get_task(conn, task.id).workflow_definition_id == "other-id"
    assert _runs(conn) == []


def test_definition_deleted_after_assignment_is_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    (workflows / "software-change.yaml").unlink()
    with pytest.raises(WorkflowLoadError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert "software-change.yaml" in str(exc_info.value)
    assert _runs(conn) == []
