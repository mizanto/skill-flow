"""SQLite persistence for the SkillFlow domain entities.

This module is the thin translation layer between the frozen dataclasses in
:mod:`skillflow.domain` and the SQLite file that :mod:`skillflow.workspace`
creates (Persistence v0, SF-A-2 §4). It consumes :func:`skillflow.workspace.connect`
and adds the seven entity tables; the workspace module stays entity-agnostic so
its "imports no ``skillflow`` module" boundary remains mechanically checkable.

Four boundaries are held deliberately:

1. **Translation, plus four persistence invariants.** Rows in, entities out.
   This module does not generate ids, read a clock, default a timestamp, or
   read/write Artifact content on the filesystem -- that boundary belongs to
   :mod:`skillflow.artifacts` (SF-15), the only module that writes both stores.
   It *does* enforce the four cross-entity invariants the domain layer cannot
   (SF-5): one running Run per Task, one canonical Result per Run, legal Run
   status transitions (a Run is never resumed; a terminal Run is final), and
   cross-parent consistency (a row's ``run_id`` must belong to its
   ``task_id``). Each invariant SQLite can
   express is a DDL constraint -- the guarantee, atomic and unbypassable --
   backed by a Python pre-check that raises :class:`InvariantViolationError`
   naming the conflicting ids -- the actionable message. Still deliberately
   absent: Task status transition validation, and rejecting an ``update_*``
   snapshot whose immutable fields diverge from the stored row. Callers supply
   fully constructed :mod:`skillflow.domain` instances.
2. **The caller owns the transaction.** Write functions execute statements but do
   **not** commit. Wrap a unit of work in ``with conn:`` -- it commits on success
   and rolls back on exception. A write made outside a committed transaction is
   lost when the connection closes.
3. **Deterministic reads.** Every relationship query that can return more than
   one row carries an explicit ``ORDER BY created_at, id``.
   :func:`get_result_for_run` is the exception -- ``results.run_id`` is
   ``UNIQUE``, so it can only ever match one row. :func:`latest_artifact`
   orders by ``version DESC`` instead; ``artifacts UNIQUE (task_id, name,
   version)`` makes that ordering total, so no tie-break column is needed.
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

Referential integrity and duplicate-id errors still surface as raw
``sqlite3.IntegrityError`` and are deliberately not wrapped: a missing parent or
a duplicate id is a caller error, not a domain rejection. A *persistence
invariant* violation is distinct -- it raises :class:`InvariantViolationError`
(not a ``sqlite3.Error`` subclass) with a message naming the conflicting ids,
and the DDL constraint stays underneath as the backstop that also closes the
check-then-write race between two sessions in one repository. ``update_task`` /
``update_run`` write only their mutable columns -- ``id``, ``created_at``,
``Run.task_id`` and the Run provenance pair are never written; ``update_run``
additionally rejects an illegal Run status transition, whereas ``update_task``
does not (Task transitions are out of SF-5's scope). Both raise ``LookupError``
rather than silently updating zero rows. The ``tasks`` and ``results`` tables
carry a ``CHECK`` mirroring an intra-entity ``domain`` invariant
(``updated_at >= created_at``; ``outcome`` all-or-nothing) so a write that splits
the pair across calls cannot persist a row ``domain`` would reject.
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
    RunStatus,
    Task,
    WorkflowDefinition,
)
from skillflow.workspace import Workspace, connect

__all__ = [
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
    "list_artifacts_for_run",
    "list_human_decisions_for_task",
    "list_lifecycle_events_for_task",
]


class InvariantViolationError(Exception):
    """A write would break a Skill Flow persistence invariant.

    Raised by the ``insert_*`` / ``update_run`` pre-checks with a message naming
    the conflicting ids, so a caller gets an actionable failure instead of a bare
    ``UNIQUE constraint failed`` / ``FOREIGN KEY constraint failed`` (AGENTS.md
    principle 13). One flat class -- callers distinguish by message. Not a
    :class:`sqlite3.Error`: this is a domain rejection, not a storage failure.
    The matching DDL constraint is the backstop; a violation that races past the
    pre-check (a second session, raw SQL) surfaces as ``sqlite3.IntegrityError``.
    """


# Legal Run status transitions (AGENTS.md "A Run is never resumed"; SF-A-1 §4):
# nothing transitions *into* ``running`` and a terminal Run is final. Same-status
# entries keep ``update_run`` usable for the other mutable columns (e.g. writing
# ``transcript_ref`` on a completed Run). A ``running`` Run may still reach any
# status, itself included. ``test_legal_run_transitions_cover_every_run_status``
# pins the key set to ``RunStatus`` so a new enum member forces a decision here.
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)
_LEGAL_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(RunStatus),
    RunStatus.WAITING_FOR_HUMAN: (
        frozenset({RunStatus.WAITING_FOR_HUMAN}) | _TERMINAL_RUN_STATUSES
    ),
    RunStatus.COMPLETED: frozenset({RunStatus.COMPLETED}),
    RunStatus.FAILED: frozenset({RunStatus.FAILED}),
    RunStatus.CANCELLED: frozenset({RunStatus.CANCELLED}),
}

# Parents first so the foreign keys resolve. Column names mirror the domain
# fields exactly (SF-A-2 §4). Ids are ``TEXT PRIMARY KEY NOT NULL`` -- in SQLite a
# non-INTEGER PRIMARY KEY permits NULL without the explicit NOT NULL. Timestamps
# are ``TEXT``; required ones carry NOT NULL. No secondary indexes for
# performance -- single-user, local, tens-to-hundreds of rows.
#
# SF-5 invariant constraints (the "guarantee" half; the matching Python
# pre-check in the write functions is the "message" half):
#   * runs UNIQUE (task_id, id) -- not an invariant itself (id is already the
#     primary key), it is the parent key the composite foreign keys require.
#   * runs_one_running_per_task partial index -- one running Run per Task. Its
#     WHERE literal must equal RunStatus.RUNNING.value; a change-detector pins it.
#   * results.run_id UNIQUE -- one canonical Result per Run.
#   * composite FOREIGN KEY (task_id, run_id) -> runs(task_id, id) on artifacts,
#     human_decisions and lifecycle_events -- a row's run_id must belong to its
#     task_id. SQLite skips a composite FK when any child column is NULL, so a
#     task-only lifecycle_events row still inserts.
# Run status transitions are not a constraint here: SQLite can only express them
# with a trigger (a second rule language), and the rule guards developer error
# rather than a race. They are a Python pre-check in update_run only.
#
# SF-15 adds one further constraint, artifacts UNIQUE (task_id, name, version):
# one row per logical version, so a new version is a new row and an old version
# is never rewritten.
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
    completed_at           TEXT,
    -- Parent key for the composite (task_id, run_id) foreign keys on artifacts,
    -- human_decisions and lifecycle_events. Not an invariant in itself.
    UNIQUE (task_id, id)
);

CREATE TABLE IF NOT EXISTS results (
    id               TEXT PRIMARY KEY NOT NULL,
    run_id           TEXT NOT NULL UNIQUE REFERENCES runs(id),
    status           TEXT NOT NULL,
    outcome_type     TEXT,
    outcome_decision TEXT,
    metadata         TEXT,
    created_at       TEXT NOT NULL,
    CHECK ((outcome_type IS NULL     AND outcome_decision IS NULL)
        OR (outcome_type IS NOT NULL AND outcome_decision IS NOT NULL))
);

-- Composite foreign key (SF-5): run_id must belong to task_id. It depends on
-- runs' UNIQUE (task_id, id) as its target.
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY NOT NULL,
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    run_id        TEXT NOT NULL,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    path          TEXT NOT NULL,
    supersedes_id TEXT REFERENCES artifacts(id),
    created_at    TEXT NOT NULL,
    FOREIGN KEY (task_id, run_id) REFERENCES runs(task_id, id),
    -- SF-15: one row per logical version. New content is a new version; an
    -- old version is never rewritten. The Python pre-check in
    -- artifacts.create_artifact computes the next version and gives the
    -- actionable message; this is the unbypassable guarantee and closes the
    -- two-session check-then-insert race.
    UNIQUE (task_id, name, version)
);

-- Composite foreign key (SF-5): run_id must belong to task_id, as on artifacts.
CREATE TABLE IF NOT EXISTS human_decisions (
    id         TEXT PRIMARY KEY NOT NULL,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    run_id     TEXT NOT NULL,
    decision   TEXT NOT NULL,
    comment    TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, run_id) REFERENCES runs(task_id, id)
);

-- Composite foreign key (SF-5): run_id must belong to task_id. run_id is
-- nullable and SQLite skips a composite FK when any child column is NULL, so a
-- task-only event still inserts.
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id         TEXT PRIMARY KEY NOT NULL,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    run_id     TEXT,
    type       TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, run_id) REFERENCES runs(task_id, id)
);

-- One running Run per Task (SF-5). The 'running' literal must equal
-- RunStatus.RUNNING.value; a change-detector test pins the two together.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_running_per_task
    ON runs(task_id) WHERE status = 'running';
"""


