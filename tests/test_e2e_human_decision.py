"""Human-decision E2E: Review -> human_required -> decide -> next action.

SF-33 (plan ref SF-034, SF-A-4 §14-D, SF-A-5 §7/§10.4): the human-decision
loop exercised as scenarios on the real reference workflow. Every Task
starts at ``steps[0]``, so each scenario opens with the happy-path prefix
(Requirements, Decomposition, Implementation), then parks the Task with a
Review completing as ``human_required``.

Each Run is driven through a freshly opened SQLite connection and only
string ids cross the Run boundary (plus the stateless ``Workspace``
checkout handle, which holds no session state) -- the mechanical form of
"one independent Claude Code session per Run". The ``decide`` step runs
in its own fresh session too: it is a human act, not a Run. Nothing else
in-memory (no ``Task``/``Run``/``RunInput``) may pass from one session to
the next; every command re-resolves its state from SQLite + workspace
files.

The main scenario pins the SF-33 contract end to end:

* the ``human_required`` completion parks the Task as
  ``waiting_for_human``, and ``resolve`` while parked is rejected with
  ``HumanDecisionRequired``;
* an outcome key submitted as a decision is rejected with
  ``InvalidHumanDecision`` -- the ``outcomes`` and ``decisions`` tables
  are separate -- leaving Task, Result, Runs, decisions, and events
  unchanged;
* ``decide request_changes`` records exactly one ``HumanDecision``
  against the review Run without touching its Result, evaluates to a
  rework Run on ``implementation``, and creates no Run itself;
* the rework Run's provenance names the review Run with reason
  ``request_changes`` (SF-A-4 §8) and resolves the v1 review context;
* the second review must produce its own ``review`` artifact despite v1
  existing (per-Run output scope); ``review.md`` v1 -> v2 is one
  immutable version chain across Runs;
* the original Runs remain in history with their canonical Results;
* the terminal tail: Task ``completed`` and no 7th Run.

Two compact branch scenarios pin the remaining declared decisions:
``approve`` completes the Task and ``cancel`` cancels it, each leaving
the parking Result untouched and creating no Run.
"""

import contextlib
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.decide import decide as decide_cmd
from skillflow.decisions import DecisionError, DecisionRequest
from skillflow.domain import RunStatus, TaskStatus
from skillflow.prepare_artifacts import prepare_artifacts as prepare
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

_TEST_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = (
    _TEST_ROOT
    / "tests"
    / "fixtures"
    / "workflows"
    / "runtime-reference"
    / "software-change.yaml"
)

#: One row per Run: step id, completion decision, artifact submissions as
#: (name, type, content), expected resolved context types in declaration
#: order, expected unresolved context types, expected declared outputs as
#: (type, required), expected pre-completion missing required output types,
#: and the expected evaluated next action as (ActionType, step-or-None,
#: reason). Rows 1-4 are the shared park prefix; rows 5-6 continue the
#: main loop after ``decide request_changes``.
STEPS = (
    (
        "requirements",
        "ready",
        (("requirements.md", "requirements", "# requirements\n"),),
        (),
        (),
        (("requirements", True),),
        ("requirements",),
        (ActionType.RUN, "decomposition", "ready"),
    ),
    (
        "decomposition",
        "ready",
        (("plan.md", "plan", "# plan\n"),),
        ("requirements",),
        # `research` resolves only on a post-research re-decomposition
        # (SF-32); on the first pass it is simply unresolved.
        ("research",),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", "ready"),
    ),
    (
        # Declares outcomes but no outputs: the legal payload is an outcome
        # with zero submissions. Its context declares `review`, which no Run
        # has produced yet on the first pass, so it stays unresolved.
        "implementation",
        "ready",
        (),
        ("requirements", "plan"),
        ("review",),
        (),
        (),
        (ActionType.RUN, "review", "ready"),
    ),
    (
        # The review that asks for a human. The outcome parks the Task as
        # `waiting_for_human` -- an ordinary outcome rule, not a parked-Run
        # state: the Run itself completes normally.
        "review",
        "human_required",
        (("review.md", "review", "# review: human required\n"),),
        ("requirements", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.HUMAN, None, "human_required"),
    ),
    (
        # The rework Run after `decide request_changes`: same step, new
        # Run. Its declared `review` context now resolves to the v1 review
        # above -- the review that asked for the human is visible to the
        # rework.
        "implementation",
        "ready",
        (),
        ("requirements", "plan", "review"),
        (),
        (),
        (),
        (ActionType.RUN, "review", "ready"),
    ),
    (
        # v1 exists, but output scope is the current Run: this Run must
        # produce its own `review` artifact, which becomes v2 of the chain.
        "review",
        "approved",
        (("review.md", "review", "# review: approved\n"),),
        ("requirements", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.COMPLETE, None, "approved"),
    ),
)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def workflows(ws):
    directory = ws.root / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / "software-change.yaml").write_text(
        REFERENCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


@contextlib.contextmanager
def _session(ws):
    """Yield a fresh connection: one simulated Claude Code session."""
    with contextlib.closing(store.open_store(ws)) as conn:
        yield conn


def _new_task(ws):
    """Register the reference workflow, create a Task, return its id."""
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id="software-change"
        )
        return task.id


