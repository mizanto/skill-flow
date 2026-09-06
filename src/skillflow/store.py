"""SQLite persistence for the SkillFlow domain entities.

This module is the thin translation layer between the frozen dataclasses in
:mod:`skillflow.domain` and the SQLite file that :mod:`skillflow.workspace`
creates (Persistence v0, SF-A-2 §4). It consumes :func:`skillflow.workspace.connect`
and adds the seven entity tables; the workspace module stays entity-agnostic so
its "imports no ``skillflow`` module" boundary remains mechanically checkable.

Four boundaries are held deliberately:

1. **Translation only.** Rows in, entities out. This module does not generate
   ids, read a clock, default a timestamp, enforce lifecycle rules (SF-5), or
   read/write Artifact content on the filesystem (SF-15). Callers supply fully
   constructed :mod:`skillflow.domain` instances.
2. **The caller owns the transaction.** Write functions execute statements but do
   **not** commit. Wrap a unit of work in ``with conn:`` -- it commits on success
   and rolls back on exception. A write made outside a committed transaction is
   lost when the connection closes.
3. **Deterministic reads.** Every relationship query carries an explicit
   ``ORDER BY created_at, id``.
4. **Current state is directly queryable** (SF-A-2 §1, §10.1). ``tasks.status`` /
   ``runs.status`` are read straight from their rows. ``lifecycle_events`` is an
   append-only history and is never replayed to reconstruct state.

Serialisation conventions: enum fields are stored as their ``.value`` and coerced
back by the dataclass ``__post_init__`` (an unknown string raises ``ValueError``
there, not here); timestamps are normalised to UTC ISO-8601 text so ``ORDER BY``
as text matches chronology; ``metadata`` / ``payload`` mappings are stored as
JSON objects with sorted keys, and a non-``str`` key or value raises
``ValueError`` on write (``skillflow.domain`` states outright that SF-2 does not
enforce this and SF-4 must).

Referential integrity and duplicate-id errors surface as ``sqlite3.IntegrityError``
and are deliberately not wrapped: SF-5 owns the domain error vocabulary for
invalid mutations. ``update_task`` / ``update_run`` write only their mutable
columns -- ``id``, ``created_at``, ``Run.task_id`` and the Run provenance pair
are never written -- with **no transition validation** (also SF-5), and raise
``LookupError`` rather than silently updating zero rows. The ``tasks`` and
``results`` tables carry a ``CHECK`` mirroring an intra-entity ``domain``
invariant (``updated_at >= created_at``; ``outcome`` all-or-nothing) so a write
that splits the pair across calls cannot persist a row ``domain`` would reject.
"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from skillflow.domain import (
    Artifact,
    HumanDecision,
    LifecycleEvent,
    Outcome,
    Result,
    Run,
    Task,
    WorkflowDefinition,
)
from skillflow.workspace import Workspace, connect

__all__ = [
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
    "list_runs_for_task",
    "list_artifacts_for_task",
    "list_human_decisions_for_task",
    "list_lifecycle_events_for_task",
]

# Parents first so the foreign keys resolve. Column names mirror the domain
# fields exactly (SF-A-2 §4). Ids are ``TEXT PRIMARY KEY NOT NULL`` -- in SQLite a
# non-INTEGER PRIMARY KEY permits NULL without the explicit NOT NULL. Timestamps
# are ``TEXT``; required ones carry NOT NULL.
#
# No UNIQUE(results.run_id), no partial index for one-running-Run-per-Task, no
# transition table: those invariants are SF-5's deliverable, and pre-1.0 has no
# migrations so adding a constraint later is a single DDL edit. No secondary
# indexes -- single-user, local, tens-to-hundreds of rows.
#
# SF-5 also owns cross-parent consistency: on `artifacts` and `human_decisions`
# the `task_id` and `run_id` foreign keys are independent, so a row may name a
# Run that belongs to a different Task. Not constrained here.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id   TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                     TEXT PRIMARY KEY NOT NULL,
    title                  TEXT NOT NULL,
    description            TEXT NOT NULL,
    status                 TEXT NOT NULL,
    workflow_definition_id TEXT REFERENCES workflow_definitions(id),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    -- Mirrors domain.Task.__post_init__: update_task writes updated_at without
    -- created_at, so nothing else keeps the pair ordered. UTC ISO-8601 text
    -- sorts chronologically.
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS runs (
    id                     TEXT PRIMARY KEY NOT NULL,
    task_id                TEXT NOT NULL REFERENCES tasks(id),
    status                 TEXT NOT NULL,
    workflow_definition_id TEXT REFERENCES workflow_definitions(id),
    step_id                TEXT,
    instructions           TEXT,
    triggered_by_run_id    TEXT REFERENCES runs(id),
    trigger_reason         TEXT,
    transcript_ref         TEXT,
    created_at             TEXT NOT NULL,
    started_at             TEXT,
    completed_at           TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id               TEXT PRIMARY KEY NOT NULL,
    run_id           TEXT NOT NULL REFERENCES runs(id),
    status           TEXT NOT NULL,
    outcome_type     TEXT,
    outcome_decision TEXT,
    metadata         TEXT,
    created_at       TEXT NOT NULL,
    CHECK ((outcome_type IS NULL     AND outcome_decision IS NULL)
        OR (outcome_type IS NOT NULL AND outcome_decision IS NOT NULL))
);

-- SF-5 owns the cross-parent invariant: run_id must belong to task_id.
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY NOT NULL,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    run_id        TEXT NOT NULL REFERENCES runs(id),
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    path          TEXT NOT NULL,
    supersedes_id TEXT REFERENCES artifacts(id),
    created_at    TEXT NOT NULL
);

-- SF-5 owns the cross-parent invariant: run_id must belong to task_id.
CREATE TABLE IF NOT EXISTS human_decisions (
    id         TEXT PRIMARY KEY NOT NULL,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    run_id     TEXT NOT NULL REFERENCES runs(id),
    decision   TEXT NOT NULL,
    comment    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id         TEXT PRIMARY KEY NOT NULL,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    run_id     TEXT REFERENCES runs(id),
    type       TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL
);
"""


