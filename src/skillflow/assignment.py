"""Deterministic ``assignment`` inspection (SF-44).

The Execution Skill contract's read end: given the workspace's single running
Run, rebuild the :class:`~skillflow.run_input.RunInput` ``resolve-task``
returned at creation -- the Run's Assignment -- so a forked Run can obtain
and verify it, and an interrupted Run can be re-dispatched, without searching
the filesystem or reading mutable working files. One pipeline, executed in
order -- the order is the contract, because it fixes error precedence:

```text
1  resolve the current Run
     store.list_running_runs(conn)
       0 rows  -> RunNotFound
       >1 rows -> AmbiguousCurrentRun (name each task/run pair)
       1 row   -> that Run
2  load the Task .................... missing -> LookupError (unreachable:
                                            runs.task_id is a foreign key)
3  run.workflow_definition_id is None -> StepUnresolved
   load_definition(workspace.workflows_dir, run.workflow_definition_id)
                                     -> WorkflowLoadError propagates
4  run.step_id is not None (a step Run)
     definition.find_step(run.step_id) is None -> WorkflowMismatch
     artifacts = store.list_artifacts_for_task(conn, task.id)
   run.step_id is None (a skill-targeted Run, SF-A-4 §9)
     skill = created_skill(conn, run) ........ None -> StepUnresolved
     artifacts = store.list_artifacts_for_run(conn, triggered_by_run_id)
                 (or () when the Run has no trigger -- defensive only:
                 resolve-task's initial action always targets steps[0])
5  run_input = run_input.resolve_stored_run_input(...)  <- shared with
   resolve-task, so a rebuild is the creation-time RunInput by construction
6  expected_skill given and != run_input.skill -> AssignmentMismatch
7  return run_input
```

Every step is a read. This module performs **no writes at all** -- not a row,
not a ``LifecycleEvent``, not a file: the mechanically testable form of
"nothing is persisted" (SF-44). No Result is created, no Run or Task status
changes, no Lifecycle Evaluation runs, no next Run is created, and Claude
Code is never launched.

Recomputation is deterministic for a running Run: at most one Run runs per
workspace (SF-43), a skill Run's trigger artifacts froze when the trigger
completed, and a step Run's Task-level selection is re-derived from the same
stored rows through the same ``select_context``. One honest edge: an artifact
registered mid-run on the running Run itself (possible through the
``create_artifact`` API, though the normal CLI flow registers only at
completion) appears in a later rebuild -- recomputation reflects stored
state, not a creation snapshot, and persisting the Assignment is out of
scope for SF-44.

Task status is deliberately not checked: SF-44's one precondition is a
running Run, and this module does not re-derive lifecycle state that is
another command's precondition (the ``prepare-artifacts`` precedent).
``expected_skill`` is matched exactly -- skills are identifiers, so there is
no case-folding or whitespace normalisation. It imports only ``sqlite3`` and
``skillflow`` value/layer modules.
"""

import sqlite3

from skillflow.domain import LifecycleEventType, Run
from skillflow.run_input import RunInput, resolve_stored_run_input
from skillflow.store import (
    get_task,
    list_artifacts_for_run,
    list_artifacts_for_task,
    list_lifecycle_events_for_task,
    list_running_runs,
)
from skillflow.workflow_loader import load_definition
from skillflow.workspace import Workspace

__all__ = ["AssignmentError", "created_skill", "resolve_assignment"]


