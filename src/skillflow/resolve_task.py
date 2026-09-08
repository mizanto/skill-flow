"""Deterministic ``resolve-task`` orchestration (SF-20).

The first vertical slice of Skill Flow: turn a Task into exactly one
``running`` Run plus the :class:`~skillflow.run_input.RunInput` the new
Claude Code session needs (SF-A-5 §4). One pipeline, executed in order --
the order is the contract, because it fixes error precedence:

```text
1  load Task ................... missing -> TaskNotFound
2  Task status ................ completed/cancelled -> TaskAlreadyCompleted /
                                                      TaskCancelled
                                 waiting_for_human -> HumanDecisionRequired
3  Run history (one query) .... a running Run -> ActiveRunExists
4  Workflow selection ......... unassigned + no --workflow
                                                    -> WorkflowSelectionRequired
                                 --workflow given -> register + assign
5  load Workflow by id .............. missing/invalid file -> WorkflowLoadError
6  resolve action (initial or subsequent) ................. -> RunNotCompleted /
                                                           ResultMissing /
                                                           EvaluationError
7  validate the action BEFORE writing -> NoLifecycleAction / WorkflowMismatch
8  read the Task's artifacts
9  service.create_run(...)  <- the single write
10 run_input.resolve_run_input(...)   <- pure projection
```

Steps 1-3 and 5-8 are reads only. Step 4 performs the one user-requested
write besides Run creation: recording the explicit ``--workflow`` selection
(register + assign, idempotent on retry). A ``--workflow`` naming a
different definition than the Task's is rejected before anything is
written. Every other rejection therefore happens **before** the single
lifecycle write (step 9), so no path can strand a ``running`` Run.
Lifecycle rules themselves are never decided here: initial resolution and
outcome mapping are delegated to :mod:`skillflow.evaluator`, context to
:func:`skillflow.run_input.resolve_run_input`, persistence to
:mod:`skillflow.service` / :mod:`skillflow.store`.

Boundaries: this module never launches Claude Code, never creates a second
Run, never selects a Workflow on its own (an unassigned Task without an
explicit ``--workflow`` is refused), and performs no LLM call. It imports
only ``sqlite3`` and ``skillflow`` value/layer modules.
"""

import sqlite3

from skillflow.domain import RunStatus, TaskStatus
from skillflow.evaluator import (
    EvaluationInput,
    WorkflowSelectionRequiredError,
    evaluate,
    resolve_initial_action,
)
from skillflow.run_input import RunInput, resolve_run_input
from skillflow.service import assign_workflow, create_run, register_workflow
from skillflow.store import (
    get_result_for_run,
    get_task,
    list_artifacts_for_task,
    list_human_decisions_for_task,
    list_runs_for_task,
)
from skillflow.workflow import ActionType
from skillflow.workflow_loader import list_definition_ids, load_definition
from skillflow.workspace import Workspace

__all__ = ["ResolveTaskError", "resolve_task"]


