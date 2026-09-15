"""Command-line entry point for SkillFlow.

``--version`` / ``--help`` plus the lifecycle subcommands (``resolve-task``,
``prepare-artifacts``, ``complete-run``, ``decide``, the ``fail-run``
operator command, the read-only ``show-task`` debug command, the
read-only ``assignment`` lookup, and the ``start`` operator command that
creates a Task).

Exit codes: ``0`` on success, ``1`` for a lifecycle/definition/input rejection
(the message goes to stderr), ``2`` for usage errors (argparse's own).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

import skillflow
from skillflow import __version__, service, store, workspace
from skillflow.artifacts import ArtifactStorageError, content_path
from skillflow.assignment import AssignmentError, resolve_assignment
from skillflow.complete_run import CompleteRunError, RunCompletion, complete_run
from skillflow.completion import (
    ArtifactSubmission,
    CompletionError,
    CompletionRequest,
)
from skillflow.decide import DecideError, DecisionRecord, decide
from skillflow.decisions import DecisionError, DecisionRequest
from skillflow.domain import Artifact, LifecycleEvent, Run, RunStatus
from skillflow.evaluator import EvaluationError, WorkflowSelectionRequiredError
from skillflow.fail_run import FailRunError, FailureRequest, RunFailure, fail_run
from skillflow.prepare_artifacts import (
    ArtifactReport,
    PrepareArtifactsError,
    prepare_artifacts,
)
from skillflow.resolve_task import ResolveTaskError, resolve_task
from skillflow.run_input import RunInput
from skillflow.show_task import RunView, ShowTaskError, TaskView, show_task
from skillflow.workflow import ActionType
from skillflow.workflow_loader import (
    WorkflowLoadError,
    load_definition,
    workflow_path,
)
from skillflow.workspace import WorkspaceError, WorkspaceLayoutError


def build_parser() -> argparse.ArgumentParser:
    """Return the SkillFlow argument parser."""
    parser = argparse.ArgumentParser(
        prog="skillflow",
        description=(
            "Lightweight lifecycle orchestration for independent Claude Code runs."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"skillflow {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    resolve_parser = subparsers.add_parser(
        "resolve-task",
        help="Resolve a Task into a new running Run and print its RunInput.",
    )
    resolve_parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="The Task to resolve (default: the workspace's single active or "
        "waiting Task).",
    )
    resolve_parser.add_argument(
        "--workflow",
        default=None,
        help="Workflow Definition id to assign (required when the Task has none).",
    )
    prepare_parser = subparsers.add_parser(
        "prepare-artifacts",
        help="Report the current Run's expected durable outputs (existing/missing).",
    )
    prepare_parser.add_argument(
        "--task",
        default=None,
        dest="task",
        help="Task id, when more than one Run is running in this workspace.",
    )
    complete_parser = subparsers.add_parser(
        "complete-run",
        help="Complete the current running Run and report the next action.",
    )
    complete_parser.add_argument(
        "--task",
        default=None,
        dest="task",
        help="Task id, when more than one Run is running in this workspace.",
    )
    complete_parser.add_argument(
        "--outcome",
        default=None,
        help="Lifecycle outcome decision for the current step "
        "(omit when the step declares no outcomes).",
    )
    complete_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME:TYPE:PATH",
        help="Durable output submission; PATH is read as UTF-8. Repeatable.",
    )
    decide_parser = subparsers.add_parser(
        "decide",
        help="Record a human decision on the waiting Task and report the next action.",
    )
    decide_parser.add_argument(
        "decision",
        help="Human decision declared by the current step "
        "(e.g. `approve` on a review step).",
    )
    decide_parser.add_argument(
        "--task",
        default=None,
        dest="task",
        help="Task id, when more than one Task is waiting for a decision.",
    )
    decide_parser.add_argument(
        "--comment",
        default=None,
        help="Optional free-text comment stored with the decision.",
    )
    fail_parser = subparsers.add_parser(
        "fail-run",
        help="Record the current running Run as failed, with diagnostics.",
    )
    fail_parser.add_argument(
        "--task",
        default=None,
        dest="task",
        help="Task id, when more than one Run is running in this workspace.",
    )
    diagnostics = fail_parser.add_mutually_exclusive_group()
    diagnostics.add_argument(
        "--message",
        default=None,
        help="Inline failure diagnostics text, stored as output.log.",
    )
    diagnostics.add_argument(
        "--diagnostics-file",
        default=None,
        metavar="PATH",
        help="File whose UTF-8 content is stored as output.log.",
    )
    fail_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME:TYPE:PATH",
        help="Partial durable output submission; PATH is read as UTF-8. Repeatable.",
    )
    show_parser = subparsers.add_parser(
        "show-task",
        help="Show a Task's lifecycle: runs, results, artifacts, decisions, events.",
    )
    show_parser.add_argument("task_id", help="The Task to inspect.")
    assignment_parser = subparsers.add_parser(
        "assignment",
        help="Print the running Run's Assignment (its rebuilt RunInput).",
    )
    assignment_parser.add_argument(
        "--skill",
        default=None,
        help="Verify the running Run targets this skill; exit 1 otherwise.",
    )
    start_parser = subparsers.add_parser(
        "start",
        help="Create a Task from a bundled Workflow Definition.",
    )
    start_parser.add_argument(
        "--title",
        required=True,
        type=_non_blank_title,
        help="Task title (must be a non-empty string).",
    )
    start_parser.add_argument(
        "--description",
        default="",
        help="Optional free-text Task description.",
    )
    start_parser.add_argument(
        "--workflow",
        default="software-change",
        help="Workflow Definition id to assign (default: software-change).",
    )
    return parser


def _artifact_relpath(ws: workspace.Workspace, artifact: Artifact) -> str:
    """Return ``artifact``'s content path, relative to the repository root.

    ``artifacts.content_path`` resolves the absolute store path (and refuses
    a stored path escaping the store); relativising to the workspace root
    keeps the printed value portable between checkouts
    (``.skillflow/artifacts/<task>/<file>``), and ``as_posix`` keeps it
    stable on Windows. Pure path derivation: the file is never read, so a
    missing file still prints -- printing is not verification.
    """
    return content_path(ws, artifact).relative_to(ws.root).as_posix()


def format_run_input(
    run_input: RunInput, ws: workspace.Workspace, workflow_definition_id: str
) -> str:
    """Render a ``RunInput`` as human-readable text.

    Pure formatting: Task id/title/description (the description is omitted
    when empty -- it defaults to ``""``), Run id and ``running`` status,
    the Workflow Definition id, step id/skill/model/effort, the selected
    context artifacts (name, type, version, plus the repo-relative content
    path) with unresolved declared types as an informational line, the
    expected outputs with ``required`` flags, and a closing block naming the
    next command. A skill-targeted Run (``step_id`` ``None``, SF-32) prints
    a skill-forward header with no step line and closes with the
    ``/skillflow:complete-run`` pointer -- it declares no outputs for
    ``/skillflow:prepare-artifacts`` to inspect. No lifecycle state is
    re-derived here. Both ``resolve-task`` and ``assignment`` render through
    this one formatter, so a rebuilt Assignment prints exactly what creation
    printed (SF-44).
    """
    if run_input.step_id is None:
        run_line = (
            f"Run {run_input.run_id} (running) "
            f"-- skill {run_input.skill!r} (no workflow step)"
        )
    else:
        run_line = (
            f"Run {run_input.run_id} (running) "
            f"-- step {run_input.step_id!r} via skill {run_input.skill!r}"
        )
    lines = [
        f"Task {run_input.task_id}: {run_input.task_title}",
        run_line,
        f"Workflow: {workflow_definition_id}",
    ]
    if run_input.task_description:
        lines.append(f"Description: {run_input.task_description}")
    params = []
    if run_input.model is not None:
        params.append(f"model: {run_input.model}")
    if run_input.effort is not None:
        params.append(f"effort: {run_input.effort}")
    if params:
        lines.append("Execution: " + ", ".join(params))
    selected = run_input.context.artifacts
    if selected:
        lines.append("Context:")
        for artifact in selected:
            lines.append(
                f"  - {artifact.name} ({artifact.type} v{artifact.version}): "
                f"{_artifact_relpath(ws, artifact)}"
            )
    else:
        lines.append("Context: none selected")
    if run_input.context.unresolved:
        lines.append(
            "Unresolved context types: " + ", ".join(run_input.context.unresolved)
        )
    if run_input.outputs:
        lines.append("Expected outputs:")
        for output in run_input.outputs:
            lines.append(
                f"  - {output.type} ({'required' if output.required else 'optional'})"
            )
    else:
        lines.append("Expected outputs: none declared")
    if run_input.step_id is None:
        lines.append(
            "Next: do the bounded work for this skill, then run "
            "`/skillflow:complete-run`."
        )
    else:
        lines.append(
            "Next: do the bounded work for this step, then run "
            "`/skillflow:prepare-artifacts`."
        )
    return "\n".join(lines)


def format_artifact_report(report: ArtifactReport) -> str:
    """Render an ``ArtifactReport`` as human-readable text.

    Pure formatting: the Run id and ``running`` status, its step and Task,
    one ``✓`` / ``✗`` line per declared output in declaration order, then the
    closing block -- SF-A-5 §5.3's all-ready recommendation of
    ``/skillflow:complete-run`` when every required output is satisfied, else
    §5.4's guidance naming each missing type and its ``required`` flag. A
    satisfied check lists every matching artifact as ``name vN``,
    comma-separated (a Run may register several versions of one output). No
    lifecycle state is re-derived here.
    """
    lines = [
        f"Run {report.run_id} (running) "
        f"-- step {report.step_id!r} of task {report.task_id}",
    ]
    checks = report.validation.checks
    if not checks:
        lines.append("Expected outputs: none declared")
    else:
        lines.append("Expected outputs:")
        lines.append("")
        for check in checks:
            if check.satisfied:
                matched = ", ".join(
                    f"{artifact.name} v{artifact.version}"
                    for artifact in check.artifacts
                )
                lines.append(f"✓ {check.type} ({matched})")
            elif check.required:
                lines.append(f"✗ {check.type} (required)")
            else:
                lines.append(f"✗ {check.type} (optional)")
    lines.append("")
    if report.validation.is_complete:
        lines.append("All required artifacts are already prepared.")
        lines.append("")
        lines.append("You can now run:")
        lines.append("")
        lines.append("/skillflow:complete-run")
    else:
        missing = report.validation.missing_required
        heading = (
            "Please create the missing artifact:"
            if len(missing) == 1
            else "Please create the missing artifacts:"
        )
        lines.append(heading)
        lines.append("")
        for check in missing:
            lines.append(f"- type: {check.type}")
            lines.append(f"  required: {str(check.required).lower()}")
            lines.append("")
        lines.append(
            "Create each missing output as a normal file with ordinary "
            "Claude Code tools, then run:"
        )
        lines.append("")
        lines.append("/skillflow:complete-run")
    return "\n".join(lines)


def format_completion(completion: RunCompletion) -> str:
    """Render a ``RunCompletion`` as human-readable text.

    Pure formatting, branching on the evaluated action (SF-A-5 §6.10): a
    ``run`` action targeting a step prints the next step with the ``Next:
    `/skillflow:work` `` continuation line; a ``run`` action targeting only
    a skill prints the skill and reason with the same continuation line
    (skill-targeted Runs resolve since SF-32); ``human``
    prints the ``/skillflow:decide`` pointer; ``complete`` / ``cancel``
    print the terminal Task status (§6.10 has no cancel template, so its
    shape is derived from §6.9); a ``None`` action (a decisionless
    skill-targeted Run, which has no outcome rules) reports that no
    lifecycle action applies with the unchanged Task status. No lifecycle
    state is re-derived here.
    """
    lines = [f"Run {completion.run.id} completed.", ""]
    action = completion.action
    if action is None:
        lines.append("No lifecycle action applies to this Run (no workflow step).")
        lines.append("")
        lines.append("Task status:")
        lines.append(completion.task.status.value)
        return "\n".join(lines)
    if action.action is ActionType.RUN:
        lines.append("Next action:")
        if action.step is not None:
            lines.append(f"Run {action.step}.")
        else:
            lines.append(f"Run skill {action.skill!r} (reason: {action.reason}).")
        lines.append("")
        lines.append("Next: /skillflow:work")
        return "\n".join(lines)
    if action.action is ActionType.HUMAN:
        lines.append("Next action:")
        lines.append("Human decision required.")
        lines.append("")
        lines.append("Use:")
        lines.append("")
        lines.append("/skillflow:decide <decision>")
        return "\n".join(lines)
    # COMPLETE / CANCEL exhaust ActionType; the Task reached a terminal status.
    lines.append("Task status:")
    lines.append(completion.task.status.value)
    return "\n".join(lines)


def format_decision(record: DecisionRecord) -> str:
    """Render a ``DecisionRecord`` as human-readable text.

    Pure formatting, branching on the evaluated action (SF-A-5 §7.8): a
    ``run`` action targeting a step prints the next step with the ``Next:
    `/skillflow:work` `` continuation line; a ``run`` action targeting only
    a skill prints the skill and reason with the same continuation line
    (skill-targeted Runs resolve since SF-32) -- both mirroring
    :func:`format_completion`; ``complete`` / ``cancel`` print
    §7.8's terminal sentence. A ``human`` action raises ``ValueError``
    instead of rendering: it is unreachable by construction
    (``WorkflowStep`` rejects a ``decisions`` rule with ``action: human``),
    and printing a second ``/skillflow:decide`` pointer would imply
    human → human is supported. No lifecycle state is re-derived here.
    """
    lines = [f"Human decision recorded: {record.decision.decision}.", ""]
    action = record.action
    if action.action is ActionType.RUN:
        lines.append("Next action:")
        if action.step is not None:
            lines.append(f"Run {action.step}.")
        else:
            lines.append(f"Run skill {action.skill!r} (reason: {action.reason}).")
        lines.append("")
        lines.append("Next: /skillflow:work")
        return "\n".join(lines)
    if action.action is ActionType.HUMAN:
        raise ValueError(
            f"cannot format a 'human' decision action (reason "
            f"{action.reason!r}); a human decision must not itself produce "
            "another 'human' action (SF-A-5 §7.7)"
        )
    # COMPLETE / CANCEL exhaust ActionType; SF-A-5 §7.8's terminal sentence.
    lines.append(f"Task {record.task.status.value}.")
    return "\n".join(lines)


def format_failure(failure: RunFailure) -> str:
    """Render a ``RunFailure`` as human-readable text.

    Pure formatting: the failed Run, the unchanged Task status (a failed Run
    never fails the Task, SF-A-5 §3.6), the diagnostics path when captured,
    and the evaluated retry action -- always ``run``, so this is
    straight-line on that invariant (the failed rule returns ``run`` by
    construction). A ``run`` action targeting a step prints the next step
    with the ``Next: `/skillflow:work` `` continuation line; targeting only
    a skill prints the skill and reason with the same continuation line --
    both mirroring :func:`format_completion`. No lifecycle state is
    re-derived here.
    """
    lines = [f"Run {failure.run.id} failed.", ""]
    lines.append("Task status:")
    lines.append(failure.task.status.value)
    lines.append("")
    if failure.diagnostics_path is not None:
        lines.append(f"Diagnostics: {failure.diagnostics_path}")
        lines.append("")
    action = failure.action
    if action.action is not ActionType.RUN:
        raise ValueError(
            f"cannot format a {action.action.value!r} failure action; a failed "
            "Run always retries as 'run' (SF-35)"
        )
    lines.append("Next action:")
    if action.step is not None:
        lines.append(f"Run {action.step}.")
    else:
        lines.append(f"Run skill {action.skill!r} (reason: {action.reason}).")
    lines.append("")
    lines.append("Next: /skillflow:work")
    return "\n".join(lines)


def format_task_view(view: TaskView) -> str:
    """Render a ``TaskView`` as human-readable text.

    Pure formatting: the Task header (id, title, status, workflow
    assignment, description when non-empty, created/updated timestamps),
    one block per Run in view order (status, stored step and workflow,
    provenance, started/completed timestamps, Result with outcome and
    diagnostics reference, artifact references, recorded decisions),
    then the lifecycle events in chronological order with their payloads
    as sorted ``k=v`` pairs. Empty sections render as explicit
    ``none``/``none recorded`` lines rather than erroring. No lifecycle
    state is re-derived here.
    """
    task = view.task
    lines = [f"Task {task.id}: {task.title} ({task.status.value})"]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.workflow_definition_id is not None:
        lines.append(f"Workflow: {task.workflow_definition_id}")
    else:
        lines.append("Workflow: none assigned")
    lines.append(
        f"Created: {task.created_at.isoformat()} Updated: {task.updated_at.isoformat()}"
    )
    lines.append("")
    if not view.runs:
        lines.append("Runs: none")
    else:
        lines.append(f"Runs ({len(view.runs)}):")
        for index, run_view in enumerate(view.runs, start=1):
            lines.append("")
            lines.extend(_format_run_view(index, run_view))
    lines.append("")
    if not view.events:
        lines.append("Events: none recorded")
    else:
        lines.append(f"Events ({len(view.events)}):")
        for event in view.events:
            lines.append(_format_event(event))
    return "\n".join(lines)


def _format_run_view(index: int, run_view: RunView) -> list[str]:
    """Render one numbered Run block of a task view.

    Pure formatting over stored fields only: a missing step or workflow
    renders as ``none`` (no definition is consulted and no skill is
    inferred), a missing Result as ``none``, and empty artifact/decision
    lists as ``none`` lines.
    """
    run = run_view.run
    step = f"step {run.step_id!r}" if run.step_id is not None else "step none"
    if run.workflow_definition_id is not None:
        workflow = f"workflow {run.workflow_definition_id!r}"
    else:
        workflow = "workflow none"
    lines = [f"  [{index}] {run.id} ({run.status.value}) -- {step}, {workflow}"]
    if run.triggered_by_run_id is None:
        trigger = (
            run.trigger_reason if run.trigger_reason is not None else "none recorded"
        )
        lines.append(f"      trigger: {trigger}")
    else:
        lines.append(
            f"      trigger: {run.trigger_reason!r} "
            f"from run {run.triggered_by_run_id!r}"
        )
    started = run.started_at.isoformat() if run.started_at is not None else "none"
    if run.completed_at is not None:
        completed = run.completed_at.isoformat()
    else:
        completed = "none"
    lines.append(f"      started: {started}  completed: {completed}")
    result = run_view.result
    if result is None:
        lines.append("      Result: none")
    else:
        lines.append(f"      Result {result.id} ({result.status.value})")
        if result.outcome is None:
            lines.append("        outcome: none")
        else:
            lines.append(
                f"        outcome: {result.outcome.type}/{result.outcome.decision}"
            )
        if result.metadata is not None and "diagnostics" in result.metadata:
            lines.append(f"        diagnostics: {result.metadata['diagnostics']}")
    if not run_view.artifacts:
        lines.append("      Artifacts: none")
    else:
        lines.append("      Artifacts:")
        for artifact in run_view.artifacts:
            lines.append(
                f"        - {artifact.name} ({artifact.type} "
                f"v{artifact.version}) id {artifact.id} path {artifact.path}"
            )
    if not run_view.decisions:
        lines.append("      Decisions: none")
    else:
        lines.append("      Decisions:")
        for decision in run_view.decisions:
            lines.append(
                f"        - {decision.decision!r} id {decision.id} "
                f"at {decision.created_at.isoformat()}"
            )
            if decision.comment is not None:
                lines.append(f"          comment: {decision.comment}")
    return lines


def _format_event(event: LifecycleEvent) -> str:
    """Render one lifecycle event line: timestamp, type, run, payload.

    Pure formatting: the run segment is omitted for task-only events and
    the payload renders as sorted ``k=v`` pairs (empty when absent), so
    the output is deterministic for any event type, including ones this
    version never emits.
    """
    parts = [event.created_at.isoformat(), event.type.value]
    if event.run_id is not None:
        parts.append(f"run {event.run_id}")
    if event.payload:
        pairs = ", ".join(
            f"{key}={event.payload[key]}" for key in sorted(event.payload)
        )
        parts.append(pairs)
    return "  " + " ".join(parts)


#: Unchanged-state sentence appended to every rejection of each command
#: (SF-37). Each sentence is true on EVERY exit-1 path of its command:
#: ``resolve-task`` rejects only before Run creation (step 9), so it claims
#: just that -- a step-4 ``--workflow`` assignment may persist -- except
#: its post-commit format stage (SF-44), which carries its own truthful
#: sentence because the Run was created;
#: ``prepare-artifacts`` never writes; ``complete-run``/``decide``/
#: ``fail-run`` reject before their single write block or roll it back
#: (SF-36). The mutating commands' sentences presuppose no identified
#: Run/Task, since loader errors precede resolution.
_UNCHANGED_STATE = {
    "resolve-task": "No Run was created.",
    "prepare-artifacts": "Nothing was changed: this command only inspects state.",
    "complete-run": "No Result was created; no Run status was changed.",
    "decide": "No decision was recorded; no Task status was changed.",
    "fail-run": "No failure was recorded; no Run status was changed.",
    "show-task": "Nothing was changed: this command only inspects state.",
    "assignment": "Nothing was changed: this command only inspects state.",
    "start": "No Task was created.",
}


def format_error(command: str, exc: Exception) -> str:
    """Render a caught rejection as the uniform stderr envelope (SF-37).

    Pure formatting: ``skillflow <command>: <CODE>: <message>`` on line 1
    and the command's unchanged-state sentence on line 2. ``CODE`` is the
    exception's ``code`` attribute when present, else the class name, so
    both coded rejections (``RequiredArtifactsMissing``) and flat lower
    layer errors (``EvaluationError``) surface a stable identifier. The
    original message is preserved verbatim. No lifecycle state is
    re-derived here.
    """
    code = getattr(exc, "code", type(exc).__name__)
    return f"skillflow {command}: {code}: {exc}\n{_UNCHANGED_STATE[command]}"


def _run_prepare_artifacts(*, task_id: str | None) -> int:
    """Execute ``prepare-artifacts``; return a process exit code."""
    try:
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = prepare_artifacts(conn, ws, task_id=task_id)
    # Note: only the layers this command calls are caught. prepare-artifacts
    # performs deterministic reads (store, workflow_loader) -- it never
    # evaluates the lifecycle or creates a Run, so the service/evaluator/store
    # invariant classes its sibling resolve-task lists are unreachable here
    # and would be dead code.
    except (
        PrepareArtifactsError,
        WorkflowLoadError,
        WorkspaceError,
    ) as exc:
        print(format_error("prepare-artifacts", exc), file=sys.stderr)
        return 1
    print(format_artifact_report(result))
    return 0


#: Characters that make an artifact name a path rather than a filename.
#: Duplicated from ``artifacts._FORBIDDEN_IN_NAME`` on purpose: this CLI-side
#: pre-check turns a service ``ValueError`` (deliberately uncaught, so that a
#: programming error tracebacks rather than masquerading as a rejection) into
#: an actionable ``InvalidArtifactSubmission``. Divergence is fail-visible --
#: the service remains the authority and still raises ``ValueError``.
_FORBIDDEN_IN_NAME = ("/", "\\", "\x00")


def _resolve_current_run(conn: sqlite3.Connection, task_id: str | None) -> Run:
    """Return the ``running`` Run ``complete-run`` applies to.

    Mirrors ``prepare_artifacts()``'s resolution bit-for-bit, but raises
    :class:`CompleteRunError` and stops after selecting the Run: no Workflow
    step is loaded, because a skill-targeted Run (no workflow step) completes
    without one. Reads only.
    """
    if task_id is None:
        running = store.list_running_runs(conn)
        if not running:
            raise CompleteRunError(
                "RunNotFound",
                "no Run is running in this workspace; start one with "
                "`skillflow resolve-task <task-id>`",
            )
        if len(running) > 1:
            pairs = ", ".join(
                f"task {run.task_id!r} / run {run.id!r}" for run in running
            )
            raise CompleteRunError(
                "AmbiguousCurrentRun",
                f"more than one Run is running ({pairs}); re-run as "
                "`skillflow complete-run --task <task-id>`",
            )
        return running[0]
    task = store.get_task(conn, task_id)
    if task is None:
        raise CompleteRunError(
            "TaskNotFound",
            f"no task with id {task_id!r}; check the id, then run "
            "`skillflow complete-run --task <task-id>` again",
        )
    runs = store.list_runs_for_task(conn, task.id)
    if not runs:
        raise CompleteRunError(
            "RunNotFound",
            f"task {task.id!r} has no Runs; start one with "
            f"`skillflow resolve-task {task.id}`",
        )
    run = next((r for r in runs if r.status is RunStatus.RUNNING), None)
    if run is None:
        latest = runs[-1]
        raise CompleteRunError(
            "RunNotActive",
            f"task {task.id!r} has no running Run (latest run "
            f"{latest.id!r} is {latest.status.value!r}); a Run is never "
            "resumed -- start a new one with "
            f"`skillflow resolve-task {task.id}`",
        )
    return run


def _check_submission_name(
    *,
    name: str,
    spec: str,
    error_cls: type[CompleteRunError] | type[FailRunError] = CompleteRunError,
    rerun: str = "`skillflow complete-run`",
) -> None:
    """Reject ``name`` unless it is a plain filename.

    Mirrors ``artifacts._artifact_name``'s rule (see ``_FORBIDDEN_IN_NAME``):
    separator-free, no drive letter, not a directory entry. Surrounding
    whitespace is already stripped by the caller, matching the service.
    ``error_cls``/``rerun`` let ``fail-run`` share this loader: the rejection
    code is the same (``"InvalidArtifactSubmission"``), only the carrying
    class and the rerun hint differ.
    """
    if (
        name in {".", ".."}
        or any(char in name for char in _FORBIDDEN_IN_NAME)
        or PureWindowsPath(name).drive
    ):
        raise error_cls(
            "InvalidArtifactSubmission",
            f"--artifact {spec!r} names {name!r}, which is not a plain "
            "filename (no directories, separators, or drive letters); fix "
            f"the NAME part of NAME:TYPE:PATH, then re-run {rerun}",
        )


def _load_submissions(
    conn: sqlite3.Connection,
    run: Run | None,
    specs: list[str],
    *,
    error_cls: type[CompleteRunError] | type[FailRunError] = CompleteRunError,
    rerun: str = "`skillflow complete-run`",
) -> list[ArtifactSubmission]:
    """Read ``--artifact NAME:TYPE:PATH`` specs into submissions.

    For each spec, in order: split into three non-empty parts (surrounding
    whitespace stripped, matching the service's stripping, so duplicate and
    chain checks agree with it), read PATH as UTF-8, reject non-filename
    names; then reject duplicate names and -- when ``run`` is given -- names
    whose established chain type differs (``store.latest_artifact`` is a
    read). Every failure raises ``error_cls`` with code
    ``"InvalidArtifactSubmission"`` -- a v0 CLI refinement in the
    ``OutcomeRequired`` / ``OutcomeNotExpected`` tradition, since SF-A-5 §6.2
    leaves transport to the implementation; ``fail-run`` shares the loader
    with ``FailRunError`` and its own rerun hint, passing ``run=None``
    because the Run resolves inside the operation, which performs the chain
    pre-check itself. Reads only: no row, event, or content file is written
    here.
    """
    submissions: list[ArtifactSubmission] = []
    for spec in specs:
        parts = [part.strip() for part in spec.split(":", 2)]
        if len(parts) != 3 or not all(parts):
            raise error_cls(
                "InvalidArtifactSubmission",
                f"malformed --artifact {spec!r}; expected NAME:TYPE:PATH "
                "(e.g. `--artifact review.md:review:review.md`), then re-run "
                f"{rerun}",
            )
        name, type_, path = parts
        try:
            content = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise error_cls(
                "InvalidArtifactSubmission",
                f"--artifact file {path!r} (submission {name!r}) is not "
                "valid UTF-8; fix the file, then re-run "
                f"{rerun}",
            ) from None
        except (OSError, ValueError) as exc:
            # ValueError: embedded null byte in the path. UnicodeDecodeError
            # is caught above first (it subclasses ValueError).
            raise error_cls(
                "InvalidArtifactSubmission",
                f"cannot read --artifact file {path!r} (submission {name!r}): "
                f"{exc}; create it as an ordinary file, then re-run "
                f"{rerun}",
            ) from None
        _check_submission_name(name=name, spec=spec, error_cls=error_cls, rerun=rerun)
        submissions.append(ArtifactSubmission(name=name, type=type_, content=content))
    seen: set[str] = set()
    for submission in submissions:
        if submission.name in seen:
            raise error_cls(
                "InvalidArtifactSubmission",
                f"duplicate --artifact name {submission.name!r}; submit "
                f"each name once, then re-run {rerun}",
            )
        seen.add(submission.name)
    if run is not None:
        for submission in submissions:
            previous = store.latest_artifact(conn, run.task_id, submission.name)
            if previous is not None and previous.type != submission.type:
                raise error_cls(
                    "InvalidArtifactSubmission",
                    f"artifact {submission.name!r} in task {run.task_id!r} is "
                    f"of type {previous.type!r}; refusing the submission as "
                    f"type {submission.type!r} -- fix the TYPE part of "
                    f"NAME:TYPE:PATH, then re-run {rerun}",
                )
    return submissions


def _load_diagnostics(
    *, message: str | None, diagnostics_file: str | None
) -> str | None:
    """Resolve the ``fail-run`` diagnostics flags into file content.

    Returns ``message`` verbatim, the UTF-8 content of ``diagnostics_file``,
    or ``None`` when neither flag was given. Every failure raises
    :class:`FailRunError` with code ``"InvalidDiagnostics"`` -- a blank
    message is neither "absent" (that would reinterpret input) nor valid
    content. Reads only: the content file is written by the operation, inside
    its transaction.
    """
    if message is not None:
        if not message.strip():
            raise FailRunError(
                "InvalidDiagnostics",
                "empty --message; omit the flag or pass non-empty "
                "diagnostics text, then re-run `skillflow fail-run ...`",
            )
        return message
    if diagnostics_file is None:
        return None
    try:
        return Path(diagnostics_file).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FailRunError(
            "InvalidDiagnostics",
            f"--diagnostics-file {diagnostics_file!r} is not valid UTF-8; "
            "fix the file, then re-run `skillflow fail-run ...`",
        ) from None
    except (OSError, ValueError) as exc:
        # ValueError: embedded null byte in the path. UnicodeDecodeError
        # is caught above first (it subclasses ValueError).
        raise FailRunError(
            "InvalidDiagnostics",
            f"cannot read --diagnostics-file {diagnostics_file!r}: {exc}; "
            "fix the path, then re-run `skillflow fail-run ...`",
        ) from None


def _non_blank_title(value: str) -> str:
    """Validate the ``start --title`` argument.

    Argparse ``type`` hook: a blank title is user input, not a domain
    programming error, so it is excluded here (exit 2 with usage) rather
    than reaching ``domain.Task`` as a ``ValueError`` traceback. The
    surviving value is returned unchanged; the domain still strips it on
    persist.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("--title must be a non-empty string")
    return value


