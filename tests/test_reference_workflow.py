"""Tests for the reference ``software-change`` Workflow Definition (SF-8).

The deliverable of SF-8 is a real definition file, ``workflows/software-change.yaml``,
not a Python module. These tests load that file through the public
``skillflow.workflow_loader.load_workflow`` API -- exactly as a caller would --
and assert:

* its full shape (name, step order, execution parameters, expected outputs);
* each of the four SF-A-4 §14 acceptance scenarios (happy path, review/rework,
  fundamental-assumption -> research, human decision);
* that only the ``review`` step branches, and that no excluded lifecycle
  concept (Router / Transition / Loop / Iteration / Rework / Handoff /
  Instance / Stage) leaks into a step id, skill name, outcome key, or
  decision key.

Tests 2-4 and 9 are change-detectors: if the reference procedure is edited
later, they fail loudly rather than silently accepting the change.
"""

from pathlib import Path

from skillflow.workflow import ActionType, Workflow
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

CHAIN = ["requirements", "decomposition", "implementation", "review"]


def test_reference_workflow_loads_and_validates():
    workflow = load_workflow(REFERENCE)
    assert isinstance(workflow, Workflow)


def test_name_and_step_order():
    workflow = load_workflow(REFERENCE)
    assert workflow.name == "software-change"
    assert [step.id for step in workflow.steps] == CHAIN
    assert workflow.initial_step.id == "requirements"


def test_execution_parameters():
    workflow = load_workflow(REFERENCE)
    params = {step.id: (step.skill, step.model, step.effort) for step in workflow.steps}
    assert params == {
        "requirements": ("requirements-analysis", "opus", "high"),
        "decomposition": ("decomposition", "opus", "high"),
        "implementation": ("implementation", "sonnet", "high"),
        "review": ("code-review", "opus", "high"),
    }


def test_expected_outputs():
    workflow = load_workflow(REFERENCE)
    outputs = {
        step.id: tuple((o.type, o.required) for o in step.outputs)
        for step in workflow.steps
    }
    assert outputs == {
        "requirements": (("requirements", True),),
        "decomposition": (("plan", True),),
        "implementation": (),
        "review": (("review", True),),
    }


def test_scenario_a_happy_path():
    workflow = load_workflow(REFERENCE)
    for current, nxt in zip(CHAIN[:-1], CHAIN[1:], strict=True):
        rule = workflow.find_step(current).outcomes["ready"]
        assert rule.action is ActionType.RUN
        assert rule.step == nxt
    approved = workflow.find_step("review").outcomes["approved"]
    assert approved.action is ActionType.COMPLETE


def test_scenario_b_review_rework():
    workflow = load_workflow(REFERENCE)
    rule = workflow.find_step("review").outcomes["changes_requested"]
    assert rule.action is ActionType.RUN
    assert rule.step == "implementation"


def test_scenario_c_research_is_a_skill_not_a_step():
    workflow = load_workflow(REFERENCE)
    rule = workflow.find_step("review").outcomes["fundamental_assumption_wrong"]
    assert rule.action is ActionType.RUN
    assert (rule.skill, rule.step) == ("research", None)
    assert workflow.find_step("research") is None


def test_scenario_d_human_decision():
    workflow = load_workflow(REFERENCE)
    review = workflow.find_step("review")
    assert review.outcomes["human_required"].action is ActionType.HUMAN

    decisions = {key: rule.action for key, rule in review.decisions.items()}
    assert decisions == {
        "approve": ActionType.COMPLETE,
        "request_changes": ActionType.RUN,
        "cancel": ActionType.CANCEL,
    }
    assert review.decisions["request_changes"].step == "implementation"
    assert all(
        rule.action is not ActionType.HUMAN for rule in review.decisions.values()
    )


def test_only_the_review_step_branches():
    workflow = load_workflow(REFERENCE)
    for step in workflow.steps:
        if step.id == "review":
            continue
        assert set(step.outcomes) == {"ready"}
        assert step.decisions == {}


def test_no_excluded_lifecycle_concepts():
    workflow = load_workflow(REFERENCE)
    banned = (
        "router",
        "transition",
        "loop",
        "iteration",
        "rework",
        "handoff",
        "instance",
        "stage",
    )
    names: list[str] = []
    for step in workflow.steps:
        names.append(step.id)
        names.append(step.skill)
        names.extend(step.outcomes)
        names.extend(step.decisions)
        for rule in (*step.outcomes.values(), *step.decisions.values()):
            if rule.skill is not None:
                names.append(rule.skill)
    for name in names:
        assert not any(frag in name.lower() for frag in banned), name
