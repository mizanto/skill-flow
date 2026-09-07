"""Skill Flow service layer: the first module that *creates* lifecycle state.

Every layer below refuses three things. :mod:`skillflow.domain` value objects
"do not generate ids and do not read a clock"; :mod:`skillflow.store` "does not
generate ids, read a clock, default a timestamp" and never commits. Creating a
Task needs all three, so they live here and nowhere else:

1. **Ids are generated here** (``uuid4``) -- ``domain`` and ``store`` take them
   from the caller.
2. **The clock is read here** (``datetime.now(UTC)``) -- likewise.
3. **Service functions own the transaction and commit.** Each wraps its writes
   in ``with conn:`` so a Task and its ``task.created`` event land as one atomic
   unit. This is the one deliberate convention difference from ``store``, whose
   functions never commit (see the ``store`` module docstring): pushing
   atomicity of that pair onto every caller is the alternative, and it is worse.

**Workflow identity rule (v0):** a Workflow Definition's persisted id *is* its
``Workflow.name``. ``Workflow`` carries no id; a Task must reference a stable
string, not a filesystem path (paths are not portable between checkouts); and
definition names are unique per file. So :func:`register_workflow` builds
``WorkflowDefinition(id=w.name, name=w.name)`` and is idempotent by
construction -- re-registering the same definition on every use is the expected
call pattern. If a future issue needs several definitions sharing a name, that
is a real contract change and belongs to that issue.

Boundaries this module keeps:

* **Reads no files, launches nothing.** :func:`register_workflow` takes an
  already-loaded ``Workflow``; the caller decides which file to load (discovery
  is a later issue). This module imports ``domain``, ``store`` and ``workflow``
  -- never ``workflow_loader``, never ``yaml``.
* **Creates no Run.** :func:`create_task` creates a Task only. The next Run is
  never created here (SF-A-3; AGENTS.md) -- that is a later issue's concern.

**Workflow assignment (v0):** :func:`assign_workflow` binds a *registered*
definition to an *existing* Task. It only ever **fills an empty slot** -- the
one path any specification describes (SF-A-5 §4.4). It never infers a Workflow:
the id is keyword-only with no default and no single-definition fallback, which
is the mechanical guarantee behind "Lifecycle Evaluation never selects a
Workflow". Reassignment and unassignment are out of scope.
"""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from skillflow import store
from skillflow.domain import (
    LifecycleEvent,
    LifecycleEventType,
    Task,
    TaskStatus,
    WorkflowDefinition,
)
from skillflow.workflow import Workflow

__all__ = [
    "UnknownWorkflowError",
    "WorkflowAssignmentError",
    "register_workflow",
    "create_task",
    "assign_workflow",
]

#: Task statuses that forbid Workflow assignment: a Workflow governs future
#: Runs and a terminal Task has none.
_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


class UnknownWorkflowError(Exception):
    """A ``workflow_definition_id`` names no registered definition.

    Raised by both :func:`create_task` and :func:`assign_workflow` before
    anything is written.

    One flat class, matching ``store.InvariantViolationError`` /
    ``workflow_loader.WorkflowLoadError``: callers distinguish causes by message.
    Deliberately **not** a ``ValueError`` (so a caller catching this does not
    also swallow the domain layer's programming errors) and **not** a
    ``sqlite3.Error`` (this is a domain rejection, not a storage failure). The
    ``tasks.workflow_definition_id`` foreign key stays underneath as the
    backstop; this pre-check only turns the bare ``IntegrityError`` into an
    actionable message (AGENTS.md principle 13).
    """


class WorkflowAssignmentError(Exception):
    """The Task's state forbids assigning a Workflow.

    Raised when the Task is terminal (``completed`` / ``cancelled``) or is
    already bound to a *different* definition. One flat class, matching
    :class:`UnknownWorkflowError`: callers distinguish causes by message.
    Deliberately **not** a ``ValueError`` (so a caller catching this does not
    also swallow the domain layer's programming errors) and **not** a
    ``sqlite3.Error`` (this is a domain rejection, not a storage failure).
    """


def _new_id(prefix: str) -> str:
    """Return ``f"{prefix}-{uuid4().hex}"``. Collisions are not defended against."""
    return f"{prefix}-{uuid4().hex}"