class ResolveTaskError(Exception):
    """A Task cannot be resolved into a new Run.

    One flat class, matching ``store.InvariantViolationError`` /
    ``EvaluationError``: callers distinguish causes by ``code``, which
    carries SF-A-5 §4.3's identifiers verbatim (``"TaskNotFound"``,
    ``"TaskAlreadyCompleted"``, ``"TaskCancelled"``,
    ``"HumanDecisionRequired"``, ``"ActiveRunExists"``, plus
    ``"WorkflowMismatch"``, ``"NoLifecycleAction"``, ``"RunNotCompleted"``
    and ``"ResultMissing"``) so tests and the Skill layer can branch
    without parsing prose. Every message ends with the concrete next
    command the user should run (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_task(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    task_id: str,
    workflow: str | None = None,
) -> RunInput:
    """Resolve ``task_id`` into a new ``running`` Run and return its ``RunInput``.

    Implements the pipeline in the module docstring verbatim. ``workflow``
    is the explicit ``--workflow`` selection: it is only ever assigned,
    never inferred, and loading it happens before any write so a typo
    fails cleanly. Raises :class:`ResolveTaskError` (with ``code``) for
    every command-level rejection, and lets ``WorkflowSelectionRequiredError``,
    ``WorkflowLoadError``, ``EvaluationError`` and the service/store errors
    propagate unchanged.
    """
    task = get_task(conn, task_id)
    if task is None:
        raise ResolveTaskError(
            "TaskNotFound",
            f"no task with id {task_id!r}; check the id, then run "
            "`skillflow resolve-task <task-id>` again",
        )

    if task.status is TaskStatus.COMPLETED:
        raise ResolveTaskError(
            "TaskAlreadyCompleted",
            f"task {task.id!r} is already 'completed'; a terminal Task takes "
            "no further Runs -- nothing to run",
        )
    if task.status is TaskStatus.CANCELLED:
        raise ResolveTaskError(
            "TaskCancelled",
            f"task {task.id!r} is 'cancelled'; a terminal Task takes no "
            "further Runs -- nothing to run",
        )
    if task.status is TaskStatus.WAITING_FOR_HUMAN:
        raise ResolveTaskError(
            "HumanDecisionRequired",
            f"task {task.id!r} is 'waiting_for_human'; record a decision with "
            f"`/skillflow:decide`, then run `skillflow resolve-task {task.id}`",
        )

    runs = list_runs_for_task(conn, task.id)
    running = next((r for r in runs if r.status is RunStatus.RUNNING), None)
    if running is not None:
        raise ResolveTaskError(
            "ActiveRunExists",
            f"task {task.id!r} already has running run {running.id!r}; finish "
            "it with `/skillflow:complete-run` before resolving another Run",
        )

    if task.workflow_definition_id is None and workflow is None:
        available = list_definition_ids(workspace.workflows_dir)
        if available:
            found = "available Workflow Definitions: " + ", ".join(
                repr(definition_id) for definition_id in available
            )
        else:
            found = (
                "no Workflow Definitions found in "
                f"{workspace.workflows_dir}; add one as <id>.yaml"
            )
        raise WorkflowSelectionRequiredError(
            f"task {task.id!r} has no Workflow Definition and none was given; "
            f"SkillFlow never selects one. {found}; re-run as "
            f"`skillflow resolve-task {task.id} --workflow NAME`"
        )
    if workflow is not None:
        # Load first so a typo fails before any write. Register + assign
        # then reuse the service's idempotence (equal id) -- except the
        # reassignment rejection (different id), which must fire before
        # register_workflow writes the definition row, so a rejected
        # --workflow leaves the database unchanged. In the else branch
        # assign_workflow provably raises WorkflowAssignmentError before any
        # write: the Task exists (step 1), is active (step 2), and names a
        # different definition.
        definition = load_definition(workspace.workflows_dir, workflow)
        if task.workflow_definition_id in (None, definition.name):
            register_workflow(conn, definition)
            task = assign_workflow(
                conn, task_id=task.id, workflow_definition_id=definition.name
            )
        else:
            assign_workflow(
                conn, task_id=task.id, workflow_definition_id=definition.name
            )

    definition = load_definition(workspace.workflows_dir, task.workflow_definition_id)
    # The SF-12 review's finding: resolve_initial_action checks this binding
    # but evaluate does not, and this command calls both -- so it is
    # established once, here, before branching. load_definition already
    # enforces it by construction; this states it explicitly.
    if definition.name != task.workflow_definition_id:
        raise ResolveTaskError(
            "WorkflowMismatch",
            f"workflow {definition.name!r} is not the Workflow assigned to "
            f"task {task.id!r} ({task.workflow_definition_id!r}); fix the "
            f"assignment, then run `skillflow resolve-task {task.id}`",
        )

    if not runs:
        action = resolve_initial_action(task, definition)
        triggered_by_run_id = None
    else:
        current = runs[-1]
        if current.status is not RunStatus.COMPLETED:
            raise ResolveTaskError(
                "RunNotCompleted",
                f"latest run {current.id!r} of task {task.id!r} is "
                f"{current.status.value!r}; only a 'completed' Run advances "
                "the lifecycle -- finish it with `/skillflow:complete-run`, "
                f"then run `skillflow resolve-task {task.id}`",
            )
        result = get_result_for_run(conn, current.id)
        if result is None:
            raise ResolveTaskError(
                "ResultMissing",
                f"completed run {current.id!r} has no canonical Result; "
                "record it with `/skillflow:complete-run`, then run "
                f"`skillflow resolve-task {task.id}`",
            )
        # The store orders by (created_at, id): the last decision on this
        # Run wins. This reproduces decide's routing deterministically in a
        # new session.
        decisions = [
            d
            for d in list_human_decisions_for_task(conn, task.id)
            if d.run_id == current.id
        ]
        action = evaluate(
            EvaluationInput(
                task=task,
                workflow=definition,
                current_run=current,
                result=result,
                human_decision=decisions[-1] if decisions else None,
            )
        )
        triggered_by_run_id = current.id

    if action.action is not ActionType.RUN:
        if action.action is ActionType.HUMAN:
            hint = (
                "record a decision with `/skillflow:decide`, then run "
                f"`skillflow resolve-task {task.id}`"
            )
        else:
            hint = (
                "the Task should already be terminal; apply the outcome with "
                "`/skillflow:complete-run` on the current Run"
            )
        raise ResolveTaskError(
            "NoLifecycleAction",
            f"lifecycle resolved to {action.action.value!r} (reason "
            f"{action.reason!r}); no new Run follows -- {hint}",
        )
    if action.step is None:
        raise ResolveTaskError(
            "NoLifecycleAction",
            f"lifecycle resolved to skill {action.skill!r} with no workflow "
            "step; a skill-targeted Run has no v0 lifecycle meaning (SF-A-4 "
            "§9) -- adjust the Workflow definition, then run "
            f"`skillflow resolve-task {task.id}`",
        )
    step = definition.find_step(action.step)
    if step is None:
        raise ResolveTaskError(
            "WorkflowMismatch",
            f"lifecycle resolved to step {action.step!r}, absent from workflow "
            f"{definition.name!r}; the definition changed under this Task -- "
            f"restore the step, then run `skillflow resolve-task {task.id}`",
        )

    artifacts = list_artifacts_for_task(conn, task.id)
    run = create_run(
        conn, task_id=task.id, action=action, triggered_by_run_id=triggered_by_run_id
    )
    return resolve_run_input(task=task, run=run, step=step, artifacts=artifacts)
