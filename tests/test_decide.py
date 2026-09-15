"""Tests for ``skillflow.decide`` (SF-27).

Following ``test_complete_run.py``'s shape: a ``tmp_path`` workspace fixture
with a ``.git`` marker, ``store.open_store``, and the frozen
``runtime-reference/software-change.yaml`` copied into ``<root>/workflows/``. Tasks
are parked in ``waiting_for_human`` with the real ``resolve_task(...)`` /
``complete_run(...)`` wherever the run history matters, and parked directly
via ``store.update_task`` where only the Task row matters (resolution and
status gates read nothing else).

* **Contract change-detectors** -- the public surface, ``DecideError`` (type
  and ``code`` attribute), the ``DecisionRecord`` validation, the AST import
  boundary (no filesystem or process access of any kind -- a decision writes
  rows only), the banned-concept scan, and the presence of the
  ``decisions`` / ``evaluator`` / ``service`` imports (the mechanical form of
  "validation, Lifecycle Evaluation, and the shared Task consequence happen
  here").
* **Behaviour tests** -- every rejection path proving the snapshot is
  byte-identical afterwards (the mechanical form of "a rejection leaves the
  Task ``waiting_for_human`` with the Result unchanged and nothing written"),
  and every success path proving the single atomic write (decision persisted,
  exactly two events, Task consequence applied, Result byte-identical, no
  next Run).
"""

import ast
import contextlib
import dataclasses
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import decide as decide_pkg
from skillflow import store, workspace
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.decide import DecideError, DecisionRecord
from skillflow.decide import decide as decide_cmd
from skillflow.decisions import DecisionError, DecisionRequest
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    HumanDecision,
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import EvaluationError, EvaluationOutput
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


def _submit(name, type):
    return ArtifactSubmission(name=name, type=type, content=f"# {name}")


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


def _parked(conn, ws, task):
    """Park ``task`` with a real ``human_required`` review outcome.

    Return the completed review Run.
    """
    run = _drive_to_review(conn, ws, task)
    complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="human_required",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    parked = store.get_task(conn, task.id)
    assert parked.status is TaskStatus.WAITING_FOR_HUMAN
    return store.get_run(conn, run.id)