def _run_resolve_task(*, task_id: str | None, workflow: str | None) -> int:
    """Execute ``resolve-task``; return a process exit code."""
    try:
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = resolve_task(conn, ws, task_id=task_id, workflow=workflow)
            # The formatter's Workflow line: set by step 4 before any Run is
            # created, so a successful resolve always has one; the Task
            # exists (step 1) and the Run just created is its proof.
            task = store.get_task(conn, result.task_id)
    # Note: LookupError (the service layer's missing-entity convention) is
    # deliberately not caught: resolve_task establishes Task existence at
    # step 1, so every LookupError path in service.py is unreachable from
    # here, and an accidental KeyError/IndexError must traceback rather
    # than masquerade as a lifecycle rejection.
    except (
        ResolveTaskError,
        WorkflowSelectionRequiredError,
        EvaluationError,
        WorkflowLoadError,
        WorkspaceError,
        service.UnknownWorkflowError,
        service.WorkflowAssignmentError,
        service.RunCreationError,
        store.InvariantViolationError,
    ) as exc:
        print(format_error("resolve-task", exc), file=sys.stderr)
        return 1
    # Formatting is its own stage, after the commit: since SF-44 it resolves
    # each selected artifact's content path, and a stored path escaping the
    # store (raw SQL only -- every writer builds versioned filenames) is a
    # rejection, not a traceback. It cannot use format_error: the Run WAS
    # created, so "No Run was created." would be false (SF-37's invariant).
    try:
        output = format_run_input(result, ws, task.workflow_definition_id)
    except ArtifactStorageError as exc:
        print(
            f"skillflow resolve-task: ArtifactStorageError: {exc}\n"
            f"Run {result.run_id!r} was created, but its Assignment could "
            "not be printed; inspect the stored paths with "
            f"`skillflow show-task {result.task_id}`, fix the rows, then run "
            "`skillflow assignment`",
            file=sys.stderr,
        )
        return 1
    print(output)
    return 0


