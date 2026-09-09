"""Lifecycle event recording: trail shape and stream independence.

SF-34 (plan ref SF-006, SF-A-2 §8): the composed event stream exercised
as tests on the real reference workflow. Per-operation emission is
already pinned in isolation by the module tests
(``test_service.py``, ``test_artifacts.py``, ``test_complete_run.py``,
``test_decide.py``); these tests pin what only composition can show:

* the exact ordered event trail of a representative lifecycle --
  create, workflow assignment, runs, artifacts, results,
  completions, status changes, and a human decision -- with run
  linkage and payload shapes (the upstream contract SF-38, the
  audit/debug view, will consume);
* that the lifecycle commands ignore the event stream: with all
  events wiped and misleading rows inserted, state still reads and
  continued commands behave identically (SF-A-2 §1: history, not
  event sourcing).

Each session runs on a freshly opened SQLite connection and only
string ids cross the session boundary (plus the stateless
``Workspace`` handle) -- the mechanical form of "one independent
Claude Code session per Run".

Same-stamp note: events sharing one ``now`` stamp have random-hex
ids, so their ``ORDER BY created_at, id`` order is random. The trail
assertions therefore sort each maximal same-stamp group by type
value first (``_normalized_trail``). Two stamp subtleties, both
verified against the code: ``resolve#1``'s assignment and creation
events have *different* stamps (separate transactions, commit
order), and ``complete_run``'s ``artifact.created`` events carry
their *own later* stamps (read inside ``register_artifact``), so
they sort after their block-mates.
"""

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.decide import decide as decide_cmd
from skillflow.decisions import DecisionRequest
from skillflow.domain import (
    LifecycleEvent,
    LifecycleEventType,
    RunStatus,
    TaskStatus,
)
from skillflow.prepare_artifacts import prepare_artifacts as prepare
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

_TEST_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = _TEST_ROOT / "workflows" / "software-change.yaml"

#: One row per Run: step id, completion decision, artifact submissions as
#: (name, type, content), expected resolved context types in declaration
#: order, expected unresolved context types, expected declared outputs as
#: (type, required), expected pre-completion missing required output types,
#: and the expected evaluated next action as (ActionType, step-or-None,
#: reason). Same shape and values as the SF-33 human-decision loop: the
#: trail test needs every emitter, and this loop exercises them all.
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
        ("research",),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", "ready"),
    ),
    (
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


def _new_task(ws, workflow_definition_id=None):
    """Register the reference workflow, create a Task, return its id."""
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id=workflow_definition_id
        )
        return task.id