def _parked_by_skill(conn, ws, task):
    """Park ``task`` with a real ``human_required`` research outcome.

    Review completes as fundamental_assumption_wrong, the research Run
    completes as human_required. Return the completed skill Run.
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
    assert done.action.skill == "research"
    skill_run = create_run(
        conn,
        task_id=task.id,
        action=done.action,
        triggered_by_run_id=review.id,
    )
    complete(
        conn,
        ws,
        run_id=skill_run.id,
        request=CompletionRequest(decision="human_required"),
    )
    parked = store.get_task(conn, task.id)
    assert parked.status is TaskStatus.WAITING_FOR_HUMAN
    return store.get_run(conn, skill_run.id)


def _waiting_task(conn, *, title="Ship it"):
    """Create a Task and park it directly (no Runs).

    Only for gates that read the Task row alone: resolution and status.
    """
    task = create_task(conn, title=title)
    parked = replace(
        task, status=TaskStatus.WAITING_FOR_HUMAN, updated_at=datetime.now(UTC)
    )
    with conn:
        store.update_task(conn, parked)
    return store.get_task(conn, task.id)


def _stored_run(conn, task_id, run_id, **over):
    kw = dict(
        id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        created_at=datetime.now(UTC),
        trigger_reason=TRIGGER_REASON_INITIAL,
        workflow_definition_id="software-change",
        step_id="review",
    )
    run = Run(**(kw | over))
    if run.workflow_definition_id is not None:
        # `runs.workflow_definition_id` is a foreign key: the reference
        # definition row must exist. Idempotent across calls.
        register_workflow(conn, load_workflow(REFERENCE))
    with conn:
        store.insert_run(conn, run)
    return store.get_run(conn, run.id)


def _stored_result(conn, run_id, outcome=None):
    result = Result(
        id=f"result-{run_id}",
        run_id=run_id,
        status=ResultStatus.COMPLETED,
        created_at=datetime.now(UTC),
        outcome=outcome,
    )
    with conn:
        store.insert_result(conn, result)
    return result


def _snapshot(conn, task_id):
    return (
        store.get_task(conn, task_id),
        store.list_human_decisions_for_task(conn, task_id),
        store.list_lifecycle_events_for_task(conn, task_id),
        len(store.list_runs_for_task(conn, task_id)),
    )


def _event_types(conn, task_id, count):
    events = store.list_lifecycle_events_for_task(conn, task_id)
    return [event.type for event in events[-count:]]


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(decide_pkg.__all__) == {
        "DecideError",
        "DecisionRecord",
        "decide",
        "resolve_waiting_step",
    }


def test_decide_error_carries_code():
    err = DecideError("TaskNotFound", "no task")
    assert isinstance(err, Exception)
    assert err.code == "TaskNotFound"
    assert str(err) == "no task"


def test_decision_record_fields_match_contract():
    assert {f.name for f in dataclasses.fields(DecisionRecord)} == {
        "decision",
        "task",
        "action",
    }


def _valid_decision_task_and_action():
    now = datetime.now(UTC)
    decision = HumanDecision(
        id="decision-1",
        task_id="task-1",
        run_id="run-1",
        decision="approve",
        created_at=now,
    )
    task = Task(
        id="task-1",
        title="Ship it",
        description="",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    action = EvaluationOutput(action=ActionType.COMPLETE, reason="approve")
    return decision, task, action


def test_decision_record_is_frozen_slotted_keyword_only():
    decision, task, action = _valid_decision_task_and_action()
    params = DecisionRecord.__dataclass_params__
    assert params.frozen and params.kw_only
    instance = DecisionRecord(decision=decision, task=task, action=action)
    assert instance.decision is decision
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.task = task


def test_decision_record_rejects_bad_members():
    decision, task, action = _valid_decision_task_and_action()
    with pytest.raises(ValueError, match="must be a HumanDecision"):
        DecisionRecord(decision="decision-1", task=task, action=action)
    with pytest.raises(ValueError, match="must be a Task"):
        DecisionRecord(decision=decision, task="task-1", action=action)
    with pytest.raises(ValueError, match="must be an EvaluationOutput"):
        DecisionRecord(decision=decision, task=task, action="complete")


def test_module_imports_are_within_the_boundary():
    source = Path(decide_pkg.__file__).read_text()
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
        for name in vars(decide_pkg)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_module_validates_evaluates_and_applies_via_siblings():
    # The mechanical form of "validation, Lifecycle Evaluation, and the
    # shared Task consequence happen here, not reimplemented here".
    source = Path(decide_pkg.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "skillflow.decisions" in imported
    assert "skillflow.evaluator" in imported
    assert "skillflow.service" in imported


# --- rejection paths (nothing written) --------------------------------------


def test_no_waiting_task_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(conn, ws, request=DecisionRequest(decision="approve"))
    assert exc_info.value.code == "HumanDecisionNotExpected"
    assert _snapshot(conn, task.id) == before


def test_two_waiting_tasks_rejected_without_task(conn, ws, workflows):
    first = _waiting_task(conn, title="First")
    second = _waiting_task(conn, title="Second")
    before_first = _snapshot(conn, first.id)
    before_second = _snapshot(conn, second.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(conn, ws, request=DecisionRequest(decision="approve"))
    assert exc_info.value.code == "AmbiguousCurrentTask"
    assert first.id in str(exc_info.value)
    assert second.id in str(exc_info.value)
    assert _snapshot(conn, first.id) == before_first
    assert _snapshot(conn, second.id) == before_second


def test_unknown_task_rejected(conn, ws, workflows):
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id="task-nope", request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "TaskNotFound"


def test_active_task_rejected_before_any_run_check(conn, ws, workflows):
    # No Runs at all: the status gate fires before the Run gate.
    _, task = _assigned(conn)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "HumanDecisionNotExpected"
    assert _snapshot(conn, task.id) == before


def test_completed_task_rejected(conn, ws, workflows):
    # Terminal status reached through the real flow, not raw SQL.
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="approved", artifacts=(_submit("review.md", "review"),)
        ),
    )
    assert store.get_task(conn, task.id).status is TaskStatus.COMPLETED
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "TaskAlreadyCompleted"
    assert _snapshot(conn, task.id) == before


def test_cancelled_task_rejected(conn, ws, workflows):
    # The reference workflow reaches 'cancelled' only through decide itself,
    # so the status gate is parked directly -- it reads the Task row alone.
    _, task = _assigned(conn)
    with conn:
        store.update_task(
            conn,
            replace(task, status=TaskStatus.CANCELLED, updated_at=datetime.now(UTC)),
        )
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="cancel")
        )
    assert exc_info.value.code == "TaskCancelled"
    assert _snapshot(conn, task.id) == before


def test_waiting_task_without_runs_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "RunNotFound"
    assert _snapshot(conn, task.id) == before


def test_latest_run_running_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", status=RunStatus.RUNNING)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "RunNotCompleted"
    assert _snapshot(conn, task.id) == before


def test_completed_run_without_result_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1")
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "ResultMissing"
    assert _snapshot(conn, task.id) == before


def test_run_without_workflow_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", workflow_definition_id=None, step_id=None)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "StepUnresolved"
    assert _snapshot(conn, task.id) == before


def test_skill_targeted_run_without_result_rejected(conn, ws, workflows):
    # SF-32 flips the code: a stepless Run's step now resolves from its
    # Result, so a missing Result is ResultMissing -- uniform with step Runs.
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", step_id=None)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "ResultMissing"
    assert _snapshot(conn, task.id) == before


def test_skill_targeted_run_without_outcome_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", step_id=None)
    _stored_result(conn, "run-1", outcome=None)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "StepUnresolved"
    assert "no outcome" in str(exc_info.value)
    assert _snapshot(conn, task.id) == before


def test_skill_targeted_run_outcome_naming_absent_step_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", step_id=None)
    _stored_result(conn, "run-1", outcome=Outcome(type="ghost", decision="approve"))
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "WorkflowMismatch"
    assert "'ghost'" in str(exc_info.value)
    assert _snapshot(conn, task.id) == before


def test_decision_on_skill_run_approve_completes_task(conn, ws, workflows):
    # SF-32: the decision validates against the routed review step's table.
    _, task = _assigned(conn)
    skill_run = _parked_by_skill(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
    )
    assert record.action.action is ActionType.COMPLETE
    assert record.task.status is TaskStatus.COMPLETED
    assert store.get_task(conn, task.id).status is TaskStatus.COMPLETED
    assert record.decision.run_id == skill_run.id
    assert record.decision.decision == "approve"


def test_decision_on_skill_run_request_changes_reactivates(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked_by_skill(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="request_changes")
    )
    assert record.action.action is ActionType.RUN
    assert (record.action.step, record.action.skill) == ("implementation", None)
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "implementation"


def test_invalid_decision_on_skill_run_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked_by_skill(conn, ws, task)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecisionError) as exc_info:
        decide_cmd(conn, ws, task_id=task.id, request=DecisionRequest(decision="bogus"))
    assert exc_info.value.code == "InvalidHumanDecision"
    assert _snapshot(conn, task.id) == before


def test_skill_targeting_decision_on_skill_run_rejected(conn, ws, workflows):
    # Approver decision 5: no command path may resolve skill→skill.
    (workflows / "custom.yaml").write_text(
        "name: custom\n"
        "\n"
        "steps:\n"
        "  - id: probe\n"
        "    skill: probe\n"
        "    outcomes:\n"
        "      stuck: { action: human }\n"
        "      divert: { action: run, skill: research }\n"
        "    decisions:\n"
        "      again: { action: run, skill: research }\n",
        encoding="utf-8",
    )
    register_workflow(conn, load_workflow(workflows / "custom.yaml"))
    task = create_task(conn, title="Custom", workflow_definition_id="custom")
    probe_input = resolve(conn, ws, task_id=task.id)
    assert probe_input.step_id == "probe"
    done = complete(
        conn,
        ws,
        run_id=probe_input.run_id,
        request=CompletionRequest(decision="divert"),
    )
    assert done.action.skill == "research"
    skill_run = create_run(
        conn,
        task_id=task.id,
        action=done.action,
        triggered_by_run_id=probe_input.run_id,
    )
    complete(
        conn,
        ws,
        run_id=skill_run.id,
        request=CompletionRequest(decision="stuck"),
    )
    assert store.get_task(conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    before = _snapshot(conn, task.id)
    with pytest.raises(EvaluationError, match="must not target another skill"):
        decide_cmd(conn, ws, task_id=task.id, request=DecisionRequest(decision="again"))
    assert _snapshot(conn, task.id) == before


def test_step_absent_from_definition_rejected(conn, ws, workflows):
    task = _waiting_task(conn)
    _stored_run(conn, task.id, "run-1", step_id="no-such-step")
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "WorkflowMismatch"
    assert _snapshot(conn, task.id) == before


def test_invalid_decision_rejected_with_result_unchanged(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    result_before = store.get_result_for_run(conn, run.id)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecisionError) as exc_info:
        decide_cmd(conn, ws, task_id=task.id, request=DecisionRequest(decision="maybe"))
    assert exc_info.value.code == "InvalidHumanDecision"
    assert _snapshot(conn, task.id) == before
    assert store.get_result_for_run(conn, run.id) == result_before
    assert store.get_task(conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN


def test_outcome_key_rejected_as_decision(conn, ws, workflows):
    # "approved" is an `outcomes` key of the review step, not a `decisions`
    # key -- the two tables are separate (SF-A-5 §7.3).
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    before = _snapshot(conn, task.id)
    with pytest.raises(DecisionError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approved")
        )
    assert exc_info.value.code == "InvalidHumanDecision"
    assert _snapshot(conn, task.id) == before


def test_non_decision_request_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    with pytest.raises(ValueError, match="must be a DecisionRequest"):
        decide_cmd(conn, ws, task_id=task.id, request="approve")


# --- success paths (one atomic write) ---------------------------------------


def test_approve_completes_task(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    result_before = store.get_result_for_run(conn, run.id)
    events_before = len(store.list_lifecycle_events_for_task(conn, task.id))
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
    )
    assert record.action.action is ActionType.COMPLETE
    assert record.action.reason == "approve"
    assert record.task.status is TaskStatus.COMPLETED
    assert record.task.id == task.id
    assert record.decision.task_id == task.id
    assert record.decision.run_id == run.id
    assert record.decision.decision == "approve"
    assert record.decision.comment is None
    (stored,) = store.list_human_decisions_for_task(conn, task.id)
    assert stored == record.decision
    after = store.get_task(conn, task.id)
    assert after.status is TaskStatus.COMPLETED
    assert after.updated_at > task.updated_at
    # Exactly two events: the decision and its Task consequence.
    assert sorted(_event_types(conn, task.id, 2)) == sorted(
        [
            LifecycleEventType.HUMAN_DECISION_MADE,
            LifecycleEventType.TASK_STATUS_CHANGED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 2
    )
    (made,) = [
        e
        for e in store.list_lifecycle_events_for_task(conn, task.id)
        if e.type is LifecycleEventType.HUMAN_DECISION_MADE
    ]
    assert made.run_id == run.id
    assert dict(made.payload) == {
        "decision_id": record.decision.id,
        "decision": "approve",
    }
    # The two events share one `now`, so their relative order is by random
    # id -- select the status event from the appended pair, not by position.
    (status_event,) = [
        e
        for e in store.list_lifecycle_events_for_task(conn, task.id)[-2:]
        if e.type is LifecycleEventType.TASK_STATUS_CHANGED
    ]
    assert status_event.run_id == run.id
    assert dict(status_event.payload) == {
        "from": "waiting_for_human",
        "to": "completed",
        "action": "complete",
        "reason": "approve",
    }
    # The previous Result is untouched and no next Run exists.
    assert store.get_result_for_run(conn, run.id) == result_before
    assert len(store.list_runs_for_task(conn, task.id)) == 4
    assert store.get_run(conn, run.id).status is RunStatus.COMPLETED


def test_request_changes_reactivates_task(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="request_changes")
    )
    assert record.action.action is ActionType.RUN
    assert record.action.step == "implementation"
    assert record.action.reason == "request_changes"
    assert record.decision.run_id == run.id
    assert record.task.status is TaskStatus.ACTIVE
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE
    (status_event,) = [
        e
        for e in store.list_lifecycle_events_for_task(conn, task.id)[-2:]
        if e.type is LifecycleEventType.TASK_STATUS_CHANGED
    ]
    assert dict(status_event.payload) == {
        "from": "waiting_for_human",
        "to": "active",
        "action": "run",
        "reason": "request_changes",
    }
    # No next Run is created here.
    assert len(store.list_runs_for_task(conn, task.id)) == 4
    # A second decision is impossible: the Task is no longer waiting.
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "HumanDecisionNotExpected"


def test_cancel_cancels_task(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="cancel")
    )
    assert record.action.action is ActionType.CANCEL
    assert record.task.status is TaskStatus.CANCELLED
    assert store.get_task(conn, task.id).status is TaskStatus.CANCELLED
    assert len(store.list_runs_for_task(conn, task.id)) == 4
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "TaskCancelled"


def test_second_decide_after_approve_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    decide_cmd(conn, ws, task_id=task.id, request=DecisionRequest(decision="approve"))
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="cancel")
        )
    assert exc_info.value.code == "TaskAlreadyCompleted"
    assert _snapshot(conn, task.id) == before


def test_second_decide_after_request_changes_rejected(conn, ws, workflows):
    # `request_changes` parks the Task back to `active`; a second decision is
    # then unexpected, and exactly one decision row exists.
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="request_changes")
    )
    assert record.task.status is TaskStatus.ACTIVE
    before = _snapshot(conn, task.id)
    with pytest.raises(DecideError) as exc_info:
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "HumanDecisionNotExpected"
    assert _snapshot(conn, task.id) == before
    assert len(store.list_human_decisions_for_task(conn, task.id)) == 1


class _CommitFails(sqlite3.Connection):
    """A connection whose transaction block fails at commit time.

    Duplicated from ``test_artifacts.py``: ``sqlite3.Connection.commit`` is
    a read-only C attribute and cannot be monkeypatched, and ``__exit__``
    is where ``with conn:`` commits -- exactly the moment this test needs
    to fail, after the decision row has been written inside the block. An
    exception already in flight from the body is left alone, so only a body
    that ran clean is failed at commit.
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


