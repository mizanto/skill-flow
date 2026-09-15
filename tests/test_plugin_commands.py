"""Contract tests for the SkillFlow Claude Code plugin (SF-29, SF-52).

The plugin is static: a root manifest plus marketplace entry, a ``bin/``
shim, and four command files under ``plugins/skillflow/``. These tests pin
what SF-29's acceptance criteria require -- the exact discovery set, valid
frontmatter, exactly one deterministic runtime operation per skill,
required cross-command pointers, the prohibited-command list in every
file, and no lifecycle business logic in skill prose -- plus what SF-52
requires: manifest pointers, the marketplace agreement, the shim
contract, and no shipped MCP config.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from skillflow import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = ROOT / ".claude-plugin" / "marketplace.json"
SHIM = ROOT / "bin" / "skillflow"
COMMANDS_DIR = ROOT / "plugins" / "skillflow" / "commands"

COMMANDS = ("resolve-task", "prepare-artifacts", "complete-run", "decide")

#: ``skillflow <subcommand>`` with a space: the CLI invocation form. Skill
#: cross-references always use the ``/skillflow:<name>`` form, which never
#: matches this pattern.
SUBCOMMAND_PATTERN = re.compile(
    r"skillflow (resolve-task|prepare-artifacts|complete-run|decide)"
)

#: SF-A-5 §11: names that must never be invoked, invented, or emulated.
PROHIBITED = (
    "start-run",
    "create-run",
    "create-artifact",
    "create-result",
    "transition",
    "next-step",
    "retry",
    "rework",
    "loop",
    "iteration",
    "handoff",
)

#: Implementation markers that must never appear in skill prose. Conceptual
#: terms such as "Lifecycle Evaluation" or "Result" are fine -- reporting the
#: runtime's output is the skill's job; reimplementing it is not.
LOGIC_MARKERS = (
    "sqlite3",
    "INSERT",
    "UPDATE tasks",
    "ActionType",
    "EvaluationInput",
    "import skillflow",
)

#: The minimal frontmatter surface SF-29 chose. A new key (``model``,
#: ``context: fork``, hooks, ...) fails loudly here so the addition is a
#: deliberate, reviewed decision.
ALLOWED_FRONTMATTER_KEYS = frozenset(
    {"description", "argument-hint", "arguments", "allowed-tools"}
)


def _read_command(name: str) -> tuple[dict, str]:
    """Return ``(frontmatter, full_text)`` for a command file."""
    text = (COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---", f"{name}: frontmatter must start on line 1"
    assert "---" in lines[1:], f"{name}: unterminated frontmatter"
    end = lines.index("---", 1)
    frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    assert isinstance(frontmatter, dict), f"{name}: frontmatter is not a map"
    return frontmatter, text


def test_manifest_is_valid_and_version_mirrored():
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "skillflow"
    assert manifest["description"]
    assert manifest["version"] == __version__
    for key in ("commands", "skills"):
        target = ROOT / str(manifest[key])
        assert target.is_dir(), f"plugin.json {key} points nowhere: {manifest[key]!r}"


def test_marketplace_manifest_agrees_with_plugin_manifest():
    marketplace = json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert marketplace["name"]
    assert marketplace["description"]
    assert marketplace["owner"]["name"]
    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["description"] == manifest["description"]
    assert entry["version"] == manifest["version"] == __version__
    assert entry["source"] == "./"


def test_no_mcp_config_shipped():
    # A committed plugin-root .mcp.json is inherited by every install
    # as the plugin's MCP config (no manifest key suppresses it). A
    # local dev copy is fine, but the ignore rule must hold: tracked
    # files are never "ignored", so this one check pins both the
    # .gitignore entry and the untracked state.
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".mcp.json"],
        cwd=ROOT,
        capture_output=True,
    )
    assert result.returncode == 0, (
        "root .mcp.json is not ignored; committing it would ship dev MCP "
        "servers to every plugin user"
    )


def test_shim_contract():
    assert SHIM.is_file(), "bin/skillflow missing"
    assert SHIM.stat().st_mode & 0o111, "bin/skillflow lost its exec bit"
    text = SHIM.read_text(encoding="utf-8")
    assert text.split("\n", 1)[0] == "#!/bin/sh"
    for token in (
        "SKILLFLOW_BUNDLED_WORKFLOWS",  # SF-45 contract: start() staging dir
        "UV_PROJECT_ENVIRONMENT",
        "CLAUDE_PLUGIN_DATA",
        "exec uv run",
        "--project",
        '"$@"',
    ):
        assert token in text, f"bin/skillflow lost {token!r}"


def test_shim_runs_version():
    # Runs under `uv run` like the rest of the suite, so the project env
    # is already synced and the shim's inner `uv run --project` is a
    # fast no-op resolving to this checkout's own CLI.
    result = subprocess.run(
        [str(SHIM), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert f"skillflow {__version__}" in result.stdout


def test_plugin_validates():
    # Pins the SF-52 verification bar in-repo: quota-free, and skipped
    # where the claude CLI is unavailable (mirrors the acceptance
    # suite's _claude_binary guard).
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")
    result = subprocess.run(
        ["claude", "plugin", "validate", "--strict", str(ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-2000:]


def test_commands_directory_contains_exactly_the_four_commands():
    assert sorted(p.name for p in COMMANDS_DIR.glob("*.md")) == sorted(
        f"{name}.md" for name in COMMANDS
    )


@pytest.mark.parametrize("name", COMMANDS)
def test_frontmatter_schema(name):
    frontmatter, _ = _read_command(name)
    assert frontmatter.get("description"), f"{name}: empty description"
    assert set(frontmatter) <= ALLOWED_FRONTMATTER_KEYS, (
        f"{name}: unexpected frontmatter keys "
        f"{sorted(set(frontmatter) - ALLOWED_FRONTMATTER_KEYS)}"
    )
    assert (
        frontmatter.get("allowed-tools") == "Bash(skillflow:*)"
    ), f"{name}: unexpected tool grant {frontmatter.get('allowed-tools')!r}"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("resolve-task", True),
        ("prepare-artifacts", False),
        ("complete-run", False),
        ("decide", True),
    ],
)
def test_argument_hint_only_where_the_user_passes_arguments(name, expected):
    frontmatter, _ = _read_command(name)
    assert ("argument-hint" in frontmatter) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("resolve-task", True),
        ("prepare-artifacts", False),
        ("complete-run", False),
        ("decide", True),
    ],
)
def test_argument_substitution_only_where_the_user_passes_arguments(name, expected):
    _, text = _read_command(name)
    assert ("$ARGUMENTS" in text) is expected


@pytest.mark.parametrize("name", COMMANDS)
def test_skill_invokes_exactly_its_own_runtime_operation(name):
    _, text = _read_command(name)
    found = SUBCOMMAND_PATTERN.findall(text)
    assert found == [
        name
    ], f"{name}: expected exactly one `skillflow {name}` invocation, found {found}"


@pytest.mark.parametrize(
    ("name", "snippet"),
    [
        ("resolve-task", "/skillflow:prepare-artifacts"),
        ("prepare-artifacts", "/skillflow:complete-run"),
        ("complete-run", "/skillflow:resolve-task"),
        ("complete-run", "new Claude Code session"),
        ("decide", "/skillflow:resolve-task"),
    ],
)
def test_required_next_command_pointers(name, snippet):
    _, text = _read_command(name)
    assert snippet in text, f"{name}: missing {snippet!r} pointer"


@pytest.mark.parametrize("name", COMMANDS)
def test_prohibited_commands_listed(name):
    _, text = _read_command(name)
    missing = [item for item in PROHIBITED if item not in text]
    assert not missing, f"{name}: prohibited list drops {missing}"


@pytest.mark.parametrize("name", COMMANDS)
def test_no_lifecycle_logic_markers(name):
    _, text = _read_command(name)
    present = [marker for marker in LOGIC_MARKERS if marker in text]
    assert not present, f"{name}: business-logic markers {present}"


@pytest.mark.parametrize("name", COMMANDS)
def test_no_shell_execution_directives(name):
    # Dynamic argv construction (repeatable --artifact, recovery flags) and
    # error-recovery re-invocation require Claude-driven Bash; ``!`...` ``
    # inline execution and ```! fenced blocks would bypass that.
    _, text = _read_command(name)
    assert "`!" not in text, f"{name}: shell-execution directive found"


@pytest.mark.parametrize("name", COMMANDS)
def test_prerequisite_and_working_directory_noted(name):
    _, text = _read_command(name)
    assert "skillflow --version" in text, f"{name}: missing CLI prerequisite"
    assert (
        "inside the target repository" in text
    ), f"{name}: missing working-directory rule"