def _run_complete_run(
    *, task_id: str | None, outcome: str | None, artifacts: list[str]
) -> int:
    """Execute ``complete-run``; return a process exit code."""
    try:
        # Pure flag-shape validation first, before touching the workspace:
        # a blank outcome is neither "absent" (that would reinterpret input)
        # nor a valid decision (CompletionRequest would raise ValueError,
        # which stays uncaught by convention).
        if outcome is not None and not outcome.strip():
            raise CompletionError(
                "InvalidOutcome",
                "empty --outcome; omit the flag or pass a non-empty outcome, "
                "then re-run `skillflow complete-run`",
            )
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            run = _resolve_current_run(conn, task_id)
            submissions = _load_submissions(conn, run, artifacts)
            result = complete_run(
                conn,
                ws,
                run_id=run.id,
                request=CompletionRequest(decision=outcome, artifacts=submissions),
            )
    # Note: LookupError (the service layer's missing-entity convention) is
    # deliberately not caught: the Task is FK-guaranteed behind the resolved
    # Run, and a programming error must traceback rather than masquerade as
    # a lifecycle rejection -- the same stance _run_resolve_task documents
    # for ValueError, which is likewise uncaught here. EvaluationError IS
    # caught: a skill-targeted Run resolving to another skill is rejected at
    # evaluation (SF-32), so unlike a step Run -- whose validated decision
    # always maps -- complete-run can surface it.
    except (
        CompleteRunError,
        CompletionError,
        EvaluationError,
        WorkflowLoadError,
        WorkspaceError,
        ArtifactStorageError,
        store.InvariantViolationError,
    ) as exc:
        print(format_error("complete-run", exc), file=sys.stderr)
        return 1
    print(format_completion(result))
    return 0


