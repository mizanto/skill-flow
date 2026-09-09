"""Atomic Run failure recording with Lifecycle Evaluation (SF-35).

The operation that records a failed Run: resolve the current ``running`` Run,
register the partial durable outputs it managed to produce, create the one
canonical failed Result, mark the Run ``failed``, persist diagnostics, evaluate
the lifecycle, and report the retry action -- all as one atomic unit, so a
rejection leaves the Run ``running`` with nothing written (SF-A-5 §8). One
pipeline, executed in order -- the order is the contract, because it fixes
error precedence (the convention every sibling module already documents):

```text
1  request is FailureRequest? ............ else  -> ValueError
2  resolve the current running Run (reads only)
     task_id None -> store.list_running_runs(conn)
                       0 rows  -> RunNotFound
                       >1 rows -> AmbiguousCurrentRun (name each task/run pair)
                       1 row   -> that Run
     task_id given -> store.get_task ................. missing -> TaskNotFound
                      no Runs for the Task ........... -> RunNotFound
                      no running Run ................. -> RunNotActive
3  resolve the retry target (reads only)
     run.workflow_definition_id None -> StepUnresolved
     load_definition(...) .......... -> WorkflowLoadError propagates
     skill Run (step_id None): skill from the Run's `run.created` payload
        (store.list_lifecycle_events_for_task, first in (created_at, id)
        order); missing -> StepUnresolved
4  SUBMISSION PRE-CHECK (reads only)
     each submission's chain type matches the established chain
        -> InvalidArtifactSubmission (the "message" half;
        `register_artifact`'s ValueError stays the backstop, mirroring the
        store's pre-check/constraint pairs)
5  LIFECYCLE EVALUATION (pure, reads only)
     load Task (runs.task_id FK guarantees it; LookupError is unreachable)
     build the Result and failed-Run snapshots (single `now`)
     evaluate(EvaluationInput(task, definition, failed, result,
                              current_skill=...)) exactly once
        -> EvaluationError propagates (the step vanished from the definition)
6  ONE TRANSACTION (`with conn:`)
     for each submission: artifacts.register_artifact(...)   # no commit;
        no required-coverage check -- partial outputs are the point (SF-A-2 §9)
        a None path reuses a byte-identical orphan -- nothing to unlink
     store.insert_result(result) + `result.created` event
     store.update_run(failed run) + `run.failed` event
     diagnostics given -> artifacts.write_diagnostics(...)  # no commit;
        None likewise reuses a byte-identical output.log
   on any exception: roll back, unlink the files this attempt
   created, re-raise
7  return RunFailure(run=..., result=..., task=..., action=...,
                     artifacts=(...), diagnostics_path=...)
```

Steps 1-5 are **reads only**. Every rejection therefore happens before the
single write block, which is the mechanical form of SF-A-5 §8 -- the same
structure ``resolve-task``, ``complete-run`` and ``decide`` use. Step 6's
failures (a pre-existing Result, an illegal status change, a bad artifact
name, a filesystem error) roll the transaction back, so the Run is still
``running`` and no Result exists there either.

There is deliberately **no** required-artifact or outcome validation: missing
outputs are often *why* the Run failed, so requiring them would make failure
unrecordable, and a failure is not a workflow outcome. Likewise there is no
``apply_lifecycle_action`` call: the failure consequence is the constant
"the Task stays ``active``" (SF-A-5 §3.6), so there is no varying consequence
to apply -- and calling it would risk resurrecting a non-``active`` Task in
states only raw SQL can produce. The stored Task is returned unchanged.

Explicit non-goals -- **no next Run, no Claude Code launch** (the evaluated
``run`` action is returned for the user to start in a new session via
``resolve-task``, which reproduces this routing from the persisted failed
Result).
"""

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from skillflow import store
from skillflow.artifacts import register_artifact, write_diagnostics
from skillflow.completion import ArtifactSubmission
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
from skillflow.workflow_loader import load_definition
from skillflow.workspace import OUTPUT_LOG_FILE_NAME, RUNS_DIR_NAME, Workspace