def test_commit_failure_rolls_back_decision_and_task_update(conn, ws, workflows):
    # The write block is one unit: a commit failure rolls back the decision
    # row, the event and the Task consequence -- the Task is still waiting
    # with the Result unchanged.
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    failing = sqlite3.connect(ws.db_path, factory=_CommitFails)
    failing.row_factory = sqlite3.Row
    failing.execute("PRAGMA foreign_keys = ON")
    before = _snapshot(conn, task.id)
    try:
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            decide_cmd(
                failing,
                ws,
                task_id=task.id,
                request=DecisionRequest(decision="approve"),
            )
    finally:
        # Closing rolls the still-open transaction back and releases the write
        # lock, so the fixture connection can read below.
        failing.close()
    assert _snapshot(conn, task.id) == before
    assert store.get_task(conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
    assert store.list_human_decisions_for_task(conn, task.id) == []


@pytest.mark.parametrize("comment", ["", "  padded  ", "line one\nline two"])
def test_comment_stored_verbatim(conn, ws, workflows, comment):
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    record = decide_cmd(
        conn,
        ws,
        task_id=task.id,
        request=DecisionRequest(decision="request_changes", comment=comment),
    )
    assert record.decision.comment == comment
    (stored,) = store.list_human_decisions_for_task(conn, task.id)
    assert stored.comment == comment


def test_task_id_selects_the_named_waiting_task(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    other = _waiting_task(conn, title="Other")
    before_other = _snapshot(conn, other.id)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
    )
    assert record.task.id == task.id
    assert record.decision.run_id == run.id
    assert _snapshot(conn, other.id) == before_other


# --- lifecycle handoff ------------------------------------------------------


def test_resolve_task_after_request_changes_creates_implementation_run(
    conn, ws, workflows
):
    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="request_changes")
    )
    run_input = resolve(conn, ws, task_id=task.id)
    assert run_input.step_id == "implementation"
    followup = store.get_run(conn, run_input.run_id)
    assert followup.status is RunStatus.RUNNING
    assert followup.triggered_by_run_id == run.id
    assert followup.trigger_reason == "request_changes"