def _drive_run(ws, task_id, row, run_ids, *, workflow=None):
    """Drive one STEPS row through a fresh session; append the run id.

    Only ``task_id`` (and the stateless ``ws`` handle) crosses the
    boundary in; only the new run id (a plain string) leaves it.
    ``workflow`` is passed to ``resolve`` for the assignment path.
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
        run_input = resolve(conn, ws, task_id=task_id, workflow=workflow)
        assert run_input.step_id == step_id
        assert run_input.task_id == task_id
        seen_types = [a.type for a in run_input.context.artifacts]
        assert seen_types == list(context_types)
        assert run_input.context.unresolved == unresolved
        seen_outputs = [(o.type, o.required) for o in run_input.outputs]
        assert seen_outputs == list(outputs)

        report = prepare(conn, ws, task_id=task_id)
        assert report.run_id == run_input.run_id
        assert report.step_id == step_id
        actual = tuple(c.type for c in report.validation.missing_required)
        assert actual == missing

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


def _normalized_trail(events):
    """Sort each maximal same-`created_at` group by type value.

    Events sharing one `now` stamp have random-hex ids, so their
    `ORDER BY created_at, id` order is random; sorting each
    same-stamp group by type makes the trail deterministic.
    Distinct stamps keep their listed order.
    """
    groups = []
    for event in events:
        if groups and groups[-1][-1].created_at == event.created_at:
            groups[-1].append(event)
        else:
            groups.append([event])
    trail = []
    for group in groups:
        trail.extend(sorted(group, key=lambda e: e.type.value))
    return trail


def _assert_run_created(event, run_id, *, reason, triggered_by, step_id):
    assert event.run_id == run_id
    expected = {
        "status": "running",
        "trigger_reason": reason,
        "workflow_definition_id": "software-change",
        "step_id": step_id,
    }
    if triggered_by is not None:
        expected["triggered_by_run_id"] = triggered_by
    assert dict(event.payload) == expected


def _assert_result_created(event, run_id, *, outcome_type, decision):
    assert event.run_id == run_id
    payload = dict(event.payload)
    assert payload.pop("result_id")
    assert payload == {
        "status": "completed",
        "outcome_type": outcome_type,
        "outcome_decision": decision,
    }


def _assert_run_completed(event, run_id):
    assert event.run_id == run_id
    assert dict(event.payload) == {"status": "completed"}


def _assert_status_changed(event, run_id, *, frm, to, action, reason):
    assert event.run_id == run_id
    assert dict(event.payload) == {
        "from": frm,
        "to": to,
        "action": action,
        "reason": reason,
    }


def _assert_artifact_created(event, task_id, run_id, *, name, type, version):
    assert event.run_id == run_id
    payload = dict(event.payload)
    assert set(payload) == {"artifact_id", "name", "type", "version", "path"}
    assert payload["name"] == name
    assert payload["type"] == type
    assert payload["version"] == version
    assert payload["artifact_id"]
    assert payload["path"].startswith(f"{task_id}/")


def _assert_human_made(event, run_id, *, decision):
    assert event.run_id == run_id
    payload = dict(event.payload)
    assert set(payload) == {"decision_id", "decision"}
    assert payload["decision"] == decision
    assert payload["decision_id"]


def test_lifecycle_event_trail_for_human_decision_loop(ws, workflows):
    task_id = _new_task(ws)  # no workflow: the first resolve assigns it
    run_ids = []
    _drive_run(ws, task_id, STEPS[0], run_ids, workflow="software-change")
    for row in STEPS[1:4]:
        _drive_run(ws, task_id, row, run_ids)

    # The decide interlude in its own fresh session (a human act, not a Run).
    with _session(ws) as conn:
        record = decide_cmd(
            conn,
            ws,
            task_id=task_id,
            request=DecisionRequest(decision="request_changes"),
        )
        assert record.action.action is ActionType.RUN

    _drive_run(ws, task_id, STEPS[4], run_ids)
    _drive_run(ws, task_id, STEPS[5], run_ids)

    t = LifecycleEventType
    with _session(ws) as conn:
        events = store.list_lifecycle_events_for_task(conn, task_id)
        assert len(events) == 28
        # The listing guarantee SF-38 relies on: stamps never go backwards.
        stamps = [e.created_at for e in events]
        assert stamps == sorted(stamps)

        trail = _normalized_trail(events)
        assert [e.type for e in trail] == [
            t.TASK_CREATED,
            t.TASK_WORKFLOW_ASSIGNED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.ARTIFACT_CREATED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.ARTIFACT_CREATED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.TASK_STATUS_CHANGED,
            t.ARTIFACT_CREATED,
            t.HUMAN_DECISION_MADE,
            t.TASK_STATUS_CHANGED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.TASK_STATUS_CHANGED,
            t.ARTIFACT_CREATED,
        ]

        # One chunk per command block, in execution order.
        chunks, offset = [], 0
        for size in (1, 2, 3, 1, 3, 1, 2, 1, 4, 2, 1, 2, 1, 4):
            chunks.append(trail[offset : offset + size])
            offset += size
        assert offset == len(trail)
        r1, r2, r3, r4, r5, r6 = run_ids

        (created,) = chunks[0]
        assert created.run_id is None
        assert dict(created.payload) == {"status": "active"}

        assigned, run1 = chunks[1]
        assert assigned.run_id is None
        assert dict(assigned.payload) == {
            "workflow_definition_id": "software-change"
        }
        _assert_run_created(
            run1, r1, reason="initial", triggered_by=None,
            step_id="requirements",
        )

        res1, done1, art1 = chunks[2]
        _assert_result_created(
            res1, r1, outcome_type="requirements", decision="ready"
        )
        _assert_run_completed(done1, r1)
        _assert_artifact_created(
            art1, task_id, r1, name="requirements.md",
            type="requirements", version="1",
        )

        (run2,) = chunks[3]
        _assert_run_created(
            run2, r2, reason="ready", triggered_by=r1,
            step_id="decomposition",
        )

        res2, done2, art2 = chunks[4]
        _assert_result_created(
            res2, r2, outcome_type="decomposition", decision="ready"
        )
        _assert_run_completed(done2, r2)
        _assert_artifact_created(
            art2, task_id, r2, name="plan.md", type="plan", version="1"
        )

        (run3,) = chunks[5]
        _assert_run_created(
            run3, r3, reason="ready", triggered_by=r2,
            step_id="implementation",
        )

        res3, done3 = chunks[6]
        _assert_result_created(
            res3, r3, outcome_type="implementation", decision="ready"
        )
        _assert_run_completed(done3, r3)

        (run4,) = chunks[7]
        _assert_run_created(
            run4, r4, reason="ready", triggered_by=r3, step_id="review"
        )

        res4, done4, parked, art4 = chunks[8]
        _assert_result_created(
            res4, r4, outcome_type="review", decision="human_required"
        )
        _assert_run_completed(done4, r4)
        _assert_status_changed(
            parked, r4, frm="active", to="waiting_for_human",
            action="human", reason="human_required",
        )
        _assert_artifact_created(
            art4, task_id, r4, name="review.md", type="review",
            version="1",
        )

        made, active = chunks[9]
        _assert_human_made(made, r4, decision="request_changes")
        _assert_status_changed(
            active, r4, frm="waiting_for_human", to="active",
            action="run", reason="request_changes",
        )

        (run5,) = chunks[10]
        _assert_run_created(
            run5, r5, reason="request_changes", triggered_by=r4,
            step_id="implementation",
        )

        res5, done5 = chunks[11]
        _assert_result_created(
            res5, r5, outcome_type="implementation", decision="ready"
        )
        _assert_run_completed(done5, r5)

        (run6,) = chunks[12]
        _assert_run_created(
            run6, r6, reason="ready", triggered_by=r5, step_id="review"
        )

        res6, done6, finished, art6 = chunks[13]
        _assert_result_created(
            res6, r6, outcome_type="review", decision="approved"
        )
        _assert_run_completed(done6, r6)
        _assert_status_changed(
            finished, r6, frm="active", to="completed",
            action="complete", reason="approved",
        )
        _assert_artifact_created(
            art6, task_id, r6, name="review.md", type="review",
            version="2",
        )


def test_lifecycle_commands_ignore_the_event_stream(ws, workflows):
    task_id = _new_task(ws, workflow_definition_id="software-change")
    run_ids = []
    for row in STEPS[:4]:
        _drive_run(ws, task_id, row, run_ids)

    # Wipe the stream, then pollute it with misleading rows. Raw SQL:
    # test instrumentation, not a new store API.
    with _session(ws) as conn:
        with conn:
            conn.execute(
                "DELETE FROM lifecycle_events WHERE task_id = ?", (task_id,)
            )
        assert store.list_lifecycle_events_for_task(conn, task_id) == []

        # Current state still reads in full with zero events.
        assert (
            store.get_task(conn, task_id).status
            is TaskStatus.WAITING_FOR_HUMAN
        )
        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert all(run.status is RunStatus.COMPLETED for run in runs)
        for run in runs:
            result = store.get_result_for_run(conn, run.id)
            assert result is not None
            assert result.run_id == run.id
        artifacts = store.list_artifacts_for_task(conn, task_id)
        assert [(a.name, a.type) for a in artifacts] == [
            ("requirements.md", "requirements"),
            ("plan.md", "plan"),
            ("review.md", "review"),
        ]
        assert [read_content(ws, a) for a in artifacts] == [
            "# requirements\n",
            "# plan\n",
            "# review: human required\n",
        ]
        assert store.list_human_decisions_for_task(conn, task_id) == []

        now = datetime.now(UTC)
        with conn:
            store.insert_lifecycle_event(
                conn,
                LifecycleEvent(
                    id="event-bogus-status",
                    task_id=task_id,
                    run_id=run_ids[3],
                    type=LifecycleEventType.TASK_STATUS_CHANGED,
                    payload={
                        "from": "waiting_for_human",
                        "to": "cancelled",
                        "action": "cancel",
                        "reason": "bogus",
                    },
                    created_at=now,
                ),
            )
            store.insert_lifecycle_event(
                conn,
                LifecycleEvent(
                    id="event-bogus-decision",
                    task_id=task_id,
                    run_id=run_ids[3],
                    type=LifecycleEventType.HUMAN_DECISION_MADE,
                    payload={"decision_id": "bogus", "decision": "cancel"},
                    created_at=now,
                ),
            )

    # The real lifecycle continues identically: the bogus `cancel` is
    # never consulted because no command reads the stream.
    with _session(ws) as conn:
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "HumanDecisionRequired"
        record = decide_cmd(
            conn,
            ws,
            task_id=task_id,
            request=DecisionRequest(decision="request_changes"),
        )
        assert record.action.action is ActionType.RUN
        assert record.action.step == "implementation"
        assert record.task.status is TaskStatus.ACTIVE

    _drive_run(ws, task_id, STEPS[4], run_ids)
    _drive_run(ws, task_id, STEPS[5], run_ids)

    t = LifecycleEventType
    with _session(ws) as conn:
        assert store.get_task(conn, task_id).status is TaskStatus.COMPLETED
        (stored,) = store.list_human_decisions_for_task(conn, task_id)
        assert stored.decision == "request_changes"
        assert stored.run_id == run_ids[3]

        # The emitters appended without reading history: the real
        # post-wipe trail stands alongside the two bogus rows.
        events = store.list_lifecycle_events_for_task(conn, task_id)
        real = _normalized_trail(
            e
            for e in events
            if e.id not in ("event-bogus-status", "event-bogus-decision")
        )
        assert [e.type for e in real] == [
            t.HUMAN_DECISION_MADE,
            t.TASK_STATUS_CHANGED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.RUN_CREATED,
            t.RESULT_CREATED,
            t.RUN_COMPLETED,
            t.TASK_STATUS_CHANGED,
            t.ARTIFACT_CREATED,
        ]
        (made,) = [e for e in real if e.type is t.HUMAN_DECISION_MADE]
        assert made.payload["decision"] == "request_changes"