def _run_decide(*, task_id: str | None, decision: str, comment: str | None) -> int:
    """Execute ``decide``; return a process exit code."""
    try:
        # Pure flag-shape validation first, before touching the workspace:
        # a blank decision is neither "absent" (that would reinterpret input)
        # nor a valid decision (DecisionRequest would raise ValueError,
        # which stays uncaught by convention).
        if not decision.strip():
            raise DecisionError(
                "InvalidHumanDecision",
                "empty decision; pass a decision declared by the current "
                "step, then re-run `skillflow decide <decision>`",
            )
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = decide(
                conn,
                ws,
                task_id=task_id,
                request=DecisionRequest(decision=decision, comment=comment),
            )
    # Note: LookupError (the service layer's missing-entity convention) is
    # deliberately not caught: the Task is established at the pipeline's
    # first step, and a programming error must traceback rather than
    # masquerade as a lifecycle rejection -- the same stance
    # _run_complete_run documents for ValueError, which is likewise uncaught
    # here. EvaluationError IS caught: decide evaluates stored history, so
    # the resolve-task precedent applies (`_run_complete_run` catches it as
    # well since SF-32 rejects skill→skill decisions at evaluation).
    except (
        DecideError,
        DecisionError,
        EvaluationError,
        WorkflowLoadError,
        WorkspaceError,
        store.InvariantViolationError,
    ) as exc:
        print(format_error("decide", exc), file=sys.stderr)
        return 1
    print(format_decision(result))
    return 0


