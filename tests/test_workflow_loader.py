"""Tests for ``skillflow.workflow_loader``.

Two kinds of test, following ``test_workflow.py``:

* **Change-detectors** for the loader contract -- exact ``__all__``, an import
  boundary (stdlib + ``yaml`` + ``skillflow.workflow`` only, so "the loader
  neither persists nor touches the workspace" stays mechanically checkable), a
  banned-fragment scan, and a check that the invalid-fixture directory and the
  expected-error table below stay in sync.
* **Behaviour tests** -- the valid fixtures load into the exact expected
  objects (including the four SF-A-4 §14 acceptance scenarios), and every
  invalid fixture is rejected with a message that names the cause.

Fixtures are real files on disk, loaded through the public ``load_workflow``
API, per the SF-7 Definition of Done ("Valid and invalid fixtures are tested").
"""

import ast
import sys
from pathlib import Path

import pytest

from skillflow import workflow_loader
from skillflow.workflow import (
    ActionType,
    ExpectedOutput,
    OutcomeRule,
    Workflow,
    WorkflowStep,
)
from skillflow.workflow_loader import (
    WorkflowLoadError,
    load_workflow,
    parse_workflow,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"
VALID = FIXTURES / "valid"
INVALID = FIXTURES / "invalid"


# --- change-detectors -----------------------------------------------------


def test_all_matches_the_public_surface():
    assert set(workflow_loader.__all__) == {
        "WorkflowLoadError",
        "load_workflow",
        "parse_workflow",
    }


def test_module_imports_only_stdlib_yaml_and_the_schema():
    """The loader must not reach into persistence or the workspace.

    Mirrors ``test_workflow.test_module_imports_stdlib_only``: the boundary is
    an architectural claim, so it is checked mechanically rather than trusted.
    """
    tree = ast.parse(Path(workflow_loader.__file__).read_text())
    modules: set[str] = set()
    skillflow_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
            if node.module.startswith("skillflow"):
                skillflow_modules.add(node.module)

    allowed = set(sys.stdlib_module_names) | {"yaml", "skillflow"}
    assert modules <= allowed, f"unexpected imports: {modules - allowed}"
    assert skillflow_modules == {"skillflow.workflow"}


def test_no_excluded_workflow_concepts_present():
    banned = (
        "transition",
        "router",
        "loop",
        "iteration",
        "rework",
        "handoff",
        "instance",
        "stage",
    )
    offenders = [
        name
        for name in vars(workflow_loader)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


# --- valid fixtures -------------------------------------------------------

EXPECTED_SOFTWARE_CHANGE = Workflow(
    name="software-change",
    steps=(
        WorkflowStep(
            id="requirements",
            skill="requirements-analysis",
            model="opus",
            effort="high",
            outputs=(ExpectedOutput(type="requirements", required=True),),
            outcomes={
                "ready": OutcomeRule(action=ActionType.RUN, step="decomposition")
            },
        ),
        WorkflowStep(
            id="decomposition",
            skill="decomposition",
            model="opus",
            effort="high",
            outputs=(ExpectedOutput(type="plan", required=True),),
            outcomes={
                "ready": OutcomeRule(action=ActionType.RUN, step="implementation")
            },
        ),
        WorkflowStep(
            id="implementation",
            skill="implementation",
            model="sonnet",
            effort="high",
            outcomes={"ready": OutcomeRule(action=ActionType.RUN, step="review")},
        ),
        WorkflowStep(
            id="review",
            skill="code-review",
            model="opus",
            effort="high",
            outputs=(ExpectedOutput(type="review", required=True),),
            outcomes={
                "approved": OutcomeRule(action=ActionType.COMPLETE),
                "changes_requested": OutcomeRule(
                    action=ActionType.RUN, step="implementation"
                ),
                "fundamental_assumption_wrong": OutcomeRule(
                    action=ActionType.RUN, skill="research"
                ),
                "human_required": OutcomeRule(action=ActionType.HUMAN),
            },
            decisions={
                "approve": OutcomeRule(action=ActionType.COMPLETE),
                "request_changes": OutcomeRule(
                    action=ActionType.RUN, step="implementation"
                ),
                "cancel": OutcomeRule(action=ActionType.CANCEL),
            },
        ),
    ),
)


def test_valid_definition_loads_to_the_expected_workflow():
    assert load_workflow(VALID / "software-change.yaml") == EXPECTED_SOFTWARE_CHANGE


def test_initial_step_is_the_first_declared_step():
    loaded = load_workflow(VALID / "software-change.yaml")
    assert loaded.initial_step.id == "requirements"
    assert loaded.initial_step.skill == "requirements-analysis"


# The four SF-A-4 §14 acceptance scenarios, asserted through the loaded object.


def test_scenario_a_happy_path():
    loaded = load_workflow(VALID / "software-change.yaml")
    chain = ["requirements", "decomposition", "implementation", "review"]
    for current, following in zip(chain, chain[1:], strict=False):
        rule = loaded.find_step(current).outcomes["ready"]
        assert rule.action is ActionType.RUN
        assert rule.step == following
    assert loaded.find_step("review").outcomes["approved"].action is ActionType.COMPLETE


def test_scenario_b_review_rework_targets_an_earlier_step():
    rule = (
        load_workflow(VALID / "software-change.yaml")
        .find_step("review")
        .outcomes["changes_requested"]
    )
    assert rule.action is ActionType.RUN
    assert rule.step == "implementation"


def test_scenario_c_skill_target_need_not_be_a_step():
    """SF-A-4 §9: ``skill: research`` works with no ``research`` step."""
    loaded = load_workflow(VALID / "software-change.yaml")
    rule = loaded.find_step("review").outcomes["fundamental_assumption_wrong"]
    assert rule.action is ActionType.RUN
    assert (rule.skill, rule.step) == ("research", None)
    assert loaded.find_step("research") is None


def test_scenario_d_human_decision():
    review = load_workflow(VALID / "software-change.yaml").find_step("review")
    assert review.outcomes["human_required"].action is ActionType.HUMAN
    assert {key: rule.action for key, rule in review.decisions.items()} == {
        "approve": ActionType.COMPLETE,
        "request_changes": ActionType.RUN,
        "cancel": ActionType.CANCEL,
    }


def test_minimal_definition_uses_schema_defaults():
    loaded = load_workflow(VALID / "minimal.yaml")
    step = loaded.initial_step
    assert loaded == Workflow(
        name="minimal", steps=(WorkflowStep(id="only", skill="implementation"),)
    )
    assert (step.model, step.effort) == (None, None)
    assert step.outputs == ()
    assert dict(step.outcomes) == {}
    assert dict(step.decisions) == {}


def test_required_defaults_to_true_and_false_is_honoured():
    assert (
        load_workflow(VALID / "software-change.yaml").initial_step.outputs[0].required
        is True
    )
    assert (
        load_workflow(VALID / "whitespace-identifiers.yaml")
        .initial_step.outputs[0]
        .required
        is False
    )


def test_identifiers_are_stripped_through_the_loader():
    loaded = load_workflow(VALID / "whitespace-identifiers.yaml")
    step = loaded.initial_step
    assert loaded.name == "padded-workflow"
    assert (step.id, step.skill, step.model, step.effort) == (
        "only",
        "implementation",
        "opus",
        "high",
    )
    assert step.outputs[0].type == "review"


@pytest.mark.parametrize("fixture", sorted(p.name for p in VALID.glob("*.yaml")))
def test_parse_workflow_matches_load_workflow(fixture):
    path = VALID / fixture
    assert parse_workflow(path.read_text(encoding="utf-8")) == load_workflow(path)


def test_explicit_empty_collections_are_accepted():
    """The escape hatch the error messages recommend must actually work."""
    step = load_workflow(VALID / "explicit-empty.yaml").initial_step
    assert step.outputs == ()
    assert dict(step.outcomes) == {}
    assert dict(step.decisions) == {}


def test_aliases_are_allowed_even_though_merge_keys_are_not():
    """Anchors/aliases stay permitted; only `<<` inheritance is rejected."""
    loaded = parse_workflow(
        "name: w\n"
        "steps:\n"
        "  - id: a\n"
        "    skill: &shared implementation\n"
        "  - id: b\n"
        "    skill: *shared\n"
    )
    assert [step.skill for step in loaded.steps] == ["implementation", "implementation"]


def test_context_list_parses_into_the_step():
    loaded = parse_workflow(
        "name: w\n"
        "steps:\n"
        "  - id: a\n"
        "    skill: implementation\n"
        "  - id: b\n"
        "    skill: implementation\n"
        "    context: [requirements, plan]\n"
    )
    assert loaded.find_step("a").context == ()
    assert loaded.find_step("b").context == ("requirements", "plan")


def test_explicit_empty_context_is_accepted():
    loaded = parse_workflow(
        "name: w\nsteps:\n  - id: a\n    skill: implementation\n    context: []\n"
    )
    assert loaded.initial_step.context == ()


def test_parse_workflow_default_source_appears_in_errors():
    with pytest.raises(WorkflowLoadError, match=r"^<string>: "):
        parse_workflow("name: w\n")


def test_parse_workflow_custom_source_appears_in_errors():
    with pytest.raises(WorkflowLoadError, match=r"^inline\.yaml: "):
        parse_workflow("name: w\n", source="inline.yaml")


# --- invalid fixtures -----------------------------------------------------

# fixture file -> (exact YAML location, fragment identifying *why* it is
# rejected). Asserting the cause and the precise location, not merely that
# something raised -- a message that names the wrong place is a defect too.
INVALID_FIXTURES = {
    "bad-syntax.yaml": ("workflow", "invalid YAML"),
    "empty.yaml": ("workflow", "expected a mapping, got nothing"),
    "not-a-mapping.yaml": ("workflow", "expected a mapping, got list"),
    "multi-document.yaml": ("workflow", "invalid YAML"),
    "unknown-top-level-key.yaml": ("workflow", "unknown key(s) 'version'"),
    "missing-name.yaml": ("workflow", "missing required key 'name'"),
    "missing-steps.yaml": ("workflow", "missing required key 'steps'"),
    "null-steps.yaml": ("workflow.steps", "expected a list, got nothing"),
    "empty-steps.yaml": ("workflow", "Workflow.steps must not be empty"),
    "steps-not-a-list.yaml": ("workflow.steps", "expected a list, got dict"),
    "step-not-a-mapping.yaml": ("steps[0]", "expected a mapping, got str"),
    "step-missing-id.yaml": ("steps[0]", "missing required key 'id'"),
    "step-missing-skill.yaml": ("steps[0]", "missing required key 'skill'"),
    "step-unknown-key.yaml": ("steps[0]", "unknown key(s) 'outcome'"),
    "non-string-step-id.yaml": ("steps[0]", "WorkflowStep.id must be a non-empty"),
    "blank-step-id.yaml": ("steps[0]", "WorkflowStep.id must be a non-empty"),
    "duplicate-step-id.yaml": ("workflow", "duplicate step id 'a'"),
    "merge-key.yaml": ("workflow", "merge keys ('<<') are not supported"),
    "duplicate-yaml-key.yaml": ("workflow", "duplicate key 'skill'"),
    "duplicate-key-deep.yaml": ("workflow", "duplicate key 'approved'"),
    "duplicate-outcome-key.yaml": ("workflow", "duplicate key 'ready'"),
    "duplicate-outcome-key-after-strip.yaml": (
        "steps[0]",
        "WorkflowStep.outcomes has a duplicate key 'ready'",
    ),
    "unknown-step-reference.yaml": ("workflow", "references unknown step 'nowhere'"),
    "unknown-decision-step-reference.yaml": (
        "workflow",
        "references unknown step 'nowhere'",
    ),
    "rule-not-a-mapping.yaml": (
        "steps[0].outcomes['approved']",
        "the shorthand 'complete' is not supported",
    ),
    "rule-missing-action.yaml": (
        "steps[0].outcomes['approved']",
        "missing required key 'action'",
    ),
    "rule-unknown-action.yaml": (
        "steps[0].outcomes['approved']",
        "not a valid ActionType",
    ),
    "rule-unknown-key.yaml": (
        "steps[0].outcomes['approved']",
        "unknown key(s) 'target'",
    ),
    "rule-run-without-target.yaml": (
        "steps[0].outcomes['ready']",
        "exactly one of 'step' or 'skill'",
    ),
    "rule-run-with-both-targets.yaml": (
        "steps[0].outcomes['ready']",
        "exactly one of 'step' or 'skill'",
    ),
    "rule-complete-with-step.yaml": (
        "steps[0].outcomes['approved']",
        "must not carry a 'step' or 'skill'",
    ),
    "decision-maps-to-human.yaml": ("steps[0]", "must not map to action 'human'"),
    "human-outcome-without-decisions.yaml": ("steps[0]", "declares no 'decisions'"),
    "null-outcomes.yaml": (
        "steps[0].outcomes",
        "expected a mapping, got nothing (write '{}'",
    ),
    "null-model.yaml": ("steps[0].model", "expected a value, got nothing"),
    "null-effort.yaml": ("steps[0].effort", "expected a value, got nothing"),
    "outputs-not-a-list.yaml": ("steps[0].outputs", "expected a list, got dict"),
    "null-context.yaml": ("steps[0].context", "expected a list, got nothing"),
    "context-not-a-list.yaml": ("steps[0].context", "expected a list, got dict"),
    "context-non-string.yaml": (
        "steps[0]",
        "WorkflowStep.context[0] must be a non-empty string",
    ),
    "duplicate-context-type.yaml": (
        "steps[0]",
        "WorkflowStep.context has a duplicate type 'plan'",
    ),
    "output-not-a-mapping.yaml": (
        "steps[0].outputs[0]",
        "expected a mapping, got str",
    ),
    "output-unknown-key.yaml": ("steps[0].outputs[0]", "unknown key(s) 'name'"),
    "output-missing-type.yaml": (
        "steps[0].outputs[0]",
        "missing required key 'type'",
    ),
    "output-duplicate-type.yaml": ("steps[0]", "duplicate type 'review'"),
    "output-non-bool-required.yaml": (
        "steps[0].outputs[0]",
        "ExpectedOutput.required must be a bool",
    ),
}


def test_every_invalid_fixture_on_disk_is_covered():
    """Adding a fixture without an expected error is a failure, and vice versa."""
    assert {p.name for p in INVALID.glob("*.yaml")} == set(INVALID_FIXTURES)


@pytest.mark.parametrize("fixture", sorted(INVALID_FIXTURES))
def test_invalid_fixture_is_rejected(fixture):
    with pytest.raises(WorkflowLoadError) as excinfo:
        load_workflow(INVALID / fixture)
    message = str(excinfo.value)
    assert INVALID_FIXTURES[fixture][1] in message
    assert fixture in message, "the message must name the source file"


@pytest.mark.parametrize("fixture", sorted(INVALID_FIXTURES))
def test_invalid_fixture_error_locates_the_fault(fixture):
    """Every message names the *exact* YAML location of the fault.

    Asserting the precise location, not merely that some location is present:
    a message that points at the enclosing mapping instead of the offending
    key is a diagnostic defect, and a loose check cannot see it.
    """
    path = INVALID / fixture
    with pytest.raises(WorkflowLoadError) as excinfo:
        load_workflow(path)
    expected_where = INVALID_FIXTURES[fixture][0]
    assert str(excinfo.value).startswith(f"{path}: {expected_where}: ")


@pytest.mark.parametrize(
    "fixture,line",
    [
        # The duplicate sits 4 lines below the mapping that contains it, so a
        # mark taken from the enclosing node would report the wrong line.
        ("duplicate-key-deep.yaml", 9),
        ("duplicate-yaml-key.yaml", 5),
        ("merge-key.yaml", 7),
    ],
)
def test_parse_time_error_names_the_offending_line(fixture, line):
    with pytest.raises(WorkflowLoadError, match=rf", line {line}, column "):
        load_workflow(INVALID / fixture)


# --- IO failures ----------------------------------------------------------


def test_missing_file_raises_workflow_load_error(tmp_path):
    missing = tmp_path / "absent.yaml"
    with pytest.raises(WorkflowLoadError, match="cannot read file") as excinfo:
        load_workflow(missing)
    assert str(missing) in str(excinfo.value)


def test_directory_raises_workflow_load_error(tmp_path):
    with pytest.raises(WorkflowLoadError, match="cannot read file"):
        load_workflow(tmp_path)


def test_non_utf8_file_raises_workflow_load_error(tmp_path):
    """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``."""
    path = tmp_path / "latin1.yaml"
    path.write_bytes(b"name: caf\xe9\nsteps: []\n")
    with pytest.raises(WorkflowLoadError, match="not valid UTF-8"):
        load_workflow(path)


def test_accepts_a_string_path():
    assert load_workflow(str(VALID / "minimal.yaml")).name == "minimal"


def test_load_error_is_not_a_value_error():
    """Callers catching ``WorkflowLoadError`` must not swallow bugs."""
    assert not issubclass(WorkflowLoadError, ValueError)
