"""Tests for ``skillflow.completion`` (SF-22).

Two kinds of test, following ``test_outputs.py`` / ``test_evaluator.py``:

* **Change-detectors** -- exact dataclass set, exact field sets, ``__all__``,
  a banned-fragment scan, an import boundary that mechanically encodes "no
  SQLite, filesystem, clock, or agent access" (the mechanical form of
  "invalid outcomes do not mutate the Run": the module *cannot* write), the
  single-exception-class shape, and the reuse of ``domain.Outcome``.
* **Behaviour tests** -- valid completion requests (including every key of
  the real ``review`` step from ``workflows/software-change.yaml``) and
  invalid ones (each proving its rejection code), plus determinism and the
  no-mutation guarantee.
"""

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillflow import completion, domain
from skillflow.completion import (
    ArtifactSubmission,
    CompletionError,
    CompletionRequest,
    validate_outcome,
)
from skillflow.domain import (
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
            "changes_requested": OutcomeRule(
                action=ActionType.RUN, step="implementation"
            ),
        },
    )
    kw.update(over)
    return WorkflowStep(**kw)


def _bare_step(**over):
    """A step declaring no outcome rules (SF-A-5 §6.5)."""
    kw = dict(id="only", skill="sk")
    kw.update(over)
    return WorkflowStep(**kw)


# --- schema change-detectors --------------------------------------------------


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(completion).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == completion.__name__
    }
    assert defined == {"ArtifactSubmission", "CompletionRequest"}


def test_all_matches_the_public_surface():
    assert set(completion.__all__) == {
        "ArtifactSubmission",
        "CompletionError",
        "CompletionRequest",
        "validate_outcome",
    }


def test_entity_fields_match_spec():
    assert {f.name for f in dataclasses.fields(CompletionRequest)} == {
        "decision",
        "artifacts",
    }
    assert {f.name for f in dataclasses.fields(ArtifactSubmission)} == {
        "name",
        "type",
        "content",
    }


@pytest.mark.parametrize("entity", [ArtifactSubmission, CompletionRequest])
def test_dataclasses_are_frozen_slotted_keyword_only(entity):
    params = entity.__dataclass_params__
    assert params.frozen and params.kw_only
    if entity is ArtifactSubmission:
        instance = ArtifactSubmission(name="n", type="t", content="c")
    else:
        instance = CompletionRequest()
    assert not hasattr(instance, "__dict__")
    field_name = next(iter(dataclasses.fields(entity))).name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, "approved")


def test_module_imports_are_within_the_boundary():
    source = Path(completion.__file__).read_text()
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
        for name in vars(completion)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_defines_no_new_enum_and_exactly_one_exception_class():
    import enum

    exceptions = []
    for name, value in vars(completion).items():
        if not isinstance(value, type) or value.__module__ != completion.__name__:
            continue
        assert not issubclass(value, enum.Enum), f"{name} is an enum"
        if issubclass(value, BaseException):
            exceptions.append(name)
    assert exceptions == ["CompletionError"]


def test_reuses_domain_outcome():
    assert completion.Outcome is domain.Outcome


# --- behaviour: valid requests ------------------------------------------------


def test_declared_key_validates_to_outcome():
    outcome = validate_outcome(
        step=_step(), request=CompletionRequest(decision="approved")
    )
    assert outcome == Outcome(type="review", decision="approved")


def test_every_reference_review_key_validates():
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    assert set(step.outcomes) == {
        "approved",
        "changes_requested",
        "fundamental_assumption_wrong",
        "human_required",
        "replan",
    }
    for key in step.outcomes:
        outcome = validate_outcome(step=step, request=CompletionRequest(decision=key))
        assert outcome == Outcome(type="review", decision=key)


def test_step_with_no_outcomes_completes_without_one():
    assert validate_outcome(step=_bare_step(), request=CompletionRequest()) is None


def test_whitespace_padded_key_is_stripped_at_construction():
    request = CompletionRequest(decision="  approved  ")
    assert request.decision == "approved"
    outcome = validate_outcome(step=_step(), request=request)
    assert outcome == Outcome(type="review", decision="approved")


def test_validation_is_deterministic():
    step = _step()
    first = validate_outcome(step=step, request=CompletionRequest(decision="approved"))
    second = validate_outcome(step=step, request=CompletionRequest(decision="approved"))
    assert first == second
    assert (
        validate_outcome(step=_bare_step(), request=CompletionRequest())
        == validate_outcome(step=_bare_step(), request=CompletionRequest())
        is None
    )


def test_step_is_not_mutated():
    step = _step()
    validate_outcome(step=step, request=CompletionRequest(decision="approved"))
    validate_outcome(step=_bare_step(), request=CompletionRequest())
    assert step == _step()
    assert _bare_step() == WorkflowStep(id="only", skill="sk")


# --- behaviour: invalid requests ----------------------------------------------


def test_missing_decision_on_step_with_outcomes_rejected():
    with pytest.raises(CompletionError) as exc_info:
        validate_outcome(step=_step(), request=CompletionRequest())
    assert exc_info.value.code == "OutcomeRequired"
    msg = str(exc_info.value)
    assert "'approved'" in msg and "'changes_requested'" in msg


def test_unknown_key_rejected_naming_value_and_accepted_keys():
    with pytest.raises(CompletionError) as exc_info:
        validate_outcome(
            step=_step(),
            request=CompletionRequest(decision="maybe_everything_is_fine"),
        )
    assert exc_info.value.code == "InvalidOutcome"
    msg = str(exc_info.value)
    assert "maybe_everything_is_fine" in msg
    assert "'approved'" in msg and "'changes_requested'" in msg


