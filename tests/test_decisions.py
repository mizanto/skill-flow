"""Tests for ``skillflow.decisions`` (SF-26).

Two kinds of test, following ``test_completion.py``:

* **Change-detectors** -- exact dataclass set, exact field sets, ``__all__``,
  a banned-fragment scan, an import boundary that mechanically encodes "no
  SQLite, filesystem, clock, or agent access" (the mechanical form of
  "invalid decisions do not mutate the Task and leave the Result unchanged":
  the module *cannot* write), the single-exception-class shape, and the
  absence of a global decision enum.
* **Behaviour tests** -- valid decision requests (including every key of
  the real ``review`` step from ``workflows/software-change.yaml``) and
  invalid ones (each proving its rejection code), plus determinism and the
  validate→evaluate seam.
"""

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import decisions
from skillflow.decisions import DecisionError, DecisionRequest, validate_decision
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
from skillflow.evaluator import EvaluationInput, evaluate
from skillflow.workflow import ActionType, OutcomeRule, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"


def _step(**over):
    kw = dict(
        id="review",
        skill="code-review",
        outcomes={
            "approved": OutcomeRule(action=ActionType.COMPLETE),
        },
        decisions={
            "approve": OutcomeRule(action=ActionType.COMPLETE),
            "request_changes": OutcomeRule(
                action=ActionType.RUN, step="implementation"
            ),
        },
    )
    kw.update(over)
    return WorkflowStep(**kw)


def _bare_step(**over):
    """A step declaring no decision rules."""
    kw = dict(id="only", skill="sk")
    kw.update(over)
    return WorkflowStep(**kw)


# --- schema change-detectors --------------------------------------------------


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(decisions).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == decisions.__name__
    }
    assert defined == {"DecisionRequest"}


def test_all_matches_the_public_surface():
    assert set(decisions.__all__) == {
        "DecisionError",
        "DecisionRequest",
        "validate_decision",
    }


def test_entity_fields_match_spec():
    assert {f.name for f in dataclasses.fields(DecisionRequest)} == {
        "decision",
        "comment",
    }


def test_dataclass_is_frozen_slotted_keyword_only():
    params = DecisionRequest.__dataclass_params__
    assert params.frozen and params.kw_only
    instance = DecisionRequest(decision="approve")
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.decision = "cancel"


def test_module_imports_are_within_the_boundary():
    source = Path(decisions.__file__).read_text()
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
        "pathlib",
        "os",
        "yaml",
        "subprocess",
        "datetime",
        "random",
        "time",
    ):
        assert forbidden not in modules


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
        for name in vars(decisions)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_defines_no_new_enum_and_exactly_one_exception_class():
    import enum

    exceptions = []
    for name, value in vars(decisions).items():
        if not isinstance(value, type) or value.__module__ != decisions.__name__:
            continue
        assert not issubclass(value, enum.Enum), f"{name} is an enum"
        if issubclass(value, BaseException):
            exceptions.append(name)
    assert exceptions == ["DecisionError"]


# --- behaviour: valid requests ------------------------------------------------


def test_declared_key_validates_to_canonical_string():
    canonical = validate_decision(
        step=_step(), request=DecisionRequest(decision="request_changes")
    )
    assert canonical == "request_changes"
    assert isinstance(canonical, str)


def test_every_reference_review_decisions_key_validates():
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    assert set(step.decisions) == {"approve", "request_changes", "cancel"}
    for key in step.decisions:
        canonical = validate_decision(step=step, request=DecisionRequest(decision=key))
        assert canonical == key


def test_whitespace_padded_decision_is_stripped_at_construction():
    request = DecisionRequest(decision="  approve  ")
    assert request.decision == "approve"
    canonical = validate_decision(step=_step(), request=request)
    assert canonical == "approve"


def test_validation_is_deterministic():
    step = _step()
    first = validate_decision(step=step, request=DecisionRequest(decision="approve"))
    second = validate_decision(step=step, request=DecisionRequest(decision="approve"))
    assert first == second


def test_step_is_not_mutated():
    step = _step()
    validate_decision(step=step, request=DecisionRequest(decision="approve"))
    with pytest.raises(DecisionError):
        validate_decision(
            step=step, request=DecisionRequest(decision="no_such_decision")
        )
    assert step == _step()
    assert _bare_step() == WorkflowStep(id="only", skill="sk")


