"""Command-line entry point for SkillFlow.

``--version`` / ``--help`` plus the lifecycle subcommands implemented so far
(``resolve-task``; ``prepare-artifacts``, ``complete-run`` and ``decide``
remain later issues and are deliberately not stubbed here).

Exit codes: ``0`` on success, ``1`` for a lifecycle/definition rejection
(the message goes to stderr), ``2`` for usage errors (argparse's own).
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from skillflow import __version__, service, store, workspace
from skillflow.evaluator import EvaluationError, WorkflowSelectionRequiredError
from skillflow.resolve_task import ResolveTaskError, resolve_task
from skillflow.run_input import RunInput
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
    return parser


def format_run_input(run_input: RunInput) -> str:
    """Render a ``RunInput`` as human-readable text.

    Pure formatting: Task id/title/description (the description is omitted
    when empty -- it defaults to ``""``), Run id and ``running`` status,
    step id/skill/model/effort, the selected context artifacts (name, type,
    version) with unresolved declared types as an informational line, the
    expected outputs with ``required`` flags, and a closing block naming the
    next command. No lifecycle state is re-derived here.
    """
    lines = [
        f"Task {run_input.task_id}: {run_input.task_title}",
        f"Run {run_input.run_id} (running) "
        f"-- step {run_input.step_id!r} via skill {run_input.skill!r}",
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
    lines.append(
        "Next: do the bounded work for this step, then run "
        "`/skillflow:prepare-artifacts`."
    )
    return "\n".join(lines)


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
    parser.print_help()
    return 0