def test_any_key_on_step_with_no_outcomes_rejected():
    with pytest.raises(CompletionError) as exc_info:
        validate_outcome(
            step=_bare_step(), request=CompletionRequest(decision="approved")
        )
    assert exc_info.value.code == "OutcomeNotExpected"
    assert "'only'" in str(exc_info.value)


def test_human_decision_key_is_not_accepted_as_an_outcome():
    # "approve" is a `decisions` key on the reference `review` step, not an
    # `outcomes` key: the tables are separate and `step.decisions` is never
    # consulted here.
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    assert "approve" in step.decisions
    assert "approve" not in step.outcomes
    with pytest.raises(CompletionError) as exc_info:
        validate_outcome(step=step, request=CompletionRequest(decision="approve"))
    assert exc_info.value.code == "InvalidOutcome"


@pytest.mark.parametrize("bad", ["", "   ", 123, ["approved"]])
def test_malformed_decision_rejected_at_construction(bad):
    with pytest.raises(ValueError) as exc_info:
        CompletionRequest(decision=bad)
    assert not isinstance(exc_info.value, CompletionError)


def test_wrong_argument_types_rejected():
    with pytest.raises(ValueError) as exc_info:
        validate_outcome(step="review", request=CompletionRequest())
    assert not isinstance(exc_info.value, CompletionError)
    with pytest.raises(ValueError) as exc_info:
        validate_outcome(step=_step(), request="approved")
    assert not isinstance(exc_info.value, CompletionError)
    with pytest.raises(ValueError) as exc_info:
        validate_outcome(step=None, request=None)
    assert not isinstance(exc_info.value, CompletionError)


def test_completion_error_carries_code_and_message():
    err = CompletionError("InvalidOutcome", "no such outcome")
    assert isinstance(err, Exception)
    assert err.code == "InvalidOutcome"
    assert str(err) == "no such outcome"


def test_case_mismatched_key_rejected():
    with pytest.raises(CompletionError) as exc_info:
        validate_outcome(step=_step(), request=CompletionRequest(decision="Approved"))
    assert exc_info.value.code == "InvalidOutcome"


def test_validated_outcome_evaluates_on_the_same_step():
    # The `validate_outcome` -> `evaluate` seam: a validated outcome is always
    # evaluable on the same step. SF-23 owns the full command integration;
    # this pins the one-field coupling here.
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
    for key, rule in step.outcomes.items():
        outcome = validate_outcome(step=step, request=CompletionRequest(decision=key))
        result = Result(
            id=f"result-{key}",
            run_id="run-1",
            status=ResultStatus.COMPLETED,
            created_at=now,
            outcome=outcome,
        )
        out = evaluate(
            EvaluationInput(
                task=task, workflow=workflow, current_run=run, result=result
            )
        )
        assert out.action is rule.action
        assert out.reason == key


# --- behaviour: ArtifactSubmission and CompletionRequest.artifacts (SF-23) ----


def test_submission_strips_name_and_type():
    submission = ArtifactSubmission(
        name="  review.md  ", type="  review  ", content="x"
    )
    assert (submission.name, submission.type) == ("review.md", "review")


@pytest.mark.parametrize("field", ["name", "type"])
@pytest.mark.parametrize("bad", ["", "   ", 123, ["x"]])
def test_submission_rejects_blank_or_non_string_name_and_type(field, bad):
    with pytest.raises(ValueError):
        ArtifactSubmission(**{"name": "n", "type": "t", "content": "c", field: bad})


@pytest.mark.parametrize("bad", [123, ["x"], None, b"bytes"])
def test_submission_rejects_non_string_content(bad):
    with pytest.raises(ValueError):
        ArtifactSubmission(name="n", type="t", content=bad)


def test_submission_accepts_empty_content():
    assert ArtifactSubmission(name="n", type="t", content="").content == ""


def test_request_artifacts_default_to_empty_tuple():
    assert CompletionRequest().artifacts == ()


def test_request_artifacts_list_is_coerced_to_tuple():
    submission = ArtifactSubmission(name="n", type="t", content="c")
    request = CompletionRequest(artifacts=[submission])
    assert request.artifacts == (submission,)


@pytest.mark.parametrize("bad", ["review.md", 123, None])
def test_request_artifacts_rejects_str_and_non_iterable(bad):
    with pytest.raises(ValueError):
        CompletionRequest(artifacts=bad)


def test_request_artifacts_rejects_non_submission_element():
    with pytest.raises(ValueError):
        CompletionRequest(artifacts=("review.md",))


def test_request_artifacts_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate name"):
        CompletionRequest(
            artifacts=(
                ArtifactSubmission(name="n", type="a", content="c"),
                ArtifactSubmission(name="n", type="b", content="c"),
            )
        )


def test_validate_outcome_ignores_artifacts():
    submission = ArtifactSubmission(name="review.md", type="review", content="c")
    step = _step()
    assert validate_outcome(
        step=step,
        request=CompletionRequest(decision="approved", artifacts=(submission,)),
    ) == validate_outcome(step=step, request=CompletionRequest(decision="approved"))
    assert (
        validate_outcome(
            step=_bare_step(),
            request=CompletionRequest(artifacts=(submission,)),
        )
        is None
    )