def open_store(workspace: Workspace) -> sqlite3.Connection:
    """Open the workspace database and ensure the entity tables exist.

    Equivalent to :func:`skillflow.workspace.connect` followed by an idempotent
    ``CREATE TABLE IF NOT EXISTS`` for the seven entity tables. Errors from
    ``connect`` (:class:`~skillflow.workspace.WorkspaceError`,
    :class:`~skillflow.workspace.SchemaVersionError`) propagate unchanged; the
    caller runs :func:`skillflow.workspace.init_workspace` first.

    The caller owns closing the connection (``contextlib.closing``).
    """
    conn = connect(workspace)
    try:
        conn.executescript(_SCHEMA_SQL)
    except BaseException:
        conn.close()
        raise
    return conn


# --- serialisation helpers -------------------------------------------------


def _dt(value: datetime) -> str:
    """Normalise an aware datetime to UTC ISO-8601 text."""
    return value.astimezone(UTC).isoformat()


def _dt_opt(value: datetime | None) -> str | None:
    return None if value is None else _dt(value)


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _parse_dt_opt(text: str | None) -> datetime | None:
    return None if text is None else _parse_dt(text)


def _dump_json(mapping: Mapping[str, str] | None, field_name: str) -> str | None:
    """Serialise a domain mapping to a sorted-key JSON object, or ``None``.

    Raises ``ValueError`` if any key or value is not a ``str``:
    ``skillflow.domain`` accepts non-string keys/values (SF-2 does not serialise)
    and defers the check to the layer that does. A non-``str`` key would
    otherwise be silently coerced by ``json.dumps`` (``{1: "a"}`` -> ``{"1": "a"}``),
    breaking round-trip equality. Sorted keys give byte-stable output.
    """
    if mapping is None:
        return None
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise ValueError(
                f"{field_name} keys must be strings to persist, got "
                f"{type(key).__name__}"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"{field_name}[{key!r}] must be a string to persist, got "
                f"{type(value).__name__}"
            )
    return json.dumps(dict(mapping), sort_keys=True)


def _load_json(text: str | None) -> dict | None:
    return None if text is None else json.loads(text)


# --- row -> entity mappers -----------------------------------------------


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        workflow_definition_id=row["workflow_definition_id"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        task_id=row["task_id"],
        status=row["status"],
        workflow_definition_id=row["workflow_definition_id"],
        step_id=row["step_id"],
        instructions=row["instructions"],
        triggered_by_run_id=row["triggered_by_run_id"],
        trigger_reason=row["trigger_reason"],
        transcript_ref=row["transcript_ref"],
        created_at=_parse_dt(row["created_at"]),
        started_at=_parse_dt_opt(row["started_at"]),
        completed_at=_parse_dt_opt(row["completed_at"]),
    )


