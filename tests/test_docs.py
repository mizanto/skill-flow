"""Rot guards between docs and code (SF-39, SF-53).

The user guide and reference example name CLI subcommands, skills, and
files; these tests fail if the docs drift from what exists. They match
narrow, unambiguous patterns (code spans, skill prefixes, markdown
links) so ordinary prose can never trip them.

``/skillflow:<name>`` references resolve against the command files under
``plugins/skillflow/commands/`` or the skill directories under
``plugins/skillflow/skills/`` (SF-53: docs name the ``/skillflow:work``
driver skill).
"""

import argparse
import re
from pathlib import Path

from skillflow.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
COMMANDS_DIR = ROOT / "plugins" / "skillflow" / "commands"
SKILLS_DIR = ROOT / "plugins" / "skillflow" / "skills"


def _parser_subcommands() -> set[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("skillflow parser has no subcommands")


def test_documented_subcommands_exist():
    # Backtick-anchored so dotted paths (`skillflow.db`), flags
    # (`skillflow --version`), and bare prose never match: only real
    # invocations in code spans are checked.
    pattern = re.compile(r"`skillflow ([a-z][a-z-]+)")
    subcommands = _parser_subcommands()
    for doc in DOCS:
        for token in sorted(set(pattern.findall(doc.read_text(encoding="utf-8")))):
            assert token in subcommands, f"{doc.name} names unknown subcommand: {token}"


def test_documented_skills_exist():
    pattern = re.compile(r"/skillflow:([a-z-]+)")
    for doc in DOCS:
        for token in sorted(set(pattern.findall(doc.read_text(encoding="utf-8")))):
            assert (COMMANDS_DIR / f"{token}.md").is_file() or (
                SKILLS_DIR / token / "SKILL.md"
            ).is_file(), f"{doc.name} names unknown command or skill: {token}"


def test_documented_relative_links_resolve():
    pattern = re.compile(r"\]\(([^)\s]+)\)")
    for doc in DOCS:
        for target in sorted(set(pattern.findall(doc.read_text(encoding="utf-8")))):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = (doc.parent / target.split("#")[0]).resolve()
            assert path.exists(), f"{doc.name} links to missing path: {target}"
