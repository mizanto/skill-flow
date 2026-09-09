"""Command-line entry point for SkillFlow.

``--version`` / ``--help`` plus the lifecycle subcommands (``resolve-task``,
``prepare-artifacts``, ``complete-run``, ``decide`` and the ``fail-run``
operator command).

Exit codes: ``0`` on success, ``1`` for a lifecycle/definition/input rejection
(the message goes to stderr), ``2`` for usage errors (argparse's own).
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

from skillflow import __version__, service, store, workspace
from skillflow.artifacts import ArtifactStorageError
from skillflow.complete_run import CompleteRunError, RunCompletion, complete_run
from skillflow.completion import (
    ArtifactSubmission,
    CompletionError,
    CompletionRequest,
)
from skillflow.decide import DecideError, DecisionRecord, decide
from skillflow.decisions import DecisionError, DecisionRequest
from skillflow.domain import Run, RunStatus
from skillflow.evaluator import EvaluationError, WorkflowSelectionRequiredError
from skillflow.fail_run import FailRunError, FailureRequest, RunFailure, fail_run
from skillflow.prepare_artifacts import (
    ArtifactReport,
    PrepareArtifactsError,
    prepare_artifacts,
)
from skillflow.resolve_task import ResolveTaskError, resolve_task
from skillflow.run_input import RunInput
from skillflow.workflow import ActionType
from skillflow.workflow_loader import WorkflowLoadError
from skillflow.workspace import WorkspaceError


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
    resolve_parser.add_argument("task_id", help="The Task to resolve.")
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
    return parser


def format_run_input(run_input: RunInput) -> str:
    """Render a ``RunInput`` as human-readable text.

    Pure formatting: Task id/title/description (the description is omitted
    when empty -- it defaults to ``""``), Run id and ``running`` status,
    step id/skill/model/effort, the selected context artifacts (name, type,
    version) with unresolved declared types as an informational line, the
    expected outputs with ``required`` flags, and a closing block naming the
    next command. A skill-targeted Run (``step_id`` ``None``, SF-32) prints
    a skill-forward header with no step line and closes with the
    ``/skillflow:complete-run`` pointer -- it declares no outputs for
    ``/skillflow:prepare-artifacts`` to inspect. No lifecycle state is
    re-derived here.
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
                f"  - {artifact.name} ({artifact.type} v{artifact.version})"
            )
    else:
        lines.append("Context: none selected")
    if run_input.context.unresolved:
        lines.append(
            "Unresolved context types: "
            + ", ".join(run_input.context.unresolved)
        )
    if run_input.outputs:
        lines.append("Expected outputs:")
        for output in run_input.outputs:
            lines.append(
                f"  - {output.type} "
                f"({'required' if output.required else 'optional'})"
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
    ``run`` action targeting a step prints the next step with the
    ``/skillflow:resolve-task`` pointer for a new Claude Code session; a
    ``run`` action targeting only a skill prints the skill and reason with
    the same pointer (skill-targeted Runs resolve since SF-32); ``human``
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
        lines.append("Start the next Run in a new Claude Code session:")
        lines.append("")
        lines.append(f"/skillflow:resolve-task {completion.task.id}")
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
    ``run`` action targeting a step prints the next step with the
    ``/skillflow:resolve-task`` pointer for a new Claude Code session; a
    ``run`` action targeting only a skill prints the skill and reason with
    the same pointer (skill-targeted Runs resolve since SF-32) -- both
    mirroring :func:`format_completion`; ``complete`` / ``cancel`` print
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
        lines.append("Start the next Run in a new Claude Code session:")
        lines.append("")
        lines.append(f"/skillflow:resolve-task {record.task.id}")
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
    with the ``/skillflow:resolve-task`` pointer for a new Claude Code
    session; targeting only a skill prints the skill and reason with the
    same pointer -- both mirroring :func:`format_completion`. No lifecycle
    state is re-derived here.
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
    lines.append("Start the next Run in a new Claude Code session:")
    lines.append("")
    lines.append(f"/skillflow:resolve-task {failure.task.id}")
    return "\n".join(lines)


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
        print(str(exc), file=sys.stderr)
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
    rerun: str = "`/skillflow:complete-run`",
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
    rerun: str = "`/skillflow:complete-run`",
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


def _run_resolve_task(*, task_id: str, workflow: str | None) -> int:
    """Execute ``resolve-task``; return a process exit code."""
    try:
        ws = workspace.Workspace(root=workspace.find_repo_root())
        with contextlib.closing(store.open_store(ws)) as conn:
            result = resolve_task(conn, ws, task_id=task_id, workflow=workflow)
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
        print(str(exc), file=sys.stderr)
        return 1
    print(format_run_input(result))
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
                "then re-run `/skillflow:complete-run`",
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
        print(str(exc), file=sys.stderr)
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
                "step, then re-run `/skillflow:decide <decision>`",
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
        print(str(exc), file=sys.stderr)
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
        print(str(exc), file=sys.stderr)
        return 1
    print(format_failure(result))
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
    parser.print_help()
    return 0