def _drive_run(ws, task_id, row, run_ids, *, extra=None):
    """Drive one STEPS row through a fresh session; append the run id.

    Only ``task_id`` (and the stateless ``ws`` handle) crosses the
    boundary in; only the new run id (a plain string) leaves it.
    ``extra`` is an optional callable taking ``(conn, run_input)`` for
    row-specific assertions inside the session.
    """
    (
        step_id,
        decision,
        submissions,
        context_types,
        unresolved,
        outputs,
        missing,
        (action, next_step, reason),
    ) = row
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task_id)
        assert run_input.step_id == step_id
        assert run_input.task_id == task_id
        seen_types = [a.type for a in run_input.context.artifacts]
        assert seen_types == list(context_types)
        assert run_input.context.unresolved == unresolved
        seen_outputs = [(o.type, o.required) for o in run_input.outputs]
        assert seen_outputs == list(outputs)
        if extra is not None:
            extra(conn, run_input)

        events_before = len(store.list_lifecycle_events_for_task(conn, task_id))
        report = prepare(conn, ws, task_id=task_id)
        assert report.run_id == run_input.run_id
        assert report.step_id == step_id
        actual = tuple(c.type for c in report.validation.missing_required)
        assert actual == missing
        # prepare-artifacts is a read-only inspection: no rows, no events.
        assert len(store.list_lifecycle_events_for_task(conn, task_id)) == events_before

        done = complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision=decision,
                artifacts=tuple(
                    ArtifactSubmission(name=name, type=type, content=body)
                    for name, type, body in submissions
                ),
            ),
        )
        assert done.action is not None
        assert done.action.action is action
        assert done.action.step == next_step
        assert done.action.reason == reason
        assert done.run.status is RunStatus.COMPLETED
        assert done.result.run_id == run_input.run_id
        assert done.result.outcome is not None
        assert (
            done.result.outcome.type,
            done.result.outcome.decision,
        ) == (step_id, decision)
        run_ids.append(run_input.run_id)
        # Only `task_id` / `run_ids` (plain strings) leave the session.


def test_human_decision_loop_to_approved(ws, workflows):
    task_id = _new_task(ws)

    run_ids = []
    for row in STEPS[:4]:
        _drive_run(ws, task_id, row, run_ids)

    # The decide interlude: a human act in its own fresh session, not a Run.
    with _session(ws) as conn:
        parked = store.get_task(conn, task_id)
        assert parked.status is TaskStatus.WAITING_FOR_HUMAN

        result_before = store.get_result_for_run(conn, run_ids[3])
        assert result_before is not None
        events_before = len(store.list_lifecycle_events_for_task(conn, task_id))

        # No Run starts while parked: the Task waits for a decision.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "HumanDecisionRequired"

        # `approved` is an outcome key of this step, not a declared
        # decision: rejected, with nothing written (SF-A-5 §8).
        with pytest.raises(DecisionError) as exc_info:
            decide_cmd(
                conn,
                ws,
                task_id=task_id,
                request=DecisionRequest(decision="approved"),
            )
        assert exc_info.value.code == "InvalidHumanDecision"
        assert store.get_task(conn, task_id).status is TaskStatus.WAITING_FOR_HUMAN
        assert store.list_human_decisions_for_task(conn, task_id) == []
        assert store.get_result_for_run(conn, run_ids[3]) == result_before
        assert len(store.list_runs_for_task(conn, task_id)) == 4
        assert len(store.list_lifecycle_events_for_task(conn, task_id)) == events_before

        record = decide_cmd(
            conn,
            ws,
            task_id=task_id,
            request=DecisionRequest(
                decision="request_changes",
                comment="Need stronger test coverage",
            ),
        )
        assert record.action.action is ActionType.RUN
        assert record.action.step == "implementation"
        assert record.action.skill is None
        assert record.action.reason == "request_changes"
        assert record.task.status is TaskStatus.ACTIVE
        assert record.task.id == task_id
        assert record.decision.task_id == task_id
        assert record.decision.run_id == run_ids[3]
        assert record.decision.decision == "request_changes"
        assert record.decision.comment == "Need stronger test coverage"
        (stored,) = store.list_human_decisions_for_task(conn, task_id)
        assert stored == record.decision
        assert store.get_task(conn, task_id).status is TaskStatus.ACTIVE
        # The decision is recorded *against* the review Run: its Result --
        # the observation of what happened -- is byte-identical.
        assert store.get_result_for_run(conn, run_ids[3]) == result_before
        # And no next Run is created here: the next Run starts via the
        # driver (/skillflow:work) or manual resolve-task.
        assert len(store.list_runs_for_task(conn, task_id)) == 4

    def _check_rework(conn, run_input):
        # The rework resolves the review that asked for the human: the
        # latest `review` artifact, produced by Run 4.
        ctx_artifacts = run_input.context.artifacts
        [review] = [a for a in ctx_artifacts if a.type == "review"]
        assert review.version == 1
        assert review.run_id == run_ids[3]

    _drive_run(ws, task_id, STEPS[4], run_ids, extra=_check_rework)
    _drive_run(ws, task_id, STEPS[5], run_ids)

    # Terminal state, observed through yet another fresh connection.
    with _session(ws) as conn:
        task = store.get_task(conn, task_id)
        assert task.status is TaskStatus.COMPLETED

        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert all(run.status is RunStatus.COMPLETED for run in runs)
        assert [run.step_id for run in runs] == [
            "requirements",
            "decomposition",
            "implementation",
            "review",
            "implementation",
            "review",
        ]
        assert runs[0].triggered_by_run_id is None
        assert runs[0].trigger_reason == "initial"
        expected_reasons = (
            "ready",
            "ready",
            "ready",
            "request_changes",
            "ready",
        )
        for previous, run, expected in zip(
            runs, runs[1:], expected_reasons, strict=False
        ):
            assert run.triggered_by_run_id == previous.id
            assert run.trigger_reason == expected

        for run in runs:
            result = store.get_result_for_run(conn, run.id)
            assert result is not None
            assert result.run_id == run.id
        parked_result = store.get_result_for_run(conn, run_ids[3])
        assert (
            parked_result.outcome.type,
            parked_result.outcome.decision,
        ) == ("review", "human_required")

        artifacts = store.list_artifacts_for_task(conn, task_id)
        assert [(a.name, a.type, a.version) for a in artifacts] == [
            ("requirements.md", "requirements", 1),
            ("plan.md", "plan", 1),
            ("review.md", "review", 1),
            ("review.md", "review", 2),
        ]
        assert [read_content(ws, a) for a in artifacts] == [
            "# requirements\n",
            "# plan\n",
            "# review: human required\n",
            "# review: approved\n",
        ]
        first_review, second_review = artifacts[2], artifacts[3]
        assert first_review.supersedes_id is None
        assert second_review.supersedes_id == first_review.id
        assert first_review.run_id == run_ids[3]
        assert second_review.run_id == run_ids[5]

        # Exactly one Human Decision answered the parked Task.
        (decision,) = store.list_human_decisions_for_task(conn, task_id)
        assert decision.run_id == run_ids[3]
        assert decision.decision == "request_changes"

        # No 7th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"


