"""Tests for ``skillflow.fail_run`` (SF-35).

Following ``test_complete_run.py``'s shape: a ``tmp_path`` workspace
fixture with a ``.git`` marker, ``store.open_store``, and the **real**
``workflows/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, so the command is exercised against state the system produces.

* **Contract change-detectors** -- the public surface, ``FailRunError``
  (type and ``code`` attribute), the ``RunFailure`` / ``FailureRequest``
  validation, the AST import boundary (no Claude Code launch of any kind),
  the banned-concept scan, and the presence of the ``evaluator`` import (the
  mechanical form of "Lifecycle Evaluation happens here").
* **Behaviour tests** -- every rejection path proving the snapshot is
  byte-identical afterwards (the mechanical form of "a rejection leaves the
  Run ``running`` with nothing written"), and every success path proving the
  single atomic write (partial artifacts registered, exactly one failed
  Result, Run failed, diagnostics persisted, lifecycle evaluated to a retry,
  Task untouched, no next Run).
"""

import ast
import contextlib
import dataclasses
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import fail_run as fail_run_pkg
from skillflow import store, workspace
from skillflow.artifacts import create_artifact, read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import (
    ArtifactSubmission,
    CompletionRequest,
)
from skillflow.domain import (
    LifecycleEventType,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    TaskStatus,
)
from skillflow.evaluator import (
    EvaluationError,
    EvaluationOutput,
)
from skillflow.fail_run import FailRunError, FailureRequest, RunFailure
from skillflow.fail_run import fail_run as fail
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType
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


def _drive_to_skill_run(conn, ws, task):
    """Return a running skill Run, reached via review/`fundamental_assumption_wrong`."""
    review = _drive_to_review(conn, ws, task)
    complete(
        conn,
        ws,
        run_id=review.id,
        request=CompletionRequest(
            decision="fundamental_assumption_wrong",
            artifacts=(_submit("review.md", "review"),),
        ),
    )
    skill_input = resolve(conn, ws, task_id=task.id)
    assert skill_input.step_id is None
    assert skill_input.skill == "research"
    return store.get_run(conn, skill_input.run_id)


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


def _files(ws):
    return sorted(
        p for p in (ws.artifacts_dir, ws.runs_dir) for p in p.rglob("*") if p.is_file()
    )


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(fail_run_pkg.__all__) == {
        "FailRunError",
        "FailureRequest",
        "RunFailure",
        "fail_run",
    }


def test_fail_run_error_carries_code():
    err = FailRunError("RunNotFound", "no run")
    assert isinstance(err, Exception)
    assert err.code == "RunNotFound"
    assert str(err) == "no run"


def test_run_failure_fields_match_contract():
    assert {f.name for f in dataclasses.fields(RunFailure)} == {
        "run",
        "result",
        "task",
        "action",
        "artifacts",
        "diagnostics_path",
    }


def test_run_failure_is_frozen_slotted_keyword_only():
    from skillflow.domain import Run, Task

    params = RunFailure.__dataclass_params__
    assert params.frozen and params.kw_only
    now = datetime.now(UTC)
    instance = RunFailure(
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
        action=EvaluationOutput(
            action=ActionType.RUN, reason="run_failed", step="requirements"
        ),
    )
    assert instance.artifacts == ()
    assert instance.diagnostics_path is None
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.artifacts = ()


