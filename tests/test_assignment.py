"""Tests for ``skillflow.assignment`` (SF-44).

Following ``test_prepare_artifacts.py``'s shape: a ``tmp_path`` workspace fixture
with a ``.git`` marker, ``store.open_store``, and the frozen
``runtime-reference/software-change.yaml`` copied into ``<root>/workflows/``. Runs
are driven into existence with the real ``resolve_task(...)`` wherever
possible, so the command is exercised against state the system produces.

* **Contract change-detectors** -- the public surface, ``AssignmentError``
  (type and ``code`` attribute), and the AST import boundary (read-only: no
  Claude Code launch, no clock, no filesystem).
* **Behaviour tests** -- current-Run resolution, step-Run and skill-Run
  rebuilds (each equal to the creation-time ``RunInput``), ``--skill``
  verification, step/workflow failures, the shared ``created_skill`` lookup,
  and the read-only proof (no lifecycle mutation).
"""

import ast
import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import assignment as assignment_pkg
from skillflow import store, workspace
from skillflow.artifacts import create_artifact
from skillflow.assignment import (
    AssignmentError,
    created_skill,
    resolve_assignment,
)
from skillflow.domain import (
    LifecycleEvent,
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    TaskStatus,
)
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.workflow_loader import WorkflowLoadError, load_workflow

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


def _artifact(conn, ws, run, *, name, type, content=None):
    return create_artifact(
        conn,
        ws,
        run_id=run.id,
        name=name,
        type=type,
        content=f"{name} content" if content is None else content,
    )


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


def _handbuilt_run(conn, task_id, **over):
    now = datetime.now(UTC)
    kw = dict(
        id="run-manual",
        task_id=task_id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        workflow_definition_id="software-change",
        step_id="requirements",
        trigger_reason="initial",
    )
    run = Run(**(kw | over))
    with conn:
        store.insert_run(conn, run)
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


# --- contract change-detectors --------------------------------------------


def test_public_surface():
    assert set(assignment_pkg.__all__) == {
        "AssignmentError",
        "created_skill",
        "resolve_assignment",
    }


def test_assignment_error_carries_code():
    err = AssignmentError("RunNotFound", "no Run")
    assert isinstance(err, Exception)
    assert err.code == "RunNotFound"
    assert str(err) == "no Run"


def test_module_imports_are_within_the_boundary():
    # assignment inspects but never launches: no subprocess, no shell, no
    # filesystem writes of its own, no clock -- the same mechanical
    # guarantee the prepare-artifacts module pins.
    source = Path(assignment_pkg.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {
        "sqlite3",
        "dataclasses",
        "skillflow",
    }, f"unexpected imports: {modules}"
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


# --- current-Run resolution -------------------------------------------------


def test_no_running_run_rejects_with_run_not_found(conn, ws, workflows):
    _, task = _assigned(conn)
    assert store.list_runs_for_task(conn, task.id) == []

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws)

    assert exc_info.value.code == "RunNotFound"
    assert "skillflow resolve-task <task-id>" in str(exc_info.value)


def test_two_running_runs_reject_naming_both_pairs(conn, ws, workflows):
    _, first = _assigned(conn, title="First")
    _, second = _assigned(conn, title="Second")
    run_a = _handbuilt_run(conn, first.id, id="run-a")
    run_b = _handbuilt_run(conn, second.id, id="run-b")

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws)

    assert exc_info.value.code == "AmbiguousCurrentRun"
    message = str(exc_info.value)
    assert f"task {first.id!r} / run {run_a.id!r}" in message
    assert f"task {second.id!r} / run {run_b.id!r}" in message
    assert "skillflow assignment" in message


def test_task_status_is_not_checked(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)
    updated = replace(task, status=TaskStatus.WAITING_FOR_HUMAN)
    with conn:
        store.update_task(conn, updated)

    rebuilt = resolve_assignment(conn, ws)

    assert rebuilt == created


# --- step-Run rebuild -------------------------------------------------------


def test_step_run_rebuild_equals_the_creation_input(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)

    rebuilt = resolve_assignment(conn, ws)

    assert rebuilt == created
    assert rebuilt.step_id == "requirements"
    assert rebuilt.skill == "requirements-analysis"


