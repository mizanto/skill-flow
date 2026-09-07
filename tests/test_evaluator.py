"""Tests for ``skillflow.evaluator``.

Two kinds of test, following ``test_domain.py`` / ``test_workflow.py``:

* **Change-detectors** -- exact dataclass set, exact field sets, ``__all__``, a
  banned-fragment scan, and an import boundary that mechanically encodes "no
  SQLite, filesystem, Claude Code, or LLM access" -- so drift toward a workflow
  engine fails loudly.
* **Behaviour tests** -- the four SF-A-4 §14 acceptance scenarios against the
  real ``workflows/software-change.yaml``, every rejection in the plan's §9, the
  linkage ``ValueError``s, and determinism.
"""

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import evaluator
from skillflow.domain import (
    HumanDecision,
    Outcome,
    Result,
    ResultStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import (
    EvaluationError,
    EvaluationInput,
    EvaluationOutput,
    evaluate,
)
from skillflow.workflow import ActionType, OutcomeRule, Workflow, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)


# --- construction helpers (test fixtures, not production abstractions) --------


def _task(**over):
    kw = dict(
        id="task-1",
        title="Do the thing",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=EARLIER,
        updated_at=NOW,
        workflow_definition_id="software-change",
    )
    kw.update(over)
    return Task(**kw)


def _run(**over):
    kw = dict(
        id="run-1",
        task_id="task-1",
        status=RunStatus.COMPLETED,
        created_at=EARLIER,
        workflow_definition_id="software-change",
        step_id="review",
    )
    kw.update(over)
    return Run(**kw)


def _result(outcome=None, **over):
    kw = dict(
        id="result-1",
        run_id="run-1",
        status=ResultStatus.COMPLETED,
        created_at=NOW,
        outcome=outcome,
    )
    kw.update(over)
    return Result(**kw)


def _decision(value, **over):
    kw = dict(
        id="hd-1",
        task_id="task-1",
        run_id="run-1",
        decision=value,
        created_at=NOW,
    )
    kw.update(over)
    return HumanDecision(**kw)


def _input(workflow, *, step_id="review", outcome=None, decision=None, **over):
    kw = dict(
        task=_task(),
        workflow=workflow,
        current_run=_run(step_id=step_id),
        result=_result(outcome=outcome),
        human_decision=None if decision is None else _decision(decision),
    )
    kw.update(over)
    return EvaluationInput(**kw)


@pytest.fixture
def workflow() -> Workflow:
    return load_workflow(REFERENCE)


# --- schema change-detectors --------------------------------------------------

V0_DATACLASSES = {"EvaluationInput", "EvaluationOutput"}


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(evaluator).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == evaluator.__name__
    }
    assert defined == V0_DATACLASSES


def test_all_matches_the_public_surface():
    assert set(evaluator.__all__) == V0_DATACLASSES | {"EvaluationError", "evaluate"}


V0_FIELDS = {
    EvaluationInput: {"task", "workflow", "current_run", "result", "human_decision"},
    EvaluationOutput: {"action", "reason", "step", "skill"},
}


