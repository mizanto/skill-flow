"""Tests for ``skillflow.artifacts``.

Two kinds of test, matching ``test_store.py`` / ``test_service.py``:

* **Contract change-detectors** -- the public surface, the import boundary (no
  ``subprocess``/VCS, no Run creation: the mechanical form of two acceptance
  criteria), and the separation of content from metadata.
* **Behaviour tests** -- storage layout and round-trip, the deterministic
  ``(task_id, name)`` version chain and the immutability of earlier versions,
  Run association and the ``artifact.created`` event, every rejection path
  proving neither store was written, atomicity in both directions, and read
  failures.
"""

import ast
import contextlib
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from skillflow import artifacts, store, workspace
from skillflow.artifacts import (
    ArtifactStorageError,
    content_path,
    create_artifact,
    read_content,
)
from skillflow.domain import LifecycleEventType
from skillflow.service import create_task


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    with contextlib.closing(store.open_store(ws)) as connection:
        yield connection


def _seed_task_run(conn, *, title="Do the thing"):
    """Create a Task and a ``running`` Run for it; return ``(task_id, run_id)``.

    Runs are created directly through ``store`` -- Run creation is a later
    issue's service, and this module only ever *reads* a Run.
    """
    from datetime import UTC, datetime

    from skillflow.domain import TRIGGER_REASON_INITIAL, Run, RunStatus

    task = create_task(conn, title=title)
    run = Run(
        id=f"run-{task.id}",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(UTC),
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    with conn:
        store.insert_run(conn, run)
    return task.id, run.id


def _add_run(conn, task_id, run_id, *, triggered_by):
    """Add a second, non-running Run to ``task_id`` (one running Run per Task)."""
    from datetime import UTC, datetime

    from skillflow.domain import Run, RunStatus

    run = Run(
        id=run_id,
        task_id=task_id,
        status=RunStatus.COMPLETED,
        created_at=datetime.now(UTC),
        triggered_by_run_id=triggered_by,
        trigger_reason="follow-up",
    )
    with conn:
        store.insert_run(conn, run)
    return run_id


def _rows(conn, table):
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def _artifact_rows(conn):
    return _rows(conn, "artifacts")


def _files(ws):
    return sorted(p for p in ws.artifacts_dir.rglob("*") if p.is_file())


# --- contract change-detectors ---------------------------------------------


def test_public_surface():
    assert set(artifacts.__all__) == {
        "ArtifactStorageError",
        "create_artifact",
        "content_path",
        "read_content",
    }


def _imported_modules() -> set[str]:
    tree = ast.parse(Path(artifacts.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # ``from skillflow import store`` records ``node.module ==
            # "skillflow"``; resolve it to the submodule actually imported so
            # ``from skillflow import service`` cannot slip past this detector.
            if node.module == "skillflow":
                modules.update(f"skillflow.{alias.name}" for alias in node.names)
            else:
                modules.add(node.module)
    return modules


def test_artifacts_module_imports_are_stdlib_plus_three_skillflow_modules():
    # "Git is not required" is an acceptance criterion: no subprocess, no VCS
    # library. The module also stays out of workflow/evaluator/service, so it
    # cannot acquire lifecycle-routing or execution responsibilities.
    modules = _imported_modules()
    top_level = {name.split(".")[0] for name in modules}
    assert top_level <= ({"sqlite3", "datetime", "pathlib", "uuid"} | {"skillflow"})
    assert {m for m in modules if m.startswith("skillflow")} == {
        "skillflow.domain",
        "skillflow.store",
        "skillflow.workspace",
    }
    assert "subprocess" not in top_level
    assert "git" not in top_level


def test_artifacts_module_creates_no_run_and_launches_nothing():
    # "The next Run is not created in the current session"; "Skill Flow does not
    # automatically launch Claude Code".
    source = Path(artifacts.__file__).read_text()
    for forbidden in ("insert_run", "update_run", "update_task", "Popen", "system("):
        assert forbidden not in source


def test_artifacts_table_has_no_content_bearing_column(conn):
    # "Content and metadata are separate": content lives on the filesystem only.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")}
    assert columns.isdisjoint({"content", "body", "data", "blob"})


# --- storage ----------------------------------------------------------------


def test_create_artifact_writes_content_and_metadata(ws, conn):
    task_id, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="# Plan\n"
    )

    assert artifact.version == 1
    assert artifact.supersedes_id is None
    assert artifact.task_id == task_id
    assert artifact.run_id == run_id
    assert artifact.path == f"{task_id}/plan-v1.md"

    path = ws.artifacts_dir / task_id / "plan-v1.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "# Plan\n"


def test_metadata_round_trips_and_survives_reopening_the_store(ws, conn):
    _, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="body"
    )
    assert store.get_artifact(conn, artifact.id) == artifact

    conn.close()
    with contextlib.closing(store.open_store(ws)) as reopened:
        assert store.get_artifact(reopened, artifact.id) == artifact


def test_read_content_round_trips(ws, conn):
    _, run_id = _seed_task_run(conn)
    content = "héllo — ünïcode\nsecond line\n"
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="notes.md", type="notes", content=content
    )
    assert read_content(ws, artifact) == content