def _run_fail_run(
    *,
    task_id: str | None,
    message: str | None,
    diagnostics_file: str | None,
    artifacts: list[str],
) -> int:
    """Execute ``fail-run``; return a process exit code."""
    try:
        # Pure flag-shape validation first, before touching the workspace:
        # diagnostics content is resolved from exactly one source (argparse
        # already rejects both flags together with exit 2).
        diagnostics = _load_diagnostics(
            message=message, diagnostics_file=diagnostics_file
        )
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            # The Run resolves inside the operation (like `decide`'s Task),
            # so the submission loader runs without the chain pre-check --
            # `fail_run` performs it once the Run is known.
            submissions = _load_submissions(
                conn,
                None,
                artifacts,
                error_cls=FailRunError,
                rerun="`skillflow fail-run ...`",
            )
            result = fail_run(
                conn,
                ws,
                task_id=task_id,
                request=FailureRequest(diagnostics=diagnostics, artifacts=submissions),
            )
    # Note: LookupError (the service layer's missing-entity convention) is
    # deliberately not caught: the Task is FK-guaranteed behind the resolved
    # Run, and a programming error must traceback rather than masquerade as
    # a lifecycle rejection -- the same stance _run_complete_run documents
    # for ValueError, which is likewise uncaught here. EvaluationError IS
    # caught: the failed step may have vanished from the definition, so
    # unlike a validated completion this evaluation can surface it.
    except (
        FailRunError,
        EvaluationError,
        WorkflowLoadError,
        WorkspaceError,
        ArtifactStorageError,
        store.InvariantViolationError,
    ) as exc:
        print(format_error("fail-run", exc), file=sys.stderr)
        return 1
    print(format_failure(result))
    return 0