__all__ = ["FailRunError", "FailureRequest", "RunFailure", "fail_run"]


# Deliberately duplicated from ``service._new_id`` / ``artifacts._new_id`` /
# ``complete_run._new_id`` / ``decide._new_id`` rather than promoted to a
# shared helper: two lines, and this module's import boundary is part of its
# contract.
def _new_id(prefix: str) -> str:
    """Return ``f"{prefix}-{uuid4().hex}"``. Collisions are not defended against."""
    return f"{prefix}-{uuid4().hex}"


class FailRunError(Exception):
    """A Run cannot be recorded as failed.

    One flat class carrying a ``code``, matching ``ResolveTaskError`` /
    ``CompleteRunError`` / ``DecideError``: ``"TaskNotFound"``,
    ``"RunNotFound"``, ``"AmbiguousCurrentRun"``, ``"RunNotActive"``,
    ``"StepUnresolved"`` (the Run names no Workflow Definition, or a
    skill-targeted Run's ``run.created`` payload names no skill to retry)
    and ``"InvalidDiagnostics"`` (raised from the CLI helpers for a blank
    ``--message`` or an unreadable / non-UTF-8 ``--diagnostics-file``).
    Note that ``"InvalidArtifactSubmission"`` arrives on this class both
    from the CLI submission loader it shares with ``complete-run``
    (malformed / unreadable / duplicate specs) and from step 4's own chain
    pre-check, so a caller handling rejections must expect it from either
    layer. Every message ends with the concrete next command (AGENTS.md
    principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class FailureRequest:
    """What is reported when a Run fails (SF-35).

    ``diagnostics`` is free-text failure output, stored verbatim in
    ``runs/<run-id>/output.log`` (SF-A-2 §7); ``None`` means "no diagnostics
    were captured" and no file is written. ``artifacts`` are the partial
    durable outputs the Run produced before failing, carried here so
    ``fail_run`` takes one value object -- they are registered without any
    required-coverage check, so later Runs may use them (SF-A-2 §9). Two
    submissions under one name in a single request is a caller error, not a
    version chain, and is rejected.

    There is deliberately no outcome/decision field: a failure is an
    observation, not a workflow outcome, and Lifecycle Evaluation retries a
    failed Result without consulting any outcome table.
    """

    diagnostics: str | None = None
    artifacts: tuple[ArtifactSubmission, ...] = ()

    def __post_init__(self) -> None:
        if self.diagnostics is not None and not isinstance(self.diagnostics, str):
            raise ValueError("FailureRequest.diagnostics must be a string or None")
        if isinstance(self.artifacts, str):
            raise ValueError("FailureRequest.artifacts must be an iterable")
        try:
            submissions = tuple(self.artifacts)
        except TypeError:
            raise ValueError("FailureRequest.artifacts must be an iterable") from None
        for submission in submissions:
            if not isinstance(submission, ArtifactSubmission):
                raise ValueError(
                    "FailureRequest.artifacts must contain ArtifactSubmission"
                )
        names = [submission.name for submission in submissions]
        if len(set(names)) != len(names):
            raise ValueError("FailureRequest.artifacts has a duplicate name")
        object.__setattr__(self, "artifacts", submissions)


@dataclass(frozen=True, kw_only=True, slots=True)
class RunFailure:
    """What failure recording produced: the failed Run, its one canonical
    failed Result, the Task in its unchanged state, the evaluated retry
    action (always ``run`` -- the failed rule returns nothing else), the
    partial Artifacts registered by this call (in submission order), and the
    diagnostics path relative to ``.skillflow/`` (``None`` when no
    diagnostics were captured).
    """

    run: Run
    result: Result
    task: Task
    action: EvaluationOutput
    artifacts: tuple[Artifact, ...] = ()
    diagnostics_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run, Run):
            raise ValueError("RunFailure.run must be a Run")
        if not isinstance(self.result, Result):
            raise ValueError("RunFailure.result must be a Result")
        if not isinstance(self.task, Task):
            raise ValueError("RunFailure.task must be a Task")
        if not isinstance(self.action, EvaluationOutput):
            raise ValueError("RunFailure.action must be an EvaluationOutput")
        if isinstance(self.artifacts, str):
            raise ValueError("RunFailure.artifacts must be an iterable")
        try:
            artifacts = tuple(self.artifacts)
        except TypeError:
            raise ValueError("RunFailure.artifacts must be an iterable") from None
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise ValueError("RunFailure.artifacts must contain Artifact")
        object.__setattr__(self, "artifacts", artifacts)
        if self.diagnostics_path is not None:
            if not isinstance(self.diagnostics_path, str) or not (
                self.diagnostics_path.strip()
            ):
                raise ValueError(
                    "RunFailure.diagnostics_path must be a non-empty string"
                )
            object.__setattr__(self, "diagnostics_path", self.diagnostics_path.strip())


def _resolve_current_run(conn: sqlite3.Connection, task_id: str | None) -> Run:
    """Return the ``running`` Run ``fail_run`` applies to.

    Mirrors ``decide``'s Task resolution crossed with ``complete-run``'s
    running-Run selection: ``task_id`` is a disambiguator only, selecting
    which Task's running Run fails when several Runs are ``running`` in one
    workspace. Reads only.
    """
    if task_id is None:
        running = store.list_running_runs(conn)
        if not running:
            raise FailRunError(
                "RunNotFound",
                "no Run is running in this workspace; start one with "
                "`skillflow resolve-task <task-id>`",
            )
        if len(running) > 1:
            pairs = ", ".join(
                f"task {run.task_id!r} / run {run.id!r}" for run in running
            )
            raise FailRunError(
                "AmbiguousCurrentRun",
                f"more than one Run is running ({pairs}); re-run as "
                "`skillflow fail-run --task <task-id>`",
            )
        return running[0]
    task = store.get_task(conn, task_id)
    if task is None:
        raise FailRunError(
            "TaskNotFound",
            f"no task with id {task_id!r}; check the id, then run "
            "`skillflow fail-run --task <task-id>` again",
        )
    runs = store.list_runs_for_task(conn, task.id)
    if not runs:
        raise FailRunError(
            "RunNotFound",
            f"task {task.id!r} has no Runs; start one with "
            f"`skillflow resolve-task {task.id}`",
        )
    run = next((r for r in runs if r.status is RunStatus.RUNNING), None)
    if run is None:
        latest = runs[-1]
        raise FailRunError(
            "RunNotActive",
            f"task {task.id!r} has no running Run (latest run "
            f"{latest.id!r} is {latest.status.value!r}); a Run is never "
            "resumed -- start a new one with "
            f"`skillflow resolve-task {task.id}`",
        )
    return run


def _created_skill(conn: sqlite3.Connection, run: Run) -> str | None:
    """Return the skill of skill-targeted ``run`` from its ``run.created`` event.

    ``create_run`` records the action's skill in the payload because the
    ``Run`` row has no skill column; the first event in ``(created_at, id)``
    order wins, so the lookup is deterministic. ``None`` when no
    ``run.created`` event for the Run carries one. Reads only.
    """
    for event in store.list_lifecycle_events_for_task(conn, run.task_id):
        if (
            event.run_id == run.id
            and event.type is LifecycleEventType.RUN_CREATED
            and event.payload is not None
            and event.payload.get("skill")
        ):
            return event.payload["skill"]
    return None


def fail_run(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    task_id: str | None = None,
    request: FailureRequest,
) -> RunFailure:
    """Record the current ``running`` Run as failed, with diagnostics.

    Implements the pipeline in the module docstring verbatim. ``task_id`` is
    a disambiguator only: it selects which Task's running Run fails when
    several Runs are ``running`` in one workspace. Raises
    :class:`FailRunError` (with ``code``) for every command-level rejection,
    and lets ``EvaluationError`` (the failed step vanished from the
    definition) and ``WorkflowLoadError`` propagate unchanged -- both are
    flat classes carrying their own identifiers, so re-wrapping would only
    lose information.
    """
    if not isinstance(request, FailureRequest):
        raise ValueError("fail_run() request must be a FailureRequest")

    run = _resolve_current_run(conn, task_id)

    if run.workflow_definition_id is None:
        raise FailRunError(
            "StepUnresolved",
            f"run {run.id!r} names no Workflow Definition, so its retry "
            "target cannot be resolved -- investigate how this Run was created",
        )
    definition = load_definition(workspace.workflows_dir, run.workflow_definition_id)

    if run.step_id is None:
        # A skill-targeted Run (SF-A-4 §9): the retry re-issues the recorded
        # skill, resolved from the Run's own `run.created` payload.
        skill = _created_skill(conn, run)
        if skill is None:
            raise FailRunError(
                "StepUnresolved",
                f"run {run.id!r} is skill-targeted but its `run.created` "
                "event names no skill, so the retry target cannot be "
                "resolved -- investigate the Run history",
            )
    else:
        skill = None

    # The Run resolves inside this operation, so the CLI submission loader
    # cannot pre-check chain types -- it runs here instead, before the write
    # block, with the same message it would carry there.
    for submission in request.artifacts:
        previous = store.latest_artifact(conn, run.task_id, submission.name)
        if previous is not None and previous.type != submission.type:
            raise FailRunError(
                "InvalidArtifactSubmission",
                f"artifact {submission.name!r} in task {run.task_id!r} is of "
                f"type {previous.type!r}; refusing the submission as type "
                f"{submission.type!r} -- fix the TYPE part of NAME:TYPE:PATH, "
                "then re-run `skillflow fail-run --task "
                f"{run.task_id}`",
            )

    # The Task is loaded here, after every rejection, so the error precedence
    # above is untouched. `runs.task_id` is a foreign key, so a missing Task
    # is unreachable except via raw SQL; LookupError matches the service
    # convention for "no task with id".
    task = store.get_task(conn, run.task_id)
    if task is None:
        raise LookupError(f"no task with id {run.task_id!r}")

    now = datetime.now(UTC)
    diagnostics_path = (
        f"{RUNS_DIR_NAME}/{run.id}/{OUTPUT_LOG_FILE_NAME}"
        if request.diagnostics is not None
        else None
    )
    result = Result(
        id=_new_id("result"),
        run_id=run.id,
        status=ResultStatus.FAILED,
        created_at=now,
        outcome=None,
        metadata=(
            {"diagnostics": diagnostics_path} if diagnostics_path is not None else None
        ),
    )
    failed = replace(run, status=RunStatus.FAILED, completed_at=now)
    # Evaluated once, before the write block, against the failed snapshots.
    action = evaluate(
        EvaluationInput(
            task=task,
            workflow=definition,
            current_run=failed,
            result=result,
            current_skill=skill,
        )
    )
    result_event = LifecycleEvent(
        id=_new_id("event"),
        task_id=run.task_id,
        run_id=run.id,
        type=LifecycleEventType.RESULT_CREATED,
        payload={"result_id": result.id, "status": result.status.value},
        created_at=now,
    )
    run_event = LifecycleEvent(
        id=_new_id("event"),
        task_id=run.task_id,
        run_id=run.id,
        type=LifecycleEventType.RUN_FAILED,
        payload={"status": failed.status.value},
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
                if path is not None:
                    # A None path reuses a byte-identical orphan from a crashed
                    # attempt (SF-36): nothing this attempt created, nothing to
                    # unlink.
                    written.append(path)
            store.insert_result(conn, result)
            store.insert_lifecycle_event(conn, result_event)
            store.update_run(conn, failed)
            store.insert_lifecycle_event(conn, run_event)
            if request.diagnostics is not None:
                diagnostics_file = write_diagnostics(
                    workspace, run_id=run.id, content=request.diagnostics
                )
                if diagnostics_file is not None:
                    written.append(diagnostics_file)
    except BaseException:
        for path in written:
            # Only files this attempt wrote are dropped; a submission that
            # failed to write never reaches this list.
            path.unlink(missing_ok=True)
        raise
    return RunFailure(
        run=failed,
        result=result,
        task=task,
        action=action,
        artifacts=tuple(registered),
        diagnostics_path=diagnostics_path,
    )