class AssignmentError(Exception):
    """The running Run's Assignment cannot be rebuilt or verified.

    One flat class carrying a ``code``, exactly like
    ``PrepareArtifactsError``: ``"RunNotFound"`` / ``"AmbiguousCurrentRun"``
    (SF-A-5 §6.3's identifiers, reused since SF-21),
    ``"StepUnresolved"`` (the Run names no Workflow Definition, or a
    skill-targeted Run's ``run.created`` payload names no skill),
    ``"WorkflowMismatch"`` (the Run's step is absent from the current
    definition file) and ``"AssignmentMismatch"`` (``--skill`` names a
    skill other than the Run's). Every message ends with the concrete next
    command (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def created_skill(conn: sqlite3.Connection, run: Run) -> str | None:
    """Return skill-targeted ``run``'s skill from its ``run.created`` event.

    ``create_run`` records the action's skill in the payload because the
    ``Run`` row has no skill column; the first event in ``(created_at, id)``
    order wins, so the lookup is deterministic. ``None`` when no
    ``run.created`` event for the Run carries one. Reads only.

    The one shared lookup ``resolve-task`` (failed-skill-run retry),
    ``fail-run`` (retry target) and ``assignment`` (skill verification) all
    call, so the payload schema is known in exactly one place.
    """
    for event in list_lifecycle_events_for_task(conn, run.task_id):
        if (
            event.run_id == run.id
            and event.type is LifecycleEventType.RUN_CREATED
            and event.payload is not None
            and event.payload.get("skill")
        ):
            return event.payload["skill"]
    return None


def resolve_assignment(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    expected_skill: str | None = None,
) -> RunInput:
    """Rebuild the workspace's running Run's ``RunInput``.

    Implements the pipeline in the module docstring verbatim.
    ``expected_skill`` (the ``--skill`` flag) verifies the caller is the
    Run's skill: step Runs compare against ``step.skill`` from the current
    definition file, skill-targeted Runs against the recorded ``run.created``
    skill -- both uniformly as the rebuilt ``run_input.skill``. Raises
    :class:`AssignmentError` (with ``code``) for every command-level
    rejection, and lets ``WorkflowLoadError`` propagate unchanged.
    """
    running = list_running_runs(conn)
    if not running:
        raise AssignmentError(
            "RunNotFound",
            "no Run is running in this workspace; start one with "
            "`skillflow resolve-task <task-id>`",
        )
    if len(running) > 1:
        pairs = ", ".join(f"task {run.task_id!r} / run {run.id!r}" for run in running)
        raise AssignmentError(
            "AmbiguousCurrentRun",
            f"more than one Run is running ({pairs}); finish all but one "
            "with `skillflow complete-run --task <task-id>`, then run "
            "`skillflow assignment`",
        )
    run = running[0]

    # The Task is FK-guaranteed behind the resolved Run; LookupError matches
    # the service convention for "no task with id" (the fail_run precedent).
    task = get_task(conn, run.task_id)
    if task is None:
        raise LookupError(f"no task with id {run.task_id!r}")

    if run.workflow_definition_id is None:
        raise AssignmentError(
            "StepUnresolved",
            f"run {run.id!r} names no Workflow Definition, so its Assignment "
            "cannot be resolved -- investigate how this Run was created",
        )
    definition = load_definition(workspace.workflows_dir, run.workflow_definition_id)

    if run.step_id is not None:
        step = definition.find_step(run.step_id)
        if step is None:
            raise AssignmentError(
                "WorkflowMismatch",
                f"run {run.id!r} targets step {run.step_id!r}, absent from "
                f"workflow {definition.name!r}; the definition changed under "
                "this Run -- restore the step, then run "
                "`skillflow assignment`",
            )
        artifacts = list_artifacts_for_task(conn, task.id)
        run_input = resolve_stored_run_input(
            task=task, run=run, step=step, skill=None, artifacts=artifacts
        )
    else:
        skill = created_skill(conn, run)
        if skill is None:
            raise AssignmentError(
                "StepUnresolved",
                f"run {run.id!r} is skill-targeted but its `run.created` "
                "event names no skill, so its Assignment cannot be resolved "
                "-- investigate the Run history",
            )
        trigger_artifacts = (
            list_artifacts_for_run(conn, run.triggered_by_run_id)
            if run.triggered_by_run_id is not None
            else ()
        )
        run_input = resolve_stored_run_input(
            task=task, run=run, step=None, skill=skill, artifacts=trigger_artifacts
        )

    if expected_skill is not None and expected_skill != run_input.skill:
        raise AssignmentError(
            "AssignmentMismatch",
            f"skill {expected_skill!r} does not match the running Run "
            f"{run.id!r} (skill {run_input.skill!r}); re-run as "
            f"`skillflow assignment --skill {run_input.skill!r}` to view its "
            "Assignment, or dispatch the Run's own skill",
        )
    return run_input