def _run_show_task(*, task_id: str) -> int:
    """Execute ``show-task``; return a process exit code."""
    try:
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = show_task(conn, task_id=task_id)
    # Note: only the layers this command calls are caught. show-task
    # performs deterministic reads -- it never writes, evaluates the
    # lifecycle, loads a Workflow, or creates a Run, so the invariant,
    # evaluation, loader, and service classes its siblings list are
    # unreachable here and would be dead code.
    except (
        ShowTaskError,
        WorkspaceError,
    ) as exc:
        print(format_error("show-task", exc), file=sys.stderr)
        return 1
    print(format_task_view(result))
    return 0


def _run_assignment(*, skill: str | None) -> int:
    """Execute ``assignment``; return a process exit code."""
    try:
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = resolve_assignment(conn, ws, expected_skill=skill)
            run = store.get_run(conn, result.run_id)
        # The Run exists -- resolve_assignment just rebuilt this input from
        # it -- and names the Workflow Definition the Workflow line prints
        # (a missing one rejects as StepUnresolved inside the operation).
        # Formatting stays inside the try: it resolves each selected
        # artifact's content path, and a stored path escaping the store is
        # a rejection, not a traceback. The unchanged-state sentence stays
        # true here -- unlike resolve-task, nothing was written.
        output = format_run_input(result, ws, run.workflow_definition_id)
    # Note: only the layers this command calls are caught. assignment
    # performs deterministic reads -- it never writes, evaluates the
    # lifecycle, or creates a Run, so the invariant, evaluation, and
    # service classes its siblings list are unreachable here and would be
    # dead code. LookupError is deliberately not caught either: the Task
    # is FK-guaranteed behind the resolved Run, and a programming error
    # must traceback rather than masquerade as a lifecycle rejection.
    except (
        AssignmentError,
        WorkflowLoadError,
        WorkspaceError,
        ArtifactStorageError,
    ) as exc:
        print(format_error("assignment", exc), file=sys.stderr)
        return 1
    print(output)
    return 0


