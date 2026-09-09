"""Deterministic ``show-task`` inspection (SF-38).

The audit/debug view of a Task lifecycle: given a Task id, assemble the
Task, its Runs in store order with each Run's canonical Result,
registered artifacts and recorded decisions, plus the Task's lifecycle
events in chronological order (SF-038). One pipeline, executed in order:

```text
1  load Task ......................... missing -> TaskNotFound
2  runs = store.list_runs_for_task      (created_at, id order)
3  per run: get_result_for_run + list_artifacts_for_run
4  decisions = store.list_human_decisions_for_task, grouped by run in memory
5  events = store.list_lifecycle_events_for_task  (created_at, id order)
6  return TaskView(task, runs, events)
```

Every step is a read. This module performs **no writes at all** -- not a
row, not a ``LifecycleEvent`` (viewing is not a lifecycle transition),
not a file. Statuses, order, and references all come from domain rows;
events are displayed as history only, never replayed into state
(SF-A-2 §1, §8).

Deliberate limits, all load-bearing for a debugger:

* The Workflow Definition is never loaded: the view shows each Run's
  stored ``step_id`` / ``workflow_definition_id`` verbatim, so it keeps
  working when the definition file is missing, changed, or broken.
* No skill is resolved: recovering it from a ``run.created`` payload
  would make content depend on events, and the view must be correct
  with zero events present.
* Artifact and diagnostics *content* is never read: the view carries
  references (name, type, version, id, path) only.

Any Task status is viewable -- active, waiting, or terminal: auditing a
finished Task is the point. It imports only ``sqlite3``, ``dataclasses``
and ``skillflow`` value/store modules.
"""

import sqlite3
from dataclasses import dataclass

from skillflow.domain import (
    Artifact,
    HumanDecision,
    LifecycleEvent,
    Result,
    Run,
    Task,
)
from skillflow.store import (
    get_result_for_run,
    get_task,
    list_artifacts_for_run,
    list_human_decisions_for_task,
    list_lifecycle_events_for_task,
    list_runs_for_task,
)

__all__ = ["RunView", "ShowTaskError", "TaskView", "show_task"]


class ShowTaskError(Exception):
    """A Task lifecycle cannot be shown.

    One flat class carrying a ``code``, exactly like
    ``PrepareArtifactsError``: ``"TaskNotFound"`` (SF-A-5 §4.3's
    identifier) is the only rejection -- every status is viewable, and
    empty sections render instead of erroring. The message ends with the
    concrete next command (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class RunView:
    """One Run with everything recorded against it: its canonical Result
    (``None`` when the Run has none yet), the artifacts it registered in
    store order, and the decisions recorded against it in store order."""

    run: Run
    result: Result | None
    artifacts: tuple[Artifact, ...] = ()
    decisions: tuple[HumanDecision, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run, Run):
            raise ValueError("RunView.run must be a Run")
        if self.result is not None and not isinstance(self.result, Result):
            raise ValueError("RunView.result must be a Result or None")
        object.__setattr__(
            self,
            "artifacts",
            _checked_tuple(self.artifacts, "RunView", "artifacts", Artifact),
        )
        object.__setattr__(
            self,
            "decisions",
            _checked_tuple(self.decisions, "RunView", "decisions", HumanDecision),
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class TaskView:
    """What ``show-task`` found: the Task, its per-Run views in store
    order, and its lifecycle events in chronological (store) order."""

    task: Task
    runs: tuple[RunView, ...] = ()
    events: tuple[LifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task, Task):
            raise ValueError("TaskView.task must be a Task")
        object.__setattr__(
            self, "runs", _checked_tuple(self.runs, "TaskView", "runs", RunView)
        )
        object.__setattr__(
            self,
            "events",
            _checked_tuple(self.events, "TaskView", "events", LifecycleEvent),
        )


def _checked_tuple(value: object, owner: str, field_name: str, kind: type) -> tuple:
    """Return ``value`` as a tuple of ``kind``, or raise ``ValueError``.

    Mirrors :class:`complete_run.RunCompletion`'s coercion: a string is
    never an iterable of values here, and every member is type-checked so
    a malformed view fails at construction, not at rendering.
    """
    if isinstance(value, str):
        raise ValueError(f"{owner}.{field_name} must be an iterable")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(f"{owner}.{field_name} must be an iterable") from None
    for item in items:
        if not isinstance(item, kind):
            raise ValueError(f"{owner}.{field_name} must contain {kind.__name__}")
    return items


def show_task(conn: sqlite3.Connection, *, task_id: str) -> TaskView:
    """Assemble the lifecycle view of ``task_id``.

    Implements the pipeline in the module docstring verbatim. Takes only
    a connection: nothing here touches the filesystem or the Workflow
    Definition, so no ``Workspace`` is needed. Raises
    :class:`ShowTaskError` (with ``code``) when the Task does not exist.
    """
    task = get_task(conn, task_id)
    if task is None:
        raise ShowTaskError(
            "TaskNotFound",
            f"no task with id {task_id!r}; check the id, then run "
            "`skillflow show-task <task-id>` again",
        )
    runs = list_runs_for_task(conn, task.id)
    decisions = list_human_decisions_for_task(conn, task.id)
    views = []
    for run in runs:
        result = get_result_for_run(conn, run.id)
        artifacts = list_artifacts_for_run(conn, run.id)
        run_decisions = tuple(d for d in decisions if d.run_id == run.id)
        views.append(
            RunView(
                run=run,
                result=result,
                artifacts=tuple(artifacts),
                decisions=run_decisions,
            )
        )
    events = list_lifecycle_events_for_task(conn, task.id)
    return TaskView(task=task, runs=tuple(views), events=tuple(events))
