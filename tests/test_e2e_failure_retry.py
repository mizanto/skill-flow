"""Failure/retry E2E: an implementation Run fails, the next Run retries it.

SF-35: the failure loop exercised as one scenario on the real reference
workflow. The scenario opens with the happy-path prefix (Requirements,
Decomposition), then drives the loop under test: the implementation Run
fails with diagnostics and a revised plan, a retry Run is created for the
same step, and the Task completes through Review.

Each Run is driven through a freshly opened SQLite connection and only
string ids cross the Run boundary (plus the stateless ``Workspace``
checkout handle, which holds no session state) -- the mechanical form of
"one independent Claude Code session per Run". Nothing else in-memory (no
``Task``/``Run``/``RunInput``) may pass from one Run to the next; every
command re-resolves its state from SQLite + workspace files.

The scenario pins the failure contract end to end:

* the failed Run stays in history with its canonical failed Result (no
  outcome) and never resumes;
* diagnostics land in ``runs/<run-id>/output.log`` and are inspectable;
* the Task is never failed by the Run failure;
* the retry is a new Run on the same step, with provenance naming the
  failed Run and reason ``run_failed``;
* the failed Run's partial artifacts are usable context (SF-A-2 §9): the
  retry resolves ``plan`` to the v2 the failed Run submitted;
* diagnostics never reach the retry's ``RunInput`` (SF-A-5 §3.4);
* the terminal tail: Task ``completed`` and no 6th Run.
"""

import contextlib
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.domain import RunStatus, TaskStatus
from skillflow.fail_run import FailureRequest
from skillflow.fail_run import fail_run as fail
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

DIAGNOSTICS = "boom: OOM while linking\n"


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


def test_failed_run_retries_as_a_new_run_to_approved(ws, workflows):
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id="software-change"
        )
        task_id = task.id

    run_ids = []

    # --- R1: requirements -------------------------------------------------
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task_id)
        assert run_input.step_id == "requirements"
        report = prepare(conn, ws, task_id=task_id)
        assert tuple(c.type for c in report.validation.missing_required) == (
            "requirements",
        )
        done = complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision="ready",
                artifacts=(
                    ArtifactSubmission(
                        name="requirements.md",
                        type="requirements",
                        content="# requirements\n",
                    ),
                ),
            ),
        )
        assert (done.action.action, done.action.step, done.action.reason) == (
            ActionType.RUN,
            "decomposition",
            "ready",
        )
        run_ids.append(run_input.run_id)

    # --- R2: decomposition ------------------------------------------------
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == "decomposition"
        assert [a.type for a in run_input.context.artifacts] == ["requirements"]
        done = complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision="ready",
                artifacts=(
                    ArtifactSubmission(name="plan.md", type="plan", content="# plan\n"),
                ),
            ),
        )
        assert (done.action.action, done.action.step, done.action.reason) == (
            ActionType.RUN,
            "implementation",
            "ready",
        )
        run_ids.append(run_input.run_id)

    # --- R3: implementation fails -----------------------------------------
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == "implementation"
        failure = fail(
            conn,
            ws,
            task_id=task_id,
            request=FailureRequest(
                diagnostics=DIAGNOSTICS,
                artifacts=(
                    ArtifactSubmission(
                        name="plan.md",
                        type="plan",
                        content="# plan revised mid-implementation\n",
                    ),
                ),
            ),
        )
        assert failure.run.status is RunStatus.FAILED
        assert failure.result.status.value == "failed"
        assert failure.result.outcome is None
        assert (failure.action.action, failure.action.step) == (
            ActionType.RUN,
            "implementation",
        )
        assert failure.action.reason == "run_failed"
        # A failed Run never fails the Task.
        assert store.get_task(conn, task_id).status is TaskStatus.ACTIVE
        run_ids.append(run_input.run_id)
        failed_id = run_input.run_id

    # --- R4: the retry ----------------------------------------------------
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task_id)
        assert run_input.step_id == "implementation"
        assert run_input.run_id != failed_id
        retry = store.get_run(conn, run_input.run_id)
        assert retry.status is RunStatus.RUNNING
        assert retry.triggered_by_run_id == failed_id
        assert retry.trigger_reason == "run_failed"
        # The failed Run's revised plan is usable context (SF-A-2 §9).
        by_type = {a.type: a for a in run_input.context.artifacts}
        assert set(by_type) == {"requirements", "plan"}
        assert by_type["plan"].version == 2
        assert by_type["plan"].run_id == failed_id
        assert run_input.context.unresolved == ("review",)
        # ... while diagnostics never reach the new Run (SF-A-5 §3.4).
        assert "boom" not in repr(run_input)
        assert "output.log" not in repr(run_input)
        report = prepare(conn, ws, task_id=task_id)
        assert report.run_id == run_input.run_id
        assert report.validation.missing_required == ()
        done = complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(decision="ready"),
        )
        assert (done.action.action, done.action.step, done.action.reason) == (
            ActionType.RUN,
            "review",
            "ready",
        )
        run_ids.append(run_input.run_id)

    # --- R5: review approves ----------------------------------------------
    with _session(ws) as conn:
        run_input = resolve(conn, ws, task_id=task.id)
        assert run_input.step_id == "review"
        done = complete(
            conn,
            ws,
            run_id=run_input.run_id,
            request=CompletionRequest(
                decision="approved",
                artifacts=(
                    ArtifactSubmission(
                        name="review.md",
                        type="review",
                        content="# review: approved\n",
                    ),
                ),
            ),
        )
        assert done.action.action is ActionType.COMPLETE
        run_ids.append(run_input.run_id)

    # Terminal state, observed through yet another fresh connection.
    with _session(ws) as conn:
        task = store.get_task(conn, task_id)
        assert task.status is TaskStatus.COMPLETED

        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert [run.status for run in runs] == [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED,
        ]
        assert [run.step_id for run in runs] == [
            "requirements",
            "decomposition",
            "implementation",
            "implementation",
            "review",
        ]
        assert runs[0].triggered_by_run_id is None
        assert runs[0].trigger_reason == "initial"
        expected_reasons = ("ready", "ready", "run_failed", "ready")
        for previous, run, expected in zip(
            runs, runs[1:], expected_reasons, strict=False
        ):
            assert run.triggered_by_run_id == previous.id
            assert run.trigger_reason == expected

        for run in runs:
            result = store.get_result_for_run(conn, run.id)
            assert result is not None
            assert result.run_id == run.id
        failed_result = store.get_result_for_run(conn, failed_id)
        assert failed_result.status.value == "failed"
        assert failed_result.outcome is None

        # Diagnostics are inspectable on disk, next to the failed Run.
        output_log = ws.run_dir(failed_id) / "output.log"
        assert output_log.read_text(encoding="utf-8") == DIAGNOSTICS

        artifacts = store.list_artifacts_for_task(conn, task_id)
        assert [(a.name, a.type, a.version) for a in artifacts] == [
            ("requirements.md", "requirements", 1),
            ("plan.md", "plan", 1),
            ("plan.md", "plan", 2),
            ("review.md", "review", 1),
        ]
        assert [read_content(ws, a) for a in artifacts] == [
            "# requirements\n",
            "# plan\n",
            "# plan revised mid-implementation\n",
            "# review: approved\n",
        ]
        first_plan, second_plan = artifacts[1], artifacts[2]
        assert first_plan.supersedes_id is None
        assert second_plan.supersedes_id == first_plan.id
        assert second_plan.run_id == failed_id

        # No 6th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"
