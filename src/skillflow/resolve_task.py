"""Deterministic ``resolve-task`` orchestration (SF-20).

The first vertical slice of Skill Flow: turn a Task into exactly one
``running`` Run plus the :class:`~skillflow.run_input.RunInput` the new
Claude Code session needs (SF-A-5 §4). One pipeline, executed in order --
the order is the contract, because it fixes error precedence:

```text
1  resolve Task (SF-43)
     task_id None -> active + waiting_for_human Tasks, by (created_at, id)
                       0 rows  -> TaskNotFound (hint: `skillflow start`)
                       >1 rows -> AmbiguousCurrentTask (names each task id)
                       1 row   -> that Task
     task_id given -> get_task ...... missing -> TaskNotFound
2  Task status ................ completed/cancelled -> TaskAlreadyCompleted /
                                                      TaskCancelled
                                 waiting_for_human -> HumanDecisionRequired
   (the waiting rejection names the waiting step's allowed decisions and
   the waiting Run's artifact paths when resolvable -- best-effort via
   decide.resolve_waiting_step, degrading to the generic message on any
   failure so the code never changes, SF-50)
3  workspace running Runs ..... any running Run (of ANY Task) -> ActiveRunExists
                                 (SF-A-7 I11: one running Run per workspace)
4  Workflow selection ......... unassigned + no --workflow
                                                    -> WorkflowSelectionRequired
                                 --workflow given -> register + assign
5  load Workflow by id .............. missing/invalid file -> WorkflowLoadError
6  resolve action (initial or subsequent) ................. -> RunNotCompleted /
                                                           ResultMissing /
                                                           StepUnresolved /
                                                           EvaluationError
   (a failed latest Run retries the same step or skill -- SF-35 -- with
   the skill recovered from its `run.created` payload)
7  validate the action BEFORE writing -> NoLifecycleAction / WorkflowMismatch
8  read artifacts: the Task's (a step Run) or the triggering Run's
   (a skill-targeted Run, SF-32)
9  service.create_run(...)  <- the single write
10 run_input.resolve_stored_run_input(...)
   <- pure projection, shared with `assignment` (SF-44)
```

Steps 1-3 and 5-8 are reads only. Step 4 performs the one user-requested
write besides Run creation: recording the explicit ``--workflow`` selection
(register + assign, idempotent on retry). A ``--workflow`` naming a
different definition than the Task's is rejected before anything is
written. Every other rejection therefore happens **before** the single
lifecycle write (step 9), so no path can strand a ``running`` Run.
Lifecycle rules themselves are never decided here: initial resolution and
outcome mapping are delegated to :mod:`skillflow.evaluator`, context to
:func:`skillflow.run_input.resolve_stored_run_input` (which delegates to
``resolve_run_input`` / ``resolve_skill_run_input`` per half, and which
``assignment`` calls too, so a rebuild is this same value), persistence to
:mod:`skillflow.service` / :mod:`skillflow.store`. The failed-skill-run skill
lookup is likewise the shared :func:`skillflow.assignment.created_skill`.

Boundaries: this module never launches Claude Code, never creates a second
Run, never selects a Workflow on its own (an unassigned Task without an
explicit ``--workflow`` is refused), and performs no LLM call. It imports
only ``sqlite3`` and ``skillflow`` value/layer modules.
"""

import sqlite3

from skillflow.artifacts import ArtifactStorageError, content_path
from skillflow.assignment import created_skill
from skillflow.decide import DecideError, resolve_waiting_step
from skillflow.domain import RunStatus, Task, TaskStatus
from skillflow.evaluator import (
    EvaluationInput,
    WorkflowSelectionRequiredError,
    evaluate,
    resolve_initial_action,
)
from skillflow.run_input import RunInput, resolve_stored_run_input
from skillflow.service import assign_workflow, create_run, register_workflow
from skillflow.store import (
    get_result_for_run,
    get_task,
    list_artifacts_for_run,
    list_artifacts_for_task,
    list_human_decisions_for_task,
    list_running_runs,
    list_runs_for_task,
    list_tasks_by_status,
)
from skillflow.workflow import ActionType
from skillflow.workflow_loader import (
    WorkflowLoadError,
    list_definition_ids,
    load_definition,
)
from skillflow.workspace import Workspace

__all__ = ["ResolveTaskError", "resolve_task"]


