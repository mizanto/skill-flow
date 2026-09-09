"""Happy-path E2E: Requirements -> Decomposition -> Implementation -> Review -> Done.

SF-30 (plan ref SF-031, SF-A-6 §11-A): the Wave 5 checkpoint exercised as one
scenario on the real reference workflow. Each Run is driven through a freshly
opened SQLite connection and only string ids cross the Run boundary (plus the
stateless ``Workspace`` checkout handle, which holds no session state) -- the
mechanical form of "one independent Claude Code session per Run". Nothing
else in-memory (no ``Task``/``Run``/``RunInput``) may pass from one Run to the
next; every command re-resolves its state from SQLite + workspace files.

The scenario pins the happy-path contract end to end:

* step sequence and the evaluated next action after each completion;
* deterministic context per Run (resolved types + unresolved declarations);
* the pre-completion ``prepare-artifacts`` missing set per step, and that the
  inspection writes nothing;
* one canonical Result per Run, Run completion, and the provenance chain;
* artifact metadata + content persistence across Runs;
* the terminal tail: Task ``completed`` and no 5th Run.
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

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

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
        # has produced yet on the happy path, so it stays unresolved.
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
        "approved",
        (("review.md", "review", "# review\n"),),
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


def test_happy_path_requirements_to_done(ws, workflows):
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
        # A new session per Run: only `task_id` (and the stateless `ws`
        # handle) crosses the boundary in.
        with _session(ws) as conn:
            run_input = resolve(conn, ws, task_id=task_id)
            assert run_input.step_id == step_id
            assert run_input.task_id == task_id
            assert [a.type for a in run_input.context.artifacts] == list(context_types)
            assert run_input.context.unresolved == unresolved
            assert [(o.type, o.required) for o in run_input.outputs] == list(outputs)

            events_before = len(store.list_lifecycle_events_for_task(conn, task_id))
            report = prepare(conn, ws, task_id=task_id)
            assert report.run_id == run_input.run_id
            assert report.step_id == step_id
            assert tuple(c.type for c in report.validation.missing_required) == missing
            # prepare-artifacts is a read-only inspection: no rows, no events.
            assert (
                len(store.list_lifecycle_events_for_task(conn, task_id))
                == events_before
            )

            if index == 1:
                # Sequential integrity inside the flow: a second resolve while
                # this Run is running is rejected; the Run itself is untouched.
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
        assert runs[0].triggered_by_run_id is None
        assert runs[0].trigger_reason == "initial"
        for previous, run in zip(runs, runs[1:], strict=False):
            assert run.triggered_by_run_id == previous.id
            assert run.trigger_reason == "ready"

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
            "# review\n",
        ]

        # No 5th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"