@pytest.mark.parametrize(
    "entity,expected",
    list(V0_FIELDS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "",
)
def test_entity_fields_match_spec(entity, expected):
    assert {f.name for f in dataclasses.fields(entity)} == expected


def test_reuses_workflow_action_type_defines_no_new_enum():
    assert evaluator.ActionType is ActionType
    import enum

    enums = [
        name
        for name, value in vars(evaluator).items()
        if isinstance(value, type)
        and issubclass(value, enum.Enum)
        and value.__module__ == evaluator.__name__
    ]
    assert enums == []


def test_no_excluded_lifecycle_concepts_present():
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
    offenders = [
        name
        for name in vars(evaluator)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_module_imports_are_within_the_boundary():
    source = Path(evaluator.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    allowed = {"dataclasses", "skillflow"}
    assert modules <= allowed, f"unexpected imports: {modules - allowed}"
    for forbidden in (
        "sqlite3",
        "yaml",
        "pathlib",
        "os",
        "subprocess",
        "random",
        "datetime",
        "time",
    ):
        assert forbidden not in modules


# --- SF-A-4 §14 acceptance scenarios (real reference definition) --------------


def test_scenario_a_happy_path(workflow):
    chain = [
        ("requirements", "ready", "decomposition"),
        ("decomposition", "ready", "implementation"),
        ("implementation", "ready", "review"),
    ]
    for step_id, decision, nxt in chain:
        out = evaluate(
            _input(
                workflow, step_id=step_id, outcome=Outcome(type="x", decision=decision)
            )
        )
        assert out.action is ActionType.RUN
        assert (out.step, out.skill) == (nxt, None)
        assert out.reason == decision

    approved = evaluate(
        _input(workflow, outcome=Outcome(type="review", decision="approved"))
    )
    assert approved.action is ActionType.COMPLETE
    assert (approved.step, approved.skill) == (None, None)
    assert approved.reason == "approved"


def test_scenario_b_review_rework(workflow):
    out = evaluate(
        _input(workflow, outcome=Outcome(type="review", decision="changes_requested"))
    )
    assert out.action is ActionType.RUN
    assert (out.step, out.skill) == ("implementation", None)
    assert out.reason == "changes_requested"


def test_scenario_c_fundamental_assumption_wrong_targets_a_skill(workflow):
    out = evaluate(
        _input(
            workflow,
            outcome=Outcome(type="review", decision="fundamental_assumption_wrong"),
        )
    )
    assert out.action is ActionType.RUN
    assert (out.step, out.skill) == (None, "research")
    assert out.reason == "fundamental_assumption_wrong"


def test_scenario_d_human_required(workflow):
    out = evaluate(
        _input(workflow, outcome=Outcome(type="review", decision="human_required"))
    )
    assert out.action is ActionType.HUMAN
    assert (out.step, out.skill) == (None, None)
    assert out.reason == "human_required"


def test_scenario_d_decision_approve(workflow):
    out = evaluate(_input(workflow, decision="approve"))
    assert out.action is ActionType.COMPLETE
    assert (out.step, out.skill) == (None, None)
    assert out.reason == "approve"


def test_scenario_d_decision_request_changes(workflow):
    out = evaluate(_input(workflow, decision="request_changes"))
    assert out.action is ActionType.RUN
    assert (out.step, out.skill) == ("implementation", None)
    assert out.reason == "request_changes"


def test_scenario_d_decision_cancel(workflow):
    out = evaluate(_input(workflow, decision="cancel"))
    assert out.action is ActionType.CANCEL
    assert (out.step, out.skill) == (None, None)
    assert out.reason == "cancel"


def test_human_decision_takes_precedence_over_outcome(workflow):
    # Both present: the decision path wins (rule 4).
    out = evaluate(
        _input(
            workflow,
            outcome=Outcome(type="review", decision="changes_requested"),
            decision="approve",
        )
    )
    assert out.action is ActionType.COMPLETE
    assert out.reason == "approve"


# --- rejections (EvaluationError, message names the cause) --------------------


def test_unknown_outcome_decision_lists_accepted_keys(workflow):
    with pytest.raises(EvaluationError) as exc:
        evaluate(
            _input(
                workflow,
                outcome=Outcome(type="review", decision="maybe_everything_is_fine"),
            )
        )
    msg = str(exc.value)
    assert "maybe_everything_is_fine" in msg
    assert "'approved'" in msg and "'changes_requested'" in msg


def test_unknown_human_decision_rejected(workflow):
    with pytest.raises(EvaluationError, match="ship_it"):
        evaluate(_input(workflow, decision="ship_it"))


def test_outcome_less_result_on_a_step_with_outcomes_rejected(workflow):
    with pytest.raises(EvaluationError) as exc:
        evaluate(_input(workflow, outcome=None))
    msg = str(exc.value)
    assert "no outcome" in msg
    assert "declares outcome rules" in msg
    assert "SF-22" in msg


def test_outcome_less_result_on_a_step_with_no_outcomes_rejected():
    # The one combination both plan §13.2 and the implementation report name as
    # the open policy question: empty `outcomes` AND `Result.outcome is None`.
    # Schema-valid (WorkflowStep permits an empty `outcomes`), yet unevaluable
    # in v0. Pinned so SF-22 sees a failing test when it changes the policy.
    workflow = Workflow(name="w", steps=(WorkflowStep(id="only", skill="sk"),))
    with pytest.raises(EvaluationError) as exc:
        evaluate(
            EvaluationInput(
                task=_task(),
                workflow=workflow,
                current_run=_run(step_id="only"),
                result=_result(outcome=None),
            )
        )
    msg = str(exc.value)
    assert "no outcome" in msg
    assert "declares no outcome rules" in msg
    assert "SF-22" in msg


def test_failed_result_rejected_naming_sf35(workflow):
    with pytest.raises(EvaluationError, match="SF-35"):
        evaluate(
            _input(
                workflow,
                result=_result(status=ResultStatus.FAILED),
            )
        )


def test_skill_targeted_run_with_no_step_rejected(workflow):
    with pytest.raises(EvaluationError, match="no workflow step"):
        evaluate(
            EvaluationInput(
                task=_task(),
                workflow=workflow,
                current_run=_run(step_id=None),
                result=_result(outcome=Outcome(type="x", decision="ready")),
            )
        )


def test_step_absent_from_workflow_rejected(workflow):
    with pytest.raises(EvaluationError, match="absent from workflow"):
        evaluate(
            _input(
                workflow,
                step_id="architecture",
                outcome=Outcome(type="x", decision="ready"),
            )
        )


def test_failed_result_precedence_over_unknown_outcome(workflow):
    # Rule 1 (failed Result) fires before rule 5 (unknown outcome).
    with pytest.raises(EvaluationError, match="failed|SF-35"):
        evaluate(
            _input(
                workflow,
                result=_result(
                    status=ResultStatus.FAILED,
                    outcome=Outcome(type="review", decision="nonsense"),
                ),
            )
        )


def test_step_with_no_outcomes_rejects_any_decision():
    workflow = Workflow(
        name="w",
        steps=(WorkflowStep(id="only", skill="sk"),),
    )
    with pytest.raises(EvaluationError, match="accepted: \\[\\]"):
        evaluate(
            _input(
                workflow,
                step_id="only",
                outcome=Outcome(type="x", decision="ready"),
            )
        )


# --- linkage ValueErrors (caller/programming errors) -------------------------


def test_run_task_id_mismatch(workflow):
    with pytest.raises(ValueError, match="current_run.task_id"):
        EvaluationInput(
            task=_task(id="task-1"),
            workflow=workflow,
            current_run=_run(task_id="task-2"),
            result=_result(),
        )


def test_result_run_id_mismatch(workflow):
    with pytest.raises(ValueError, match="result.run_id"):
        EvaluationInput(
            task=_task(),
            workflow=workflow,
            current_run=_run(id="run-1"),
            result=_result(run_id="run-2"),
        )


def test_human_decision_run_id_mismatch(workflow):
    with pytest.raises(ValueError, match="human_decision.run_id"):
        EvaluationInput(
            task=_task(),
            workflow=workflow,
            current_run=_run(id="run-1"),
            result=_result(run_id="run-1"),
            human_decision=_decision("approve", run_id="run-2"),
        )


def test_human_decision_task_id_mismatch(workflow):
    with pytest.raises(ValueError, match="human_decision.task_id"):
        EvaluationInput(
            task=_task(id="task-1"),
            workflow=workflow,
            current_run=_run(task_id="task-1"),
            result=_result(),
            human_decision=_decision("approve", task_id="task-2", run_id="run-1"),
        )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("task", "not-a-task"),
        ("workflow", "not-a-workflow"),
        ("current_run", "not-a-run"),
        ("result", "not-a-result"),
        ("human_decision", "not-a-decision"),
    ],
)
def test_wrong_field_types_rejected(workflow, field, bad):
    kw = dict(
        task=_task(),
        workflow=workflow,
        current_run=_run(),
        result=_result(),
    )
    kw[field] = bad
    with pytest.raises(ValueError, match=rf"EvaluationInput\.{field} must be"):
        EvaluationInput(**kw)


# --- structural / purity ----------------------------------------------------


def _sample(entity, workflow):
    if entity is EvaluationOutput:
        return EvaluationOutput(action=ActionType.COMPLETE, reason="approved")
    return EvaluationInput(
        task=_task(), workflow=workflow, current_run=_run(), result=_result()
    )


@pytest.mark.parametrize("entity", [EvaluationInput, EvaluationOutput])
def test_dataclasses_are_frozen_slotted_keyword_only(entity, workflow):
    params = entity.__dataclass_params__
    assert params.frozen, f"{entity.__name__} must be frozen"
    assert params.kw_only, f"{entity.__name__} must be keyword-only"
    instance = _sample(entity, workflow)
    assert not hasattr(instance, "__dict__"), f"{entity.__name__} must be slotted"
    field_name = next(iter(dataclasses.fields(entity))).name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, "x")


