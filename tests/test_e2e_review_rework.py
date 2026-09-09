"""Review/rework E2E: Implementation -> Review -> changes_requested.

SF-31 (plan ref SF-032, SF-A-4 §14-B): the rework loop exercised as one
scenario on the real reference workflow. Every Task starts at ``steps[0]``,
so the scenario opens with the happy-path prefix (Requirements,
Decomposition, Implementation), then drives the loop under test: Review
completes with ``changes_requested``, a new implementation Run is created,
and the second Review completes with ``approved``.

Each Run is driven through a freshly opened SQLite connection and only
string ids cross the Run boundary (plus the stateless ``Workspace``
checkout handle, which holds no session state) -- the mechanical form of
"one independent Claude Code session per Run". Nothing else in-memory (no
``Task``/``Run``/``RunInput``) may pass from one Run to the next; every
command re-resolves its state from SQLite + workspace files.

The scenario pins the rework contract end to end:

* the ``changes_requested`` completion evaluates to a rework Run on the
  ``implementation`` step, and the next resolve yields that step;
* the rework Run's provenance names the review Run with reason
  ``changes_requested`` (SF-A-4 §8);
* the rework Run resolves the ``review`` context to the v1 review that
  requested the changes (unresolved on the first implementation pass);
* the second review Run must produce its own ``review`` artifact despite v1
  existing (per-Run output scope);
* ``review.md`` v1 -> v2 is one immutable version chain across Runs;
* the original Runs remain in history with their canonical Results;
* the terminal tail: Task ``completed`` and no 7th Run.
"""

import contextlib
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.domain import RunStatus, TaskStatus
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
#: reason).
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
        (),
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
        # The review that requests changes. The required `review` output is
        # still missing before completion (per-Run scope: nothing exists
        # yet), and the outcome routes back to `implementation` -- an
        # ordinary outcome rule, not a Rework entity.
        "review",
        "changes_requested",
        (("review.md", "review", "# review: changes requested\n"),),
        ("requirements", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.RUN, "implementation", "changes_requested"),
    ),
    (
        # The rework Run: same step, new Run. Its declared `review` context
        # now resolves to the v1 review above -- the review that requested
        # the changes is visible to the rework.
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


def test_review_rework_loop_to_approved(ws, workflows):
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id="software-change"
        )
        task_id = task.id

    run_ids = []
    for index, (
        step_id,
        decision,
        submissions,
        context_types,
        unresolved,
        outputs,
        missing,
        (action, next_step, reason),
    ) in enumerate(STEPS):
        # A new session per Run: only string ids (`task_id`, the
        # collected `run_ids`) and the stateless `ws` handle cross in.
        with _session(ws) as conn:
            run_input = resolve(conn, ws, task_id=task_id)
            assert run_input.step_id == step_id
            assert run_input.task_id == task_id
            seen_types = [a.type for a in run_input.context.artifacts]
            assert seen_types == list(context_types)
            assert run_input.context.unresolved == unresolved
            seen_outputs = [(o.type, o.required) for o in run_input.outputs]
            assert seen_outputs == list(outputs)

            events = store.list_lifecycle_events_for_task(conn, task_id)
            events_before = len(events)
            report = prepare(conn, ws, task_id=task_id)
            assert report.run_id == run_input.run_id
            assert report.step_id == step_id
            actual = tuple(c.type for c in report.validation.missing_required)
            assert actual == missing
            # prepare-artifacts is a read-only inspection: no rows, no events.
            assert (
                len(store.list_lifecycle_events_for_task(conn, task_id))
                == events_before
            )

            if index == 4:
                # The rework Run resolves the review that requested the
                # changes: latest `review` artifact, produced by Run 4.
                ctx_artifacts = run_input.context.artifacts
                [review] = [a for a in ctx_artifacts if a.type == "review"]
                assert review.version == 1
                assert review.run_id == run_ids[3]
                # Sequential integrity across loop iterations: a second
                # resolve while this Run is running is rejected; the Run
                # itself is untouched.
                with pytest.raises(ResolveTaskError) as exc_info:
                    resolve(conn, ws, task_id=task_id)
                assert exc_info.value.code == "ActiveRunExists"

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
            "changes_requested",
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
            "# review: changes requested\n",
            "# review: approved\n",
        ]
        first_review, second_review = artifacts[2], artifacts[3]
        assert first_review.supersedes_id is None
        assert second_review.supersedes_id == first_review.id
        assert first_review.run_id == run_ids[3]
        assert second_review.run_id == run_ids[5]

        # No 7th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"
