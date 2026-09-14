"""Tests for ``skillflow.outputs``.

Two kinds of test, following ``test_evaluator.py`` / ``test_workflow.py``:

* **Change-detectors** -- exact dataclass set, exact field sets, ``__all__``, a
  banned-fragment scan, an import boundary that mechanically encodes "no SQLite,
  filesystem, clock, or LLM access" (decision (b) of the plan), and a pin that
  the module adds no enum and no exception class.
* **Behaviour tests** -- the required / optional / valid / invalid cases the
  issue's DoD names, every rejection, determinism, the real reference
  definition, and one end-to-end test against real storage covering the rework
  case per-Run scope exists for.
"""

import ast
import contextlib
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import outputs, store, workspace
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    Artifact,
    Run,
    RunStatus,
)
from skillflow.outputs import OutputCheck, OutputValidation, validate_outputs
from skillflow.service import create_task
from skillflow.workflow import ExpectedOutput, Workflow, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "workflows"
    / "runtime-reference"
    / "software-change.yaml"
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)


# --- construction helpers (test fixtures, not production abstractions) --------


def _step(step_id="review", *, outputs=()):
    return WorkflowStep(id=step_id, skill="sk", outputs=outputs)


def _run(**over):
    kw = dict(
        id="run-1",
        task_id="task-1",
        status=RunStatus.RUNNING,
        created_at=EARLIER,
        step_id="review",
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    kw.update(over)
    return Run(**kw)


def _artifact(**over):
    kw = dict(
        id="artifact-1",
        task_id="task-1",
        run_id="run-1",
        name="review.md",
        type="review",
        version=1,
        path="task-1/review-v1.md",
        created_at=NOW,
    )
    kw.update(over)
    return Artifact(**kw)


# --- schema change-detectors -------------------------------------------------

V0_DATACLASSES = {"OutputCheck", "OutputValidation"}


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(outputs).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == outputs.__name__
    }
    assert defined == V0_DATACLASSES


def test_all_matches_the_public_surface():
    assert set(outputs.__all__) == V0_DATACLASSES | {"validate_outputs"}


V0_FIELDS = {
    OutputCheck: {"type", "required", "artifacts"},
    OutputValidation: {"checks"},
}


@pytest.mark.parametrize(
    "entity,expected",
    list(V0_FIELDS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "",
)
def test_entity_fields_match_spec(entity, expected):
    assert {f.name for f in dataclasses.fields(entity)} == expected


def test_module_imports_are_within_the_boundary():
    source = Path(outputs.__file__).read_text()
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    allowed = {"dataclasses", "collections", "skillflow"}
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
        for name in vars(outputs)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_defines_no_new_enum_and_no_exception_class():
    import enum

    for name, value in vars(outputs).items():
        if not isinstance(value, type) or value.__module__ != outputs.__name__:
            continue
        assert not issubclass(value, enum.Enum), f"{name} is an enum"
        assert not issubclass(value, BaseException), f"{name} is an exception"


@pytest.mark.parametrize("entity", [OutputCheck, OutputValidation])
def test_dataclasses_are_frozen_slotted_keyword_only(entity):
    params = entity.__dataclass_params__
    assert params.frozen and params.kw_only
    instance = (
        OutputCheck(type="review", required=True)
        if entity is OutputCheck
        else (OutputValidation())
    )
    assert not hasattr(instance, "__dict__")
    field_name = next(iter(dataclasses.fields(entity))).name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, "x")


# --- behaviour -------------------------------------------------------------


def test_step_with_no_outputs_is_vacuously_complete():
    result = validate_outputs(step=_step(), run=_run(), artifacts=())
    assert result.checks == ()
    assert result.is_complete
    assert result.missing_required == ()


def test_required_output_with_matching_artifact_is_satisfied():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    result = validate_outputs(step=step, run=_run(), artifacts=[_artifact()])
    (check,) = result.checks
    assert check.satisfied
    assert check.artifacts == (_artifact(),)
    assert result.is_complete


def test_required_output_with_no_artifacts_is_missing():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    result = validate_outputs(step=step, run=_run(), artifacts=[])
    (check,) = result.checks
    assert not check.satisfied
    assert result.missing_required == (check,)
    assert not result.is_complete


def test_required_output_with_only_a_different_type_present_is_missing():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    other = _artifact(
        id="a-2", name="notes.md", type="notes", path="task-1/notes-v1.md"
    )
    result = validate_outputs(step=step, run=_run(), artifacts=[other])
    (check,) = result.checks
    assert not check.satisfied
    assert not result.is_complete


def test_optional_output_missing_does_not_block():
    step = _step(outputs=(ExpectedOutput(type="review", required=False),))
    result = validate_outputs(step=step, run=_run(), artifacts=[])
    (check,) = result.checks
    assert not check.satisfied
    assert result.missing_required == ()
    assert result.is_complete


