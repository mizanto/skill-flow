"""Tests for ``skillflow.run_input``.

Two kinds of test, following ``test_context.py`` / ``test_outputs.py``:

* **Change-detectors** -- exact dataclass set, exact field set, ``__all__``, an
  AST import boundary, a banned-fragment scan, a pin that the module adds no
  enum and no exception class, and the frozen/slotted/kw-only shape.
* **The acceptance criterion, mechanically** -- SF-19 requires that a ``RunInput``
  "contains only required durable context and Workflow execution parameters" and
  "no accumulated conversation history". These are field-name and ``repr`` pins:
  no Run provenance, no transcript reference, and no step routing can reach the
  projection, because no field can hold them.
* **Behaviour tests** -- the §4.8 projection over the real reference definition,
  every rejection and its precedence, the ``select_context`` delegations, and one
  end-to-end test against real storage where SF-17 + SF-18 + SF-19 compose.
"""

import ast
import contextlib
import dataclasses
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import run_input as run_input_module
from skillflow import store, workspace
from skillflow.context import ContextEntry, ContextSelection
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    Artifact,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.evaluator import EvaluationOutput
from skillflow.run_input import RunInput, resolve_run_input
from skillflow.service import create_run, create_task, register_workflow
from skillflow.workflow import ActionType, ExpectedOutput, WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)


# --- construction helpers (test fixtures, not production abstractions) --------


def _step(step_id="implementation", **over):
    kw = dict(id=step_id, skill="sk")
    kw.update(over)
    return WorkflowStep(**kw)


def _task(**over):
    kw = dict(
        id="task-1",
        title="Change something",
        description="",
        status=TaskStatus.ACTIVE,
        created_at=EARLIER,
        updated_at=EARLIER,
    )
    kw.update(over)
    return Task(**kw)


def _run(**over):
    kw = dict(
        id="run-1",
        task_id="task-1",
        status=RunStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        step_id="implementation",
        trigger_reason=TRIGGER_REASON_INITIAL,
    )
    kw.update(over)
    return Run(**kw)


def _artifact(**over):
    kw = dict(
        id="artifact-1",
        task_id="task-1",
        run_id="run-0",
        name="requirements.md",
        type="requirements",
        version=1,
        path="task-1/requirements-v1.md",
        created_at=EARLIER,
    )
    kw.update(over)
    return Artifact(**kw)


def _resolve(**over):
    """``resolve_run_input`` with a consistent default triple."""
    kw = dict(
        task=_task(),
        run=_run(),
        step=_step(),
        artifacts=[],
    )
    kw.update(over)
    return resolve_run_input(**kw)


# --- schema change-detectors -------------------------------------------------

V0_DATACLASSES = {"RunInput"}

V0_FIELDS = {
    "task_id",
    "task_title",
    "task_description",
    "run_id",
    "step_id",
    "skill",
    "model",
    "effort",
    "instructions",
    "context",
    "outputs",
}


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(run_input_module).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == run_input_module.__name__
    }
    assert defined == V0_DATACLASSES


def test_all_matches_the_public_surface():
    assert set(run_input_module.__all__) == V0_DATACLASSES | {"resolve_run_input"}


def test_entity_fields_match_spec():
    # SF-A-5 §4.8, projected flat: task{id,title,description}, run{id},
    # step{id,skill,model,effort}, instructions, context, outputs.
    assert {f.name for f in dataclasses.fields(RunInput)} == V0_FIELDS


def test_module_imports_are_within_the_boundary():
    source = Path(run_input_module.__file__).read_text()
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
        for name in vars(run_input_module)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_defines_no_new_enum_and_no_exception_class():
    import enum

    for name, value in vars(run_input_module).items():
        if not isinstance(value, type) or value.__module__ != run_input_module.__name__:
            continue
        assert not issubclass(value, enum.Enum), f"{name} is an enum"
        assert not issubclass(value, BaseException), f"{name} is an exception"


