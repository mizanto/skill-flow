"""Tests for ``skillflow.complete_run`` (SF-23).

Following ``test_prepare_artifacts.py``'s shape: a ``tmp_path`` workspace
fixture with a ``.git`` marker, ``store.open_store``, and the **real**
``workflows/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, and earlier steps are completed with the real ``complete_run(...)``
itself, so the command is exercised against state the system produces.

* **Contract change-detectors** -- the public surface, ``CompleteRunError``
  (type and ``code`` attribute), the ``RunCompletion`` validation, the AST
  import boundary (no Claude Code launch of any kind), the banned-concept
  scan, and the absence of an ``evaluator`` import (the mechanical form of
  "no Lifecycle Evaluation here" -- SF-24 will relax that test deliberately).
* **Behaviour tests** -- every rejection path proving the snapshot is
  byte-identical afterwards (the mechanical form of "a rejection leaves the
  Run ``running`` with nothing written"), and every success path proving the
  single atomic write (artifacts registered, exactly one Result, Run
  completed, no Task write, no next Run).
"""

import ast
import contextlib
import dataclasses
from dataclasses import replace
from datetime import timedelta
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
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import EvaluationOutput
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

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
        "artifacts",
    }


def test_run_completion_is_frozen_slotted_keyword_only():
    from datetime import UTC, datetime

    from skillflow.domain import Result, Run

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
    )
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.artifacts = ()


def _valid_run_and_result():
    from datetime import UTC, datetime

    from skillflow.domain import Run

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
    return run, result


def test_run_completion_rejects_bad_members():
    run, result = _valid_run_and_result()
    with pytest.raises(ValueError, match="must be a Run"):
        RunCompletion(run="run-1", result=result)
    with pytest.raises(ValueError, match="must be a Result"):
        RunCompletion(run=run, result="result-1")
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunCompletion(run=run, result=result, artifacts="review.md")
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunCompletion(run=run, result=result, artifacts=123)
    with pytest.raises(ValueError, match="must contain Artifact"):
        RunCompletion(run=run, result=result, artifacts=(123,))


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


def test_module_does_not_evaluate():
    source = Path(complete_run_pkg.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "skillflow.evaluator" not in imported


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


def test_skill_targeted_run_with_decision_rejected(conn, ws, workflows):
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
    assert skill_run.step_id is None
    before = _snapshot(conn, task.id, skill_run.id)
    with pytest.raises(CompleteRunError) as exc_info:
        complete(
            conn,
            ws,
            run_id=skill_run.id,
            request=CompletionRequest(decision="ready"),
        )
    assert exc_info.value.code == "OutcomeNotExpected"
    assert _snapshot(conn, task.id, skill_run.id) == before
    done = complete(conn, ws, run_id=skill_run.id, request=CompletionRequest())
    assert done.run.status is RunStatus.COMPLETED
    assert done.result.outcome is None


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
    # A multiset comparison: the three rows share one `created_at`, so the
    # store's (created_at, id) order between them is uuid noise.
    assert sorted(_event_types(conn, task.id, 3)) == sorted(
        [
            LifecycleEventType.ARTIFACT_CREATED,
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_COMPLETED,
        ]
    )
    assert len(store.list_lifecycle_events_for_task(conn, task.id)) == (
        events_before + 3
    )
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


def test_completion_has_no_lifecycle_consequence(conn, ws, workflows):
    _, task = _assigned(conn)
    updated_before = store.get_task(conn, task.id).updated_at
    run = _drive_to_review(conn, ws, task)
    complete(
        conn,
        ws,
        run_id=run.id,
        request=CompletionRequest(
            decision="changes_requested",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    after = store.get_task(conn, task.id)
    assert after.status is TaskStatus.ACTIVE
    assert after.updated_at == updated_before
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
