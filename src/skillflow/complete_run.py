"""Atomic Run completion with Lifecycle Evaluation (SF-23, SF-24).

The operation that finishes a Run: validate the Run's required artifacts and
its reported outcome, register the artifacts, create the one canonical
Result, complete the Run, evaluate the lifecycle, and apply the evaluated
action's Task-status consequence -- all as one atomic unit, so a rejection
leaves the Run ``running`` with nothing written (SF-A-5 §6.4-§6.9, §6.11).
One pipeline, executed in order -- the order is the contract, because it fixes
error precedence (the convention every sibling module already documents):

```text
1  load Run (store.get_run) .............. missing        -> RunNotFound
2  run.status is RUNNING? ................ else           -> RunNotActive
3  resolve the Run's step (reads only)
     no workflow_definition_id / no step_id -> step = None (skill-targeted Run)
     load_definition(...)                   -> WorkflowLoadError propagates
     definition.find_step(run.step_id) None -> WorkflowMismatch
4  ARTIFACT VALIDATION (SF-A-5 §6.4, reads only)
     existing   = store.list_artifacts_for_run(conn, run.id)
     validation = outputs.validate_outputs(step=step, run=run, artifacts=existing)
     uncovered  = missing_required whose type no submission supplies
     uncovered  -> RequiredArtifactsMissing
     (step is None -> nothing declared -> nothing missing)
5  OUTCOME VALIDATION (SF-A-5 §6.6, pure + reads)
     step set -> completion.validate_outcome(step=step, request=request)
                 -> Outcome | None; CompletionError propagates
     step None, no decision -> outcome = None (a decisionless
                 skill-targeted Run reports no lifecycle outcome)
     step None + decision   -> resolve the triggering step through
                 triggered_by_run_id provenance (SF-32 gap-fill):
                 no workflow / trigger / triggering step -> StepUnresolved,
                 triggering step absent from the definition ->
                 WorkflowMismatch; then completion.validate_outcome(
                 step=trigger step, request=request) -> Outcome
6  LIFECYCLE EVALUATION (SF-A-5 §6.8, pure, reads only)
     load Task (runs.task_id FK guarantees it; LookupError is unreachable)
     build the Result and completed-Run snapshots (single `now`)
     step None + no outcome -> action = None (a decisionless
                 skill-targeted Run has no outcome rules; the Task is left
                 unchanged)
     else -> evaluate(EvaluationInput(task, definition, done, result))
                 exactly once -- incl. a skill Run's outcome, routed via
                 Outcome.type; a skill-targeting rule raises EvaluationError
7  ONE TRANSACTION (`with conn:`)
     for each submission: artifacts.register_artifact(...)   # no commit
     store.insert_result(result) + `result.created` event
     store.update_run(completed run) + `run.completed` event
     action is not None -> service.apply_lifecycle_action(...)  # no commit;
        Task-status consequence + `task.status_changed` event (a no-op action
        writes nothing)
   on any exception: roll back, unlink the content files written in this
   attempt, re-raise
8  return RunCompletion(run=..., result=..., task=..., action=..., artifacts=(...))
```

Steps 1-6 are **reads only**. Every rejection therefore happens before the
single write block, which is the mechanical form of the first acceptance
criterion -- the same structure ``resolve-task`` uses. Step 7's failures (a
pre-existing Result, an illegal transition, a bad artifact name, a filesystem
error, a Task-consequence failure) roll the transaction back, so the Run is
still ``running`` and no Result exists there either.

Explicit non-goals -- **no next Run, no Claude Code launch** (the evaluated
``run`` action is returned for the user to start in a new session; SF-25 owns
the ``/skillflow:complete-run`` command and its CLI wiring).
"""

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from skillflow import store
from skillflow.artifacts import register_artifact
from skillflow.completion import CompletionRequest, validate_outcome
from skillflow.domain import (
    Artifact,
    LifecycleEvent,
    LifecycleEventType,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
)
from skillflow.evaluator import EvaluationInput, EvaluationOutput, evaluate
from skillflow.outputs import validate_outputs
from skillflow.service import apply_lifecycle_action
from skillflow.store import get_run, list_artifacts_for_run
from skillflow.workflow_loader import load_definition
from skillflow.workspace import Workspace

__all__ = ["CompleteRunError", "RunCompletion", "complete_run"]


# Deliberately duplicated from ``service._new_id`` / ``artifacts._new_id``
# rather than promoted to a shared helper: two lines, and this module's
# import boundary is part of its contract.
def _new_id(prefix: str) -> str:
    """Return ``f"{prefix}-{uuid4().hex}"``. Collisions are not defended against."""
    return f"{prefix}-{uuid4().hex}"


