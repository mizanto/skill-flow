"""Tests for ``skillflow.workflow``.

Two kinds of test, following ``test_domain.py``:

* **Change-detectors** for the schema contract -- exact dataclass set, exact
  field sets, ``ActionType`` membership, a banned-fragment scan, and a
  stdlib-only import boundary -- so drift toward a generic workflow DSL fails
  loudly.
* **Validation / structural tests** -- one per rule enforced at construction,
  plus the four acceptance scenarios expressed as a test-local fixture.
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from skillflow import workflow
from skillflow.workflow import (
    ActionType,
    ExpectedOutput,
    OutcomeRule,
    Workflow,
    WorkflowStep,
)

# --- schema change-detectors ----------------------------------------------

V0_SCHEMA = {"ExpectedOutput", "OutcomeRule", "WorkflowStep", "Workflow"}


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(workflow).items()
        if dataclasses.is_dataclass(value) and not name.startswith("_")
    }
    assert defined == V0_SCHEMA


def test_all_matches_the_public_surface():
    assert set(workflow.__all__) == V0_SCHEMA | {"ActionType"}


def test_action_type_values_match_spec():
    assert {a.value for a in ActionType} == {"run", "human", "complete", "cancel"}


V0_FIELDS = {
    ExpectedOutput: {"type", "required"},
    OutcomeRule: {"action", "step", "skill"},
    WorkflowStep: {
        "id",
        "skill",
        "model",
        "effort",
        "outputs",
        "context",
        "outcomes",
        "decisions",
    },
    Workflow: {"name", "steps"},
}


@pytest.mark.parametrize(
    "entity,expected",
    list(V0_FIELDS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "",
)
def test_entity_fields_match_spec(entity, expected):
    assert {f.name for f in dataclasses.fields(entity)} == expected


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
        for name in vars(workflow)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_module_imports_stdlib_only():
    source = Path(workflow.__file__).read_text()
    tree = ast.parse(source)
    allowed = {"dataclasses", "enum", "types", "typing", "collections"}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= allowed, f"unexpected imports: {modules - allowed}"


# --- structural properties ----------------------------------------------


def test_dataclasses_are_frozen_and_slotted():
    step = WorkflowStep(id="s1", skill="sk")
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.skill = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.typo = 1
    assert not hasattr(step, "__dict__")


def test_dataclasses_are_keyword_only():
    with pytest.raises(TypeError, match="positional"):
        ExpectedOutput("review")


def test_rule_mappings_are_read_only_snapshots():
    rule = OutcomeRule(action=ActionType.COMPLETE)
    source = {"approved": rule}
    step = WorkflowStep(id="s1", skill="sk", outcomes=source)
    assert step.outcomes["approved"] is rule
    with pytest.raises(TypeError):
        step.outcomes["x"] = rule
    source["approved"] = OutcomeRule(action=ActionType.CANCEL)
    assert step.outcomes["approved"].action is ActionType.COMPLETE


def test_outputs_is_a_tuple():
    step = WorkflowStep(id="s1", skill="sk", outputs=[ExpectedOutput(type="review")])
    assert isinstance(step.outputs, tuple)


def test_identifiers_are_stripped():
    step = WorkflowStep(id="  s1 ", skill="  sk ", model=" opus ", effort=" high ")
    assert (step.id, step.skill, step.model, step.effort) == (
        "s1",
        "sk",
        "opus",
        "high",
    )
    out = ExpectedOutput(type="  review  ")
    assert out.type == "review"
    wf = Workflow(name="  w  ", steps=[step])
    assert wf.name == "w"


# --- ExpectedOutput validation ----------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_expected_output_blank_type_rejected(blank):
    with pytest.raises(ValueError, match="ExpectedOutput.type"):
        ExpectedOutput(type=blank)


@pytest.mark.parametrize("bad", [1, 0, "true", None])
def test_expected_output_required_must_be_bool(bad):
    with pytest.raises(ValueError, match="ExpectedOutput.required must be a bool"):
        ExpectedOutput(type="review", required=bad)


def test_expected_output_defaults_to_required():
    assert ExpectedOutput(type="review").required is True


# --- OutcomeRule validation -----------------------------------------


def test_outcome_rule_unknown_action_rejected():
    with pytest.raises(ValueError, match="ActionType"):
        OutcomeRule(action="proceed")


def test_outcome_rule_bare_action_string_is_coerced():
    assert OutcomeRule(action="complete").action is ActionType.COMPLETE


def test_run_rule_with_step_ok():
    rule = OutcomeRule(action=ActionType.RUN, step="implementation")
    assert rule.step == "implementation"
    assert rule.skill is None


def test_run_rule_with_skill_ok():
    rule = OutcomeRule(action=ActionType.RUN, skill="research")
    assert rule.skill == "research"
    assert rule.step is None


def test_run_rule_with_neither_rejected():
    with pytest.raises(ValueError, match="exactly one of"):
        OutcomeRule(action=ActionType.RUN)


def test_run_rule_with_both_rejected():
    with pytest.raises(ValueError, match="exactly one of"):
        OutcomeRule(action=ActionType.RUN, step="impl", skill="research")


@pytest.mark.parametrize(
    "action", [ActionType.COMPLETE, ActionType.CANCEL, ActionType.HUMAN]
)
def test_non_run_rule_with_target_rejected(action):
    with pytest.raises(ValueError, match="must not carry"):
        OutcomeRule(action=action, step="implementation")


@pytest.mark.parametrize(
    "action", [ActionType.COMPLETE, ActionType.CANCEL, ActionType.HUMAN]
)
def test_non_run_rule_without_target_ok(action):
    assert OutcomeRule(action=action).step is None


def test_outcome_rule_blank_step_rejected():
    with pytest.raises(ValueError, match="OutcomeRule.step"):
        OutcomeRule(action=ActionType.RUN, step="   ")


# --- WorkflowStep validation ---------------------------------------


@pytest.mark.parametrize("field_name", ["id", "skill"])
def test_workflow_step_blank_required_field_rejected(field_name):
    kw = {"id": "s1", "skill": "sk", field_name: "  "}
    with pytest.raises(ValueError, match=f"WorkflowStep.{field_name}"):
        WorkflowStep(**kw)


@pytest.mark.parametrize("field_name", ["model", "effort"])
def test_workflow_step_blank_optional_field_rejected(field_name):
    with pytest.raises(ValueError, match=f"WorkflowStep.{field_name}"):
        WorkflowStep(id="s1", skill="sk", **{field_name: "  "})


def test_workflow_step_optional_params_default_none():
    step = WorkflowStep(id="s1", skill="sk")
    assert step.model is None and step.effort is None
    assert step.outputs == ()
    assert dict(step.outcomes) == {} and dict(step.decisions) == {}


def test_workflow_step_duplicate_output_type_rejected():
    with pytest.raises(ValueError, match="duplicate type"):
        WorkflowStep(
            id="s1",
            skill="sk",
            outputs=[ExpectedOutput(type="review"), ExpectedOutput(type="review")],
        )


def test_workflow_step_non_output_rejected():
    with pytest.raises(ValueError, match="must contain ExpectedOutput"):
        WorkflowStep(id="s1", skill="sk", outputs=["review"])


def test_workflow_step_outcomes_must_be_mapping():
    with pytest.raises(ValueError, match="WorkflowStep.outcomes must be a mapping"):
        WorkflowStep(id="s1", skill="sk", outcomes=[("approved", None)])


def test_workflow_step_outcome_value_must_be_rule():
    with pytest.raises(ValueError, match="must be an OutcomeRule"):
        WorkflowStep(id="s1", skill="sk", outcomes={"approved": "complete"})


def test_workflow_step_blank_outcome_key_rejected():
    with pytest.raises(ValueError, match="key"):
        WorkflowStep(
            id="s1",
            skill="sk",
            outcomes={"  ": OutcomeRule(action=ActionType.COMPLETE)},
        )


def test_workflow_step_decision_key_is_stripped():
    step = WorkflowStep(
        id="s1",
        skill="sk",
        decisions={" approve ": OutcomeRule(action=ActionType.COMPLETE)},
    )
    assert set(step.decisions) == {"approve"}


def test_workflow_step_decision_mapping_to_human_rejected():
    with pytest.raises(ValueError, match="must not map to action 'human'"):
        WorkflowStep(
            id="s1",
            skill="sk",
            decisions={"escalate": OutcomeRule(action=ActionType.HUMAN)},
        )


def test_workflow_step_human_outcome_with_decisions_is_allowed():
    step = WorkflowStep(
        id="review",
        skill="code-review",
        outcomes={"human_required": OutcomeRule(action=ActionType.HUMAN)},
        decisions={"approve": OutcomeRule(action=ActionType.COMPLETE)},
    )
    assert step.outcomes["human_required"].action is ActionType.HUMAN


def test_workflow_step_human_outcome_without_decisions_rejected():
    # SF-A-5 §7.5: 'decide' validates against the step's declared decisions.
    # A 'human' outcome with none declared strands the Task in
    # 'waiting_for_human' with no v0 command able to recover it.
    with pytest.raises(ValueError, match="maps to action 'human' but the step"):
        WorkflowStep(
            id="review",
            skill="code-review",
            outcomes={"human_required": OutcomeRule(action=ActionType.HUMAN)},
        )


def test_workflow_step_duplicate_outcome_key_after_stripping_rejected():
    with pytest.raises(ValueError, match="duplicate key 'approved'"):
        WorkflowStep(
            id="s1",
            skill="sk",
            outcomes={
                " approved ": OutcomeRule(action=ActionType.COMPLETE),
                "approved": OutcomeRule(action=ActionType.CANCEL),
            },
        )


@pytest.mark.parametrize("bad", [None, 42, "review"])
def test_workflow_step_non_iterable_outputs_rejected(bad):
    with pytest.raises(ValueError, match="WorkflowStep.outputs must be an iterable"):
        WorkflowStep(id="s1", skill="sk", outputs=bad)


# --- WorkflowStep.context validation --------------------------------------


def test_workflow_step_context_defaults_to_empty_tuple():
    assert WorkflowStep(id="s1", skill="sk").context == ()


def test_workflow_step_context_is_a_tuple_and_strips_items():
    step = WorkflowStep(id="s1", skill="sk", context=[" requirements ", "plan"])
    assert step.context == ("requirements", "plan")


@pytest.mark.parametrize("bad", [None, 42, "requirements"])
def test_workflow_step_non_iterable_context_rejected(bad):
    with pytest.raises(ValueError, match="WorkflowStep.context must be an iterable"):
        WorkflowStep(id="s1", skill="sk", context=bad)


@pytest.mark.parametrize("bad", [1, None, {"type": "x"}])
def test_workflow_step_non_string_context_item_rejected(bad):
    with pytest.raises(ValueError, match=r"WorkflowStep.context\[0\]"):
        WorkflowStep(id="s1", skill="sk", context=[bad])


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_workflow_step_blank_context_item_rejected(blank):
    with pytest.raises(ValueError, match=r"WorkflowStep.context\[0\]"):
        WorkflowStep(id="s1", skill="sk", context=[blank])


def test_workflow_step_duplicate_context_type_after_stripping_rejected():
    with pytest.raises(ValueError, match="duplicate type 'plan'"):
        WorkflowStep(id="s1", skill="sk", context=[" plan ", "plan"])


# --- Workflow validation -----------------------------------------


def test_workflow_blank_name_rejected():
    with pytest.raises(ValueError, match="Workflow.name"):
        Workflow(name="  ", steps=[WorkflowStep(id="s1", skill="sk")])


def test_workflow_empty_steps_rejected():
    with pytest.raises(ValueError, match="steps must not be empty"):
        Workflow(name="w", steps=[])


def test_workflow_non_step_rejected():
    with pytest.raises(ValueError, match="must contain WorkflowStep"):
        Workflow(name="w", steps=["requirements"])


@pytest.mark.parametrize("bad", [None, 42, "requirements"])
def test_workflow_non_iterable_steps_rejected(bad):
    with pytest.raises(ValueError, match="Workflow.steps must be an iterable"):
        Workflow(name="w", steps=bad)


def test_workflow_duplicate_step_id_rejected():
    with pytest.raises(ValueError, match="duplicate step id"):
        Workflow(
            name="w",
            steps=[
                WorkflowStep(id="s1", skill="a"),
                WorkflowStep(id="s1", skill="b"),
            ],
        )


def test_workflow_outcome_referencing_unknown_step_rejected():
    with pytest.raises(ValueError, match="references unknown step 'nope'"):
        Workflow(
            name="w",
            steps=[
                WorkflowStep(
                    id="review",
                    skill="code-review",
                    outcomes={
                        "changes_requested": OutcomeRule(
                            action=ActionType.RUN, step="nope"
                        )
                    },
                )
            ],
        )


def test_workflow_decision_referencing_unknown_step_rejected():
    with pytest.raises(ValueError, match="decisions\\['request_changes'\\]"):
        Workflow(
            name="w",
            steps=[
                WorkflowStep(
                    id="review",
                    skill="code-review",
                    decisions={
                        "request_changes": OutcomeRule(
                            action=ActionType.RUN, step="missing"
                        )
                    },
                )
            ],
        )


def test_workflow_outcome_referencing_unknown_skill_is_accepted():
    # SF-A-4 §9: run/skill targets are not validated against the step list.
    wf = Workflow(
        name="w",
        steps=[
            WorkflowStep(
                id="review",
                skill="code-review",
                outcomes={
                    "fundamental_assumption_wrong": OutcomeRule(
                        action=ActionType.RUN, skill="research"
                    )
                },
            )
        ],
    )
    assert wf.find_step("research") is None


def test_instances_are_unconditionally_unhashable():
    # The outcomes/decisions fields default to a MappingProxyType (not None, as
    # in skillflow.domain), so every WorkflowStep and Workflow is unhashable --
    # even with no mappings supplied. Identify steps by id, not by hash().
    with pytest.raises(TypeError):
        hash(WorkflowStep(id="s1", skill="sk"))
    with pytest.raises(TypeError):
        hash(Workflow(name="w", steps=[WorkflowStep(id="s1", skill="sk")]))


def test_initial_step_and_find_step():
    a = WorkflowStep(id="requirements", skill="requirements-analysis")
    b = WorkflowStep(id="review", skill="code-review")
    wf = Workflow(name="w", steps=[a, b])
    assert wf.initial_step is a
    assert wf.find_step("review") is b
    assert wf.find_step("absent") is None


# --- acceptance scenarios (SF-A-4 §14) -------------------------------


def _reference_workflow() -> Workflow:
    """The reference software-change workflow as a test-local fixture.

    This is NOT the SF-009 deliverable (a YAML file loaded via SF-008). It
    proves the four scenarios are expressible in the schema without a loader.
    """
    return Workflow(
        name="software-change",
        steps=[
            WorkflowStep(
                id="requirements",
                skill="requirements-analysis",
                model="opus",
                effort="high",
                outputs=[ExpectedOutput(type="requirements")],
                outcomes={
                    "ready": OutcomeRule(action=ActionType.RUN, step="decomposition")
                },
            ),
            WorkflowStep(
                id="decomposition",
                skill="decomposition",
                model="opus",
                effort="high",
                outputs=[ExpectedOutput(type="plan")],
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
                outputs=[ExpectedOutput(type="review")],
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
        ],
    )


def test_scenario_a_happy_path():
    wf = _reference_workflow()
    chain = ["requirements", "decomposition", "implementation"]
    for step_id, nxt in zip(chain, chain[1:] + ["review"], strict=True):
        rule = wf.find_step(step_id).outcomes["ready"]
        assert rule.action is ActionType.RUN and rule.step == nxt
    assert wf.find_step("review").outcomes["approved"].action is ActionType.COMPLETE


def test_scenario_b_review_rework():
    rule = _reference_workflow().find_step("review").outcomes["changes_requested"]
    assert rule.action is ActionType.RUN and rule.step == "implementation"


def test_scenario_c_fundamental_assumption_wrong():
    wf = _reference_workflow()
    rule = wf.find_step("review").outcomes["fundamental_assumption_wrong"]
    assert rule.action is ActionType.RUN
    assert rule.skill == "research" and rule.step is None
    assert wf.find_step("research") is None


def test_scenario_d_human_decision():
    review = _reference_workflow().find_step("review")
    assert review.outcomes["human_required"].action is ActionType.HUMAN
    assert review.decisions["approve"].action is ActionType.COMPLETE
    req = review.decisions["request_changes"]
    assert req.action is ActionType.RUN and req.step == "implementation"
    assert review.decisions["cancel"].action is ActionType.CANCEL