def test_empty_content_is_allowed(ws, conn):
    # An artifact body is content, like Task.description -- not an identifier.
    _, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="empty.md", type="notes", content=""
    )
    assert content_path(ws, artifact).read_text(encoding="utf-8") == ""
    assert read_content(ws, artifact) == ""


def test_content_path_is_absolute_and_inside_the_artifacts_dir(ws, conn):
    _, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
    )
    path = content_path(ws, artifact)
    assert path.is_absolute()
    assert path.is_relative_to(ws.artifacts_dir)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("plan.md", "plan-v1.md"),
        ("plan", "plan-v1"),
        ("a.b.md", "a.b-v1.md"),
        ("review.report.txt", "review.report-v1.txt"),
    ],
)
def test_versioned_filename_derivation(ws, conn, name, expected):
    task_id, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name=name, type="doc", content="x"
    )
    assert artifact.path == f"{task_id}/{expected}"
    assert (ws.artifacts_dir / task_id / expected).is_file()


def test_name_is_stripped_before_use(ws, conn):
    task_id, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="  plan.md  ", type="plan", content="x"
    )
    assert artifact.name == "plan.md"
    assert artifact.path == f"{task_id}/plan-v1.md"


# --- versioning and immutability --------------------------------------------


def test_second_create_is_version_two_and_leaves_version_one_untouched(ws, conn):
    task_id, run_id = _seed_task_run(conn)
    v1 = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="first"
    )
    v1_before = store.get_artifact(conn, v1.id)

    v2 = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="second"
    )

    assert v2.version == 2
    assert v2.supersedes_id == v1.id
    assert v2.id != v1.id
    assert v2.path == f"{task_id}/plan-v2.md"

    # Version 1's row and file are byte-identical to before.
    assert store.get_artifact(conn, v1.id) == v1_before == v1
    assert read_content(ws, v1) == "first"
    assert read_content(ws, v2) == "second"
    assert {p.name for p in _files(ws)} == {"plan-v1.md", "plan-v2.md"}


def test_chain_extends_to_a_third_version(ws, conn):
    _, run_id = _seed_task_run(conn)
    kw = dict(run_id=run_id, name="plan.md", type="plan")
    v1 = create_artifact(conn, ws, content="1", **kw)
    v2 = create_artifact(conn, ws, content="2", **kw)
    v3 = create_artifact(conn, ws, content="3", **kw)

    assert [a.version for a in (v1, v2, v3)] == [1, 2, 3]
    assert v3.supersedes_id == v2.id
    assert v2.supersedes_id == v1.id
    assert v1.supersedes_id is None


def test_version_chain_spans_runs_within_a_task(ws, conn):
    # SF-A-1 §7: plan.md v1 -> v2 -> v3 is a Task-level progression, and v2 is
    # normally produced by a different Run than v1.
    task_id, run_1 = _seed_task_run(conn)
    run_2 = _add_run(conn, task_id, "run-second", triggered_by=run_1)

    v1 = create_artifact(
        conn, ws, run_id=run_1, name="plan.md", type="plan", content="1"
    )
    v2 = create_artifact(
        conn, ws, run_id=run_2, name="plan.md", type="plan", content="2"
    )

    assert v2.version == 2
    assert v2.supersedes_id == v1.id
    assert v2.run_id == run_2
    assert v2.task_id == task_id


def test_different_names_in_one_task_are_independent_chains(ws, conn):
    _, run_id = _seed_task_run(conn)
    plan = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="p"
    )
    review = create_artifact(
        conn, ws, run_id=run_id, name="review.md", type="review", content="r"
    )
    assert plan.version == review.version == 1
    assert review.supersedes_id is None