def open_store(workspace: Workspace) -> sqlite3.Connection:
    """Open the workspace database and ensure the entity tables exist.

    Equivalent to :func:`skillflow.workspace.connect` followed by an idempotent
    ``CREATE TABLE IF NOT EXISTS`` for the seven entity tables and the
    ``runs_one_running_per_task`` partial index. Errors from
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


# --- invariant pre-checks ----------------------------------------------
#
# Cheap SELECTs that turn a constraint failure into an actionable message. The
# DDL constraint remains the guarantee (atomic, unbypassable, closes the
# two-session race); these only improve the error a single caller sees.


def _running_run_id(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Return the id of the Task's ``running`` Run, or ``None``."""
    row = conn.execute(
        "SELECT id FROM runs WHERE task_id = ? AND status = ?",
        (task_id, RunStatus.RUNNING.value),
    ).fetchone()
    return row["id"] if row is not None else None


def _require_run_in_task(
    conn: sqlite3.Connection, entity: str, task_id: str, run_id: str
) -> None:
    """Raise :class:`InvariantViolationError` if ``run_id`` belongs to another Task.

    A *missing* Run is not this check's concern: the composite foreign key
    rejects it as ``sqlite3.IntegrityError``, unchanged from SF-4. Once a Run is
    named, its ``task_id`` is only meaningful relative to that Run.
    """
    row = conn.execute("SELECT task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is not None and row["task_id"] != task_id:
        raise InvariantViolationError(
            f"{entity} names run {run_id!r}, which belongs to task "
            f"{row['task_id']!r}, not to its own task {task_id!r}"
        )


# --- writes --------------------------------------------------------------
#
# Each is a single parameterised statement plus, where an invariant applies, one
# pre-check. Inserts return ``None``; a duplicate id or a missing parent surfaces
# as ``sqlite3.IntegrityError`` (foreign keys are ON from ``connect``); a broken
# persistence invariant raises :class:`InvariantViolationError`. None of these
# commit -- see the module docstring.


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
    """Insert a Run row, including its provenance columns. Does not commit.

    Enforces **one running Run per Task**: if ``run`` is ``running`` and the Task
    already has a running Run, raises :class:`InvariantViolationError` naming
    both. The ``runs_one_running_per_task`` partial index is the backstop.

    Does **not** check that ``triggered_by_run_id`` names a Run belonging to
    ``run.task_id`` -- the foreign key proves only that the Run exists. That
    cross-Task provenance check is ``service.create_run``'s (SF-18); do not add a
    duplicate here. If a later issue adds a composite ``(task_id, id)`` foreign
    key on ``triggered_by_run_id`` it should replace that service check, not sit
    beside it.
    """
    if run.status is RunStatus.RUNNING:
        existing = _running_run_id(conn, run.task_id)
        if existing is not None:
            raise InvariantViolationError(
                f"task {run.task_id!r} already has a running run {existing!r}; "
                f"cannot add running run {run.id!r} (a Run is never resumed -- "
                f"complete, fail or cancel {existing!r} first)"
            )
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
    """Insert a Result row, flattening ``outcome`` into two columns. Does not commit.

    Enforces **one canonical Result per Run**: a second Result for the same Run
    raises :class:`InvariantViolationError`. The ``results.run_id`` UNIQUE
    constraint is the backstop.
    """
    existing = get_result_for_run(conn, result.run_id)
    if existing is not None:
        raise InvariantViolationError(
            f"run {result.run_id!r} already has canonical result {existing.id!r}; "
            f"cannot add result {result.id!r}"
        )
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
    """Insert an Artifact metadata row. Writes no file. Does not commit.

    Enforces **cross-parent consistency**: ``run_id`` must belong to ``task_id``
    (:class:`InvariantViolationError`). The composite foreign key is the backstop.
    """
    _require_run_in_task(conn, "Artifact", artifact.task_id, artifact.run_id)
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
    """Insert a Human Decision row, linked to its Task and Run. Does not commit.

    Enforces **cross-parent consistency**: ``run_id`` must belong to ``task_id``
    (:class:`InvariantViolationError`). The composite foreign key is the backstop.
    """
    _require_run_in_task(conn, "HumanDecision", decision.task_id, decision.run_id)
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
    """Append a Lifecycle Event row (history, never replayed). Does not commit.

    Enforces **cross-parent consistency** when ``run_id`` is set: it must belong
    to ``task_id`` (:class:`InvariantViolationError`). A task-only event
    (``run_id is None``) skips the check, as the composite foreign key does.
    """
    if event.run_id is not None:
        _require_run_in_task(conn, "LifecycleEvent", event.task_id, event.run_id)
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
    and are never written. No Task status transition validation -- a domain
    change is a new frozen instance (``dataclasses.replace``) and this writes
    that snapshot back. That validation is deliberately absent (SF-5 scoped it
    out): it belongs with the lifecycle operations that drive Task status,
    unlike Run transitions, which :func:`update_run` enforces here.

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
    corrupt :func:`list_runs_for_task`.

    Enforces **legal Run status transitions** (``_LEGAL_RUN_TRANSITIONS``): a
    terminal Run is final and nothing returns to ``running``. An illegal change
    raises :class:`InvariantViolationError` and leaves the row untouched. This
    needs no one-running-Run check: nothing transitions *into* ``running``
    except a ``running`` Run staying ``running`` (same row, no count change), so
    the transition rule and the ``runs_one_running_per_task`` index interlock.
    Task transitions are out of SF-5's scope, so :func:`update_task` has no
    equivalent.

    Raises ``LookupError`` if no row has ``run.id`` (a single existence check via
    ``SELECT``, replacing the old ``rowcount`` guard so there is one mechanism).
    A stored ``status`` outside :class:`~skillflow.domain.RunStatus` -- reachable
    only via raw SQL or a future enum removal, i.e. outside this module's
    contract -- surfaces as the ``ValueError`` from coercing it. Does not commit.
    """
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (run.id,)).fetchone()
    if row is None:
        raise LookupError(f"no run with id {run.id!r}")
    current = RunStatus(row["status"])
    if run.status not in _LEGAL_RUN_TRANSITIONS[current]:
        raise InvariantViolationError(
            f"run {run.id!r} cannot change status from {current.value!r} to "
            f"{run.status.value!r} (a Run is never resumed; a terminal Run is final)"
        )
    conn.execute(
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
    """Return the canonical Result for ``run_id``, or ``None`` if there is none.

    ``results.run_id`` is ``UNIQUE`` (SF-5), so there is at most one row and no
    ordering is needed; ``LIMIT 1`` stays as a defensive cap. SF-4's
    ``ORDER BY created_at, id`` tie-break was dropped -- the ``UNIQUE`` (which
    raw SQL cannot bypass either) makes it unreachable.
    """
    row = conn.execute(
        "SELECT * FROM results WHERE run_id = ? LIMIT 1",
        (run_id,),
    ).fetchone()
    return _to_result(row) if row is not None else None


def latest_artifact(
    conn: sqlite3.Connection, task_id: str, name: str
) -> Artifact | None:
    """Return the highest-version Artifact named ``name`` in ``task_id``.

    ``None`` if the Task has no Artifact under that name. The version chain is
    keyed by ``(task_id, name)`` and spans Runs (SF-A-1 §7: ``plan.md`` v1 ->
    v2 -> v3 is a Task-level progression). ``UNIQUE (task_id, name, version)``
    makes ``ORDER BY version DESC`` total, so no tie-break is needed -- the
    same reasoning :func:`get_result_for_run` documents.
    """
    row = conn.execute(
        "SELECT * FROM artifacts WHERE task_id = ? AND name = ? "
        "ORDER BY version DESC LIMIT 1",
        (task_id, name),
    ).fetchone()
    return _to_artifact(row) if row is not None else None


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


def list_artifacts_for_run(conn: sqlite3.Connection, run_id: str) -> list[Artifact]:
    """Return every Artifact registered by ``run_id``, ordered by ``created_at, id``.

    The per-Run half of :func:`list_artifacts_for_task`: SF-16 checks a step's
    declared outputs against the artifacts the *current* Run produced, because
    the ``(task_id, name)`` version chain spans Runs (SF-A-1 §7) and a Task-level
    match would let a rework Run pass on an earlier Run's artifact.
    """
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id",
        (run_id,),
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