def test_dataclass_is_frozen_slotted_keyword_only():
    params = RunInput.__dataclass_params__
    assert params.frozen and params.kw_only
    instance = _resolve()
    assert not hasattr(instance, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.skill = "x"


# --- "no accumulated conversation history", mechanically ---------------------


def test_no_field_can_carry_history_or_routing():
    banned = (
        "result",
        "transcript",
        "history",
        "events",
        "previous",
        "runs",
        "decision",
    )
    offenders = [
        f.name for f in dataclasses.fields(RunInput) if any(b in f.name for b in banned)
    ]
    assert not offenders


def test_run_provenance_and_transcript_never_reach_the_run_input():
    # SF-A-5 §3.4: transcripts, previous Runs and Results are never passed
    # automatically. The projection has no field that could hold them.
    run = _run(
        id="run-2",
        transcript_ref="/tmp/session-abc.jsonl",
        triggered_by_run_id="run-previous",
        trigger_reason="changes_requested",
    )
    resolved = _resolve(run=run)
    rendered = repr(resolved)
    assert "session-abc" not in rendered
    assert "run-previous" not in rendered
    assert "changes_requested" not in rendered
    assert "transcript" not in rendered
    assert resolved.run_id == "run-2"


def test_step_routing_never_reaches_the_run_input():
    # The reference 'review' step carries outcomes and decisions; those are
    # lifecycle routing, not execution parameters (SF-A-4 §7).
    step = load_workflow(REFERENCE).find_step("review")
    assert step.outcomes and step.decisions
    run = _run(step_id="review")
    resolved = _resolve(run=run, step=step)
    rendered = repr(resolved)
    for routing in ("changes_requested", "approved", "request_changes", "cancel"):
        assert routing not in rendered
    assert resolved.skill == "code-review"


def test_only_the_selected_artifacts_reach_the_run_input():
    wanted = _artifact()
    unwanted = _artifact(
        id="artifact-2", name="notes.md", type="notes", path="task-1/notes-v1.md"
    )
    resolved = _resolve(
        step=_step(context=["requirements"]), artifacts=[wanted, unwanted]
    )
    assert resolved.context.artifacts == (wanted,)
    assert "notes.md" not in repr(resolved)


# --- behaviour: the §4.8 projection ------------------------------------------


def test_full_projection_from_the_reference_review_step():
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("review")
    task = _task(id="task-9", title="Ship it", description="  body  ")
    run = _run(id="run-9", task_id="task-9", step_id="review")
    requirements = _artifact(task_id="task-9")
    plan = _artifact(
        id="artifact-2",
        task_id="task-9",
        name="plan.md",
        type="plan",
        path="task-9/plan-v1.md",
    )

    resolved = resolve_run_input(
        task=task, run=run, step=step, artifacts=[requirements, plan]
    )

    assert resolved.task_id == "task-9"
    assert resolved.task_title == "Ship it"
    assert resolved.task_description == "  body  "  # verbatim, not stripped
    assert resolved.run_id == "run-9"
    assert resolved.step_id == "review"
    assert resolved.skill == "code-review"
    assert resolved.model == "opus"
    assert resolved.effort == "high"
    assert resolved.outputs == step.outputs
    assert resolved.context.artifacts == (requirements, plan)
    assert resolved.context.unresolved == ()


def test_step_without_model_or_effort_projects_none():
    resolved = _resolve(step=_step(), run=_run())
    assert resolved.model is None
    assert resolved.effort is None


def test_step_outputs_are_projected_verbatim():
    step = _step(
        outputs=[
            ExpectedOutput(type="plan"),
            ExpectedOutput(type="review", required=False),
        ]
    )
    resolved = _resolve(step=step)
    assert resolved.outputs == step.outputs
    assert [o.type for o in resolved.outputs] == ["plan", "review"]
    assert resolved.outputs[1].required is False


def test_step_declaring_no_outputs_projects_an_empty_tuple():
    # The reference 'implementation' step declares none.
    step = load_workflow(REFERENCE).find_step("implementation")
    resolved = _resolve(step=step, run=_run(step_id="implementation"))
    assert resolved.outputs == ()


@pytest.mark.parametrize("instructions", [None, "", "  Address the review  "])
def test_instructions_are_the_runs_own_verbatim(instructions):
    resolved = _resolve(run=_run(instructions=instructions))
    assert resolved.instructions == instructions


def test_step_with_no_context_projects_an_empty_selection():
    resolved = _resolve(step=_step(), artifacts=[_artifact()])
    assert resolved.context.entries == ()
    assert resolved.context.artifacts == ()
    assert resolved.context.unresolved == ()


def test_declared_type_the_task_never_produced_is_unresolved_not_an_error():
    resolved = _resolve(
        step=_step(context=["requirements", "review"]), artifacts=[_artifact()]
    )
    assert resolved.context.unresolved == ("review",)
    assert resolved.context.artifacts == (_artifact(),)


def test_latest_version_reaches_the_run_input():
    v1 = _artifact(id="a-1", name="plan.md", type="plan", path="task-1/plan-v1.md")
    v2 = _artifact(
        id="a-2",
        name="plan.md",
        type="plan",
        version=2,
        path="task-1/plan-v2.md",
        supersedes_id="a-1",
    )
    resolved = _resolve(step=_step(context=["plan"]), artifacts=[v1, v2])
    assert resolved.context.artifacts == (v2,)


def test_artifact_from_another_run_of_the_same_task_is_selected():
    other_run = _artifact(id="a-x", run_id="run-earlier")
    resolved = _resolve(step=_step(context=["requirements"]), artifacts=[other_run])
    assert resolved.context.artifacts == (other_run,)


def test_resolution_is_deterministic_across_shuffled_input():
    arts = [
        _artifact(id="a1", name="a.md", type="plan", path="task-1/a-v1.md"),
        _artifact(id="a2", name="a.md", type="plan", version=2, path="task-1/a-v2.md"),
        _artifact(id="b1", name="b.md", type="plan", path="task-1/b-v1.md"),
    ]
    step = _step(context=["plan"])
    forward = _resolve(step=step, artifacts=arts)
    backward = _resolve(step=step, artifacts=list(reversed(arts)))
    assert forward == backward


def test_resolution_accepts_a_one_shot_iterable_and_does_not_mutate_inputs():
    arts = [_artifact()]
    step = _step(context=["requirements"])
    run = _run(instructions="do it")
    task = _task()
    resolved = resolve_run_input(
        task=task, run=run, step=step, artifacts=(a for a in arts)
    )
    assert resolved.context.artifacts == (_artifact(),)
    assert arts == [_artifact()]
    assert step.context == ("requirements",)
    assert run.instructions == "do it"


def test_run_status_is_not_checked_here():
    # Whether the Run is 'running' is resolve-task's precondition (SF-A-5 §4.3),
    # not this projection's -- validate_outputs sets the same precedent.
    resolved = _resolve(run=_run(status=RunStatus.COMPLETED))
    assert resolved.run_id == "run-1"


# --- rejections (ValueError, message names the cause) ------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"task": "nope"}, "task must be a Task"),
        ({"run": "nope"}, "run must be a Run"),
        ({"step": "nope"}, "step must be a WorkflowStep"),
    ],
)
def test_non_domain_arguments_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        _resolve(**kwargs)