def test_the_same_name_in_two_tasks_are_independent_chains(ws, conn):
    task_a, run_a = _seed_task_run(conn, title="A")
    task_b, run_b = _seed_task_run(conn, title="B")

    a1 = create_artifact(
        conn, ws, run_id=run_a, name="plan.md", type="plan", content="a"
    )
    b1 = create_artifact(
        conn, ws, run_id=run_b, name="plan.md", type="plan", content="b"
    )

    assert a1.version == b1.version == 1
    assert b1.supersedes_id is None
    assert content_path(ws, a1) != content_path(ws, b1)
    assert a1.path == f"{task_a}/plan-v1.md"
    assert b1.path == f"{task_b}/plan-v1.md"
    assert read_content(ws, a1) == "a"
    assert read_content(ws, b1) == "b"


def test_an_existing_file_at_the_target_path_is_never_overwritten(ws, conn):
    # A crash-orphaned file (content without a row) must not be silently
    # rewritten -- it is metadata/content drift and is reported as such.
    task_id, run_id = _seed_task_run(conn)
    orphan = ws.artifacts_dir / task_id / "plan-v1.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("pre-existing", encoding="utf-8")

    with pytest.raises(ArtifactStorageError) as exc:
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type="plan", content="new"
        )

    assert str(orphan) in str(exc.value)
    assert orphan.read_text(encoding="utf-8") == "pre-existing"
    assert _artifact_rows(conn) == []


# --- Run association and the lifecycle event --------------------------------


def test_create_artifact_emits_exactly_one_artifact_created_event(ws, conn):
    task_id, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
    )

    events = [
        e
        for e in store.list_lifecycle_events_for_task(conn, task_id)
        if e.type is LifecycleEventType.ARTIFACT_CREATED
    ]
    assert len(events) == 1
    event = events[0]
    assert event.run_id == run_id
    assert event.task_id == task_id
    assert dict(event.payload) == {
        "artifact_id": artifact.id,
        "name": "plan.md",
        "type": "plan",
        "version": "1",
        "path": artifact.path,
    }


def test_artifact_from_a_terminal_run_is_allowed(ws, conn):
    # SF-A-2 §9 keeps artifacts from failed Runs; constraining this is not SF-15's.
    task_id, first_run = _seed_task_run(conn)
    completed = _add_run(conn, task_id, "run-done", triggered_by=first_run)
    artifact = create_artifact(
        conn, ws, run_id=completed, name="notes.md", type="notes", content="x"
    )
    assert artifact.run_id == completed


def test_unknown_run_id_raises_lookup_error_and_writes_nothing(ws, conn):
    _seed_task_run(conn)
    with pytest.raises(LookupError):
        create_artifact(
            conn, ws, run_id="run-missing", name="plan.md", type="plan", content="x"
        )
    assert _artifact_rows(conn) == []
    assert _files(ws) == []


# --- rejections: each proves no row, no event, and no file ------------------


def _assert_nothing_written(ws, conn):
    assert _artifact_rows(conn) == []
    assert not [
        e
        for e in _rows(conn, "lifecycle_events")
        if e["type"] == LifecycleEventType.ARTIFACT_CREATED.value
    ]
    assert _files(ws) == []


@pytest.mark.parametrize("name", ["", "   ", "\n\t", None, 42, b"plan.md"])
def test_a_blank_or_non_string_name_is_rejected(ws, conn, name):
    _, run_id = _seed_task_run(conn)
    with pytest.raises(ValueError):
        create_artifact(conn, ws, run_id=run_id, name=name, type="plan", content="x")
    _assert_nothing_written(ws, conn)


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..",
        ".",
        "sub/plan.md",
        "/absolute/plan.md",
        "/",
        "..\\..\\windows",
        "C:plan.md",
        "plan\x00.md",
    ],
)
def test_a_name_that_is_a_path_is_rejected(ws, conn, name):
    _, run_id = _seed_task_run(conn)
    with pytest.raises(ValueError):
        create_artifact(conn, ws, run_id=run_id, name=name, type="plan", content="x")
    _assert_nothing_written(ws, conn)