def _bundled_workflows_dir() -> Path:
    """Return the directory holding the bundled Workflow Definitions.

    ``SKILLFLOW_BUNDLED_WORKFLOWS`` (exported by the SF-52 plugin shim)
    wins when set and non-empty; otherwise fall back to the ``workflows/``
    directory beside the installed package (the repository root in a
    checkout). Pure path resolution: existence is checked by the caller,
    so a misconfigured install surfaces as the actionable
    unknown-workflow rejection, not here.
    """
    configured = os.environ.get("SKILLFLOW_BUNDLED_WORKFLOWS")
    if configured:
        return Path(configured)
    return Path(skillflow.__file__).resolve().parents[2] / "workflows"


def _stage_workflow_definition(
    ws: workspace.Workspace, workflow_id: str, bundled_dir: Path
) -> None:
    """Pin the bundled definition for ``workflow_id`` into the repository.

    A present ``<repo>/workflows/<id>.yaml`` — pinned earlier or
    hand-written — is authoritative and never overwritten. Otherwise the
    bundled file is copied byte-exact (creating ``workflows/`` when
    needed). When neither copy exists, raise ``WorkflowLoadError``
    before any database write, so the rejection carries the standard
    envelope and no Task is created.
    """
    dest = workflow_path(ws.workflows_dir, workflow_id)
    if dest.exists():
        return
    src = workflow_path(bundled_dir, workflow_id)
    if not src.is_file():
        raise WorkflowLoadError(
            f"unknown workflow {workflow_id!r}: no {dest.name} in "
            f"{ws.workflows_dir} and no bundled definition in {bundled_dir}; "
            "pass --workflow with an id that has a bundled definition, "
            "or place the definition file in the repository workflows/ "
            "directory, then re-run `skillflow start`"
        )
    if dest.parent.exists() and not dest.parent.is_dir():
        raise WorkspaceLayoutError(f"expected a directory, found a file: {dest.parent}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())


def _run_start(*, title: str, description: str, workflow: str) -> int:
    """Execute ``start``; return a process exit code."""
    try:
        ws = workspace.init_workspace()
        _stage_workflow_definition(ws, workflow, _bundled_workflows_dir())
        with contextlib.closing(store.open_store(ws)) as conn:
            definition = load_definition(ws.workflows_dir, workflow)
            service.register_workflow(conn, definition)
            task = service.create_task(
                conn,
                title=title,
                description=description,
                workflow_definition_id=workflow,
            )
    # Note: only the layers this command calls are caught.
    # UnknownWorkflowError is unreachable (register_workflow commits the
    # verified id before create_task reads it); LookupError/ValueError are
    # deliberately not caught (a blank --title is excluded at argparse, so
    # a ValueError here is a programming error and must traceback rather
    # than masquerade as a lifecycle rejection).
    except (
        WorkflowLoadError,
        WorkspaceError,
    ) as exc:
        print(format_error("start", exc), file=sys.stderr)
        return 1
    print(task.id)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run SkillFlow.

    With no arguments, print help and exit ``0`` so that "the executable starts"
    is observable without a subcommand. Returns a process exit code.

    Note that argparse's ``version`` and ``help`` actions exit the process
    directly (via ``SystemExit``) from inside ``parse_args``, so ``main`` does
    not return on those paths.
    """
    parser = build_parser()
    argv_list = argv if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv_list)
    if not argv_list:
        parser.print_help()
        return 0
    if args.command == "resolve-task":
        return _run_resolve_task(task_id=args.task_id, workflow=args.workflow)
    if args.command == "prepare-artifacts":
        return _run_prepare_artifacts(task_id=args.task)
    if args.command == "complete-run":
        return _run_complete_run(
            task_id=args.task, outcome=args.outcome, artifacts=args.artifact
        )
    if args.command == "decide":
        return _run_decide(
            task_id=args.task, decision=args.decision, comment=args.comment
        )
    if args.command == "fail-run":
        return _run_fail_run(
            task_id=args.task,
            message=args.message,
            diagnostics_file=args.diagnostics_file,
            artifacts=args.artifact,
        )
    if args.command == "show-task":
        return _run_show_task(task_id=args.task_id)
    if args.command == "assignment":
        return _run_assignment(skill=args.skill)
    if args.command == "start":
        return _run_start(
            title=args.title, description=args.description, workflow=args.workflow
        )
    parser.print_help()
    return 0