def _to_result(row: sqlite3.Row) -> Result:
    outcome = None
    if row["outcome_type"] is not None:
        outcome = Outcome(type=row["outcome_type"], decision=row["outcome_decision"])
    return Result(
        id=row["id"],
        run_id=row["run_id"],
        status=row["status"],
        outcome=outcome,
        metadata=_load_json(row["metadata"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        name=row["name"],
        type=row["type"],
        version=row["version"],
        path=row["path"],
        supersedes_id=row["supersedes_id"],
        created_at=_parse_dt(row["created_at"]),
    )


def _to_workflow_definition(row: sqlite3.Row) -> WorkflowDefinition:
    return WorkflowDefinition(id=row["id"], name=row["name"])


def _to_human_decision(row: sqlite3.Row) -> HumanDecision:
    return HumanDecision(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        decision=row["decision"],
        comment=row["comment"],
        created_at=_parse_dt(row["created_at"]),
    )


def _to_lifecycle_event(row: sqlite3.Row) -> LifecycleEvent:
    return LifecycleEvent(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        type=row["type"],
        payload=_load_json(row["payload"]),
        created_at=_parse_dt(row["created_at"]),
    )


# --- writes --------------------------------------------------------------
#
# Each is a single parameterised statement. Inserts return ``None``; a duplicate
# id or a missing parent surfaces as ``sqlite3.IntegrityError`` (foreign keys are
# ON from ``connect``). None of these commit -- see the module docstring.


def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    """Insert a Task row. Does not commit."""
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, workflow_definition_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            task.id,
            task.title,
            task.description,
            task.status.value,
            task.workflow_definition_id,
            _dt(task.created_at),
            _dt(task.updated_at),
        ),
    )


def insert_run(conn: sqlite3.Connection, run: Run) -> None:
    """Insert a Run row, including its provenance columns. Does not commit."""
    conn.execute(
        "INSERT INTO runs (id, task_id, status, workflow_definition_id, step_id, "
        "instructions, triggered_by_run_id, trigger_reason, transcript_ref, "
        "created_at, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run.id,
            run.task_id,
            run.status.value,
            run.workflow_definition_id,
            run.step_id,
            run.instructions,
            run.triggered_by_run_id,
            run.trigger_reason,
            run.transcript_ref,
            _dt(run.created_at),
            _dt_opt(run.started_at),
            _dt_opt(run.completed_at),
        ),
    )


def insert_result(conn: sqlite3.Connection, result: Result) -> None:
    """Insert a Result row, flattening ``outcome`` into two columns. Does not commit."""
    outcome_type = result.outcome.type if result.outcome is not None else None
    outcome_decision = result.outcome.decision if result.outcome is not None else None
    conn.execute(
        "INSERT INTO results (id, run_id, status, outcome_type, outcome_decision, "
        "metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            result.id,
            result.run_id,
            result.status.value,
            outcome_type,
            outcome_decision,
            _dump_json(result.metadata, "Result.metadata"),
            _dt(result.created_at),
        ),
    )


def insert_artifact(conn: sqlite3.Connection, artifact: Artifact) -> None:
    """Insert an Artifact metadata row. Writes no file. Does not commit."""
    conn.execute(
        "INSERT INTO artifacts (id, task_id, run_id, name, type, version, path, "
        "supersedes_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact.id,
            artifact.task_id,
            artifact.run_id,
            artifact.name,
            artifact.type,
            artifact.version,
            artifact.path,
            artifact.supersedes_id,
            _dt(artifact.created_at),
        ),
    )


def insert_workflow_definition(
    conn: sqlite3.Connection, definition: WorkflowDefinition
) -> None:
    """Insert a Workflow Definition row (identity only in v0). Does not commit."""
    conn.execute(
        "INSERT INTO workflow_definitions (id, name) VALUES (?, ?)",
        (definition.id, definition.name),
    )


def insert_human_decision(conn: sqlite3.Connection, decision: HumanDecision) -> None:
    """Insert a Human Decision row, linked to its Task and Run. Does not commit."""
    conn.execute(
        "INSERT INTO human_decisions (id, task_id, run_id, decision, comment, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            decision.id,
            decision.task_id,
            decision.run_id,
            decision.decision,
            decision.comment,
            _dt(decision.created_at),
        ),
    )


def insert_lifecycle_event(conn: sqlite3.Connection, event: LifecycleEvent) -> None:
    """Append a Lifecycle Event row (history, never replayed). Does not commit."""
    conn.execute(
        "INSERT INTO lifecycle_events (id, task_id, run_id, type, payload, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            event.id,
            event.task_id,
            event.run_id,
            event.type.value,
            _dump_json(event.payload, "LifecycleEvent.payload"),
            _dt(event.created_at),
        ),
    )