def _valid_run_result_task_and_action():
    from skillflow.domain import Run, Task

    now = datetime.now(UTC)
    run = Run(
        id="run-1",
        task_id="task-1",
        status=RunStatus.FAILED,
        created_at=now,
    )
    result = Result(
        id="result-1",
        run_id="run-1",
        status=ResultStatus.FAILED,
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
    action = EvaluationOutput(
        action=ActionType.RUN, reason="run_failed", step="requirements"
    )
    return run, result, task, action


def test_run_failure_rejects_bad_members():
    run, result, task, action = _valid_run_result_task_and_action()
    with pytest.raises(ValueError, match="must be a Run"):
        RunFailure(run="run-1", result=result, task=task, action=action)
    with pytest.raises(ValueError, match="must be a Result"):
        RunFailure(run=run, result="result-1", task=task, action=action)
    with pytest.raises(ValueError, match="must be a Task"):
        RunFailure(run=run, result=result, task="task-1", action=action)
    with pytest.raises(ValueError, match="must be an EvaluationOutput"):
        RunFailure(run=run, result=result, task=task, action="run")
    # Unlike RunCompletion.action, the failure action is never None: the
    # failed rule always returns `run`.
    with pytest.raises(ValueError, match="must be an EvaluationOutput"):
        RunFailure(run=run, result=result, task=task, action=None)
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunFailure(
            run=run, result=result, task=task, action=action, artifacts="review.md"
        )
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        RunFailure(run=run, result=result, task=task, action=action, artifacts=123)
    with pytest.raises(ValueError, match="must contain Artifact"):
        RunFailure(run=run, result=result, task=task, action=action, artifacts=(123,))
    with pytest.raises(ValueError, match="diagnostics_path"):
        RunFailure(
            run=run, result=result, task=task, action=action, diagnostics_path="  "
        )
    with pytest.raises(ValueError, match="diagnostics_path"):
        RunFailure(
            run=run, result=result, task=task, action=action, diagnostics_path=123
        )


def test_failure_request_defaults():
    request = FailureRequest()
    assert request.diagnostics is None
    assert request.artifacts == ()


def test_failure_request_rejects_bad_members():
    with pytest.raises(ValueError, match="diagnostics"):
        FailureRequest(diagnostics=123)
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        FailureRequest(artifacts="review.md")
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        FailureRequest(artifacts=123)
    with pytest.raises(ValueError, match="must contain ArtifactSubmission"):
        FailureRequest(artifacts=(123,))
    with pytest.raises(ValueError, match="duplicate name"):
        FailureRequest(
            artifacts=(
                _submit("notes.md", "notes"),
                _submit("notes.md", "notes"),
            )
        )


def test_module_imports_are_within_the_boundary():
    source = Path(fail_run_pkg.__file__).read_text()
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
        for name in vars(fail_run_pkg)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_module_evaluates_via_evaluator():
    # Importing the pure evaluator is the mechanical form of "Lifecycle
    # Evaluation happens here" -- the retry action is evaluated, not invented.
    source = Path(fail_run_pkg.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "skillflow.evaluator" in imported


# --- rejection paths (nothing written) --------------------------------------


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM results").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0],
    )


def test_request_must_be_a_failure_request(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    before = _snapshot(conn, task.id, run_id)
    with pytest.raises(ValueError, match="must be a FailureRequest"):
        fail(conn, ws, request="boom")
    assert _snapshot(conn, task.id, run_id) == before
    assert _files(ws) == []


def test_no_running_run_in_workspace_rejected(conn, ws, workflows):
    _assigned(conn)
    before = _counts(conn)
    with pytest.raises(FailRunError) as exc_info:
        fail(conn, ws, request=FailureRequest(diagnostics="boom"))
    assert exc_info.value.code == "RunNotFound"
    assert "resolve-task" in str(exc_info.value)
    assert _counts(conn) == before
    assert _files(ws) == []


def test_ambiguous_running_runs_rejected(conn, ws, workflows):
    _, first = _assigned(conn, title="First")
    _, second = _assigned(conn, title="Second")
    first_run = resolve(conn, ws, task_id=first.id).run_id
    second_run = resolve(conn, ws, task_id=second.id).run_id
    before = _counts(conn)
    with pytest.raises(FailRunError) as exc_info:
        fail(conn, ws, request=FailureRequest(diagnostics="boom"))
    assert exc_info.value.code == "AmbiguousCurrentRun"
    msg = str(exc_info.value)
    assert first.id in msg and second.id in msg
    assert "fail-run --task" in msg
    assert _counts(conn) == before
    assert _files(ws) == []
    assert store.get_run(conn, first_run).status is RunStatus.RUNNING
    assert store.get_run(conn, second_run).status is RunStatus.RUNNING


def test_unknown_task_rejected(conn, ws, workflows):
    before = _counts(conn)
    with pytest.raises(FailRunError) as exc_info:
        fail(conn, ws, task_id="task-missing", request=FailureRequest())
    assert exc_info.value.code == "TaskNotFound"
    assert _counts(conn) == before
    assert _files(ws) == []


def test_task_without_runs_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    before = _counts(conn)
    with pytest.raises(FailRunError) as exc_info:
        fail(conn, ws, task_id=task.id, request=FailureRequest())
    assert exc_info.value.code == "RunNotFound"
    assert _counts(conn) == before
    assert _files(ws) == []


@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_task_without_running_run_rejected(conn, ws, workflows, terminal):
    # A completed Run cannot fail; a failed Run cannot fail twice. A Run is
    # never resumed.
    _, task = _assigned(conn)
    run = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    done = replace(
        run, status=terminal, completed_at=run.started_at + timedelta(seconds=1)
    )
    with conn:
        store.update_run(conn, done)
    before = _snapshot(conn, task.id, run.id)
    with pytest.raises(FailRunError) as exc_info:
        fail(
            conn,
            ws,
            task_id=task.id,
            request=FailureRequest(diagnostics="boom"),
        )
    assert exc_info.value.code == "RunNotActive"
    assert "never resumed" in str(exc_info.value)
    assert _snapshot(conn, task.id, run.id) == before
    assert _files(ws) == []


def test_run_without_workflow_rejected(conn, ws, workflows):
    task = create_task(conn, title="No workflow")
    run = Run(
        id="run-orphan",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        trigger_reason="initial",
    )
    with conn:
        store.insert_run(conn, run)
    before = _counts(conn)
    with pytest.raises(FailRunError) as exc_info:
        fail(conn, ws, task_id=task.id, request=FailureRequest())
    assert exc_info.value.code == "StepUnresolved"
    assert _counts(conn) == before
    assert _files(ws) == []


def test_skill_run_without_payload_skill_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    skill_run = _drive_to_skill_run(conn, ws, task)
    with conn:
        conn.execute(
            "DELETE FROM lifecycle_events WHERE run_id = ? AND type = ?",
            (skill_run.id, "run.created"),
        )
    before = _snapshot(conn, task.id, skill_run.id)
    files_before = _files(ws)
    with pytest.raises(FailRunError) as exc_info:
        fail(
            conn,
            ws,
            task_id=task.id,
            request=FailureRequest(diagnostics="boom"),
        )
    assert exc_info.value.code == "StepUnresolved"
    assert "run.created" in str(exc_info.value)
    assert _snapshot(conn, task.id, skill_run.id) == before
    assert _files(ws) == files_before


def test_deleted_definition_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    (workflows / "software-change.yaml").unlink()
    before = _snapshot(conn, task.id, run_id)
    with pytest.raises(WorkflowLoadError):
        fail(
            conn,
            ws,
            task_id=task.id,
            request=FailureRequest(diagnostics="boom"),
        )
    assert _snapshot(conn, task.id, run_id) == before
    assert _files(ws) == []


def test_run_naming_an_absent_step_rejected(conn, ws, workflows):
    # The definition changed under a live Task: the retry has no target.
    _, task = _assigned(conn)
    stray = create_run(
        conn,
        task_id=task.id,
        action=EvaluationOutput(action=ActionType.RUN, reason="initial", step="gone"),
    )
    before = _snapshot(conn, task.id, stray.id)
    with pytest.raises(EvaluationError, match="absent from workflow"):
        fail(
            conn,
            ws,
            task_id=task.id,
            request=FailureRequest(diagnostics="boom"),
        )
    assert _snapshot(conn, task.id, stray.id) == before
    assert _files(ws) == []


def test_chain_type_conflict_rejected(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    create_artifact(
        conn, ws, run_id=run_id, name="notes.md", type="notes", content="v1"
    )
    before = _snapshot(conn, task.id, run_id)
    files_before = _files(ws)
    with pytest.raises(FailRunError) as exc_info:
        fail(
            conn,
            ws,
            task_id=task.id,
            request=FailureRequest(
                diagnostics="boom",
                artifacts=(_submit("notes.md", "plan", "v2"),),
            ),
        )
    assert exc_info.value.code == "InvalidArtifactSubmission"
    assert "'notes'" in str(exc_info.value)
    assert _snapshot(conn, task.id, run_id) == before
    assert _files(ws) == files_before


# --- success paths (one atomic write) ---------------------------------------


def test_fail_step_run_with_diagnostics(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    updated_before = store.get_task(conn, task.id).updated_at

    failure = fail(
        conn,
        ws,
        task_id=task.id,
        request=FailureRequest(diagnostics="boom: OOM\n"),
    )

    run = store.get_run(conn, run_id)
    assert run.status is RunStatus.FAILED
    assert run.completed_at is not None
    assert failure.run == run
    result = store.get_result_for_run(conn, run_id)
    assert result is not None
    assert result.status is ResultStatus.FAILED
    assert result.outcome is None
    assert result.created_at == run.completed_at
    assert failure.result == result
    expected_log = f"runs/{run_id}/output.log"
    assert dict(result.metadata) == {"diagnostics": expected_log}
    assert failure.diagnostics_path == expected_log
    assert (ws.path / expected_log).read_text(encoding="utf-8") == "boom: OOM\n"
    # Same-timestamp events order by random id, so the tail is compared as a
    # set (the convention test_complete_run.py uses).
    assert sorted(_event_types(conn, task.id, 2)) == sorted(
        [
            LifecycleEventType.RESULT_CREATED,
            LifecycleEventType.RUN_FAILED,
        ]
    )
    events = store.list_lifecycle_events_for_task(conn, task.id)
    by_type = {event.type: event for event in events[-2:]}
    assert by_type[LifecycleEventType.RESULT_CREATED].payload["result_id"] == (
        result.id
    )
    assert by_type[LifecycleEventType.RESULT_CREATED].payload["status"] == "failed"
    assert by_type[LifecycleEventType.RUN_FAILED].run_id == run_id
    assert by_type[LifecycleEventType.RUN_FAILED].payload == {"status": "failed"}
    # A failed Run never fails the Task: no status change, no Task write at
    # all (updated_at byte-identical), no status_changed event.
    stored_task = store.get_task(conn, task.id)
    assert stored_task.status is TaskStatus.ACTIVE
    assert stored_task.updated_at == updated_before
    assert failure.task == stored_task
    assert LifecycleEventType.TASK_STATUS_CHANGED not in _event_types(
        conn, task.id, len(events)
    )
    assert failure.action.action is ActionType.RUN
    assert (failure.action.step, failure.action.skill) == ("requirements", None)
    assert failure.action.reason == "run_failed"
    assert failure.artifacts == ()
    assert len(store.list_runs_for_task(conn, task.id)) == 1


def test_fail_without_diagnostics_writes_no_file(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id

    failure = fail(conn, ws, task_id=task.id, request=FailureRequest())

    assert store.get_run(conn, run_id).status is RunStatus.FAILED
    result = store.get_result_for_run(conn, run_id)
    assert result.metadata is None
    assert failure.diagnostics_path is None
    assert not (ws.runs_dir / run_id).exists()
    assert _files(ws) == []


def test_fail_registers_partial_artifacts_without_required_coverage(
    conn, ws, workflows
):
    # The requirements step requires a `requirements` output, yet the Run
    # fails with only an unrelated partial submission: missing required
    # outputs never block failure recording, and the partial output is
    # registered for the retry (SF-A-2 §9).
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id

    failure = fail(
        conn,
        ws,
        task_id=task.id,
        request=FailureRequest(
            diagnostics="boom",
            artifacts=(_submit("notes.md", "notes", "partial"),),
        ),
    )

    assert store.get_run(conn, run_id).status is RunStatus.FAILED
    assert [(a.name, a.type, a.version) for a in failure.artifacts] == [
        ("notes.md", "notes", 1)
    ]
    registered = store.list_artifacts_for_run(conn, run_id)
    assert [a.id for a in registered] == [a.id for a in failure.artifacts]
    assert read_content(ws, registered[0]) == "partial"
    assert LifecycleEventType.ARTIFACT_CREATED in _event_types(conn, task.id, 4)


def test_fail_versions_against_an_earlier_runs_chain(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id
    first = create_artifact(
        conn, ws, run_id=run_id, name="notes.md", type="notes", content="v1"
    )

    failure = fail(
        conn,
        ws,
        task_id=task.id,
        request=FailureRequest(artifacts=(_submit("notes.md", "notes", "v2"),)),
    )

    assert len(failure.artifacts) == 1
    second = failure.artifacts[0]
    assert (second.version, second.supersedes_id) == (2, first.id)
    assert read_content(ws, second) == "v2"
    assert read_content(ws, first) == "v1"


def test_fail_skill_run_retries_the_skill(conn, ws, workflows):
    _, task = _assigned(conn)
    skill_run = _drive_to_skill_run(conn, ws, task)

    failure = fail(
        conn,
        ws,
        task_id=task.id,
        request=FailureRequest(diagnostics="research blew up"),
    )

    assert failure.run.id == skill_run.id
    assert failure.run.status is RunStatus.FAILED
    assert failure.run.step_id is None
    assert failure.action.action is ActionType.RUN
    assert (failure.action.step, failure.action.skill) == (None, "research")
    assert failure.action.reason == "run_failed"
    assert store.get_task(conn, task.id).status is TaskStatus.ACTIVE


def test_fail_resolves_the_lone_running_run_without_task(conn, ws, workflows):
    _, task = _assigned(conn)
    run_id = resolve(conn, ws, task_id=task.id).run_id

    failure = fail(conn, ws, request=FailureRequest(diagnostics="boom"))

    assert failure.run.id == run_id
    assert failure.run.status is RunStatus.FAILED


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


def test_commit_failure_rolls_everything_back_and_unlinks_files(conn, ws, workflows):
    # The write block is one unit: a commit failure rolls back the Result,
    # the Run failure, the artifact rows and the events -- and unlinks both
    # the submission files and output.log written by this attempt.
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    failing = sqlite3.connect(ws.db_path, factory=_CommitFails)
    failing.row_factory = sqlite3.Row
    failing.execute("PRAGMA foreign_keys = ON")
    files_before = _files(ws)
    before = _snapshot(conn, task.id, run.id)
    try:
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            fail(
                failing,
                ws,
                task_id=task.id,
                request=FailureRequest(
                    diagnostics="boom",
                    artifacts=(_submit("notes.md", "notes"),),
                ),
            )
    finally:
        # Closing rolls the still-open transaction back and releases the
        # write lock, so the fixture connection can read below.
        failing.close()
    assert _snapshot(conn, task.id, run.id) == before
    assert _files(ws) == files_before
    assert not (ws.run_dir(run.id) / "output.log").exists()


def test_retry_after_crash_reuses_orphan_files(conn, ws, workflows):
    # A kill between the file writes and the commit leaves orphan files and
    # no rows; the retry reuses the artifact orphan and output.log when the
    # bytes are identical, and the Run fails normally.
    _, task = _assigned(conn)
    run = _drive_to_review(conn, ws, task)
    submission = _submit("notes.md", "notes")
    orphan = ws.artifacts_dir / task.id / "notes-v1.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(submission.content, encoding="utf-8")
    log = ws.run_dir(run.id) / workspace.OUTPUT_LOG_FILE_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("boom", encoding="utf-8")

    failure = fail(
        conn,
        ws,
        task_id=task.id,
        request=FailureRequest(diagnostics="boom", artifacts=(submission,)),
    )

    assert failure.run.status is RunStatus.FAILED
    assert failure.result.status is ResultStatus.FAILED
    assert store.get_result_for_run(conn, run.id).id == failure.result.id
    (artifact,) = store.list_artifacts_for_run(conn, run.id)
    assert artifact.version == 1
    assert read_content(ws, artifact) == submission.content
    assert failure.diagnostics_path == f"runs/{run.id}/output.log"
    assert log.read_text(encoding="utf-8") == "boom"
