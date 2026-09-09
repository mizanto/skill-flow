"""Deterministic ``prepare-artifacts`` inspection (SF-21).

The Run protocol command between ``resolve-task`` and ``complete-run``: given
the current ``running`` Run, report which of its Workflow step's declared
expected outputs are already satisfied and which are missing, and tell Claude
what must be created (SF-A-5 §5). One pipeline, executed in order -- the order
is the contract, because it fixes error precedence:

```text
1  resolve the current Run
     task_id is None -> store.list_running_runs(conn)
                          0 rows  -> RunNotFound
                          >1 rows -> AmbiguousCurrentRun (name each task/run)
                          1 row   -> that Run
     task_id given   -> store.get_task            none -> TaskNotFound
                        store.list_runs_for_task   the RUNNING one, else
                          no runs at all          -> RunNotFound
                          runs, none running      -> RunNotActive (name latest
                                                     run id + status)
2  resolve the Run's step (all reads)
     run.workflow_definition_id is None -> StepUnresolved
     run.step_id is None                -> StepUnresolved
     load_definition(workspace.workflows_dir, run.workflow_definition_id)
                                        -> WorkflowLoadError propagates
     definition.find_step(run.step_id) is None -> WorkflowMismatch
3  artifacts = store.list_artifacts_for_run(conn, run.id)
4  validation = outputs.validate_outputs(step=step, run=run, artifacts=artifacts)
5  return ArtifactReport(task_id=run.task_id, run_id=run.id, step_id=step.id,
                         validation=validation)
```

Every step is a read. This module performs **no writes at all** -- not a row,
not a ``LifecycleEvent``, not a file. That is the mechanically testable form
of "no lifecycle mutation, Result, or next Run" (SF-A-5 §5.6): no Result is
created, no Run or Task status changes, no Lifecycle Evaluation runs, no next
Run is created, and Claude Code is never launched.

"Existing artifacts" means registered Artifact metadata for the current Run
(``list_artifacts_for_run`` + ``validate_outputs``, whose per-Run scope is
enforced in SF-16). The working tree is not inspected: ``ExpectedOutput``
declares only ``{type, required}`` (``docs/workflow-schema.md``), so there is
no declared filename to look for, and inventing a ``<type>.md`` convention
would be a workflow-schema change and DSL drift.

Task status is deliberately not checked: SF-A-5 §5.2 states one precondition
(a current Run exists and is ``running``), and this module does not re-derive
lifecycle state that is another command's precondition. It imports only
``sqlite3`` and ``skillflow`` value/layer modules.
"""

import sqlite3
from dataclasses import dataclass

from skillflow.domain import RunStatus
from skillflow.outputs import OutputValidation, validate_outputs
from skillflow.store import (
    get_task,
    list_artifacts_for_run,
    list_running_runs,
    list_runs_for_task,
)
from skillflow.workflow_loader import load_definition
from skillflow.workspace import Workspace

__all__ = ["ArtifactReport", "PrepareArtifactsError", "prepare_artifacts"]


class PrepareArtifactsError(Exception):
    """The current Run's expected outputs cannot be inspected.

    One flat class carrying a ``code``, exactly like ``ResolveTaskError``:
    ``"RunNotFound"`` / ``"RunNotActive"`` (SF-A-5 §6.3's identifiers),
    ``"TaskNotFound"`` (§4.3), plus ``"AmbiguousCurrentRun"``,
    ``"StepUnresolved"`` and ``"WorkflowMismatch"``. Every message ends with the
    concrete next command (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.domain` on purpose: keeping this module's
    "imports only ``sqlite3`` and ``skillflow``" boundary mechanically
    checkable is worth a few lines, and matches how ``domain`` / ``workflow``
    / ``outputs`` already stay independent.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactReport:
    """What ``prepare-artifacts`` found: the current Run, its step, the verdict."""

    task_id: str
    run_id: str
    step_id: str
    validation: OutputValidation

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _require_text(self.task_id, "task_id"))
        object.__setattr__(self, "run_id", _require_text(self.run_id, "run_id"))
        object.__setattr__(self, "step_id", _require_text(self.step_id, "step_id"))
        if not isinstance(self.validation, OutputValidation):
            raise ValueError("validation must be an OutputValidation")


def prepare_artifacts(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    task_id: str | None = None,
) -> ArtifactReport:
    """Report the current ``running`` Run's expected-output status.

    Implements the pipeline in the module docstring verbatim. ``task_id`` is a
    disambiguator only: it selects which Run to read when several are running,
    never influencing the report. Raises :class:`PrepareArtifactsError` (with
    ``code``) for every command-level rejection, and lets ``WorkflowLoadError``
    propagate unchanged.
    """
    if task_id is None:
        running = list_running_runs(conn)
        if not running:
            raise PrepareArtifactsError(
                "RunNotFound",
                "no Run is running in this workspace; start one with "
                "`skillflow resolve-task <task-id>`",
            )
        if len(running) > 1:
            pairs = ", ".join(
                f"task {run.task_id!r} / run {run.id!r}" for run in running
            )
            raise PrepareArtifactsError(
                "AmbiguousCurrentRun",
                f"more than one Run is running ({pairs}); re-run as "
                "`skillflow prepare-artifacts --task <task-id>`",
            )
        run = running[0]
    else:
        task = get_task(conn, task_id)
        if task is None:
            raise PrepareArtifactsError(
                "TaskNotFound",
                f"no task with id {task_id!r}; check the id, then run "
                "`skillflow prepare-artifacts --task <task-id>` again",
            )
        runs = list_runs_for_task(conn, task.id)
        if not runs:
            raise PrepareArtifactsError(
                "RunNotFound",
                f"task {task.id!r} has no Runs; start one with "
                f"`skillflow resolve-task {task.id}`",
            )
        run = next((r for r in runs if r.status is RunStatus.RUNNING), None)
        if run is None:
            latest = runs[-1]
            raise PrepareArtifactsError(
                "RunNotActive",
                f"task {task.id!r} has no running Run (latest run "
                f"{latest.id!r} is {latest.status.value!r}); a Run is never "
                "resumed -- start a new one with "
                f"`skillflow resolve-task {task.id}`",
            )

    if run.workflow_definition_id is None:
        raise PrepareArtifactsError(
            "StepUnresolved",
            f"run {run.id!r} names no Workflow Definition, so it has no "
            "declared outputs; record its outcome with "
            "`/skillflow:complete-run`",
        )
    if run.step_id is None:
        raise PrepareArtifactsError(
            "StepUnresolved",
            f"run {run.id!r} targets no workflow step (a skill-targeted Run, "
            "SF-A-4 §9), so it has no declared outputs; record its outcome "
            "with `/skillflow:complete-run`",
        )
    definition = load_definition(workspace.workflows_dir, run.workflow_definition_id)
    step = definition.find_step(run.step_id)
    if step is None:
        raise PrepareArtifactsError(
            "WorkflowMismatch",
            f"run {run.id!r} targets step {run.step_id!r}, absent from "
            f"workflow {definition.name!r}; the definition changed under this "
            "Run -- restore the step, then run "
            "`skillflow prepare-artifacts`",
        )

    artifacts = list_artifacts_for_run(conn, run.id)
    validation = validate_outputs(step=step, run=run, artifacts=artifacts)
    return ArtifactReport(
        task_id=run.task_id,
        run_id=run.id,
        step_id=step.id,
        validation=validation,
    )