def update_task(conn: sqlite3.Connection, task: Task) -> None:
    """Persist the mutable columns of an existing Task from ``task``.

    Writes ``title``, ``description``, ``status``, ``workflow_definition_id`` and
    ``updated_at`` where ``id`` matches. ``id`` and ``created_at`` are immutable
    and are never written. No transition validation -- a domain change is a new
    frozen instance (``dataclasses.replace``) and this writes that snapshot back;
    legal-transition enforcement is SF-5's deliverable.

    Raises ``LookupError`` if no row has ``task.id``; a silent no-op update is the
    exact failure that makes a lifecycle bug unfindable (AGENTS.md principle 13).
    Does not commit.
    """
    cursor = conn.execute(
        "UPDATE tasks SET title = ?, description = ?, status = ?, "
        "workflow_definition_id = ?, updated_at = ? WHERE id = ?",
        (
            task.title,
            task.description,
            task.status.value,
            task.workflow_definition_id,
            _dt(task.updated_at),
            task.id,
        ),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"no task with id {task.id!r}")


def update_run(conn: sqlite3.Connection, run: Run) -> None:
    """Persist the mutable columns of an existing Run from ``run``.

    Writes ``status``, ``workflow_definition_id``, ``step_id``, ``instructions``,
    ``transcript_ref``, ``started_at`` and ``completed_at`` where ``id`` matches.
    The parent ``task_id``, the ``created_at`` stamp and the provenance pair
    (``triggered_by_run_id`` / ``trigger_reason``) are immutable history and are
    never written -- rewriting ``task_id`` would silently re-parent the Run and
    corrupt :func:`list_runs_for_task`. No transition validation (see
    :func:`update_task`).

    Raises ``LookupError`` if no row has ``run.id``. Does not commit.
    """
    cursor = conn.execute(
        "UPDATE runs SET status = ?, workflow_definition_id = ?, step_id = ?, "
        "instructions = ?, transcript_ref = ?, started_at = ?, completed_at = ? "
        "WHERE id = ?",
        (
            run.status.value,
            run.workflow_definition_id,
            run.step_id,
            run.instructions,
            run.transcript_ref,
            _dt_opt(run.started_at),
            _dt_opt(run.completed_at),
            run.id,
        ),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"no run with id {run.id!r}")


# --- reads --------------------------------------------------------------


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    """Return the Task with ``task_id``, or ``None`` if absent."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _to_task(row) if row is not None else None


def get_run(conn: sqlite3.Connection, run_id: str) -> Run | None:
    """Return the Run with ``run_id``, or ``None`` if absent."""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _to_run(row) if row is not None else None


def get_result(conn: sqlite3.Connection, result_id: str) -> Result | None:
    """Return the Result with ``result_id``, or ``None`` if absent."""
    row = conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
    return _to_result(row) if row is not None else None


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    """Return the Artifact metadata with ``artifact_id``, or ``None`` if absent."""
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    return _to_artifact(row) if row is not None else None


def get_workflow_definition(
    conn: sqlite3.Connection, definition_id: str
) -> WorkflowDefinition | None:
    """Return the Workflow Definition with ``definition_id``, or ``None`` if absent."""
    row = conn.execute(
        "SELECT * FROM workflow_definitions WHERE id = ?", (definition_id,)
    ).fetchone()
    return _to_workflow_definition(row) if row is not None else None


def get_result_for_run(conn: sqlite3.Connection, run_id: str) -> Result | None:
    """Return the earliest Result for ``run_id``, or ``None`` if there is none.

    Uses ``LIMIT 1`` and does not assert uniqueness: the one-canonical-Result
    invariant is SF-5's, and asserting it here would pre-empt that issue.
    """
    row = conn.execute(
        "SELECT * FROM results WHERE run_id = ? ORDER BY created_at, id LIMIT 1",
        (run_id,),
    ).fetchone()
    return _to_result(row) if row is not None else None


def list_runs_for_task(conn: sqlite3.Connection, task_id: str) -> list[Run]:
    """Return every Run for ``task_id``, ordered by ``created_at, id``."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at, id", (task_id,)
    ).fetchall()
    return [_to_run(row) for row in rows]


def list_artifacts_for_task(conn: sqlite3.Connection, task_id: str) -> list[Artifact]:
    """Return every Artifact for ``task_id``, ordered by ``created_at, id``."""
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [_to_artifact(row) for row in rows]


def list_human_decisions_for_task(
    conn: sqlite3.Connection, task_id: str
) -> list[HumanDecision]:
    """Return every Human Decision for ``task_id``, ordered by ``created_at, id``."""
    rows = conn.execute(
        "SELECT * FROM human_decisions WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [_to_human_decision(row) for row in rows]


def list_lifecycle_events_for_task(
    conn: sqlite3.Connection, task_id: str
) -> list[LifecycleEvent]:
    """Return every Lifecycle Event for ``task_id``, ordered by ``created_at, id``."""
    rows = conn.execute(
        "SELECT * FROM lifecycle_events WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [_to_lifecycle_event(row) for row in rows]