def test_step_run_rebuild_selects_the_same_context(conn, ws, workflows):
    _, task = _assigned(conn)
    first = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _artifact(conn, ws, first, name="requirements.md", type="requirements")
    _result(conn, _complete(conn, first), decision="ready", type="requirements")

    created = resolve(conn, ws, task_id=task.id)
    rebuilt = resolve_assignment(conn, ws)

    assert rebuilt == created
    assert [a.name for a in rebuilt.context.artifacts] == ["requirements.md"]
    assert rebuilt.context.unresolved == ("research",)


def test_rebuild_reflects_artifacts_registered_mid_run(conn, ws, workflows):
    # Recomputation reflects stored state, not a creation snapshot: an
    # artifact registered on the running Run itself (possible through the
    # API, though the CLI flow registers only at completion) appears in a
    # later rebuild.
    _, task = _assigned(conn)
    first = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _result(conn, _complete(conn, first), decision="ready", type="requirements")
    created = resolve(conn, ws, task_id=task.id)
    assert created.context.unresolved == ("requirements", "research")
    running = store.get_run(conn, created.run_id)
    _artifact(conn, ws, running, name="findings.md", type="research")

    rebuilt = resolve_assignment(conn, ws)

    assert [a.name for a in rebuilt.context.artifacts] == ["findings.md"]
    assert rebuilt.context.unresolved == ("requirements",)
    assert rebuilt != created


# --- skill-Run rebuild ------------------------------------------------------


def _drive_to_skill_run(conn, ws, task):
    review = _drive_to_review(conn, ws, task)
    _artifact(conn, ws, review, name="review.md", type="review")
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")
    created = resolve(conn, ws, task_id=task.id)
    assert created.step_id is None
    return created


def test_skill_run_rebuild_equals_the_creation_input(conn, ws, workflows):
    _, task = _assigned(conn)
    created = _drive_to_skill_run(conn, ws, task)

    rebuilt = resolve_assignment(conn, ws)

    assert rebuilt == created
    assert rebuilt.skill == "research"
    assert [a.name for a in rebuilt.context.artifacts] == ["review.md"]
    assert rebuilt.context.unresolved == ()


def test_skill_run_context_comes_from_the_triggering_run(conn, ws, workflows):
    # A Task-level artifact from an earlier Run must not leak into a skill
    # Run's trigger-scoped context.
    _, task = _assigned(conn)
    first = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _artifact(conn, ws, first, name="requirements.md", type="requirements")
    _result(conn, _complete(conn, first), decision="ready", type="requirements")
    second = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _result(conn, _complete(conn, second), decision="ready", type="requirements")
    third = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    _result(conn, _complete(conn, third), decision="ready", type="requirements")
    review = store.get_run(conn, resolve(conn, ws, task_id=task.id).run_id)
    review = _complete(conn, review)
    _artifact(conn, ws, review, name="review.md", type="review")
    _result(conn, review, decision="fundamental_assumption_wrong", type="review")
    created = resolve(conn, ws, task_id=task.id)

    rebuilt = resolve_assignment(conn, ws)

    assert rebuilt == created
    assert [a.name for a in rebuilt.context.artifacts] == ["review.md"]


def test_skill_run_without_a_recorded_skill_is_step_unresolved(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-skill", step_id=None)

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws)

    assert exc_info.value.code == "StepUnresolved"
    assert run.id in str(exc_info.value)


# --- skill verification -----------------------------------------------------


def test_expected_skill_match_returns_the_input(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)

    rebuilt = resolve_assignment(conn, ws, expected_skill="requirements-analysis")

    assert rebuilt == created


def test_expected_skill_mismatch_rejects(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws, expected_skill="nope")

    assert exc_info.value.code == "AssignmentMismatch"
    message = str(exc_info.value)
    assert "'nope'" in message
    assert "'requirements-analysis'" in message
    assert created.run_id in message
    assert "skillflow assignment" in message


def test_expected_skill_mismatch_on_a_skill_run_names_the_skill(conn, ws, workflows):
    _, task = _assigned(conn)
    _drive_to_skill_run(conn, ws, task)

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws, expected_skill="code-review")

    assert exc_info.value.code == "AssignmentMismatch"
    assert "'research'" in str(exc_info.value)