class CompleteRunError(Exception):
    """A Run cannot be completed as reported.

    One flat class carrying a ``code``, matching ``ResolveTaskError`` /
    ``PrepareArtifactsError``: ``"RunNotFound"``, ``"RunNotActive"``
    (SF-A-5 §6.3's identifiers), ``"RequiredArtifactsMissing"`` (§6.4),
    ``"WorkflowMismatch"``, ``"StepUnresolved"`` (a skill-targeted Run
    reporting a decision names no workflow / trigger / triggering step to
    validate it against, SF-32) and ``"OutcomeNotExpected"``. Note that
    ``"OutcomeNotExpected"`` also arrives as ``CompletionError`` from
    :func:`completion.validate_outcome` (a step declaring no outcome rules),
    so a caller handling rejections must catch both classes. Every message
    ends with the concrete next command (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class RunCompletion:
    """What completion produced: the completed Run, its one canonical Result,
    the Task in its post-completion state, the evaluated lifecycle action
    (``None`` for a decisionless skill-targeted Run, which has no outcome
    rules), and the Artifacts registered by this call (in submission order)."""

    run: Run
    result: Result
    task: Task
    action: EvaluationOutput | None = None
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run, Run):
            raise ValueError("RunCompletion.run must be a Run")
        if not isinstance(self.result, Result):
            raise ValueError("RunCompletion.result must be a Result")
        if not isinstance(self.task, Task):
            raise ValueError("RunCompletion.task must be a Task")
        if self.action is not None and not isinstance(self.action, EvaluationOutput):
            raise ValueError("RunCompletion.action must be an EvaluationOutput or None")
        if isinstance(self.artifacts, str):
            raise ValueError("RunCompletion.artifacts must be an iterable")
        try:
            artifacts = tuple(self.artifacts)
        except TypeError:
            raise ValueError("RunCompletion.artifacts must be an iterable") from None
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise ValueError("RunCompletion.artifacts must contain Artifact")
        object.__setattr__(self, "artifacts", artifacts)


def complete_run(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    request: CompletionRequest,
) -> RunCompletion:
    """Complete the ``running`` Run ``run_id`` as ``request`` reports.

    Implements the pipeline in the module docstring verbatim. Raises
    :class:`CompleteRunError` (with ``code``) for every command-level
    rejection, and lets ``CompletionError`` (SF-22), ``EvaluationError``
    (a skill-targeted Run resolving to another skill, SF-32) and
    ``WorkflowLoadError`` propagate unchanged -- all are flat classes
    carrying their own identifiers, so re-wrapping would only lose
    information.
    """
    if not isinstance(request, CompletionRequest):
        raise ValueError("complete_run() request must be a CompletionRequest")

    run = get_run(conn, run_id)
    if run is None:
        raise CompleteRunError(
            "RunNotFound",
            f"no run with id {run_id!r}; check the id, then run "
            "`skillflow resolve-task <task-id>` to start one",
        )
    if run.status is not RunStatus.RUNNING:
        raise CompleteRunError(
            "RunNotActive",
            f"run {run.id!r} is {run.status.value!r}; a Run is never "
            "resumed -- start a new one with "
            f"`skillflow resolve-task {run.task_id}`",
        )

    definition = None
    if run.workflow_definition_id is None or run.step_id is None:
        step = None
    else:
        definition = load_definition(
            workspace.workflows_dir, run.workflow_definition_id
        )
        step = definition.find_step(run.step_id)
        if step is None:
            raise CompleteRunError(
                "WorkflowMismatch",
                f"run {run.id!r} targets step {run.step_id!r}, absent from "
                f"workflow {definition.name!r}; the definition changed under "
                "this Run -- restore the step, then run "
                "`/skillflow:complete-run`",
            )

    if step is None:
        uncovered = ()
    else:
        existing = list_artifacts_for_run(conn, run.id)
        validation = validate_outputs(step=step, run=run, artifacts=existing)
        submitted_types = {submission.type for submission in request.artifacts}
        uncovered = tuple(
            check
            for check in validation.missing_required
            if check.type not in submitted_types
        )
        if uncovered:
            missing = ", ".join(repr(check.type) for check in uncovered)
            raise CompleteRunError(
                "RequiredArtifactsMissing",
                f"run {run.id!r} is missing required artifacts of type "
                f"{missing}; create them as ordinary files, then re-run "
                "`/skillflow:complete-run` -- or inspect the gap with "
                "`/skillflow:prepare-artifacts`",
            )

    if step is None:
        if request.decision is None:
            outcome = None
        else:
            # A skill-targeted Run reporting a decision (SF-32 gap-fill for
            # SF-A-4 §9): the decision is validated against the outcome table
            # of the step whose rule launched this Run, resolved through
            # `triggered_by_run_id` provenance.
            if run.workflow_definition_id is None:
                raise CompleteRunError(
                    "StepUnresolved",
                    f"run {run.id!r} names no Workflow Definition, so outcome "
                    f"{request.decision!r} has no outcome table to validate "
                    "against; a skill-targeted Run reports through its "
                    "triggering step -- investigate how this Run was created",
                )
            trigger = (
                get_run(conn, run.triggered_by_run_id)
                if run.triggered_by_run_id is not None
                else None
            )
            trigger_step_id = trigger.step_id if trigger is not None else None
            if trigger_step_id is None:
                raise CompleteRunError(
                    "StepUnresolved",
                    f"run {run.id!r} is skill-targeted but names no "
                    "triggering step, so outcome "
                    f"{request.decision!r} has no outcome table to validate "
                    "against; complete the Run without an outcome by running "
                    "`/skillflow:complete-run` with no `--outcome` flag",
                )
            definition = load_definition(
                workspace.workflows_dir, run.workflow_definition_id
            )
            trigger_step = definition.find_step(trigger_step_id)
            if trigger_step is None:
                raise CompleteRunError(
                    "WorkflowMismatch",
                    f"run {run.id!r} was triggered from step "
                    f"{trigger_step_id!r}, absent from workflow "
                    f"{definition.name!r}; the definition changed under this "
                    "Task -- restore the step, then run "
                    "`/skillflow:complete-run`",
                )
            outcome = validate_outcome(step=trigger_step, request=request)
    else:
        outcome = validate_outcome(step=step, request=request)

    # The Task is loaded here, after every rejection, so the steps 1-5 error
    # precedence is untouched. `runs.task_id` is a foreign key, so a missing
    # Task is unreachable except via raw SQL; LookupError matches the service
    # convention for "no task with id".
    task = store.get_task(conn, run.task_id)
    if task is None:
        raise LookupError(f"no task with id {run.task_id!r}")

    now = datetime.now(UTC)
    result = Result(
        id=_new_id("result"),
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=now,
        outcome=outcome,
    )
    done = replace(run, status=RunStatus.COMPLETED, completed_at=now)
    if step is None and outcome is None:
        action = None
    else:
        # `definition` is not None here: for a step Run it is loaded from the
        # Run's own step above; for a skill Run reporting a decision it is
        # loaded from the triggering step. Evaluated once, before the write
        # block, against the completed snapshots.
        action = evaluate(
            EvaluationInput(
                task=task, workflow=definition, current_run=done, result=result
            )
        )
    result_payload = {"result_id": result.id, "status": result.status.value}
    if outcome is not None:
        result_payload["outcome_type"] = outcome.type
        result_payload["outcome_decision"] = outcome.decision
    result_event = LifecycleEvent(
        id=_new_id("event"),
        task_id=run.task_id,
        run_id=run.id,
        type=LifecycleEventType.RESULT_CREATED,
        payload=result_payload,
        created_at=now,
    )
    run_event = LifecycleEvent(
        id=_new_id("event"),
        task_id=run.task_id,
        run_id=run.id,
        type=LifecycleEventType.RUN_COMPLETED,
        payload={"status": done.status.value},
        created_at=now,
    )

    registered: list[Artifact] = []
    # Paths this attempt wrote; unlinked if the commit fails. Unannotated on
    # purpose: annotating it would import pathlib, which the AST
    # import-boundary test forbids (this module never touches paths directly).
    written = []
    try:
        with conn:  # the single write: commits at block exit, rolls back on exception
            for submission in request.artifacts:
                artifact, path = register_artifact(
                    conn,
                    workspace,
                    run_id=run.id,
                    name=submission.name,
                    type=submission.type,
                    content=submission.content,
                )
                registered.append(artifact)
                written.append(path)
            store.insert_result(conn, result)
            store.insert_lifecycle_event(conn, result_event)
            store.update_run(conn, done)
            store.insert_lifecycle_event(conn, run_event)
            if action is not None:
                # Transaction-neutral: joins this write block, commits with it.
                task = apply_lifecycle_action(
                    conn, task=task, action=action, run_id=run.id, now=now
                )
    except BaseException:
        for path in written:
            # Only files this attempt wrote are dropped; a submission that
            # failed to write never reaches this list.
            path.unlink(missing_ok=True)
        raise
    return RunCompletion(
        run=done, result=result, task=task, action=action, artifacts=tuple(registered)
    )