def test_output_run_requires_exactly_one_target():
    with pytest.raises(ValueError, match="exactly one"):
        EvaluationOutput(action=ActionType.RUN, reason="r")
    with pytest.raises(ValueError, match="exactly one"):
        EvaluationOutput(action=ActionType.RUN, reason="r", step="s", skill="k")


def test_output_terminal_action_rejects_a_target():
    with pytest.raises(ValueError, match="must not carry"):
        EvaluationOutput(action=ActionType.COMPLETE, reason="r", step="s")


def test_output_reason_is_required_non_empty():
    with pytest.raises(ValueError, match="reason"):
        EvaluationOutput(action=ActionType.COMPLETE, reason="  ")


def test_output_unknown_action_string_rejected():
    with pytest.raises(ValueError):
        EvaluationOutput(action="teleport", reason="r")


def test_evaluate_is_deterministic(workflow):
    inp = _input(workflow, outcome=Outcome(type="review", decision="changes_requested"))
    assert evaluate(inp) == evaluate(inp)


def test_evaluate_ignores_outcome_mapping_insertion_order():
    rule_a = OutcomeRule(action=ActionType.COMPLETE)
    rule_b = OutcomeRule(action=ActionType.CANCEL)
    forward = WorkflowStep(id="s", skill="sk", outcomes={"a": rule_a, "b": rule_b})
    reverse = WorkflowStep(id="s", skill="sk", outcomes={"b": rule_b, "a": rule_a})
    wf_f = Workflow(name="w", steps=(forward,))
    wf_r = Workflow(name="w", steps=(reverse,))
    out_f = evaluate(_input(wf_f, step_id="s", outcome=Outcome(type="x", decision="a")))
    out_r = evaluate(_input(wf_r, step_id="s", outcome=Outcome(type="x", decision="a")))
    assert out_f == out_r == EvaluationOutput(action=ActionType.COMPLETE, reason="a")


def test_schema_still_forbids_a_decision_mapping_to_human():
    # The assumption behind the missing 'human -> human' branch in evaluate().
    with pytest.raises(ValueError, match="must not map to action 'human'"):
        WorkflowStep(
            id="s",
            skill="sk",
            decisions={"loop_forever": OutcomeRule(action=ActionType.HUMAN)},
        )