def test_optional_output_present_is_satisfied():
    step = _step(outputs=(ExpectedOutput(type="review", required=False),))
    result = validate_outputs(step=step, run=_run(), artifacts=[_artifact()])
    (check,) = result.checks
    assert check.satisfied
    assert result.is_complete


def test_mixed_required_and_optional_only_optional_present():
    step = _step(
        outputs=(
            ExpectedOutput(type="review"),
            ExpectedOutput(type="notes", required=False),
        )
    )
    notes = _artifact(
        id="a-2", name="notes.md", type="notes", path="task-1/notes-v1.md"
    )
    result = validate_outputs(step=step, run=_run(), artifacts=[notes])
    assert [c.type for c in result.missing_required] == ["review"]
    assert not result.is_complete


def test_two_artifacts_of_the_same_type_are_both_matched_in_input_order():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    v1 = _artifact(id="a-1", version=1, path="task-1/review-v1.md")
    v2 = _artifact(id="a-2", version=2, path="task-1/review-v2.md")
    result = validate_outputs(step=step, run=_run(), artifacts=[v1, v2])
    (check,) = result.checks
    assert check.artifacts == (v1, v2)


def test_undeclared_artifact_types_are_ignored():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    stray = _artifact(id="a-2", name="x.md", type="scratch", path="task-1/x-v1.md")
    result = validate_outputs(step=step, run=_run(), artifacts=[_artifact(), stray])
    (check,) = result.checks
    assert check.artifacts == (_artifact(),)
    assert result.is_complete


def test_checks_follow_declaration_order_independent_of_artifact_order():
    step = _step(
        outputs=(
            ExpectedOutput(type="plan"),
            ExpectedOutput(type="review"),
        )
    )
    review = _artifact(id="a-r", name="review.md", type="review")
    plan = _artifact(id="a-p", name="plan.md", type="plan", path="task-1/plan-v1.md")
    result = validate_outputs(step=step, run=_run(), artifacts=[review, plan])
    assert [c.type for c in result.checks] == ["plan", "review"]


def test_validation_is_deterministic_across_equal_but_distinct_inputs():
    # Two independently built, value-equal input sets must yield equal verdicts.
    def build():
        return (
            _step(outputs=(ExpectedOutput(type="review"),)),
            _run(),
            [_artifact()],
        )

    step_a, run_a, arts_a = build()
    step_b, run_b, arts_b = build()
    assert (step_a, run_a, arts_a) == (step_b, run_b, arts_b)
    assert validate_outputs(
        step=step_a, run=run_a, artifacts=arts_a
    ) == validate_outputs(step=step_b, run=run_b, artifacts=arts_b)


def test_validation_does_not_mutate_its_inputs():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    run = _run()
    arts = [_artifact()]
    validate_outputs(step=step, run=run, artifacts=arts)
    assert arts == [_artifact()]
    assert step.outputs == (ExpectedOutput(type="review"),)


def test_accepts_a_one_shot_iterable():
    # `artifacts` is materialised once; a generator that can only be consumed
    # once must still produce the right verdict. A regression that iterated the
    # raw argument before materialising it would exhaust it and report every
    # required output as missing -- silently wrong, not loud.
    step = _step(outputs=(ExpectedOutput(type="review"),))
    result = validate_outputs(
        step=step, run=_run(), artifacts=(a for a in [_artifact()])
    )
    (check,) = result.checks
    assert check.artifacts == (_artifact(),)
    assert result.is_complete


def test_type_matching_is_exact_after_stripping():
    step = _step(outputs=(ExpectedOutput(type="  review  "),))
    result = validate_outputs(
        step=step, run=_run(), artifacts=[_artifact(type="review")]
    )
    assert result.is_complete


def test_type_matching_is_case_sensitive_not_a_substring_match():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    cased = _artifact(id="a-c", type="Review")
    prefix = _artifact(id="a-p", name="rr.md", type="review-notes", path="task-1/rr.md")
    result = validate_outputs(step=step, run=_run(), artifacts=[cased, prefix])
    (check,) = result.checks
    assert not check.satisfied
    assert not result.is_complete


# --- rejections (ValueError, message names the cause) ------------------------


def test_non_workflowstep_step_rejected():
    with pytest.raises(ValueError, match="step must be a WorkflowStep"):
        validate_outputs(step="nope", run=_run(), artifacts=[])


def test_non_run_run_rejected():
    with pytest.raises(ValueError, match="run must be a Run"):
        validate_outputs(step=_step(), run="nope", artifacts=[])


def test_artifacts_given_as_a_str_rejected():
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        validate_outputs(step=_step(), run=_run(), artifacts="review.md")


def test_artifacts_given_as_none_rejected():
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        validate_outputs(step=_step(), run=_run(), artifacts=None)


