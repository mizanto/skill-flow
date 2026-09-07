"""Tests for ``skillflow.store``.

Two kinds of test, matching ``test_domain.py`` / ``test_workspace.py``:

* **Contract change-detectors** -- the public surface, the exact set of tables,
  the import boundary, the schema version, and the SF-4/SF-15 line (an Artifact
  insert writes no file).
* **Behaviour tests** -- round-trips by full dataclass equality, durability,
  transaction ownership, timestamp normalisation, the ``Outcome`` CHECK, JSON
  columns, provenance, referential integrity, ordered reads, unvalidated
  updates, and "current state is directly queryable".
"""

import ast
import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from skillflow import domain, store
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    Artifact,
    HumanDecision,
    LifecycleEvent,
    LifecycleEventType,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    WorkflowDefinition,
)
from skillflow.store import (
    InvariantViolationError,
    get_artifact,
    get_result,
    get_result_for_run,
    get_run,
    get_task,
    get_workflow_definition,
    insert_artifact,
    insert_human_decision,
    insert_lifecycle_event,
    insert_result,
    insert_run,
    insert_task,
    insert_workflow_definition,
    latest_artifact,
    list_artifacts_for_task,
    list_human_decisions_for_task,
    list_lifecycle_events_for_task,
    list_runs_for_task,
    open_store,
    update_run,
    update_task,
)
from skillflow.workspace import (
    SchemaVersionError,
    Workspace,
    WorkspaceError,
    init_workspace,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
LATER = NOW + timedelta(hours=1)

_CONCEPTUAL_TABLES = {
    "tasks",
    "runs",
    "results",
    "artifacts",
    "workflow_definitions",
    "human_decisions",
    "lifecycle_events",
}


# --- fixtures & builders (test scaffolding, not production abstractions) ------


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    connection = open_store(ws)
    try:
        yield connection
    finally:
        connection.close()


def _task(**over):
    kw = dict(
        id="task-1",
        title="Do the thing",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=EARLIER,
        updated_at=NOW,
    )
    return Task(**(kw | over))


def _run(**over):
    kw = dict(
        id="run-1",
        task_id="task-1",
        status=RunStatus.RUNNING,
        created_at=NOW,
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    return Run(**(kw | over))


def _result(**over):
    kw = dict(
        id="result-1",
        run_id="run-1",
        status=ResultStatus.COMPLETED,
        created_at=NOW,
    )
    return Result(**(kw | over))


def _artifact(**over):
    kw = dict(
        id="artifact-1",
        task_id="task-1",
        run_id="run-1",
        name="plan.md",
        type="plan",
        version=1,
        path="task-1/plan.md",
        created_at=NOW,
    )
    return Artifact(**(kw | over))


def _wfd(**over):
    kw = dict(id="wf-1", name="software-change")
    return WorkflowDefinition(**(kw | over))


def _decision(**over):
    kw = dict(
        id="decision-1",
        task_id="task-1",
        run_id="run-1",
        decision="approved",
        created_at=NOW,
    )
    return HumanDecision(**(kw | over))


def _event(**over):
    kw = dict(
        id="event-1",
        task_id="task-1",
        type=LifecycleEventType.TASK_CREATED,
        created_at=NOW,
    )
    return LifecycleEvent(**(kw | over))


def _seed_task_run(connection, *, task_id="task-1", run_id="run-1"):
    """Insert a parent Task + Run so entity FKs resolve."""
    with connection:
        insert_task(connection, _task(id=task_id))
        insert_run(connection, _run(id=run_id, task_id=task_id))


# --- contract change-detectors ---------------------------------------------


def test_public_surface():
    assert set(store.__all__) == {
        "InvariantViolationError",
        "open_store",
        "insert_task",
        "insert_run",
        "insert_result",
        "insert_artifact",
        "insert_workflow_definition",
        "insert_human_decision",
        "insert_lifecycle_event",
        "update_task",
        "update_run",
        "get_task",
        "get_run",
        "get_result",
        "get_artifact",
        "get_workflow_definition",
        "get_result_for_run",
        "latest_artifact",
        "list_runs_for_task",
        "list_artifacts_for_task",
        "list_human_decisions_for_task",
        "list_lifecycle_events_for_task",
    }


def test_open_store_creates_exactly_the_seven_conceptual_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {row["name"] for row in rows} == _CONCEPTUAL_TABLES


_TABLE_TO_ENTITY = {
    "tasks": domain.Task,
    "runs": domain.Run,
    "results": domain.Result,
    "artifacts": domain.Artifact,
    "workflow_definitions": domain.WorkflowDefinition,
    "human_decisions": domain.HumanDecision,
    "lifecycle_events": domain.LifecycleEvent,
}


@pytest.mark.parametrize("table,entity", list(_TABLE_TO_ENTITY.items()))
def test_ddl_column_names_match_domain_field_names(conn, table, entity):
    # Change-detector for SF-A-2 §4: a rename made consistently in _SCHEMA_SQL and
    # its _to_* mapper round-trips perfectly and passes every other test, while
    # silently drifting from the spec. This is the mechanised form of the
    # hand-check implementation-report.md §7.1 asks the reviewer to perform.
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    fields = {f.name for f in dataclasses.fields(entity)}
    if entity is domain.Result:
        # Documented flattening of `outcome: Outcome | None` into two columns.
        fields = (fields - {"outcome"}) | {"outcome_type", "outcome_decision"}
    assert columns == fields


def _imported_modules() -> set[str]:
    tree = ast.parse(Path(store.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_store_module_imports_are_stdlib_plus_two_skillflow_modules():
    # Mirrors test_workspace_module_imports_stdlib_only: the store may depend on
    # skillflow.domain and skillflow.workspace and nothing else in the package,
    # and on the standard library only otherwise.
    modules = _imported_modules()
    allowed_stdlib = {"json", "sqlite3", "collections", "datetime"}
    top_level = {name.split(".")[0] for name in modules}
    assert top_level <= (allowed_stdlib | {"skillflow"})
    assert {m for m in modules if m.startswith("skillflow")} == {
        "skillflow.domain",
        "skillflow.workspace",
    }


def test_schema_version_is_four():
    from skillflow import workspace

    # SF-15 bumped 3 -> 4 for artifacts UNIQUE (task_id, name, version).
    assert workspace.SCHEMA_VERSION == 4


def test_insert_artifact_writes_no_file(ws, conn):
    # SF-4 persists Artifact metadata only; content is skillflow.artifacts'.
    # This is the line separating store from artifacts, and it stays true.
    _seed_task_run(conn)
    with conn:
        insert_artifact(conn, _artifact())
    assert list(ws.artifacts_dir.iterdir()) == []


def test_artifact_version_is_unique_per_task_and_name(conn):
    # SF-15 DDL backstop: the unbypassable half of "a new version is a new
    # row". artifacts.create_artifact's pre-check gives the actionable
    # message; this closes the two-session check-then-insert race.
    _seed_task_run(conn)
    with conn:
        insert_artifact(conn, _artifact(id="artifact-1"))
    with pytest.raises(sqlite3.IntegrityError), conn:
        insert_artifact(conn, _artifact(id="artifact-2"))


def test_store_has_no_update_artifact():
    # Artifacts are immutable: a new version is a new row, never an edit.
    assert not hasattr(store, "update_artifact")
    assert "update_artifact" not in store.__all__


# --- round-trips: minimal (all optionals None) and maximal -------------------


def test_task_round_trip_minimal(conn):
    task = _task(id="t-min", workflow_definition_id=None)
    with conn:
        insert_task(conn, task)
    assert get_task(conn, "t-min") == task


def test_task_round_trip_maximal(conn):
    with conn:
        insert_workflow_definition(conn, _wfd())
        task = _task(id="t-max", workflow_definition_id="wf-1")
        insert_task(conn, task)
    assert get_task(conn, "t-max") == task


def test_run_round_trip_minimal(conn):
    with conn:
        insert_task(conn, _task())
        run = Run(
            id="r-min",
            task_id="task-1",
            status=RunStatus.RUNNING,
            created_at=NOW,
        )
        insert_run(conn, run)
    got = get_run(conn, "r-min")
    assert got == run
    assert got.started_at is None
    assert got.completed_at is None
    assert got.trigger_reason is None


def test_run_instructions_empty_string_is_distinct_from_none(conn):
    # plan.md §9: an empty-string body round-trips as "", distinct from None,
    # in a nullable TEXT column.
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="r-empty", instructions="", trigger_reason=None))
        # r-null is terminal: two running Runs on one Task is an SF-5 violation,
        # and this test is about the nullable TEXT column, not Run status.
        insert_run(
            conn,
            _run(
                id="r-null",
                instructions=None,
                trigger_reason=None,
                status=RunStatus.COMPLETED,
            ),
        )
    assert get_run(conn, "r-empty").instructions == ""
    assert get_run(conn, "r-null").instructions is None


def test_run_round_trip_maximal(conn):
    with conn:
        insert_workflow_definition(conn, _wfd())
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", trigger_reason=TRIGGER_REASON_INITIAL))
        run = Run(
            id="r-max",
            task_id="task-1",
            status=RunStatus.COMPLETED,
            created_at=NOW,
            workflow_definition_id="wf-1",
            step_id="implement",
            instructions="  do the work  ",
            triggered_by_run_id="run-1",
            trigger_reason="review_changes_requested",
            transcript_ref="claude://transcript/42",
            started_at=NOW,
            completed_at=LATER,
        )
        insert_run(conn, run)
    assert get_run(conn, "r-max") == run


def test_result_round_trip_minimal(conn):
    _seed_task_run(conn)
    result = _result(id="res-min", outcome=None, metadata=None)
    with conn:
        insert_result(conn, result)
    assert get_result(conn, "res-min") == result


def test_result_round_trip_maximal(conn):
    _seed_task_run(conn)
    result = _result(
        id="res-max",
        status=ResultStatus.FAILED,
        outcome=Outcome(type="review", decision="changes_requested"),
        metadata={"reviewer": "opus", "blocking": "2"},
    )
    with conn:
        insert_result(conn, result)
    assert get_result(conn, "res-max") == result


def test_workflow_definition_round_trip(conn):
    definition = _wfd(id="wf-rt", name="  software-change  ")
    with conn:
        insert_workflow_definition(conn, definition)
    assert get_workflow_definition(conn, "wf-rt") == definition


def test_artifact_round_trip_minimal(conn):
    _seed_task_run(conn)
    artifact = _artifact(id="a-min", supersedes_id=None)
    with conn:
        insert_artifact(conn, artifact)
    assert get_artifact(conn, "a-min") == artifact


def test_artifact_round_trip_maximal(conn):
    _seed_task_run(conn)
    with conn:
        insert_artifact(conn, _artifact(id="a-1", version=1))
        artifact = _artifact(id="a-2", version=2, supersedes_id="a-1")
        insert_artifact(conn, artifact)
    assert get_artifact(conn, "a-2") == artifact


def test_human_decision_round_trip_minimal(conn):
    _seed_task_run(conn)
    decision = _decision(id="hd-min", comment=None)
    with conn:
        insert_human_decision(conn, decision)
    assert list_human_decisions_for_task(conn, "task-1") == [decision]


def test_human_decision_round_trip_maximal(conn):
    _seed_task_run(conn)
    decision = _decision(id="hd-max", decision="rejected", comment="  needs work\n")
    with conn:
        insert_human_decision(conn, decision)
    assert list_human_decisions_for_task(conn, "task-1") == [decision]


def test_lifecycle_event_round_trip_minimal(conn):
    with conn:
        insert_task(conn, _task())
        event = _event(id="ev-min", run_id=None, payload=None)
        insert_lifecycle_event(conn, event)
    assert list_lifecycle_events_for_task(conn, "task-1") == [event]


def test_lifecycle_event_round_trip_maximal(conn):
    _seed_task_run(conn)
    event = _event(
        id="ev-max",
        type=LifecycleEventType.RUN_COMPLETED,
        run_id="run-1",
        payload={"result_id": "result-1"},
    )
    with conn:
        insert_lifecycle_event(conn, event)
    assert list_lifecycle_events_for_task(conn, "task-1") == [event]


# --- durability & transaction ownership ------------------------------------


def test_row_survives_commit_close_reopen(ws):
    conn = open_store(ws)
    task = _task(id="t-dur")
    with conn:
        insert_task(conn, task)
    conn.close()

    reopened = open_store(ws)
    try:
        assert get_task(reopened, "t-dur") == task
    finally:
        reopened.close()


def test_with_conn_commits_on_success(conn):
    with conn:
        insert_task(conn, _task(id="t-commit"))
    # A fresh cursor after the block sees the committed row.
    assert get_task(conn, "t-commit") is not None


def test_exception_inside_with_conn_rolls_back(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_task(conn, _task(id="t-roll"))
            insert_task(conn, _task(id="t-roll"))  # duplicate id -> IntegrityError
    assert get_task(conn, "t-roll") is None


def test_write_without_commit_is_lost_after_close(ws):
    conn = open_store(ws)
    insert_task(conn, _task(id="t-nocommit"))  # no commit
    conn.close()

    reopened = open_store(ws)
    try:
        assert get_task(reopened, "t-nocommit") is None
    finally:
        reopened.close()


# --- timestamp normalisation ---------------------------------------------


def test_non_utc_timestamp_round_trips_to_the_same_instant(conn):
    plus_two = timezone(timedelta(hours=2))
    task = _task(id="t-tz", created_at=EARLIER, updated_at=NOW.astimezone(plus_two))
    with conn:
        insert_task(conn, task)

    got = get_task(conn, "t-tz")
    assert got == task  # datetime equality compares instants
    assert got.updated_at == NOW
    assert got.updated_at.utcoffset() == timedelta(0)  # stored & read as UTC


def test_timestamp_ordering_matches_chronology_as_text(conn):
    plus_two = timezone(timedelta(hours=2))
    with conn:
        insert_task(conn, _task())
        # created_at is 14:00+02:00 == 12:00Z, i.e. before the 13:00Z run below.
        insert_run(
            conn,
            _run(
                id="run-early",
                created_at=NOW.astimezone(plus_two),
                trigger_reason=None,
            ),
        )
        # run-late is terminal: one running Run per Task (SF-5). This test is
        # about text ordering of created_at, not status.
        insert_run(
            conn,
            _run(
                id="run-late",
                created_at=LATER,
                trigger_reason=None,
                status=RunStatus.COMPLETED,
            ),
        )

    assert [r.id for r in list_runs_for_task(conn, "task-1")] == [
        "run-early",
        "run-late",
    ]


def test_microseconds_survive_round_trip(conn):
    stamp = datetime(2026, 9, 6, 12, 0, 0, 123456, tzinfo=UTC)
    task = _task(id="t-us", created_at=stamp, updated_at=stamp)
    with conn:
        insert_task(conn, task)
    assert get_task(conn, "t-us").created_at == stamp


# --- Outcome flattening & CHECK ----------------------------------------


def test_outcome_none_round_trips(conn):
    _seed_task_run(conn)
    with conn:
        insert_result(conn, _result(id="res-no-outcome", outcome=None))
    assert get_result(conn, "res-no-outcome").outcome is None


def test_outcome_populated_round_trips(conn):
    _seed_task_run(conn)
    outcome = Outcome(type="review", decision="approved")
    with conn:
        insert_result(conn, _result(id="res-outcome", outcome=outcome))
    assert get_result(conn, "res-outcome").outcome == outcome


def test_half_populated_outcome_is_rejected_by_check(conn):
    _seed_task_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO results (id, run_id, status, outcome_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("res-bad", "run-1", "completed", "review", store._dt(NOW)),
            )


# --- JSON columns -----------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"a": "1", "b": "2"}],
    ids=["none", "empty", "populated"],
)
def test_metadata_json_round_trips(conn, metadata):
    _seed_task_run(conn)
    result = _result(id="res-md", metadata=metadata)
    with conn:
        insert_result(conn, result)
    assert get_result(conn, "res-md") == result


def test_metadata_non_string_value_raises_value_error_naming_the_field(conn):
    _seed_task_run(conn)
    with pytest.raises(ValueError, match="Result.metadata"):
        with conn:
            insert_result(conn, _result(id="res-bad-md", metadata={"count": 3}))


def test_metadata_non_string_key_raises_value_error_naming_the_field(conn):
    # json.dumps would silently coerce {1: "a"} -> {"1": "a"}, breaking round-trip
    # equality; the store rejects it the same way it rejects non-str values.
    _seed_task_run(conn)
    with pytest.raises(ValueError, match="Result.metadata keys must be strings"):
        with conn:
            insert_result(conn, _result(id="res-bad-key", metadata={1: "a"}))


def test_payload_non_string_key_raises_value_error_naming_the_field(conn):
    with conn:
        insert_task(conn, _task())
    with pytest.raises(ValueError, match="LifecycleEvent.payload keys must be strings"):
        with conn:
            insert_lifecycle_event(conn, _event(id="ev-bad-key", payload={2: "b"}))


def test_json_keys_are_written_in_stable_order(conn):
    _seed_task_run(conn)
    with conn:
        insert_result(
            conn, _result(id="res-order", metadata={"b": "2", "a": "1", "c": "3"})
        )
    raw = conn.execute(
        "SELECT metadata FROM results WHERE id = ?", ("res-order",)
    ).fetchone()[0]
    assert raw == '{"a": "1", "b": "2", "c": "3"}'


def test_payload_json_round_trips(conn):
    _seed_task_run(conn)
    event = _event(id="ev-payload", run_id="run-1", payload={"k": "v"})
    with conn:
        insert_lifecycle_event(conn, event)
    assert list_lifecycle_events_for_task(conn, "task-1")[0] == event


def test_payload_non_string_value_raises_value_error_naming_the_field(conn):
    with conn:
        insert_task(conn, _task())
    with pytest.raises(ValueError, match="LifecycleEvent.payload"):
        with conn:
            insert_lifecycle_event(conn, _event(id="ev-bad", payload={"n": 1}))


# --- Run provenance --------------------------------------------------


def test_initial_run_provenance_round_trips(conn):
    with conn:
        insert_task(conn, _task())
        run = _run(id="run-initial", trigger_reason=TRIGGER_REASON_INITIAL)
        insert_run(conn, run)
    got = get_run(conn, "run-initial")
    assert got == run
    assert got.trigger_reason == "initial"
    assert got.triggered_by_run_id is None


def test_triggered_run_provenance_round_trips(conn):
    with conn:
        insert_task(conn, _task())
        # run-1 is terminal: run-2 below is running, and one running Run per Task
        # is an SF-5 invariant. The provenance link under test is unaffected.
        insert_run(
            conn,
            _run(id="run-1", created_at=EARLIER, status=RunStatus.COMPLETED),
        )
        triggered = _run(
            id="run-2",
            created_at=NOW,
            trigger_reason="review_changes_requested",
            triggered_by_run_id="run-1",
        )
        insert_run(conn, triggered)

    got = get_run(conn, "run-2")
    assert got == triggered
    assert got.triggered_by_run_id == "run-1"
    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["run-1", "run-2"]


# --- referential integrity ------------------------------------------


def test_run_with_missing_task_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_run(conn, _run(task_id="ghost"))


def test_result_with_missing_run_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_result(conn, _result(run_id="ghost"))


def test_artifact_naming_a_run_outside_its_task_is_rejected(conn):
    # _artifact names run-1, which exists and belongs to task-1, so task_id
    # "ghost" is a *cross-parent* violation (SF-5), not a missing parent: once a
    # Run is named, task_id is only meaningful relative to it. With run_id
    # NOT NULL + the composite FK, artifacts.task_id REFERENCES tasks(id) is
    # unreachable through the public API -- a harmless belt-and-braces constraint.
    _seed_task_run(conn)
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_artifact(conn, _artifact(task_id="ghost"))


def test_artifact_with_missing_run_is_rejected(conn):
    with conn:
        insert_task(conn, _task())
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_artifact(conn, _artifact(run_id="ghost"))


def test_human_decision_with_missing_run_is_rejected(conn):
    with conn:
        insert_task(conn, _task())
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_human_decision(conn, _decision(run_id="ghost"))


def test_lifecycle_event_with_missing_task_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_lifecycle_event(conn, _event(task_id="ghost"))


def test_run_with_missing_triggered_by_run_is_rejected(conn):
    with conn:
        insert_task(conn, _task())
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_run(
                conn,
                _run(
                    id="run-x",
                    trigger_reason="retry",
                    triggered_by_run_id="ghost",
                ),
            )


def test_artifact_with_missing_supersedes_is_rejected(conn):
    _seed_task_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_artifact(conn, _artifact(id="a-x", version=2, supersedes_id="ghost"))


def test_task_with_missing_workflow_definition_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_task(conn, _task(workflow_definition_id="ghost"))


def test_duplicate_primary_key_is_rejected(conn):
    with conn:
        insert_task(conn, _task(id="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            insert_task(conn, _task(id="dup", title="other"))


def test_lifecycle_event_without_run_id_inserts(conn):
    with conn:
        insert_task(conn, _task())
        insert_lifecycle_event(conn, _event(id="ev-no-run", run_id=None))
    assert list_lifecycle_events_for_task(conn, "task-1")[0].run_id is None


# --- reads ---------------------------------------------------------


@pytest.mark.parametrize(
    "getter",
    [get_task, get_run, get_result, get_artifact, get_workflow_definition],
)
def test_get_returns_none_for_unknown_id(conn, getter):
    assert getter(conn, "nope") is None


def test_get_result_for_run_returns_none_when_absent(conn):
    _seed_task_run(conn)
    assert get_result_for_run(conn, "run-1") is None


@pytest.mark.parametrize(
    "lister",
    [
        list_runs_for_task,
        list_artifacts_for_task,
        list_human_decisions_for_task,
        list_lifecycle_events_for_task,
    ],
)
def test_list_returns_empty_for_unknown_task(conn, lister):
    assert lister(conn, "nope") == []


def test_list_queries_exclude_other_tasks_rows(conn):
    with conn:
        insert_task(conn, _task(id="task-1"))
        insert_task(conn, _task(id="task-2"))
        insert_run(conn, _run(id="run-1", task_id="task-1"))
        insert_run(conn, _run(id="run-2", task_id="task-2"))

    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["run-1"]
    assert [r.id for r in list_runs_for_task(conn, "task-2")] == ["run-2"]


def test_get_result_for_run_links_result_to_run(conn):
    _seed_task_run(conn)
    result = _result(id="res-link")
    with conn:
        insert_result(conn, result)
    assert get_result_for_run(conn, "run-1") == result


def test_get_result_for_run_returns_the_single_canonical_result(conn):
    # SF-5: one canonical Result per Run. A second insert for the same Run is
    # rejected, so get_result_for_run resolves to exactly the one that landed.
    _seed_task_run(conn)
    with conn:
        insert_result(conn, _result(id="res-canonical", created_at=NOW))
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_result(conn, _result(id="res-second", created_at=LATER))
    assert get_result_for_run(conn, "run-1").id == "res-canonical"


def test_list_ordering_is_deterministic_across_equal_created_at(conn):
    with conn:
        insert_task(conn, _task())
        # Same created_at; inserted out of id order. run-a is terminal so the
        # two Runs do not trip the one-running-Run-per-Task invariant (SF-5).
        insert_run(conn, _run(id="run-b", created_at=NOW, trigger_reason=None))
        insert_run(
            conn,
            _run(
                id="run-a",
                created_at=NOW,
                trigger_reason=None,
                status=RunStatus.COMPLETED,
            ),
        )
    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["run-a", "run-b"]


# --- updates: unvalidated, mutable columns only -----------------------


def test_update_task_persists_status_and_timestamp(conn):
    with conn:
        insert_task(conn, _task(id="t-upd"))
        updated = _task(
            id="t-upd",
            status=TaskStatus.COMPLETED,
            created_at=EARLIER,
            updated_at=LATER,
        )
        update_task(conn, updated)
    assert get_task(conn, "t-upd") == updated


def test_update_run_persists_completion(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="r-upd", status=RunStatus.RUNNING))
        completed = _run(
            id="r-upd",
            status=RunStatus.COMPLETED,
            created_at=NOW,
            trigger_reason=TRIGGER_REASON_INITIAL,
            started_at=NOW,
            completed_at=LATER,
        )
        update_run(conn, completed)
    assert get_run(conn, "r-upd") == completed


def test_update_task_on_unknown_id_raises_lookup_error(conn):
    with pytest.raises(LookupError, match="t-ghost"):
        with conn:
            update_task(conn, _task(id="t-ghost"))


def test_update_run_on_unknown_id_raises_and_changes_nothing(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="r-real"))
    with pytest.raises(LookupError, match="r-ghost"):
        with conn:
            update_run(conn, _run(id="r-ghost"))
    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["r-real"]


def test_update_run_cannot_reparent_a_run(conn):
    # task_id is immutable identity: a stale/mis-fetched snapshot whose in-memory
    # task_id was changed must not move the persisted Run to another Task.
    with conn:
        insert_task(conn, _task(id="task-1"))
        insert_task(conn, _task(id="task-2"))
        insert_run(conn, _run(id="r-parent", task_id="task-1"))
        moved = _run(
            id="r-parent",
            task_id="task-2",
            status=RunStatus.COMPLETED,
            created_at=NOW,
            trigger_reason=TRIGGER_REASON_INITIAL,
            completed_at=LATER,
        )
        update_run(conn, moved)

    assert get_run(conn, "r-parent").task_id == "task-1"
    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["r-parent"]
    assert list_runs_for_task(conn, "task-2") == []


def test_update_does_not_rewrite_immutable_columns(conn):
    # created_at (both) and the Run provenance pair are never written back, even
    # when the passed snapshot carries different values.
    with conn:
        insert_task(conn, _task(id="t-imm", created_at=EARLIER, updated_at=EARLIER))
        # r-src is terminal: r-imm below is running, one running Run per Task.
        insert_run(
            conn,
            _run(
                id="r-src",
                task_id="t-imm",
                created_at=EARLIER,
                status=RunStatus.COMPLETED,
            ),
        )
        insert_run(
            conn,
            _run(
                id="r-imm",
                task_id="t-imm",
                created_at=EARLIER,
                trigger_reason=TRIGGER_REASON_INITIAL,
            ),
        )
        update_task(
            conn,
            _task(
                id="t-imm",
                status=TaskStatus.COMPLETED,
                created_at=NOW,  # ignored
                updated_at=LATER,
            ),
        )
        update_run(
            conn,
            _run(
                id="r-imm",
                status=RunStatus.COMPLETED,
                created_at=NOW,  # ignored
                trigger_reason="review_changes_requested",  # ignored
                triggered_by_run_id="r-src",  # ignored
                completed_at=LATER,
            ),
        )

    task = get_task(conn, "t-imm")
    run = get_run(conn, "r-imm")
    assert task.created_at == EARLIER
    assert task.status is TaskStatus.COMPLETED
    assert run.created_at == EARLIER
    assert run.trigger_reason == TRIGGER_REASON_INITIAL
    assert run.triggered_by_run_id is None
    assert run.status is RunStatus.COMPLETED


def test_update_task_rejecting_out_of_order_timestamps_keeps_row_readable(conn):
    # update_task writes updated_at but not created_at. A snapshot whose
    # updated_at precedes the *stored* created_at must fail at the write (the
    # tasks CHECK, mirroring domain.Task) rather than persist a row that
    # get_task -> Task.__post_init__ then refuses to read.
    with conn:
        insert_task(conn, _task(id="t-ord", created_at=NOW, updated_at=NOW))

    stale = _task(id="t-ord", created_at=EARLIER, updated_at=EARLIER)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            update_task(conn, stale)

    got = get_task(conn, "t-ord")  # not poisoned: still readable, still original
    assert got.created_at == NOW
    assert got.updated_at == NOW


# --- SF-5: one running Run per Task ----------------------------------

_TERMINAL_RUN_STATUSES = [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED]


def test_second_running_run_for_a_task_is_rejected(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1"))
    with pytest.raises(InvariantViolationError) as exc:
        with conn:
            insert_run(conn, _run(id="run-2", trigger_reason=None))
    message = str(exc.value)
    assert "run-1" in message and "run-2" in message and "task-1" in message


def test_running_runs_in_different_tasks_are_allowed(conn):
    with conn:
        insert_task(conn, _task(id="task-1"))
        insert_task(conn, _task(id="task-2"))
        insert_run(conn, _run(id="run-1", task_id="task-1"))
        insert_run(conn, _run(id="run-2", task_id="task-2"))
    assert get_run(conn, "run-1").status is RunStatus.RUNNING
    assert get_run(conn, "run-2").status is RunStatus.RUNNING


def test_a_new_running_run_is_allowed_after_the_previous_one_completes(conn):
    # The retry flow: a Run is never resumed, its successor is a new running Run.
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1"))
        update_run(
            conn,
            _run(
                id="run-1",
                status=RunStatus.COMPLETED,
                completed_at=LATER,
                trigger_reason=TRIGGER_REASON_INITIAL,
            ),
        )
        insert_run(
            conn,
            _run(
                id="run-2",
                trigger_reason="review_changes_requested",
                triggered_by_run_id="run-1",
            ),
        )
    assert get_run(conn, "run-2").status is RunStatus.RUNNING


def test_a_rejected_running_run_leaves_no_partial_mutation(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1"))
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_lifecycle_event(conn, _event(id="ev-legal"))
            insert_run(conn, _run(id="run-2", trigger_reason=None))
    # The whole transaction rolled back: the legal insert did not land either.
    assert list_lifecycle_events_for_task(conn, "task-1") == []
    assert [r.id for r in list_runs_for_task(conn, "task-1")] == ["run-1"]


def test_database_rejects_a_second_running_run_written_by_raw_sql(conn):
    # Bypass the Python pre-check: the partial index is the real guarantee.
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1"))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO runs (id, task_id, status, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("run-2", "task-1", "running", store._dt(NOW)),
            )


# --- SF-5: legal Run status transitions -----------------------------


@pytest.mark.parametrize("terminal", _TERMINAL_RUN_STATUSES)
def test_terminal_run_cannot_become_running(conn, terminal):
    with conn:
        insert_task(conn, _task())
        insert_run(
            conn,
            _run(id="run-1", status=terminal, trigger_reason=TRIGGER_REASON_INITIAL),
        )
    with pytest.raises(InvariantViolationError):
        with conn:
            update_run(
                conn,
                _run(
                    id="run-1",
                    status=RunStatus.RUNNING,
                    trigger_reason=TRIGGER_REASON_INITIAL,
                ),
            )
    assert get_run(conn, "run-1").status is terminal  # row not poisoned


def test_waiting_for_human_run_cannot_become_running(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", status=RunStatus.WAITING_FOR_HUMAN))
    with pytest.raises(InvariantViolationError):
        with conn:
            update_run(conn, _run(id="run-1", status=RunStatus.RUNNING))
    assert get_run(conn, "run-1").status is RunStatus.WAITING_FOR_HUMAN


def test_terminal_run_status_cannot_change_to_another_terminal_status(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", status=RunStatus.COMPLETED))
    with pytest.raises(InvariantViolationError):
        with conn:
            update_run(conn, _run(id="run-1", status=RunStatus.FAILED))
    assert get_run(conn, "run-1").status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    "target",
    [
        RunStatus.RUNNING,
        RunStatus.WAITING_FOR_HUMAN,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    ],
)
def test_running_run_can_move_to_each_other_status(conn, target):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1"))
        update_run(
            conn,
            _run(id="run-1", status=target, trigger_reason=TRIGGER_REASON_INITIAL),
        )
    assert get_run(conn, "run-1").status is target


def test_same_status_update_is_allowed_on_a_terminal_run(conn):
    # An over-tight rule would break update_run's other mutable columns.
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", status=RunStatus.COMPLETED))
        update_run(
            conn,
            _run(
                id="run-1",
                status=RunStatus.COMPLETED,
                trigger_reason=TRIGGER_REASON_INITIAL,
                transcript_ref="claude://transcript/7",
            ),
        )
    assert get_run(conn, "run-1").transcript_ref == "claude://transcript/7"


def test_waiting_for_human_run_can_reach_a_terminal_status(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", status=RunStatus.WAITING_FOR_HUMAN))
        update_run(conn, _run(id="run-1", status=RunStatus.CANCELLED))
    assert get_run(conn, "run-1").status is RunStatus.CANCELLED


def test_legal_run_transitions_cover_every_run_status():
    # Change-detector: a new RunStatus member forces an explicit decision;
    # nothing but a running Run may transition *into* running; and a terminal
    # Run is final -- each terminal status maps to itself only, so widening one
    # to another terminal status (a plausible edit) fails here loudly.
    assert set(store._LEGAL_RUN_TRANSITIONS) == set(RunStatus)
    for source, targets in store._LEGAL_RUN_TRANSITIONS.items():
        if RunStatus.RUNNING in targets:
            assert source is RunStatus.RUNNING
    for terminal in _TERMINAL_RUN_STATUSES:
        assert store._LEGAL_RUN_TRANSITIONS[terminal] == frozenset({terminal})


# --- SF-5: one canonical Result per Run -----------------------------


def test_duplicate_result_for_a_run_is_rejected(conn):
    _seed_task_run(conn)
    with conn:
        insert_result(conn, _result(id="result-1"))
    with pytest.raises(InvariantViolationError) as exc:
        with conn:
            insert_result(conn, _result(id="result-2"))
    message = str(exc.value)
    assert "result-1" in message and "result-2" in message and "run-1" in message


def test_results_for_different_runs_are_allowed(conn):
    with conn:
        insert_task(conn, _task())
        insert_run(conn, _run(id="run-1", status=RunStatus.COMPLETED))
        insert_run(conn, _run(id="run-2", trigger_reason=None))
        insert_result(conn, _result(id="result-1", run_id="run-1"))
        insert_result(conn, _result(id="result-2", run_id="run-2"))
    assert get_result_for_run(conn, "run-1").id == "result-1"
    assert get_result_for_run(conn, "run-2").id == "result-2"


def test_database_rejects_a_duplicate_result_written_by_raw_sql(conn):
    _seed_task_run(conn)
    with conn:
        insert_result(conn, _result(id="result-1"))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO results (id, run_id, status, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("result-2", "run-1", "completed", store._dt(NOW)),
            )


# --- SF-5: cross-parent consistency --------------------------------


def _seed_two_tasks_each_with_a_run(connection):
    with connection:
        insert_task(connection, _task(id="task-1"))
        insert_task(connection, _task(id="task-2"))
        insert_run(connection, _run(id="run-1", task_id="task-1"))
        insert_run(connection, _run(id="run-2", task_id="task-2"))


def test_artifact_naming_a_run_from_another_task_is_rejected(conn):
    _seed_two_tasks_each_with_a_run(conn)
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_artifact(conn, _artifact(id="a-x", task_id="task-1", run_id="run-2"))


def test_human_decision_naming_a_run_from_another_task_is_rejected(conn):
    _seed_two_tasks_each_with_a_run(conn)
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_human_decision(
                conn, _decision(id="hd-x", task_id="task-1", run_id="run-2")
            )


def test_lifecycle_event_naming_a_run_from_another_task_is_rejected(conn):
    _seed_two_tasks_each_with_a_run(conn)
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_lifecycle_event(
                conn,
                _event(
                    id="ev-x",
                    task_id="task-1",
                    run_id="run-2",
                    type=LifecycleEventType.RUN_COMPLETED,
                ),
            )


def test_latest_artifact_returns_none_when_the_task_has_no_such_name(conn):
    _seed_task_run(conn)
    with conn:
        insert_artifact(conn, _artifact(id="a-1", name="plan.md"))
    assert latest_artifact(conn, "task-1", "review.md") is None
    assert latest_artifact(conn, "task-missing", "plan.md") is None


def test_latest_artifact_returns_the_highest_version_regardless_of_insert_order(conn):
    # ORDER BY version DESC is total because of UNIQUE (task_id, name, version),
    # so insertion order and created_at cannot change the answer.
    _seed_task_run(conn)
    with conn:
        insert_artifact(conn, _artifact(id="a-2", name="plan.md", version=2))
        insert_artifact(
            conn, _artifact(id="a-1", name="plan.md", version=1, created_at=NOW)
        )
    assert latest_artifact(conn, "task-1", "plan.md").id == "a-2"


def test_latest_artifact_scopes_by_task_and_name(conn):
    _seed_two_tasks_each_with_a_run(conn)
    with conn:
        insert_artifact(
            conn,
            _artifact(id="a-1", task_id="task-1", run_id="run-1", name="plan.md"),
        )
        insert_artifact(
            conn,
            _artifact(
                id="a-2",
                task_id="task-2",
                run_id="run-2",
                name="plan.md",
                version=7,
            ),
        )
        insert_artifact(
            conn,
            _artifact(id="a-3", task_id="task-1", run_id="run-1", name="review.md"),
        )
    assert latest_artifact(conn, "task-1", "plan.md").id == "a-1"
    assert latest_artifact(conn, "task-1", "review.md").id == "a-3"
    assert latest_artifact(conn, "task-2", "plan.md").id == "a-2"


def test_list_artifacts_for_task_cannot_return_a_run_from_another_task(conn):
    # The reason the invariant exists: "durable context per Task" must not leak.
    _seed_two_tasks_each_with_a_run(conn)
    with conn:
        insert_artifact(conn, _artifact(id="a-1", task_id="task-1", run_id="run-1"))
    with pytest.raises(InvariantViolationError):
        with conn:
            insert_artifact(conn, _artifact(id="a-2", task_id="task-1", run_id="run-2"))
    got = list_artifacts_for_task(conn, "task-1")
    assert [a.id for a in got] == ["a-1"]
    assert all(a.run_id == "run-1" for a in got)


def test_database_rejects_a_cross_parent_artifact_written_by_raw_sql(conn):
    _seed_two_tasks_each_with_a_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO artifacts (id, task_id, run_id, name, type, version, "
                "path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("a-x", "task-1", "run-2", "plan.md", "plan", 1, "p", store._dt(NOW)),
            )


# --- SF-5: schema change-detectors --------------------------------


def test_running_literal_in_the_partial_index_matches_run_status_enum():
    assert f"status = '{RunStatus.RUNNING.value}'" in store._SCHEMA_SQL


def test_partial_running_index_is_created_by_open_store(conn):
    # test_open_store_creates_exactly_the_seven_conceptual_tables already proves
    # the index is not mistaken for a table; this confirms it actually exists.
    index = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("runs_one_running_per_task",),
    ).fetchone()
    assert index is not None


# --- current state is directly queryable -----------------------------


def test_current_state_reads_without_any_lifecycle_events(conn):
    with conn:
        insert_task(conn, _task(id="task-1"))
        insert_run(conn, _run(id="run-1"))
        update_task(
            conn,
            _task(
                id="task-1",
                status=TaskStatus.COMPLETED,
                created_at=EARLIER,
                updated_at=LATER,
            ),
        )
        update_run(
            conn,
            _run(
                id="run-1",
                status=RunStatus.COMPLETED,
                created_at=NOW,
                trigger_reason=TRIGGER_REASON_INITIAL,
                completed_at=LATER,
            ),
        )

    assert list_lifecycle_events_for_task(conn, "task-1") == []
    assert get_task(conn, "task-1").status is TaskStatus.COMPLETED
    assert get_run(conn, "run-1").status is RunStatus.COMPLETED

    # Appending events does not change the answer: state is not replayed.
    with conn:
        insert_lifecycle_event(
            conn,
            _event(
                id="ev-1",
                type=LifecycleEventType.TASK_STATUS_CHANGED,
                run_id="run-1",
            ),
        )
    assert get_task(conn, "task-1").status is TaskStatus.COMPLETED
    assert get_run(conn, "run-1").status is RunStatus.COMPLETED


# --- open_store ------------------------------------------------------


def test_open_store_is_idempotent(ws):
    first = open_store(ws)
    try:
        with first:
            insert_task(first, _task(id="t-idem"))
    finally:
        first.close()

    second = open_store(ws)  # schema re-applied; existing rows survive
    try:
        assert get_task(second, "t-idem") == _task(id="t-idem")
        rows = second.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert {row["name"] for row in rows} == _CONCEPTUAL_TABLES
    finally:
        second.close()


def test_open_store_on_uninitialised_workspace_raises(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(WorkspaceError, match="does not exist"):
        open_store(Workspace(root=tmp_path))


def test_open_store_rejects_a_database_stamped_with_an_old_version(ws):
    con = sqlite3.connect(ws.db_path)
    try:
        con.execute("PRAGMA user_version = 1")
        con.commit()
    finally:
        con.close()
    with pytest.raises(SchemaVersionError):
        open_store(ws)
