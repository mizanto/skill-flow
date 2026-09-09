"""Atomic Run completion (SF-23).

The operation that finishes a Run: validate the Run's required artifacts and
its reported outcome, register the artifacts, create the one canonical
Result, and complete the Run -- all as one atomic unit, so a rejection leaves
the Run ``running`` with nothing written (SF-A-5 §6.4-§6.7, §6.11). One
pipeline, executed in order -- the order is the contract, because it fixes
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
5  OUTCOME VALIDATION (SF-A-5 §6.6, pure)
     step is None -> request.decision must be None, else OutcomeNotExpected
     else         -> completion.validate_outcome(step=step, request=request)
                     -> Outcome | None; CompletionError propagates
6  ONE TRANSACTION (`with conn:`)
     for each submission: artifacts.register_artifact(...)   # no commit
     store.insert_result(result) + `result.created` event
     store.update_run(completed run) + `run.completed` event
   on any exception: unlink the content files written in this attempt, re-raise
7  return RunCompletion(run=..., result=..., artifacts=(...))
```

Steps 1-5 are **reads only**. Every rejection therefore happens before the
single write block, which is the mechanical form of the first acceptance
criterion -- the same structure ``resolve-task`` uses. Step 6's failures (a
pre-existing Result, an illegal transition, a bad artifact name, a filesystem
error) roll the transaction back, so the Run is still ``running`` and no
Result exists there either.

Explicit non-goals -- **no Lifecycle Evaluation, no Task-status change, no
next Run, no Claude Code launch** (SF-24 owns evaluation and the Task update;
SF-25 owns the ``/skillflow:complete-run`` command and its CLI wiring).
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
)
from skillflow.outputs import validate_outputs
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
    ``"WorkflowMismatch"`` and ``"OutcomeNotExpected"``. Note that
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
    and the Artifacts registered by this call (in submission order)."""

    run: Run
    result: Result
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run, Run):
            raise ValueError("RunCompletion.run must be a Run")
        if not isinstance(self.result, Result):
            raise ValueError("RunCompletion.result must be a Result")
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
    rejection, and lets ``CompletionError`` (SF-22) and ``WorkflowLoadError``
    propagate unchanged -- both are flat classes carrying their own
    identifiers, so re-wrapping would only lose information.
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
        if request.decision is not None:
            raise CompleteRunError(
                "OutcomeNotExpected",
                f"run {run.id!r} targets no workflow step, so outcome "
                f"{request.decision!r} is not expected; complete the Run "
                "without an outcome by running `/skillflow:complete-run` "
                "with no `--outcome` flag",
            )
        outcome = None
    else:
        outcome = validate_outcome(step=step, request=request)

    now = datetime.now(UTC)
    result = Result(
        id=_new_id("result"),
        run_id=run.id,
        status=ResultStatus.COMPLETED,
        created_at=now,
        outcome=outcome,
    )
    done = replace(run, status=RunStatus.COMPLETED, completed_at=now)
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
    except BaseException:
        for path in written:
            # Only files this attempt wrote are dropped; a submission that
            # failed to write never reaches this list.
            path.unlink(missing_ok=True)
        raise
    return RunCompletion(run=done, result=result, artifacts=tuple(registered))