def test_blank_expected_skill_never_matches(conn, ws, workflows):
    # The flag is a matcher, not stored data: "" is simply never equal to a
    # real skill, so it mismatches without its own rejection code.
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws, expected_skill="")

    assert exc_info.value.code == "AssignmentMismatch"


# --- step/workflow failures -------------------------------------------------


def test_run_without_workflow_rejects_with_step_unresolved(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-nowf", workflow_definition_id=None)

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws)

    assert exc_info.value.code == "StepUnresolved"
    assert run.id in str(exc_info.value)


def test_step_removed_from_the_definition_is_workflow_mismatch(conn, ws, workflows):
    (workflows / "two.yaml").write_text(
        "name: two\nsteps:\n"
        "  - id: first\n    skill: do-first\n"
        "  - id: second\n    skill: do-second\n",
        encoding="utf-8",
    )
    register_workflow(conn, load_workflow(workflows / "two.yaml"))
    task = create_task(conn, title="Two steps", workflow_definition_id="two")
    created = resolve(conn, ws, task_id=task.id)
    assert created.step_id == "first"
    (workflows / "two.yaml").write_text(
        "name: two\nsteps:\n  - id: second\n    skill: do-second\n",
        encoding="utf-8",
    )

    with pytest.raises(AssignmentError) as exc_info:
        resolve_assignment(conn, ws)

    assert exc_info.value.code == "WorkflowMismatch"
    message = str(exc_info.value)
    assert "'first'" in message
    assert "skillflow assignment" in message


def test_missing_definition_file_propagates(conn, ws, workflows):
    _, task = _assigned(conn)
    resolve(conn, ws, task_id=task.id)
    (workflows / "software-change.yaml").unlink()

    with pytest.raises(WorkflowLoadError):
        resolve_assignment(conn, ws)


# --- created_skill ----------------------------------------------------------


def test_created_skill_returns_the_recorded_skill(conn, ws, workflows):
    _, task = _assigned(conn)
    created = _drive_to_skill_run(conn, ws, task)
    run = store.get_run(conn, created.run_id)

    assert created_skill(conn, run) == "research"


def test_created_skill_without_events_returns_none(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-bare", step_id=None)

    assert created_skill(conn, run) is None


def test_created_skill_first_event_wins(conn, ws, workflows):
    _, task = _assigned(conn)
    created = _drive_to_skill_run(conn, ws, task)
    run = store.get_run(conn, created.run_id)
    now = datetime.now(UTC)
    late = LifecycleEvent(
        id="event-late",
        task_id=task.id,
        run_id=run.id,
        type=LifecycleEventType.RUN_CREATED,
        payload={"skill": "late-skill"},
        created_at=now + timedelta(seconds=60),
    )
    with conn:
        store.insert_lifecycle_event(conn, late)

    assert created_skill(conn, run) == "research"


# --- read-only proof ----------------------------------------------------------


def test_successful_rebuild_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)
    before = _snapshot(conn, task.id, created.run_id)

    resolve_assignment(conn, ws)

    assert _snapshot(conn, task.id, created.run_id) == before


def test_mismatch_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    created = resolve(conn, ws, task_id=task.id)
    before = _snapshot(conn, task.id, created.run_id)

    with pytest.raises(AssignmentError):
        resolve_assignment(conn, ws, expected_skill="nope")

    assert _snapshot(conn, task.id, created.run_id) == before


def test_run_not_found_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    before_tasks = conn.execute("SELECT * FROM tasks").fetchall()
    before_runs = conn.execute("SELECT * FROM runs").fetchall()

    with pytest.raises(AssignmentError):
        resolve_assignment(conn, ws)

    assert conn.execute("SELECT * FROM tasks").fetchall() == before_tasks
    assert conn.execute("SELECT * FROM runs").fetchall() == before_runs


def test_step_unresolved_mutates_nothing(conn, ws, workflows):
    _, task = _assigned(conn)
    run = _handbuilt_run(conn, task.id, id="run-skill", step_id=None)
    before = _snapshot(conn, task.id, run.id)

    with pytest.raises(AssignmentError):
        resolve_assignment(conn, ws)

    assert _snapshot(conn, task.id, run.id) == before
