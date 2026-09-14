"""Tests for the reference ``software-change`` Workflow Definition (SF-8, SF-42).

The deliverable of SF-8 is a real definition file, ``workflows/software-change.yaml``,
not a Python module. These tests load that file through the public
``skillflow.workflow_loader.load_workflow`` API -- exactly as a caller would --
and assert:

* its full shape (name, step order, execution parameters, expected outputs);
* each of the four SF-A-4 §14 acceptance scenarios (happy path, review/rework,
  fundamental-assumption -> the research step, human decision);
* the exact outcome/decision keys per step, that every ``run`` rule targets a
  step (SF-42: no skill-targeted Runs in the product workflow), and that no
  excluded lifecycle concept (Router / Transition / Loop / Iteration /
  Rework / Handoff / Instance / Stage) leaks into a step id, skill name,
  outcome key, or decision key.

Tests 2-4 and 9 are change-detectors: if the reference procedure is edited
later, they fail loudly rather than silently accepting the change.
"""

from pathlib import Path

from skillflow.workflow import ActionType, Workflow
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

CHAIN = ["research", "decomposition", "implementation", "review"]


def test_reference_workflow_loads_and_validates():
    workflow = load_workflow(REFERENCE)
    assert isinstance(workflow, Workflow)


def test_name_and_step_order():
    workflow = load_workflow(REFERENCE)
    assert workflow.name == "software-change"
    assert [step.id for step in workflow.steps] == CHAIN
    assert workflow.initial_step.id == "research"


def test_execution_parameters():
    workflow = load_workflow(REFERENCE)
    params = {step.id: (step.skill, step.model, step.effort) for step in workflow.steps}
    assert params == {
        "research": ("skillflow:research", "opus", "high"),
        "decomposition": ("skillflow:decomposition", "opus", "high"),
        "implementation": ("skillflow:implementation", "sonnet", "high"),
        "review": ("skillflow:code-review", "opus", "high"),
    }


def test_expected_outputs():
    workflow = load_workflow(REFERENCE)
    outputs = {
        step.id: tuple((o.type, o.required) for o in step.outputs)
        for step in workflow.steps
    }
    assert outputs == {
        "research": (("research", True),),
        "decomposition": (("plan", True),),
        "implementation": (),
        "review": (("review", True),),
    }


def test_context_declarations():
    workflow = load_workflow(REFERENCE)
    context = {step.id: step.context for step in workflow.steps}
    assert context == {
        "research": ("review", "plan", "research"),
        "decomposition": ("research", "review"),
        "implementation": ("plan", "review"),
        "review": ("research", "plan"),
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


def test_scenario_c_research_is_a_step():
    # SF-42: a failed assumption returns the Task to the research step (the
    # initial step), not to a skill-targeted Run.
    workflow = load_workflow(REFERENCE)
    rule = workflow.find_step("review").outcomes["fundamental_assumption_wrong"]
    assert rule.action is ActionType.RUN
    assert (rule.step, rule.skill) == ("research", None)
    assert workflow.find_step("research") is workflow.initial_step


def test_scenario_c_replan_returns_to_decomposition():
    # `replan` is an ordinary outcome key on the research step; it no longer
    # lives on review.
    workflow = load_workflow(REFERENCE)
    rule = workflow.find_step("research").outcomes["replan"]
    assert rule.action is ActionType.RUN
    assert (rule.step, rule.skill) == ("decomposition", None)
    assert "replan" not in workflow.find_step("review").outcomes


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


def test_outcome_and_decision_keys():
    workflow = load_workflow(REFERENCE)
    assert {step.id: set(step.outcomes) for step in workflow.steps} == {
        "research": {"ready", "replan"},
        "decomposition": {"ready"},
        "implementation": {"ready"},
        "review": {
            "approved",
            "changes_requested",
            "fundamental_assumption_wrong",
            "human_required",
        },
    }
    for step in workflow.steps:
        if step.id != "review":
            assert step.decisions == {}


def test_every_run_rule_targets_a_step():
    workflow = load_workflow(REFERENCE)
    for step in workflow.steps:
        for rule in (*step.outcomes.values(), *step.decisions.values()):
            if rule.action is ActionType.RUN:
                assert rule.step is not None, step.id
                assert rule.skill is None, step.id


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
