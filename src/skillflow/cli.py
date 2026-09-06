"""Command-line entry point for SkillFlow.

SF-1 introduces only ``--version`` and ``--help``. The lifecycle commands
(``resolve-task``, ``prepare-artifacts``, ``complete-run``, ``decide``) are the
responsibility of later issues and are deliberately not stubbed here.
"""

from __future__ import annotations

import argparse
import sys

from skillflow import __version__


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
    return parser


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
    parser.parse_args(argv_list)
    if not argv_list:
        parser.print_help()
    return 0