def register_workflow(
    conn: sqlite3.Connection, workflow: Workflow
) -> WorkflowDefinition:
    """Register a loaded ``Workflow`` as a ``WorkflowDefinition`` identity row.

    Idempotent: the id is ``workflow.name`` (see the module docstring), so a
    definition already registered is returned unchanged and nothing is written.
    Otherwise the row is inserted and committed.

    A check-then-insert race between two sessions surfaces as
    ``sqlite3.IntegrityError``, unwrapped -- the same stance ``store`` takes on
    duplicate ids.
    """
    definition = WorkflowDefinition(id=workflow.name, name=workflow.name)
    existing = store.get_workflow_definition(conn, definition.id)
    if existing is not None:
        return existing
    with conn:
        store.insert_workflow_definition(conn, definition)
    return definition


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str = "",
    workflow_definition_id: str | None = None,
) -> Task:
    """Create an ``active`` Task atomically with its ``task.created`` event.

    The generated id and timestamps are read back from the returned ``Task``;
    there is no id-injection knob.

    ``title`` / ``description`` are validated by ``domain.Task`` (a blank title
    or a non-string description raises ``ValueError``, propagated). If
    ``workflow_definition_id`` names no registered definition, raises
    :class:`UnknownWorkflowError` before anything is written; register the
    loaded ``Workflow`` with :func:`register_workflow` first.
    """
    now = datetime.now(UTC)
    task = Task(
        id=_new_id("task"),
        title=title,
        description=description,
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        workflow_definition_id=workflow_definition_id,
    )

    # Validate against the domain-normalised id: Task.__post_init__ strips it, so
    # " software-change " must be looked up (and later stored) as "software-change".
    if (
        task.workflow_definition_id is not None
        and store.get_workflow_definition(conn, task.workflow_definition_id) is None
    ):
        raise UnknownWorkflowError(
            f"no workflow definition {task.workflow_definition_id!r} is registered; "
            "load the Workflow and pass it to register_workflow() first"
        )

    payload = {"status": task.status.value}
    if task.workflow_definition_id is not None:
        payload["workflow_definition_id"] = task.workflow_definition_id

    with conn:
        store.insert_task(conn, task)
        store.insert_lifecycle_event(
            conn,
            LifecycleEvent(
                id=_new_id("event"),
                task_id=task.id,
                run_id=None,
                type=LifecycleEventType.TASK_CREATED,
                payload=payload,
                created_at=now,
            ),
        )
    return task


def assign_workflow(
    conn: sqlite3.Connection, *, task_id: str, workflow_definition_id: str
) -> Task:
    """Bind a registered Workflow Definition to an existing Task.

    Fills an empty slot only -- this never chooses a Workflow, so the caller
    must name the definition (SF-A-5 §4.4). On an effective assignment the
    ``tasks`` row and one ``task.workflow_assigned`` lifecycle event are written
    in a single transaction; ``updated_at`` is bumped.

    Rules, evaluated in this order (the order is the contract -- it fixes error
    precedence):

    1. Task must exist -> ``LookupError`` (matching ``store.update_task``).
    2. ``workflow_definition_id`` must be given -- ``None`` raises ``ValueError``;
       assignment does not unassign.
    3. Task must not be terminal (``completed`` / ``cancelled``) ->
       :class:`WorkflowAssignmentError`. ``active`` and ``waiting_for_human``
       are accepted.
    4. The id is normalised by ``domain.Task`` (stripped; ``""`` -> ``ValueError``).
    5. If the normalised id equals the stored one, the **stored** Task is
       returned unchanged -- no write, no event, no ``updated_at`` bump. A
       ``resolve-task`` retry is therefore safe.
    6. If the Task is already bound to a *different* definition ->
       :class:`WorkflowAssignmentError`. Reassignment is a separate concern.
    7. The definition must be registered -> :class:`UnknownWorkflowError`. The
       ``tasks.workflow_definition_id`` foreign key stays underneath as the
       backstop.

    Rules 5-6 are check-then-act: ``store.get_task`` runs before the ``with
    conn:`` block. Two concurrent sessions assigning to the same unbound Task
    can both pass rule 6 and both write -- last commit wins, and the FK still
    guarantees each recorded definition is registered. This matches
    :func:`register_workflow`'s stance on its own check-then-insert race:
    single-session guarantee, FK as the cross-session backstop, distributed
    locking out of scope for v0 (AGENTS.md).
    """
    task = store.get_task(conn, task_id)
    if task is None:
        raise LookupError(f"no task with id {task_id!r}")
    if workflow_definition_id is None:
        raise ValueError(
            "workflow_definition_id is required; "
            "assignment does not unassign a Workflow"
        )
    if task.status in _TERMINAL_TASK_STATUSES:
        raise WorkflowAssignmentError(
            f"task {task.id!r} is {task.status.value!r}; a Workflow governs "
            "future Runs and a terminal Task has none"
        )

    now = datetime.now(UTC)
    updated = replace(
        task, workflow_definition_id=workflow_definition_id, updated_at=now
    )
    if task.workflow_definition_id == updated.workflow_definition_id:
        return task  # already assigned to exactly this definition; no write
    if task.workflow_definition_id is not None:
        raise WorkflowAssignmentError(
            f"task {task.id!r} is already assigned to workflow "
            f"{task.workflow_definition_id!r}; reassignment is not supported"
        )
    if store.get_workflow_definition(conn, updated.workflow_definition_id) is None:
        raise UnknownWorkflowError(
            f"no workflow definition {updated.workflow_definition_id!r} is "
            "registered; load the Workflow and pass it to register_workflow() first"
        )

    with conn:
        store.update_task(conn, updated)
        store.insert_lifecycle_event(
            conn,
            LifecycleEvent(
                id=_new_id("event"),
                task_id=updated.id,
                run_id=None,
                type=LifecycleEventType.TASK_WORKFLOW_ASSIGNED,
                payload={"workflow_definition_id": updated.workflow_definition_id},
                created_at=now,
            ),
        )
    return updated
