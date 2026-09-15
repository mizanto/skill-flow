"""Record a Human Decision and apply its lifecycle consequence (SF-27).

The operation that answers a parked Task: resolve the ``waiting_for_human``
Task, validate the reported decision against the current step's declared
``decisions`` table, persist the one ``HumanDecision``, evaluate the lifecycle
with it, and apply the evaluated action's Task-status consequence -- all as
one atomic unit, so a rejection leaves the Task ``waiting_for_human`` with the
Result unchanged and nothing written (SF-A-5 §7.4-§7.8, §8). One pipeline,
executed in order -- the order is the contract, because it fixes error
precedence (the convention every sibling module already documents):

```text
1  resolve Task
     task_id None -> store.list_tasks_by_status(conn, WAITING_FOR_HUMAN)
                       0 rows  -> HumanDecisionNotExpected
                       >1 rows -> AmbiguousCurrentTask (name each task id)
                       1 row   -> that Task
     task_id given -> store.get_task ................. missing -> TaskNotFound
2  Task status ............. completed/cancelled -> TaskAlreadyCompleted /
                                                  TaskCancelled (terminal
                                                  first, SF-A-5 §7.4)
                             active -> HumanDecisionNotExpected
3  current Run = latest Run of the Task (list_runs_for_task order)
     no runs ................. -> RunNotFound
     latest not COMPLETED .... -> RunNotCompleted
   (steps 3-4 are :func:`resolve_waiting_step`, shared with
   ``resolve-task``'s waiting rejection, SF-50)
4  resolve the Run's step (reads only)
     run.workflow_definition_id None -> StepUnresolved
     load_definition(...) .......... -> WorkflowLoadError propagates
     step Run: find_step(run.step_id) None -> WorkflowMismatch
     skill Run (SF-32): load the Result (missing -> ResultMissing, the
       same code a step Run without one gets), require its outcome
       (None -> StepUnresolved), then find_step(outcome.type) None ->
       WorkflowMismatch
5  Result = get_result_for_run(current.id) ....... missing -> ResultMissing
   (step Runs only; already loaded for skill Runs in step 4)
6  DECISION VALIDATION (SF-A-5 §7.5, pure)
     decisions.validate_decision(step=step, request=request)
     -> DecisionError("InvalidHumanDecision") propagates
7  LIFECYCLE EVALUATION (SF-A-5 §7.7, pure, reads only)
     evaluate(EvaluationInput(task, definition, current, result,
                              human_decision=snapshot))
     exactly once -> EvaluationError propagates (a stored Result whose
     step vanished from the definition, unreachable except via raw SQL)
8  ONE TRANSACTION (`with conn:`)
     store.insert_human_decision(decision)
     store.insert_lifecycle_event(human.decision_made)
     service.apply_lifecycle_action(...)  # no commit; Task-status
        consequence + `task.status_changed` event
   on any exception: roll back, re-raise (no filesystem writes exist, so
   there is nothing to unlink -- unlike `complete_run`)
9  return DecisionRecord(decision=..., task=..., action=...)
```

Steps 1-7 are **reads only**. Every rejection therefore happens before the
single write block, which is the mechanical form of §8 -- the same structure
``resolve-task`` and ``complete-run`` use. Step 8's failures roll the
transaction back, so the Task is still ``waiting_for_human`` with no decision
there either. The previous Result is never touched: a decision is recorded
*against* the completed Run, not written into its observation (SF-A-5 §7.6).

Explicit non-goals -- **no next Run, no Claude Code launch** (the evaluated
``run`` action is returned for the user to start in a new session via
``resolve-task``, which reproduces this routing from the persisted decision;
a ``human`` action is unreachable because ``WorkflowStep`` rejects a
``decisions`` rule with ``action: human`` at construction, SF-A-5 §7.7).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from skillflow import store
from skillflow.decisions import DecisionRequest, validate_decision
from skillflow.domain import (
    HumanDecision,
    LifecycleEvent,
    LifecycleEventType,
    Result,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import EvaluationInput, EvaluationOutput, evaluate
from skillflow.service import apply_lifecycle_action
from skillflow.store import (
    get_result_for_run,
    get_task,
    list_runs_for_task,
    list_tasks_by_status,
)
from skillflow.workflow import Workflow, WorkflowStep
from skillflow.workflow_loader import load_definition
from skillflow.workspace import Workspace

__all__ = ["DecideError", "DecisionRecord", "decide", "resolve_waiting_step"]


# Deliberately duplicated from ``service._new_id`` / ``artifacts._new_id`` /
# ``complete_run._new_id`` rather than promoted to a shared helper: two lines,
# and this module's import boundary is part of its contract.
def _new_id(prefix: str) -> str:
    """Return ``f"{prefix}-{uuid4().hex}"``. Collisions are not defended against."""
    return f"{prefix}-{uuid4().hex}"


class DecideError(Exception):
    """A Human Decision cannot be recorded as reported.

    One flat class carrying a ``code``, matching ``ResolveTaskError`` /
    ``PrepareArtifactsError`` / ``CompleteRunError``: ``"TaskNotFound"``,
    ``"TaskAlreadyCompleted"``, ``"TaskCancelled"``,
    ``"HumanDecisionNotExpected"`` (SF-A-5 §7.4's identifiers),
    ``"AmbiguousCurrentTask"``, ``"RunNotFound"``, ``"RunNotCompleted"``,
    ``"ResultMissing"``, ``"StepUnresolved"`` and ``"WorkflowMismatch"``.
    Note that ``"InvalidHumanDecision"`` arrives as ``DecisionError`` from
    :func:`decisions.validate_decision`, so a caller handling rejections must
    catch both classes. Every message ends with the concrete next step
    (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class DecisionRecord:
    """What ``decide`` produced: the persisted ``HumanDecision``, the Task in
    its post-decision state, and the evaluated lifecycle action.

    The Run is deliberately absent: it is unchanged by a decision (already
    ``completed`` before the Task was parked), and SF-A-5 §7.8's output never
    names it.
    """

    decision: HumanDecision
    task: Task
    action: EvaluationOutput

    def __post_init__(self) -> None:
        if not isinstance(self.decision, HumanDecision):
            raise ValueError("DecisionRecord.decision must be a HumanDecision")
        if not isinstance(self.task, Task):
            raise ValueError("DecisionRecord.task must be a Task")
        if not isinstance(self.action, EvaluationOutput):
            raise ValueError("DecisionRecord.action must be an EvaluationOutput")


def resolve_waiting_step(
    conn: sqlite3.Connection, workspace: Workspace, task: Task
) -> tuple[Workflow, WorkflowStep, Run, Result]:
    """Resolve the waiting step behind a ``waiting_for_human`` Task.

    The shared form of ``decide``'s steps 3-4 (SF-50): the Task's latest
    Run, its Workflow Definition, the step whose ``decisions`` table
    applies, and the Run's canonical Result. Step Runs resolve by
    ``run.step_id``; skill-targeted Runs (SF-32) by the parking
    ``Result``'s ``outcome.type``.

    Reads only. Raises :class:`DecideError` (with ``code``) for every
    unresolvable history, and lets ``WorkflowLoadError`` propagate
    unchanged -- the same rejections ``decide`` reports. ``resolve-task``
    calls this best-effort to enrich its ``HumanDecisionRequired``
    rejection, degrading to its generic message on any failure.
    """
    if not isinstance(task, Task):
        raise ValueError("resolve_waiting_step requires a Task task")
    runs = list_runs_for_task(conn, task.id)
    if not runs:
        raise DecideError(
            "RunNotFound",
            f"task {task.id!r} is 'waiting_for_human' but has no Runs; only "
            "a completed Run's human outcome can park a Task -- investigate "
            "how this Task was parked",
        )
    current = runs[-1]
    if current.status is not RunStatus.COMPLETED:
        raise DecideError(
            "RunNotCompleted",
            f"latest run {current.id!r} of task {task.id!r} is "
            f"{current.status.value!r}; a decision answers a completed Run's "
            "human outcome -- finish the Run with `skillflow complete-run`",
        )

    if current.workflow_definition_id is None:
        raise DecideError(
            "StepUnresolved",
            f"run {current.id!r} names no Workflow Definition, so it "
            "declares no decisions; a decision needs the step that produced "
            f"the human outcome -- investigate how task {task.id!r} was parked",
        )
    definition = load_definition(
        workspace.workflows_dir, current.workflow_definition_id
    )
    if current.step_id is None:
        # A skill-targeted Run (SF-32 gap-fill for SF-A-4 §9): the step is
        # resolved from the outcome that parked the Task.
        result = get_result_for_run(conn, current.id)
        if result is None:
            raise DecideError(
                "ResultMissing",
                f"completed run {current.id!r} has no canonical Result; a "
                "decision answers a completed Run's recorded outcome -- "
                "investigate the Run history",
            )
        if result.outcome is None:
            raise DecideError(
                "StepUnresolved",
                f"run {current.id!r} targets no workflow step and reported "
                "no outcome, so no step's decisions apply; a decision needs "
                "the step that produced the human outcome -- investigate how "
                f"task {task.id!r} was parked",
            )
        step = definition.find_step(result.outcome.type)
        if step is None:
            raise DecideError(
                "WorkflowMismatch",
                f"run {current.id!r} names step {result.outcome.type!r} in "
                f"its outcome, absent from workflow {definition.name!r}; the "
                "definition changed under this Task -- restore the step, "
                "then run `skillflow decide <decision>`",
            )
    else:
        step = definition.find_step(current.step_id)
        if step is None:
            raise DecideError(
                "WorkflowMismatch",
                f"run {current.id!r} targets step {current.step_id!r}, absent "
                f"from workflow {definition.name!r}; the definition changed "
                "under this Task -- restore the step, then run "
                "`skillflow decide <decision>`",
            )

        result = get_result_for_run(conn, current.id)
        if result is None:
            raise DecideError(
                "ResultMissing",
                f"completed run {current.id!r} has no canonical Result; a "
                "decision answers a completed Run's recorded outcome -- "
                "investigate the Run history",
            )
    return definition, step, current, result


def decide(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    task_id: str | None = None,
    request: DecisionRequest,
) -> DecisionRecord:
    """Record ``request`` on the ``waiting_for_human`` Task and apply it.

    Implements the pipeline in the module docstring verbatim. ``task_id`` is a
    disambiguator only: it selects which Task to decide when several are
    waiting, never influencing the decision. Raises :class:`DecideError` (with
    ``code``) for every command-level rejection, and lets ``DecisionError``
    (SF-26), ``EvaluationError`` and ``WorkflowLoadError`` propagate
    unchanged -- all are flat classes carrying their own identifiers, so
    re-wrapping would only lose information.
    """
    if not isinstance(request, DecisionRequest):
        raise ValueError("decide() request must be a DecisionRequest")

    if task_id is None:
        waiting = list_tasks_by_status(conn, TaskStatus.WAITING_FOR_HUMAN)
        if not waiting:
            raise DecideError(
                "HumanDecisionNotExpected",
                "no Task is 'waiting_for_human' in this workspace; decisions "
                "are recorded only after a Run completes with a human outcome "
                "-- nothing to decide",
            )
        if len(waiting) > 1:
            ids = ", ".join(repr(task.id) for task in waiting)
            raise DecideError(
                "AmbiguousCurrentTask",
                f"more than one Task is waiting for a human decision ({ids}); "
                "re-run as `skillflow decide <decision> --task <task-id>`",
            )
        task = waiting[0]
    else:
        task = get_task(conn, task_id)
        if task is None:
            raise DecideError(
                "TaskNotFound",
                f"no task with id {task_id!r}; check the id, then run "
                "`skillflow decide <decision> --task <task-id>` again",
            )

    if task.status is TaskStatus.COMPLETED:
        raise DecideError(
            "TaskAlreadyCompleted",
            f"task {task.id!r} is already 'completed'; a terminal Task takes "
            "no decisions -- nothing to decide",
        )
    if task.status is TaskStatus.CANCELLED:
        raise DecideError(
            "TaskCancelled",
            f"task {task.id!r} is 'cancelled'; a terminal Task takes no "
            "decisions -- nothing to decide",
        )
    if task.status is TaskStatus.ACTIVE:
        raise DecideError(
            "HumanDecisionNotExpected",
            f"task {task.id!r} is 'active', not 'waiting_for_human'; only a "
            "waiting Task takes a decision -- finish the current Run with "
            "`skillflow complete-run`, or start one with "
            f"`skillflow resolve-task {task.id}`",
        )

    # Shared with `resolve-task`'s waiting rejection (SF-50): the same
    # rows, codes, and messages either caller would resolve.
    definition, step, current, result = resolve_waiting_step(conn, workspace, task)

    validated = validate_decision(step=step, request=request)

    now = datetime.now(UTC)
    decision = HumanDecision(
        id=_new_id("decision"),
        task_id=task.id,
        run_id=current.id,
        decision=validated,
        created_at=now,
        comment=request.comment,
    )
    # Evaluated once, before the write block, with the to-be-persisted
    # decision. Linkage holds by construction: the snapshot carries this
    # Task's and Run's ids.
    action = evaluate(
        EvaluationInput(
            task=task,
            workflow=definition,
            current_run=current,
            result=result,
            human_decision=decision,
        )
    )
    event = LifecycleEvent(
        id=_new_id("event"),
        task_id=task.id,
        run_id=current.id,
        type=LifecycleEventType.HUMAN_DECISION_MADE,
        payload={"decision_id": decision.id, "decision": decision.decision},
        created_at=now,
    )

    with conn:  # the single write: commits at block exit, rolls back on exception
        store.insert_human_decision(conn, decision)
        store.insert_lifecycle_event(conn, event)
        # Transaction-neutral: joins this write block, commits with it.
        task = apply_lifecycle_action(
            conn, task=task, action=action, run_id=current.id, now=now
        )
    return DecisionRecord(decision=decision, task=task, action=action)