def test_resolve_task_after_approve_is_blocked(conn, ws, workflows):
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    decide_cmd(conn, ws, task_id=task.id, request=DecisionRequest(decision="approve"))
    with pytest.raises(ResolveTaskError) as exc_info:
        resolve(conn, ws, task_id=task.id)
    assert exc_info.value.code == "TaskAlreadyCompleted"


@pytest.mark.parametrize("decision", ["approve", "request_changes", "cancel"])
def test_no_decision_returns_a_human_action(conn, ws, workflows, decision):
    # human -> human is unsupported in v0 (SF-A-5 §7.7): every declared
    # decision moves the Task out of `waiting_for_human`.
    _, task = _assigned(conn)
    _parked(conn, ws, task)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision=decision)
    )
    assert record.action.action is not ActionType.HUMAN
    assert record.task.status is not TaskStatus.WAITING_FOR_HUMAN
    assert store.get_task(conn, task.id).status is not TaskStatus.WAITING_FOR_HUMAN


def test_evaluate_is_called_exactly_once_with_decision_snapshots(
    conn, ws, workflows, monkeypatch
):
    # Patched where `decide` looks it up: its local `evaluate` binding.
    calls = []
    real_evaluate = decide_pkg.evaluate

    def counting(evaluation):
        calls.append(evaluation)
        return real_evaluate(evaluation)

    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    monkeypatch.setattr(decide_pkg, "evaluate", counting)
    record = decide_cmd(
        conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
    )
    (evaluation,) = calls
    assert evaluation.task.id == task.id
    assert evaluation.current_run.id == run.id
    assert evaluation.current_run.status is RunStatus.COMPLETED
    assert evaluation.result.run_id == run.id
    assert evaluation.result.outcome.decision == "human_required"
    assert evaluation.human_decision == record.decision


def test_task_consequence_failure_rolls_everything_back(
    conn, ws, workflows, monkeypatch
):
    # The Task consequence joins the single write block: a failure there rolls
    # back the decision row, the decision event, and the Task update.
    real_apply = decide_pkg.apply_lifecycle_action

    def boom(conn, **kwargs):
        real_apply(conn, **kwargs)  # the Task write really happens first
        raise RuntimeError("consequence write failed")

    _, task = _assigned(conn)
    run = _parked(conn, ws, task)
    result_before = store.get_result_for_run(conn, run.id)
    monkeypatch.setattr(decide_pkg, "apply_lifecycle_action", boom)
    before = _snapshot(conn, task.id)
    with pytest.raises(RuntimeError, match="consequence write failed"):
        decide_cmd(
            conn, ws, task_id=task.id, request=DecisionRequest(decision="approve")
        )
    assert _snapshot(conn, task.id) == before
    assert store.get_result_for_run(conn, run.id) == result_before
    assert store.get_task(conn, task.id).status is TaskStatus.WAITING_FOR_HUMAN