class ResolveTaskError(Exception):
    """A Task cannot be resolved into a new Run.

    One flat class, matching ``store.InvariantViolationError`` /
    ``EvaluationError``: callers distinguish causes by ``code``, which
    carries SF-A-5 §4.3's identifiers verbatim (``"TaskNotFound"``,
    ``"TaskAlreadyCompleted"``, ``"TaskCancelled"``,
    ``"HumanDecisionRequired"``, ``"ActiveRunExists"``, plus
    ``"AmbiguousCurrentTask"`` (the omitted-id form, SF-43),
    ``"WorkflowMismatch"``, ``"NoLifecycleAction"``, ``"RunNotCompleted"``,
    ``"ResultMissing"`` and ``"StepUnresolved"`` -- the last for a failed
    skill-targeted Run whose ``run.created`` payload names no skill to
    retry, the same code family ``complete-run``/``fail-run`` use for
    unresolvable skill routing) so tests and the Skill layer can branch
    without parsing prose. Every message ends with the concrete next
    command the user should run (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _waiting_message(conn: sqlite3.Connection, workspace: Workspace, task: Task) -> str:
    """Build the ``HumanDecisionRequired`` message for ``task`` (SF-50).

    Best-effort enrichment, reads only: resolve the waiting step through
    the shared :func:`skillflow.decide.resolve_waiting_step` and list its
    ``decisions`` keys (in Workflow declaration order) plus the waiting
    Run's artifact paths. Any failure -- unresolvable history
    (``DecideError``), a missing/changed Workflow (``WorkflowLoadError``),
    or a stored path escaping the store (``ArtifactStorageError``) --
    degrades to the generic message, so a waiting Task always reports
    ``HumanDecisionRequired`` and never leaks another code or tracebacks.
    A waiting Run with no artifacts lists decisions without an
    ``artifacts:`` segment.
    """
    try:
        _, step, waiting_run, _ = resolve_waiting_step(conn, workspace, task)
        rendered = []
        for artifact in list_artifacts_for_run(conn, waiting_run.id):
            # Twin of ``cli._artifact_relpath`` (SF-44): this module cannot
            # import ``cli`` (``cli`` imports this module), so the one-line
            # derivation is duplicated -- the codebase convention for tiny
            # helpers. Pure path derivation: the file is never read, so a
            # missing file still prints.
            path = (
                content_path(workspace, artifact).relative_to(workspace.root).as_posix()
            )
            rendered.append(
                f"{artifact.name} ({artifact.type} v{artifact.version}): {path}"
            )
    except (
        DecideError,
        WorkflowLoadError,
        ArtifactStorageError,
    ):
        return (
            f"task {task.id!r} is 'waiting_for_human'; record a decision "
            "with `skillflow decide <decision>`, then run "
            f"`skillflow resolve-task {task.id}`"
        )
    message = (
        f"task {task.id!r} is 'waiting_for_human' "
        f"(step {step.id!r} of run {waiting_run.id!r}); allowed decisions: "
        + ", ".join(step.decisions)
    )
    if rendered:
        message += "; artifacts: " + "; ".join(rendered)
    message += (
        "; record a decision with `skillflow decide <decision> --task "
        f"{task.id}`, then run `skillflow resolve-task {task.id}`"
    )
    return message


def resolve_task(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    task_id: str | None = None,
    workflow: str | None = None,
) -> RunInput:
    """Resolve ``task_id`` into a new ``running`` Run and return its ``RunInput``.

    Implements the pipeline in the module docstring verbatim. ``task_id``
    ``None`` selects the workspace's single non-terminal Task (SF-43). ``workflow``
    is the explicit ``--workflow`` selection: it is only ever assigned,
    never inferred, and loading it happens before any write so a typo
    fails cleanly. Raises :class:`ResolveTaskError` (with ``code``) for
    every command-level rejection, and lets ``WorkflowSelectionRequiredError``,
    ``WorkflowLoadError``, ``EvaluationError`` and the service/store errors
    propagate unchanged.
    """
    if task_id is None:
        candidates = sorted(
            list_tasks_by_status(conn, TaskStatus.ACTIVE)
            + list_tasks_by_status(conn, TaskStatus.WAITING_FOR_HUMAN),
            key=lambda candidate: (candidate.created_at, candidate.id),
        )
        if not candidates:
            raise ResolveTaskError(
                "TaskNotFound",
                "no active or waiting_for_human Task in this workspace; "
                "create one with `skillflow start`, then run "
                "`skillflow resolve-task`",
            )
        if len(candidates) > 1:
            ids = ", ".join(repr(candidate.id) for candidate in candidates)
            raise ResolveTaskError(
                "AmbiguousCurrentTask",
                f"more than one non-terminal Task in this workspace ({ids}); "
                "re-run as `skillflow resolve-task <task-id>`",
            )
        task = candidates[0]
    else:
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
            _waiting_message(conn, workspace, task),
        )

    # Workspace-wide (SF-43): a running Run of ANY Task blocks resolution.
    # This subsumes the former per-Task check. Several rows are possible only
    # in a pre-SF-43 workspace or the accepted concurrent race; the oldest
    # is named.
    running = list_running_runs(conn)
    if running:
        raise ResolveTaskError(
            "ActiveRunExists",
            f"run {running[0].id!r} of task {running[0].task_id!r} is already "
            "running in this workspace; finish it with "
            "`skillflow complete-run` before resolving another Run",
        )
    runs = list_runs_for_task(conn, task.id)

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
        if current.status not in (RunStatus.COMPLETED, RunStatus.FAILED):
            # A `running` latest Run is unreachable here -- step 3 rejects an
            # active Run first -- so this branch names a terminal-but-closed
            # Run (cancelled) or a parked one: nothing to finish, only
            # history to investigate.
            raise ResolveTaskError(
                "RunNotCompleted",
                f"latest run {current.id!r} of task {task.id!r} is "
                f"{current.status.value!r}; only a 'completed' or 'failed' "
                "Run advances the lifecycle -- investigate the Run history, "
                f"then run `skillflow resolve-task {task.id}`",
            )
        result = get_result_for_run(conn, current.id)
        if result is None:
            hint = (
                "record it with `skillflow complete-run`"
                if current.status is RunStatus.COMPLETED
                else "investigate the Run history (a failed Run cannot complete)"
            )
            raise ResolveTaskError(
                "ResultMissing",
                f"{current.status.value} run {current.id!r} has no canonical "
                f"Result; {hint}, then run "
                f"`skillflow resolve-task {task.id}`",
            )
        if current.status is RunStatus.FAILED and current.step_id is None:
            # A failed skill-targeted Run (SF-35): the retry re-issues the
            # recorded skill, resolved from the Run's own `run.created`
            # payload -- the one shared lookup (SF-44), also used by
            # `fail_run` and `assignment`.
            skill = created_skill(conn, current)
            if skill is None:
                raise ResolveTaskError(
                    "StepUnresolved",
                    f"run {current.id!r} is skill-targeted but its "
                    "`run.created` event names no skill, so the retry "
                    "target cannot be resolved -- investigate the Run "
                    "history, then run "
                    f"`skillflow resolve-task {task.id}`",
                )
        else:
            skill = None
        # The store orders by (created_at, id): the last decision on this
        # Run wins. This reproduces decide's routing deterministically in a
        # new session. (A failed Run never carries one -- `decide` answers a
        # completed Run only -- so this filter is empty on the retry path.)
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
                current_skill=skill,
            )
        )
        triggered_by_run_id = current.id

    if action.action is not ActionType.RUN:
        if action.action is ActionType.HUMAN:
            hint = (
                "record a decision with `skillflow decide <decision>`, then "
                f"run `skillflow resolve-task {task.id}`"
            )
        else:
            hint = (
                "the Task should already be terminal -- the definition "
                "changed under this Task or its history was edited; restore "
                "the definition, then run "
                f"`skillflow resolve-task {task.id}`"
            )
        raise ResolveTaskError(
            "NoLifecycleAction",
            f"lifecycle resolved to {action.action.value!r} (reason "
            f"{action.reason!r}); no new Run follows -- {hint}",
        )
    if action.step is None:
        # A skill-targeted Run (SF-32 gap-fill for SF-A-4 §9): no workflow
        # step, so its context is the triggering Run's registered artifacts.
        # `triggered_by_run_id` is always set here -- initial actions target
        # steps[0] by construction -- but the lookup is total anyway.
        trigger_artifacts = (
            list_artifacts_for_run(conn, triggered_by_run_id)
            if triggered_by_run_id is not None
            else ()
        )
        run = create_run(
            conn,
            task_id=task.id,
            action=action,
            triggered_by_run_id=triggered_by_run_id,
        )
        return resolve_stored_run_input(
            task=task,
            run=run,
            step=None,
            skill=action.skill,
            artifacts=trigger_artifacts,
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
    return resolve_stored_run_input(
        task=task, run=run, step=step, skill=None, artifacts=artifacts
    )
