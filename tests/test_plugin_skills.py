"""Contract tests for the SkillFlow Execution Skills (SF-46) and driver (SF-47).

Execution Skills are static: one directory per skill under
``plugins/skillflow/skills/``, each with a ``SKILL.md`` that validates its
assignment, performs exactly one workflow step, and completes or fails its Run.
These tests pin what SF-46's acceptance criteria require -- every skill
directory maps to the reference-workflow step whose skill is
``skillflow:<dir>``; forked, non-interactive execution metadata matching the
step; the exact assignment render line first; outcomes and artifact types drawn
from the step's declarations; model/effort equal to the step's; no verbose
command invocation and no lifecycle business logic in skill prose.

The tests are parametrized over the discovered skill directories so SF-48 can
add skills without rewriting this file (it adds the reverse check -- every
workflow step has a skill directory).

``work/`` is the driver skill (SF-47), not an Execution Skill: it orchestrates
Runs in-session instead of performing one step. It is excluded from the
execution contract below and covered by the dedicated ``test_work_*`` tests.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest
import yaml

from skillflow.cli import build_parser
from skillflow.workflow_loader import load_workflow

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "skillflow"
SKILLS_DIR = PLUGIN_ROOT / "skills"

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

#: ``skillflow <subcommand>`` with a space: the CLI invocation form. Skill
#: cross-references always use the ``/skillflow:<name>`` form, which never
#: matches this pattern.
SUBCOMMAND_PATTERN = re.compile(r"skillflow ([a-z][a-z-]+)")

#: The only CLI operations an Execution Skill may invoke: validate its
#: assignment, complete its Run, or record its failure. Anything else
#: (``resolve-task``, ``prepare-artifacts``, ``decide``, ...) orchestrates
#: across Runs or belongs to another role.
ALLOWED_SUBCOMMANDS = frozenset({"assignment", "complete-run", "fail-run"})

#: Implementation markers that must never appear in skill prose. Conceptual
#: terms such as "lifecycle evaluation" or "Result" are fine -- reporting the
#: runtime's output is the skill's job; reimplementing it is not.
LOGIC_MARKERS = (
    "sqlite3",
    "INSERT",
    "UPDATE tasks",
    "ActionType",
    "EvaluationInput",
    "import skillflow",
)

#: The exact frontmatter surface SF-46 prescribes. A new key fails loudly here
#: so the addition is a deliberate, reviewed decision.
EXPECTED_FRONTMATTER_KEYS = frozenset(
    {
        "description",
        "context",
        "background",
        "user-invocable",
        "model",
        "effort",
        "allowed-tools",
    }
)

#: SF-46 requires skills of ≲60 lines; a little headroom keeps this a
#: change-detector rather than a tripwire.
MAX_SKILL_LINES = 64


def _skill_dirs() -> list[str]:
    """Return the sorted names of shipped skill directories."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


SKILL_DIRS = _skill_dirs()

#: Execution Skills only: ``work/`` is the SF-47 driver and satisfies none of
#: the per-step contract below (it has no workflow step to map to).
EXECUTION_SKILL_DIRS = [dirname for dirname in SKILL_DIRS if dirname != "work"]

#: The only CLI operations the driver (SF-47) may invoke: create a Task,
#: inspect the running Run, or resolve the next Run. Anything else
#: (``complete-run``, ``fail-run``, ``decide``, ...) belongs to an Execution
#: Skill or to a later issue (SF-50 owns ``decide``).
DRIVER_SUBCOMMANDS = frozenset({"start", "assignment", "resolve-task"})

#: The exact frontmatter surface SF-47 prescribes for the driver. A new key
#: fails loudly here so the addition is a deliberate, reviewed decision. The
#: driver inherits its model and runs in-session, so it carries none of the
#: Execution Skill keys (``context``, ``background``, ``model``, ``effort``).
WORK_FRONTMATTER_KEYS = frozenset(
    {
        "description",
        "user-invocable",
        "allowed-tools",
    }
)

