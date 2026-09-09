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

   The one exception is :func:`apply_lifecycle_action`, which is
   **transaction-neutral**: it writes via ``store`` and never commits, so the
   caller -- ``complete-run`` (SF-24), ``decide`` (SF-27) -- can apply the
   evaluated action's Task consequence inside its own atomic write block. It
   must therefore only ever be called from inside a ``with conn:`` block owned
   by the caller.

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
* **Creates a Run only from a resolved lifecycle action, never a next Run.**
  :func:`create_run` turns an :class:`skillflow.evaluator.EvaluationOutput` whose
  ``action`` is ``run`` into a persisted ``running`` Run. It does not decide
  *whether* to run, pick the action, load a Workflow file, launch anything, or
  touch the ``tasks`` row. The *next* Run after a Run completes is still never
  created in the current session (SF-A-3; AGENTS.md): ``complete-run`` must not
  call :func:`create_run`; only ``resolve-task``, which runs in a new session,
  does.

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
    Run,
    RunStatus,
    Task,
    TaskStatus,
    WorkflowDefinition,
)
from skillflow.evaluator import EvaluationOutput
from skillflow.workflow import ActionType, Workflow

__all__ = [
    "UnknownWorkflowError",
    "WorkflowAssignmentError",
    "RunCreationError",
    "register_workflow",
    "create_task",
    "assign_workflow",
    "create_run",
    "apply_lifecycle_action",
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


class RunCreationError(Exception):
    """The Task's state or the named triggering Run forbids creating a Run.

    Raised when the Task does not exist in an ``active`` state, or when
    ``triggered_by_run_id`` names a Run that belongs to a different Task. One
    flat class, matching :class:`UnknownWorkflowError` / :class:`store.
    InvariantViolationError`: callers distinguish causes by message. Deliberately
    **not** a ``ValueError`` (so a caller catching this does not also swallow the
    domain layer's programming errors) and **not** a ``sqlite3.Error`` (this is a
    domain rejection, not a storage failure).

    A duplicate running Run keeps raising ``store.InvariantViolationError``
    unwrapped -- that message already names both Run ids -- so the service adds
    no second pre-check for it.
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


#: Task status that permits creating a Run. Every legitimate creation path --
#: initial resolution (SF-A-5 §4.9), ``complete-run`` with a ``run`` action
#: (§6.9), ``decide`` with a ``run`` action (§7.7, ``waiting_for_human`` ->
#: ``active``) -- reaches an ``active`` Task. Creating a ``running`` Run while the
#: Task waits on a human would strand ``/skillflow:decide``.
_RUNNABLE_TASK_STATUS = TaskStatus.ACTIVE


def create_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    action: EvaluationOutput,
    triggered_by_run_id: str | None = None,
    instructions: str | None = None,
) -> Run:
    """Create a ``running`` Run from a resolved lifecycle action, atomically with
    its ``run.created`` event.

    The trigger provenance is **derived from the action, not supplied as free
    text**: ``trigger_reason`` is ``action.reason`` and ``step_id`` is
    ``action.step``. Both :func:`skillflow.evaluator.resolve_initial_action`
    (``reason == "initial"``) and :func:`skillflow.evaluator.evaluate`
    (``reason == <outcome/decision>``) produce an ``EvaluationOutput``, so the
    initial and subsequent cases go through one code path and the service is
    structurally unable to invent a step or a reason -- the same mechanical
    guarantee :func:`assign_workflow` uses for Workflow selection.

    ``action`` is keyword-only with no default: :func:`create_run` never chooses
    the action.

    Rules, evaluated in this order (the order is the contract -- it fixes error
    precedence):

    1. ``action`` must be an :class:`~skillflow.evaluator.EvaluationOutput` ->
       ``ValueError``.
    2. ``action.action`` must be ``run`` -> ``ValueError``. ``human`` /
       ``complete`` / ``cancel`` do not create a Run (SF-A-5 §6.9 / §7.7).
    3. Task must exist -> ``LookupError`` (matching :func:`assign_workflow`,
       ``artifacts.create_artifact``).
    4. Task must be ``active`` -> :class:`RunCreationError` (see
       ``_RUNNABLE_TASK_STATUS``).
    5. If ``triggered_by_run_id`` is given, that Run must exist and its
       ``task_id`` must equal the Task's id -> :class:`RunCreationError` naming
       both. ``runs.triggered_by_run_id``'s foreign key only proves the Run
       exists, not that it belongs to this Task.
    6. If ``action.step`` is set but the Task has no ``workflow_definition_id``
       -> ``ValueError``: a step id is meaningless without its Workflow
       (unreachable through ``resolve_initial_action``, which raises
       ``WorkflowSelectionRequiredError`` first).
    7. Construct the ``Run`` -- ``created_at == started_at == datetime.now(UTC)``
       read once, ``status`` ``running`` (no ``pending`` state, SF-A-5 §4.7).
       ``domain.Run.__post_init__`` then enforces the two provenance shapes --
       an ``initial`` reason must have no triggering Run; any other reason must
       have one -- and those checks are reused here, not restated.
    8. In one ``with conn:`` block: ``store.insert_run`` then the ``run.created``
       event. ``insert_run`` raises ``store.InvariantViolationError`` if the Task
       already has a running Run, and the transaction rolls back, so no event
       survives.

    Deviation (AGENTS.md principle 14): **``run.started`` is not emitted.** There
    is no ``pending`` state, so creation and start are the same instant; a second
    event with an identical ``created_at`` and no distinct state change is noise.
    ``LifecycleEventType.RUN_STARTED`` stays unused in v0.

    Known dead end: a **skill-targeted action** (SF-A-4 §9) produces a Run with
    ``step_id is None``. When that Run completes, ``evaluator.evaluate`` cannot
    map it -- a Run with no step has no outcome rules -- and no v0 lifecycle rule
    can advance the Task. The action's ``skill`` is recorded in the
    ``run.created`` payload only (the ``Run`` has no skill column). Giving a
    skill-targeted Run a lifecycle meaning is a later issue; ``create_run`` still
    records it, because rejecting a valid action here would contradict SF-A-4 §9.

    Non-goals: it never chooses *whether* to run, never picks the action, never
    loads a Workflow file, never launches anything, never creates a *next* Run
    (``complete-run`` does not call it), and never touches the ``tasks`` row --
    ``resolve-task`` leaves the Task ``active`` (SF-A-5 §4.9).
    """
    if not isinstance(action, EvaluationOutput):
        raise ValueError("create_run() action must be an EvaluationOutput")
    if action.action is not ActionType.RUN:
        raise ValueError(
            f"create_run() action must be 'run', got {action.action.value!r}; "
            "a 'human' / 'complete' / 'cancel' action does not create a Run"
        )

    task = store.get_task(conn, task_id)
    if task is None:
        raise LookupError(f"no task with id {task_id!r}")
    if task.status is not _RUNNABLE_TASK_STATUS:
        raise RunCreationError(
            f"task {task.id!r} is {task.status.value!r}; a Run can only be "
            f"created for an {_RUNNABLE_TASK_STATUS.value!r} Task"
        )

    if triggered_by_run_id is not None:
        trigger = store.get_run(conn, triggered_by_run_id)
        if trigger is None:
            raise RunCreationError(
                f"triggered_by_run_id {triggered_by_run_id!r} names no Run"
            )
        if trigger.task_id != task.id:
            raise RunCreationError(
                f"triggering run {trigger.id!r} belongs to task "
                f"{trigger.task_id!r}, not {task.id!r}"
            )

    if action.step is not None and task.workflow_definition_id is None:
        raise ValueError(
            f"action targets step {action.step!r} but task {task.id!r} has no "
            "workflow definition"
        )

    now = datetime.now(UTC)
    run = Run(
        id=_new_id("run"),
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        workflow_definition_id=task.workflow_definition_id,
        step_id=action.step,
        instructions=instructions,
        triggered_by_run_id=triggered_by_run_id,
        trigger_reason=action.reason,
    )

    payload = {"status": run.status.value, "trigger_reason": run.trigger_reason}
    if run.triggered_by_run_id is not None:
        payload["triggered_by_run_id"] = run.triggered_by_run_id
    if run.workflow_definition_id is not None:
        payload["workflow_definition_id"] = run.workflow_definition_id
    if run.step_id is not None:
        payload["step_id"] = run.step_id
    if action.skill is not None:
        payload["skill"] = action.skill

    with conn:
        store.insert_run(conn, run)
        store.insert_lifecycle_event(
            conn,
            LifecycleEvent(
                id=_new_id("event"),
                task_id=run.task_id,
                run_id=run.id,
                type=LifecycleEventType.RUN_CREATED,
                payload=payload,
                created_at=now,
            ),
        )
    return run


#: The evaluated-action -> Task-status consequence (SF-A-5 §6.9 / §7.7),
#: centralised here so ``complete-run`` (SF-24) and ``decide`` (SF-27) cannot
#: disagree. ``run`` keeps the Task ``active`` -- the next Run is created later
#: by ``resolve-task`` in a new session, never here.
_ACTION_TASK_STATUS = {
    ActionType.RUN: TaskStatus.ACTIVE,
    ActionType.HUMAN: TaskStatus.WAITING_FOR_HUMAN,
    ActionType.COMPLETE: TaskStatus.COMPLETED,
    ActionType.CANCEL: TaskStatus.CANCELLED,
}


def apply_lifecycle_action(
    conn: sqlite3.Connection,
    *,
    task: Task,
    action: EvaluationOutput,
    run_id: str | None = None,
    now: datetime | None = None,
) -> Task:
    """Apply an evaluated lifecycle action's Task-status consequence (SF-24).

    The shared action -> status mapping for the two commands that evaluate the
    lifecycle: ``complete-run`` applies a Result-driven action (SF-A-5 §6.9),
    ``decide`` a Human-Decision-driven one (§7.7). On a real status change the
    ``tasks`` row and one ``task.status_changed`` lifecycle event are written;
    the Task is returned in its post-application state.

    Transaction-neutral -- the deliberate exception to this module's "service
    functions own the transaction" convention (see the module docstring): this
    never commits, so the caller applies the consequence inside its own atomic
    write block. Call it only from inside a ``with conn:`` block.

    Rules, evaluated in this order (the order is the contract -- it fixes error
    precedence):

    1. ``action`` must be an :class:`~skillflow.evaluator.EvaluationOutput` ->
       ``ValueError`` (matching :func:`create_run`).
    2. ``task`` must be a :class:`~skillflow.domain.Task` -> ``ValueError``.
    3. The Task must exist -> ``LookupError`` (matching
       :func:`assign_workflow`, :func:`create_run`). The current status is read
       from the stored row, not from the passed object, so a stale caller
       cannot resurrect an overwritten status.
    4. If the target status equals the stored status, return the stored Task
       unchanged -- no write, no event, no ``updated_at`` bump (matching
       :func:`assign_workflow`'s idempotent no-op). A ``run`` action on an
       ``active`` Task is therefore silent.
    5. Otherwise write ``store.update_task`` plus exactly one
       ``task.status_changed`` event carrying the causing ``run_id`` when given
       and the string-only payload ``{"from", "to", "action", "reason"}``.
       ``updated_at`` and the event share ``now`` (``datetime.now(UTC)`` read
       once when not supplied, so ``complete-run`` reuses its own stamp).

    Non-goals: it never chooses the action, never validates the action against
    a Workflow (the evaluator already did), never creates a Run, and never
    touches any row but this Task's.
    """
    if not isinstance(action, EvaluationOutput):
        raise ValueError("apply_lifecycle_action() action must be an EvaluationOutput")
    if not isinstance(task, Task):
        raise ValueError("apply_lifecycle_action() task must be a Task")

    stored = store.get_task(conn, task.id)
    if stored is None:
        raise LookupError(f"no task with id {task.id!r}")

    target = _ACTION_TASK_STATUS[action.action]
    if stored.status is target:
        return stored

    stamp = datetime.now(UTC) if now is None else now
    updated = replace(stored, status=target, updated_at=stamp)
    store.update_task(conn, updated)
    store.insert_lifecycle_event(
        conn,
        LifecycleEvent(
            id=_new_id("event"),
            task_id=updated.id,
            run_id=run_id,
            type=LifecycleEventType.TASK_STATUS_CHANGED,
            payload={
                "from": stored.status.value,
                "to": target.value,
                "action": action.action.value,
                "reason": action.reason,
            },
            created_at=stamp,
        ),
    )
    return updated