def test_non_artifact_element_rejected():
    with pytest.raises(ValueError, match="must contain Artifact"):
        validate_outputs(step=_step(), run=_run(), artifacts=[_artifact(), "nope"])


def test_skill_targeted_run_with_no_step_rejected():
    with pytest.raises(ValueError, match="no workflow step"):
        validate_outputs(step=_step(), run=_run(step_id=None), artifacts=[])


def test_step_id_mismatch_between_run_and_step_rejected():
    with pytest.raises(ValueError, match="not 'review'"):
        validate_outputs(
            step=_step("review"), run=_run(step_id="implementation"), artifacts=[]
        )


def test_artifact_from_another_run_rejected():
    step = _step(outputs=(ExpectedOutput(type="review"),))
    foreign = _artifact(id="a-old", run_id="run-0")
    with pytest.raises(ValueError) as exc:
        validate_outputs(step=step, run=_run(id="run-1"), artifacts=[foreign])
    msg = str(exc.value)
    assert "a-old" in msg and "run-0" in msg and "run-1" in msg


def test_artifact_from_another_task_rejected():
    # A hand-built Artifact whose run_id matches but task_id does not: the module
    # distrusts its input rather than leaning on the (task_id, run_id) FK, which
    # only constrains artifacts read back from the store.
    step = _step(outputs=(ExpectedOutput(type="review"),))
    foreign = _artifact(id="a-x", run_id="run-1", task_id="other-task")
    with pytest.raises(ValueError) as exc:
        validate_outputs(
            step=step, run=_run(id="run-1", task_id="task-1"), artifacts=[foreign]
        )
    msg = str(exc.value)
    assert "a-x" in msg and "other-task" in msg and "task-1" in msg


# --- OutputCheck / OutputValidation construction guards ---------------------


def test_output_check_rejects_bad_fields():
    with pytest.raises(ValueError, match="type must be a non-empty string"):
        OutputCheck(type="  ", required=True)
    with pytest.raises(ValueError, match="required must be a bool"):
        OutputCheck(type="review", required=1)
    with pytest.raises(ValueError, match="artifacts must contain Artifact"):
        OutputCheck(type="review", required=True, artifacts=("nope",))
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        OutputCheck(type="review", required=True, artifacts="x")


def test_output_validation_rejects_non_check_members():
    with pytest.raises(ValueError, match="checks must contain OutputCheck"):
        OutputValidation(checks=("nope",))


# --- against the real reference definition ---------------------------------


@pytest.fixture
def reference() -> Workflow:
    return load_workflow(REFERENCE)


def test_reference_requirements_step_requires_one_requirements_output(reference):
    step = reference.find_step("requirements")
    run = _run(step_id="requirements")
    result = validate_outputs(step=step, run=run, artifacts=[])
    assert [c.type for c in result.missing_required] == ["requirements"]


def test_reference_implementation_step_declares_no_outputs(reference):
    step = reference.find_step("implementation")
    run = _run(step_id="implementation")
    result = validate_outputs(step=step, run=run, artifacts=[])
    assert result.checks == ()
    assert result.is_complete


def test_reference_review_step_incomplete_without_artifacts(reference):
    step = reference.find_step("review")
    result = validate_outputs(step=step, run=_run(step_id="review"), artifacts=[])
    assert not result.is_complete
    assert [c.type for c in result.missing_required] == ["review"]


# --- end-to-end against real storage (the rework case, decision (a)) --------


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    with contextlib.closing(store.open_store(ws)) as connection:
        yield connection


def test_end_to_end_per_run_scope_covers_rework(conn, ws):
    from skillflow import artifacts as artifact_store

    reference = load_workflow(REFERENCE)
    review_step = reference.find_step("review")

    task = create_task(conn, title="Change something")
    first = Run(
        id="run-review-1",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(UTC),
        step_id="review",
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    with conn:
        store.insert_run(conn, first)
    artifact_store.create_artifact(
        conn, ws, run_id=first.id, name="review.md", type="review", content="ok"
    )

    result = validate_outputs(
        step=review_step,
        run=first,
        artifacts=store.list_artifacts_for_run(conn, first.id),
    )
    assert result.is_complete

    # Complete the first Run so a second may start (one running Run per Task).
    with conn:
        store.update_run(
            conn,
            dataclasses.replace(
                first, status=RunStatus.COMPLETED, completed_at=datetime.now(UTC)
            ),
        )

    # A second review Run after rework produces nothing durable of its own.
    second = Run(
        id="run-review-2",
        task_id=task.id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(UTC),
        step_id="review",
        triggered_by_run_id=first.id,
        trigger_reason="changes_requested",
    )
    with conn:
        store.insert_run(conn, second)
    rework = validate_outputs(
        step=review_step,
        run=second,
        artifacts=store.list_artifacts_for_run(conn, second.id),
    )
    assert not rework.is_complete
    assert [c.type for c in rework.missing_required] == ["review"]