def test_run_of_another_task_rejected():
    with pytest.raises(ValueError) as exc:
        _resolve(run=_run(task_id="other-task"))
    msg = str(exc.value)
    assert "run-1" in msg and "other-task" in msg and "task-1" in msg


def test_skill_targeted_run_rejected():
    run = _run(step_id=None, trigger_reason=None)
    with pytest.raises(ValueError, match="has no workflow step"):
        _resolve(run=run)


def test_run_targeting_another_step_rejected():
    with pytest.raises(ValueError) as exc:
        _resolve(run=_run(step_id="review"))
    msg = str(exc.value)
    assert "review" in msg and "implementation" in msg


@pytest.mark.parametrize("artifacts", ["requirements.md", None])
def test_artifacts_validation_is_delegated_to_select_context(artifacts):
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        _resolve(step=_step(context=["requirements"]), artifacts=artifacts)


def test_non_artifact_element_is_delegated_to_select_context():
    with pytest.raises(ValueError, match="must contain Artifact"):
        _resolve(artifacts=[_artifact(), "nope"])


def test_foreign_artifact_is_delegated_to_select_context():
    with pytest.raises(ValueError, match="belongs to task"):
        _resolve(artifacts=[_artifact(task_id="other-task")])


# --- error precedence (the rule order is the contract) -----------------------


def test_type_checks_precede_the_id_checks():
    with pytest.raises(ValueError, match="task must be a Task"):
        resolve_run_input(
            task="nope", run=_run(task_id="other"), step=_step(), artifacts=[]
        )


def test_task_linkage_precedes_the_missing_step_check():
    with pytest.raises(ValueError, match="belongs to task"):
        _resolve(run=_run(task_id="other-task", step_id=None, trigger_reason=None))


def test_missing_step_precedes_the_step_mismatch_check():
    with pytest.raises(ValueError, match="has no workflow step"):
        _resolve(run=_run(step_id=None, trigger_reason=None))


def test_step_mismatch_precedes_artifact_validation():
    with pytest.raises(ValueError, match="targets step"):
        _resolve(run=_run(step_id="review"), artifacts="requirements.md")


# --- RunInput construction guards -------------------------------------------


def _kwargs(**over):
    kw = dict(
        task_id="task-1",
        task_title="Title",
        task_description="",
        run_id="run-1",
        step_id="implementation",
        skill="sk",
    )
    kw.update(over)
    return kw


@pytest.mark.parametrize(
    "field", ["task_id", "task_title", "run_id", "step_id", "skill", "model", "effort"]
)
def test_blank_identifiers_rejected(field):
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        RunInput(**_kwargs(**{field: "  "}))


def test_identifiers_are_stored_stripped():
    resolved = RunInput(**_kwargs(task_id=" task-1 ", model=" opus "))
    assert resolved.task_id == "task-1"
    assert resolved.model == "opus"


def test_non_string_body_rejected():
    with pytest.raises(ValueError, match="task_description must be a string"):
        RunInput(**_kwargs(task_description=None))
    with pytest.raises(ValueError, match="instructions must be a string"):
        RunInput(**_kwargs(instructions=7))