#: The exact tool grant the driver needs: run the CLI, dispatch Execution
#: Skills, and ask the user which Task to continue. No step-work tools
#: (Read/Write/Edit) -- the driver never does step work.
WORK_ALLOWED_TOOLS = frozenset({"Bash(skillflow:*)", "Skill", "AskUserQuestion"})


def _parser_subcommands() -> set[str]:
    """Return the subcommands registered on the skillflow CLI parser."""
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("skillflow parser has no subcommands")


def _allowed_tools(frontmatter: dict) -> set[str]:
    """Normalize the driver's ``allowed-tools`` value into a tool set."""
    value = frontmatter.get("allowed-tools")
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    assert isinstance(value, list), (
        f"work: allowed-tools must be a string or a list, got {type(value).__name__}"
    )
    return set(value)


def _read_skill(dirname: str) -> tuple[dict, list[str], str]:
    """Return ``(frontmatter, body_lines, full_text)`` for a skill file."""
    text = (SKILLS_DIR / dirname / "SKILL.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---", f"{dirname}: frontmatter must start on line 1"
    assert "---" in lines[1:], f"{dirname}: unterminated frontmatter"
    end = lines.index("---", 1)
    frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    assert isinstance(frontmatter, dict), f"{dirname}: frontmatter is not a map"
    return frontmatter, lines[end + 1 :], text


def _step_for_skill(skill: str):
    """Return the reference-workflow step executed under ``skill``.

    The mapping is by skill name, not step id: a skill directory may differ
    from its step id (SF-48 precedent: step ``review`` is executed by skill
    ``skillflow:code-review``).
    """
    workflow = load_workflow(REFERENCE)
    for step in workflow.steps:
        if step.skill == skill:
            return step
    raise AssertionError(f"no reference-workflow step uses skill {skill!r}")


def _outcome_tokens(body: str) -> list[str]:
    """Expand every ``--outcome <tok>`` token, splitting ``<a|b>`` groups."""
    tokens: list[str] = []
    for raw in re.findall(r"--outcome\s+(\S+)", body):
        token = raw.strip("`'\"")
        if token.startswith("<") and token.endswith(">"):
            tokens.extend(part.strip("`'\"") for part in token[1:-1].split("|"))
        else:
            tokens.append(token)
    return tokens


def _artifact_submissions(body: str) -> list[tuple[str, str, str]]:
    """Split every ``--artifact NAME:TYPE:PATH`` token into its parts."""
    submissions: list[tuple[str, str, str]] = []
    for raw in re.findall(r"--artifact\s+(\S+)", body):
        parts = raw.strip("`'\"").split(":")
        assert len(parts) >= 3, f"malformed --artifact submission: {raw!r}"
        submissions.append((parts[0], parts[1], ":".join(parts[2:])))
    return submissions


def test_research_skill_shipped():
    assert (
        SKILLS_DIR / "research" / "SKILL.md"
    ).is_file(), "SF-46 ships plugins/skillflow/skills/research/SKILL.md"
    assert SKILL_DIRS, "no skill directories found under plugins/skillflow/skills/"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_skill_maps_to_workflow_step(dirname):
    step = _step_for_skill(f"skillflow:{dirname}")
    assert step.skill == f"skillflow:{dirname}"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_frontmatter_schema(dirname):
    frontmatter, _, _ = _read_skill(dirname)
    assert set(frontmatter) == EXPECTED_FRONTMATTER_KEYS, (
        f"{dirname}: unexpected frontmatter keys "
        f"{sorted(set(frontmatter) ^ EXPECTED_FRONTMATTER_KEYS)}"
    )
    assert frontmatter.get("context") == "fork", f"{dirname}: must run forked"
    assert (
        frontmatter.get("background") is False
    ), f"{dirname}: must not run in the background"
    assert (
        frontmatter.get("user-invocable") is False
    ), f"{dirname}: dispatched by the driver, not the user"
    assert (
        frontmatter.get("allowed-tools") == "Bash(skillflow:*)"
    ), f"{dirname}: unexpected tool grant {frontmatter.get('allowed-tools')!r}"
    step = _step_for_skill(f"skillflow:{dirname}")
    assert (
        frontmatter.get("model") == step.model
    ), f"{dirname}: model {frontmatter.get('model')!r} != step's {step.model!r}"
    assert (
        frontmatter.get("effort") == step.effort
    ), f"{dirname}: effort {frontmatter.get('effort')!r} != step's {step.effort!r}"
    assert frontmatter.get("description"), f"{dirname}: empty description"
    assert (
        "/skillflow:work" in frontmatter["description"]
    ), f"{dirname}: description must name its dispatcher"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_assignment_render_line_is_first_body_line(dirname):
    _, body_lines, _ = _read_skill(dirname)
    assert body_lines, f"{dirname}: empty skill body"
    assert body_lines[0] == f"!`skillflow assignment --skill skillflow:{dirname}`", (
        f"{dirname}: first body line must validate the assignment before "
        f"the model sees the body, found {body_lines[0]!r}"
    )


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_outcomes_subset_of_step(dirname):
    _, _, text = _read_skill(dirname)
    step = _step_for_skill(f"skillflow:{dirname}")
    tokens = _outcome_tokens(text)
    assert tokens, f"{dirname}: skill completes its Run with no outcome"
    unknown = [token for token in tokens if token not in step.outcomes]
    assert (
        not unknown
    ), f"{dirname}: outcomes {unknown} are not declared by step {step.id!r}"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_artifact_types_match_step_outputs(dirname):
    _, _, text = _read_skill(dirname)
    step = _step_for_skill(f"skillflow:{dirname}")
    submissions = _artifact_submissions(text)
    declared = {output.type for output in step.outputs}
    unknown = [type_ for _, type_, _ in submissions if type_ not in declared]
    assert (
        not unknown
    ), f"{dirname}: artifact types {unknown} are not declared by step {step.id!r}"
    required = {output.type for output in step.outputs if output.required}
    submitted = {type_ for _, type_, _ in submissions}
    missing = required - submitted
    assert (
        not missing
    ), f"{dirname}: required step outputs {sorted(missing)} are never submitted"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_no_verbose_command_or_foreign_subcommand(dirname):
    _, body_lines, text = _read_skill(dirname)
    body = "\n".join(body_lines)
    assert (
        "/skillflow:" not in body
    ), f"{dirname}: body must not invoke the verbose /skillflow:* commands"
    found = SUBCOMMAND_PATTERN.findall(text)
    foreign = [sub for sub in found if sub not in ALLOWED_SUBCOMMANDS]
    assert not foreign, (
        f"{dirname}: skill must only validate/complete/fail its Run, "
        f"found `skillflow {foreign}`"
    )


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_no_lifecycle_logic_markers(dirname):
    _, _, text = _read_skill(dirname)
    present = [marker for marker in LOGIC_MARKERS if marker in text]
    assert not present, f"{dirname}: business-logic markers {present}"


@pytest.mark.parametrize("dirname", EXECUTION_SKILL_DIRS)
def test_skill_is_brief(dirname):
    _, _, text = _read_skill(dirname)
    assert (
        len(text.splitlines()) <= MAX_SKILL_LINES
    ), f"{dirname}: skill exceeds {MAX_SKILL_LINES} lines"


def test_research_pins():
    _, _, text = _read_skill("research")
    submissions = _artifact_submissions(text)
    assert ("research.md", "research") in [
        (name, type_) for name, type_, _ in submissions
    ], "research skill must submit fixed artifact research.md of type research"
    assert any(
        path.startswith(".skillflow/runs/") for _, _, path in submissions if path
    ), "research artifact must be written under .skillflow/runs/<run-id>/"
    assert {"ready", "replan"} <= set(
        _outcome_tokens(text)
    ), "research skill must support both ready and replan outcomes"
    assert (
        "skillflow fail-run --message" in text
    ), "research skill must fail its Run explicitly when it cannot complete"
    assert "FAILED:" in text, "research skill must report failure as `FAILED: <why>`"
    assert "5 lines" in text, "research skill must bound its reply length"


def test_work_skill_shipped():
    assert (
        SKILLS_DIR / "work" / "SKILL.md"
    ).is_file(), "SF-47 ships plugins/skillflow/skills/work/SKILL.md"


def test_work_frontmatter_schema():
    frontmatter, _, _ = _read_skill("work")
    assert set(frontmatter) == WORK_FRONTMATTER_KEYS, (
        "work: unexpected frontmatter keys "
        f"{sorted(set(frontmatter) ^ WORK_FRONTMATTER_KEYS)}"
    )
    assert frontmatter.get("user-invocable") is True, (
        "work: the driver is user-invocable as /skillflow:work"
    )
    assert _allowed_tools(frontmatter) == WORK_ALLOWED_TOOLS, (
        f"work: unexpected tool grant {frontmatter.get('allowed-tools')!r}"
    )
    assert frontmatter.get("description"), "work: empty description"


def test_work_references_only_existing_driver_subcommands():
    _, _, text = _read_skill("work")
    found = SUBCOMMAND_PATTERN.findall(text)
    assert found, "work: driver references no CLI subcommand"
    subcommands = _parser_subcommands()
    unknown = [sub for sub in found if sub not in subcommands]
    assert not unknown, f"work: references unknown subcommands {unknown}"
    foreign = [sub for sub in found if sub not in DRIVER_SUBCOMMANDS]
    assert not foreign, (
        "work: driver must only start, inspect, or resolve Runs, "
        f"found `skillflow {foreign}`"
    )
    assert set(found) == DRIVER_SUBCOMMANDS, (
        f"work: driver must use all of {sorted(DRIVER_SUBCOMMANDS)}, "
        f"found {sorted(set(found))}"
    )


def test_work_has_no_hardcoded_skill_names():
    _, _, text = _read_skill("work")
    workflow = load_workflow(REFERENCE)
    hardcoded = [step.skill for step in workflow.steps if step.skill in text]
    assert not hardcoded, (
        f"work: driver must parse the skill from CLI output, not name it; "
        f"found {hardcoded}"
    )


def test_work_branches_on_envelope_codes():
    _, _, text = _read_skill("work")
    for code in (
        "TaskAlreadyCompleted",
        "TaskCancelled",
        "AmbiguousCurrentTask",
        "HumanDecisionRequired",
    ):
        assert code in text, f"work: driver has no branch for {code}"
    assert "AskUserQuestion" in text, (
        "work: driver must ask the user which Task to continue on ambiguity"
    )


def test_work_after_skill_checks():
    _, _, text = _read_skill("work")
    assert "FAILED" in text, (
        "work: a FAILED skill reply must stop the loop with no retry"
    )
    assert "did not complete" in text, (
        "work: a skill that leaves its Run running must be reported and stop"
    )
    assert "remember the Run id" in text, (
        "work: the dispatched Run id must be remembered for the same-Run check"
    )


def test_work_uses_cli_form_only():
    _, _, text = _read_skill("work")
    assert "/skillflow:" not in text, (
        "work: body must use the `skillflow <subcommand>` CLI form, never the "
        "verbose /skillflow:* commands (they do in-session step work)"
    )


def test_work_no_lifecycle_logic_markers():
    _, _, text = _read_skill("work")
    present = [marker for marker in LOGIC_MARKERS if marker in text]
    assert not present, f"work: business-logic markers {present}"


def test_work_never_rules():
    _, _, text = _read_skill("work")
    assert ".skillflow/" in text, "work: driver must never edit `.skillflow/`"
    assert "YAML" in text, "work: driver must never read workflow YAML"
    assert "outcome" in text, "work: driver must never choose outcomes"