@pytest.mark.parametrize("type_", ["", "   ", None])
def test_a_blank_type_is_rejected(ws, conn, type_):
    _, run_id = _seed_task_run(conn)
    with pytest.raises(ValueError):
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type=type_, content="x"
        )
    _assert_nothing_written(ws, conn)


@pytest.mark.parametrize("content", [None, 42, b"bytes", ["a"]])
def test_non_string_content_is_rejected(ws, conn, content):
    _, run_id = _seed_task_run(conn)
    with pytest.raises(ValueError):
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type="plan", content=content
        )
    _assert_nothing_written(ws, conn)


def test_a_type_that_contradicts_the_chain_is_rejected(ws, conn):
    # A name carries one artifact type; a typo would silently split the chain.
    _, run_id = _seed_task_run(conn)
    v1 = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="1"
    )
    with pytest.raises(ValueError) as exc:
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type="review", content="2"
        )
    assert "plan" in str(exc.value) and "review" in str(exc.value)

    assert [row["id"] for row in _artifact_rows(conn)] == [v1.id]
    assert {p.name for p in _files(ws)} == {"plan-v1.md"}


# --- atomicity across the two stores ----------------------------------------


def test_a_filesystem_failure_rolls_the_database_back(ws, conn, monkeypatch):
    _, run_id = _seed_task_run(conn)

    def boom(path, content):
        raise ArtifactStorageError("disk on fire")

    monkeypatch.setattr(artifacts, "_write_new_file", boom)
    with pytest.raises(ArtifactStorageError):
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
        )
    _assert_nothing_written(ws, conn)


def test_a_database_failure_leaves_no_orphan_file(ws, conn, monkeypatch):
    _, run_id = _seed_task_run(conn)

    def boom(connection, artifact):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "insert_artifact", boom)
    with pytest.raises(sqlite3.OperationalError):
        create_artifact(
            conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
        )
    assert _files(ws) == []


class _CommitFails(sqlite3.Connection):
    """A connection whose transaction block fails at commit time.

    ``sqlite3.Connection.commit`` is a read-only C attribute and cannot be
    monkeypatched, and ``__exit__`` is where ``with conn:`` commits -- which is
    exactly the moment this test needs to fail: after the content file has been
    written inside the block.
    """

    def __exit__(self, *exc_info):
        raise sqlite3.OperationalError("commit failed")


def test_a_commit_failure_removes_the_written_file(ws, conn):
    task_id, run_id = _seed_task_run(conn)

    failing = sqlite3.connect(ws.db_path, factory=_CommitFails)
    failing.row_factory = sqlite3.Row
    failing.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.OperationalError):
            create_artifact(
                failing, ws, run_id=run_id, name="plan.md", type="plan", content="x"
            )
    finally:
        # Closing rolls the still-open transaction back and releases the write
        # lock, so the fixture connection can read below.
        failing.close()

    assert not (ws.artifacts_dir / task_id / "plan-v1.md").exists()
    assert _files(ws) == []
    assert _artifact_rows(conn) == []


# --- read failures ----------------------------------------------------------


def test_read_content_reports_a_missing_file_with_both_ids(ws, conn):
    _, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
    )
    path = content_path(ws, artifact)
    path.unlink()

    with pytest.raises(ArtifactStorageError) as exc:
        read_content(ws, artifact)
    message = str(exc.value)
    assert artifact.id in message
    assert str(path) in message


def test_content_path_refuses_an_artifact_whose_path_escapes_the_store(ws, conn):
    # An Artifact read back from SQLite is trusted for everything except its
    # path: a row whose ``path`` climbs out of the store is refused, not
    # followed. ``create_artifact`` cannot produce such a row (the name is
    # guarded), so this is the read-side mirror of ``_artifact_name``.
    _, run_id = _seed_task_run(conn)
    good = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
    )
    escaped = dataclasses.replace(good, path="../../../etc/passwd")

    with pytest.raises(ArtifactStorageError) as exc:
        content_path(ws, escaped)
    assert escaped.id in str(exc.value)

    with pytest.raises(ArtifactStorageError):
        read_content(ws, escaped)


def test_read_content_reports_undecodable_content(ws, conn):
    _, run_id = _seed_task_run(conn)
    artifact = create_artifact(
        conn, ws, run_id=run_id, name="plan.md", type="plan", content="x"
    )
    content_path(ws, artifact).write_bytes(b"\xff\xfe\x00invalid")

    with pytest.raises(ArtifactStorageError):
        read_content(ws, artifact)