def test_empty_description_and_instructions_are_valid():
    resolved = RunInput(**_kwargs(task_description="", instructions=""))
    assert resolved.task_description == ""
    assert resolved.instructions == ""


def test_non_context_selection_context_rejected():
    with pytest.raises(ValueError, match="context must be a ContextSelection"):
        RunInput(**_kwargs(context=ContextEntry(type="plan")))


def test_outputs_must_be_expected_outputs():
    with pytest.raises(ValueError, match="outputs must contain ExpectedOutput"):
        RunInput(**_kwargs(outputs=("nope",)))
    with pytest.raises(ValueError, match="outputs must be an iterable"):
        RunInput(**_kwargs(outputs="plan"))


def test_context_default_is_an_empty_selection():
    assert RunInput(**_kwargs()).context == ContextSelection()
    assert RunInput(**_kwargs()).outputs == ()


# --- end-to-end against real storage (SF-17 + SF-18 + SF-19) -----------------


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    with contextlib.closing(store.open_store(ws)) as connection:
        yield connection


def test_end_to_end_rework_run_input_over_real_storage(conn, ws):
    from skillflow import artifacts as artifact_store

    workflow = load_workflow(REFERENCE)
    register_workflow(conn, workflow)
    task = create_task(
        conn, title="Change something", workflow_definition_id=workflow.name
    )

    def _advance(step_id, *, triggered_by=None, reason=TRIGGER_REASON_INITIAL, **kw):
        action = EvaluationOutput(action=ActionType.RUN, reason=reason, step=step_id)
        return create_run(
            conn,
            task_id=task.id,
            action=action,
            triggered_by_run_id=triggered_by,
            **kw,
        )

    def _complete(run):
        done = replace(
            run,
            status=RunStatus.COMPLETED,
            completed_at=run.started_at + timedelta(seconds=1),
        )
        with conn:
            store.update_run(conn, done)
        return store.get_run(conn, run.id)

    def _write(run, name, type, content):
        artifact_store.create_artifact(
            conn, ws, run_id=run.id, name=name, type=type, content=content
        )

    req_run = _complete(_advance("requirements"))
    _write(req_run, "requirements.md", "requirements", "reqs")

    dec_run = _advance("decomposition", triggered_by=req_run.id, reason="ready")
    _write(dec_run, "plan.md", "plan", "plan v1")
    _write(dec_run, "plan.md", "plan", "plan v2")  # revised before completing
    dec_run = _complete(dec_run)

    impl_run = _complete(
        _advance("implementation", triggered_by=dec_run.id, reason="ready")
    )

    review_run = _advance("review", triggered_by=impl_run.id, reason="ready")
    _write(review_run, "review.md", "review", "changes requested")
    review_run = _complete(review_run)

    rework_run = _advance(
        "implementation",
        triggered_by=review_run.id,
        reason="changes_requested",
        instructions="  Address the review findings.  ",
    )

    resolved = resolve_run_input(
        task=task,
        run=rework_run,
        step=workflow.find_step(rework_run.step_id),
        artifacts=store.list_artifacts_for_task(conn, task.id),
    )

    by_type = {e.type: e.artifacts for e in resolved.context.entries}
    assert [e.type for e in resolved.context.entries] == [
        "requirements",
        "plan",
        "review",
    ]
    assert [(a.name, a.version) for a in by_type["requirements"]] == [
        ("requirements.md", 1)
    ]
    assert [(a.name, a.version) for a in by_type["plan"]] == [("plan.md", 2)]
    # The prior review comes from another Run: Task scope is the point.
    assert [(a.name, a.version, a.run_id) for a in by_type["review"]] == [
        ("review.md", 1, review_run.id)
    ]
    assert resolved.context.unresolved == ()

    assert resolved.task_id == task.id
    assert resolved.run_id == rework_run.id
    assert resolved.step_id == "implementation"
    assert resolved.skill == "implementation"
    assert resolved.model == "sonnet"
    assert resolved.effort == "high"
    assert resolved.outputs == ()
    # Run.instructions, verbatim: unstripped, exactly what create_run stored.
    assert resolved.instructions == "  Address the review findings.  "

    # No history: neither the trigger reason nor the transcript reaches the
    # projection. The selected review.md keeps its producing run_id -- that is
    # the artifact reference SF-A-5 §4.8 asks for, not conversation history.
    rendered = repr(resolved)
    assert "changes_requested" not in rendered
    assert "transcript" not in rendered
    # Outside the selected artifacts, nothing names the triggering Run.
    assert all(
        review_run.id not in repr(getattr(resolved, field.name))
        for field in dataclasses.fields(resolved)
        if field.name != "context"
    )