def _decide_terminal(ws, task_id, run_ids, *, decision, action, status):
    """Record ``decision`` on the parked Task; assert its terminal tail.

    The park prefix (STEPS rows 1-4) is already driven. ``action`` is the
    expected ``ActionType``, ``status`` the expected terminal Task status.
    """
    with _session(ws) as conn:
        assert store.get_task(conn, task_id).status is TaskStatus.WAITING_FOR_HUMAN
        result_before = store.get_result_for_run(conn, run_ids[3])
        assert result_before is not None

        record = decide_cmd(
            conn,
            ws,
            task_id=task_id,
            request=DecisionRequest(decision=decision),
        )
        assert record.action.action is action
        assert record.action.step is None
        assert record.action.reason == decision
        assert record.task.status is status
        assert record.decision.task_id == task_id
        assert record.decision.run_id == run_ids[3]
        assert record.decision.decision == decision
        assert record.decision.comment is None
        (stored,) = store.list_human_decisions_for_task(conn, task_id)
        assert stored == record.decision
        # The parking Result is untouched and no Run is created.
        assert store.get_result_for_run(conn, run_ids[3]) == result_before
        assert len(store.list_runs_for_task(conn, task_id)) == 4

    with _session(ws) as conn:
        assert store.get_task(conn, task_id).status is status
        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert all(run.status is RunStatus.COMPLETED for run in runs)
        (terminal_decision,) = store.list_human_decisions_for_task(conn, task_id)
        assert terminal_decision.decision == decision


def test_human_decision_approve_completes_task(ws, workflows):
    task_id = _new_task(ws)
    run_ids = []
    for row in STEPS[:4]:
        _drive_run(ws, task_id, row, run_ids)
    _decide_terminal(
        ws,
        task_id,
        run_ids,
        decision="approve",
        action=ActionType.COMPLETE,
        status=TaskStatus.COMPLETED,
    )
    with _session(ws) as conn:
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"


def test_human_decision_cancel_cancels_task(ws, workflows):
    task_id = _new_task(ws)
    run_ids = []
    for row in STEPS[:4]:
        _drive_run(ws, task_id, row, run_ids)
    _decide_terminal(
        ws,
        task_id,
        run_ids,
        decision="cancel",
        action=ActionType.CANCEL,
        status=TaskStatus.CANCELLED,
    )
    with _session(ws) as conn:
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskCancelled"