@pytest.mark.parametrize("comment", [None, "", "  needs work\n"])
def test_validate_decision_ignores_comment(comment):
    request = DecisionRequest(decision="approve", comment=comment)
    assert request.comment == comment
    assert validate_decision(step=_step(), request=request) == "approve"


# --- behaviour: invalid requests ----------------------------------------------


def test_unknown_key_rejected_naming_value_and_accepted_keys():
    with pytest.raises(DecisionError) as exc_info:
        validate_decision(
            step=_step(),
            request=DecisionRequest(decision="maybe_everything_is_fine"),
        )
    assert exc_info.value.code == "InvalidHumanDecision"
    msg = str(exc_info.value)
    assert "maybe_everything_is_fine" in msg
    assert "'approve'" in msg and "'request_changes'" in msg
    assert "/skillflow:decide" in msg


def test_outcome_key_is_not_accepted_as_a_decision():
    # "approved" is an `outcomes` key on the reference `review` step, not a
    # `decisions` key: the tables are separate and `step.outcomes` is never
    # consulted here.
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    assert "approved" in step.outcomes
    assert "approved" not in step.decisions
    with pytest.raises(DecisionError) as exc_info:
        validate_decision(step=step, request=DecisionRequest(decision="approved"))
    assert exc_info.value.code == "InvalidHumanDecision"


def test_case_mismatched_key_rejected():
    with pytest.raises(DecisionError) as exc_info:
        validate_decision(step=_step(), request=DecisionRequest(decision="Approve"))
    assert exc_info.value.code == "InvalidHumanDecision"


def test_any_key_on_step_with_no_decisions_rejected():
    with pytest.raises(DecisionError) as exc_info:
        validate_decision(
            step=_bare_step(), request=DecisionRequest(decision="approve")
        )
    assert exc_info.value.code == "InvalidHumanDecision"
    assert "accepted: []" in str(exc_info.value)


@pytest.mark.parametrize("bad", ["", "   ", 123, ["approve"]])
def test_malformed_decision_rejected_at_construction(bad):
    with pytest.raises(ValueError) as exc_info:
        DecisionRequest(decision=bad)
    assert not isinstance(exc_info.value, DecisionError)


@pytest.mark.parametrize("bad", [123, ["x"], b"bytes"])
def test_malformed_comment_rejected_at_construction(bad):
    with pytest.raises(ValueError):
        DecisionRequest(decision="approve", comment=bad)


@pytest.mark.parametrize("comment", ["", "  padded\n"])
def test_comment_is_stored_verbatim(comment):
    assert DecisionRequest(decision="approve", comment=comment).comment == comment


def test_wrong_argument_types_rejected():
    with pytest.raises(ValueError) as exc_info:
        validate_decision(step="review", request=DecisionRequest(decision="x"))
    assert not isinstance(exc_info.value, DecisionError)
    with pytest.raises(ValueError) as exc_info:
        validate_decision(step=_step(), request="approve")
    assert not isinstance(exc_info.value, DecisionError)
    with pytest.raises(ValueError) as exc_info:
        validate_decision(step=None, request=None)
    assert not isinstance(exc_info.value, DecisionError)


def test_decision_error_carries_code_and_message():
    err = DecisionError("InvalidHumanDecision", "no such decision")
    assert isinstance(err, Exception)
    assert err.code == "InvalidHumanDecision"
    assert str(err) == "no such decision"


def test_validated_decision_evaluates_on_the_same_step():
    # The `validate_decision` -> `evaluate` seam: a validated decision is
    # always evaluable on the same step. SF-27 owns the full command
    # integration; this pins the one-field coupling here.
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    task = Task(
        id="task-1",
        title="Do the thing",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        workflow_definition_id="software-change",
    )
    run = Run(
        id="run-1",
        task_id="task-1",
        status=RunStatus.COMPLETED,
        created_at=now,
        workflow_definition_id="software-change",
        step_id="review",
    )
    for key, rule in step.decisions.items():
        canonical = validate_decision(step=step, request=DecisionRequest(decision=key))
        decision = HumanDecision(
            id=f"decision-{key}",
            task_id="task-1",
            run_id="run-1",
            decision=canonical,
            created_at=now,
        )
        result = Result(
            id=f"result-{key}",
            run_id="run-1",
            status=ResultStatus.COMPLETED,
            created_at=now,
            outcome=Outcome(type="review", decision="human_required"),
        )
        out = evaluate(
            EvaluationInput(
                task=task,
                workflow=workflow,
                current_run=run,
                result=result,
                human_decision=decision,
            )
        )
        assert out.action is rule.action
        assert out.reason == key
