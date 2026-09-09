"""Tests for ``skillflow.context``.

Two kinds of test, following ``test_outputs.py``:

* **Change-detectors** -- exact dataclass set, exact field sets, ``__all__``, a
  banned-fragment scan, an import boundary that mechanically encodes "no SQLite,
  filesystem, clock, or LLM access", and a pin that the module adds no enum and
  no exception class.
* **Behaviour tests** -- the selection rules the issue's DoD names (only declared
  types, latest version per name, deterministic), every rejection, Task scope
  across Runs, and one end-to-end test against real storage covering the rework
  case.
"""

import ast
import contextlib
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillflow import context, store, workspace
from skillflow.context import (
    ContextEntry,
    ContextSelection,
    select_context,
    select_trigger_context,
)
from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    Artifact,
    RunStatus,
    Task,
    TaskStatus,
)
from skillflow.domain import Run as DomainRun
from skillflow.service import create_task
from skillflow.workflow import WorkflowStep
from skillflow.workflow_loader import load_workflow

REFERENCE = Path(__file__).resolve().parents[1] / "workflows" / "software-change.yaml"

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)


# --- construction helpers (test fixtures, not production abstractions) --------


def _step(step_id="implementation", *, context=()):
    return WorkflowStep(id=step_id, skill="sk", context=context)


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


def _artifact(**over):
    kw = dict(
        id="artifact-1",
        task_id="task-1",
        run_id="run-1",
        name="requirements.md",
        type="requirements",
        version=1,
        path="task-1/requirements-v1.md",
        created_at=NOW,
    )
    kw.update(over)
    return Artifact(**kw)


# --- schema change-detectors -------------------------------------------------

V0_DATACLASSES = {"ContextEntry", "ContextSelection"}


def test_module_defines_exactly_the_v0_dataclasses():
    defined = {
        name
        for name, value in vars(context).items()
        if dataclasses.is_dataclass(value)
        and not name.startswith("_")
        and getattr(value, "__module__", None) == context.__name__
    }
    assert defined == V0_DATACLASSES


def test_all_matches_the_public_surface():
    assert set(context.__all__) == V0_DATACLASSES | {
        "select_context",
        "select_trigger_context",
    }


V0_FIELDS = {
    ContextEntry: {"type", "artifacts"},
    ContextSelection: {"entries"},
}


@pytest.mark.parametrize(
    "entity,expected",
    list(V0_FIELDS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "",
)
def test_entity_fields_match_spec(entity, expected):
    assert {f.name for f in dataclasses.fields(entity)} == expected


def test_module_imports_are_within_the_boundary():
    source = Path(context.__file__).read_text()
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
        for name in vars(context)
        if not name.startswith("_") and any(frag in name.lower() for frag in banned)
    ]
    assert not offenders


def test_defines_no_new_enum_and_no_exception_class():
    import enum

    for name, value in vars(context).items():
        if not isinstance(value, type) or value.__module__ != context.__name__:
            continue
        assert not issubclass(value, enum.Enum), f"{name} is an enum"
        assert not issubclass(value, BaseException), f"{name} is an exception"


@pytest.mark.parametrize("entity", [ContextEntry, ContextSelection])
def test_dataclasses_are_frozen_slotted_keyword_only(entity):
    params = entity.__dataclass_params__
    assert params.frozen and params.kw_only
    instance = (
        ContextEntry(type="requirements")
        if entity is ContextEntry
        else ContextSelection()
    )
    assert not hasattr(instance, "__dict__")
    field_name = next(iter(dataclasses.fields(entity))).name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, "x")


# --- behaviour -------------------------------------------------------------


def test_step_with_no_context_selects_nothing():
    result = select_context(step=_step(), task=_task(), artifacts=[_artifact()])
    assert result.entries == ()
    assert result.artifacts == ()
    assert result.unresolved == ()


def test_single_declared_type_resolves_to_the_chain_head():
    v1 = _artifact(id="a-1", version=1, path="task-1/requirements-v1.md")
    v2 = _artifact(
        id="a-2", version=2, path="task-1/requirements-v2.md", supersedes_id="a-1"
    )
    v3 = _artifact(
        id="a-3", version=3, path="task-1/requirements-v3.md", supersedes_id="a-2"
    )
    result = select_context(
        step=_step(context=["requirements"]), task=_task(), artifacts=[v1, v2, v3]
    )
    (entry,) = result.entries
    assert entry.type == "requirements"
    assert entry.artifacts == (v3,)
    assert entry.resolved
    assert result.unresolved == ()


def test_two_names_sharing_a_type_each_resolve_to_their_head_ordered_by_name():
    a1 = _artifact(
        id="a1", name="api.md", type="plan", version=1, path="task-1/api-v1.md"
    )
    a2 = _artifact(
        id="a2", name="api.md", type="plan", version=2, path="task-1/api-v2.md"
    )
    z1 = _artifact(
        id="z1", name="zzz.md", type="plan", version=1, path="task-1/zzz-v1.md"
    )
    result = select_context(
        step=_step(context=["plan"]), task=_task(), artifacts=[z1, a1, a2]
    )
    (entry,) = result.entries
    assert entry.artifacts == (a2, z1)


def test_declared_type_absent_from_the_task_is_unresolved():
    step = _step(context=["requirements", "review"])
    result = select_context(step=step, task=_task(), artifacts=[_artifact()])
    kinds = {e.type: e.artifacts for e in result.entries}
    assert kinds["requirements"] == (_artifact(),)
    assert kinds["review"] == ()
    assert result.unresolved == ("review",)


def test_entries_follow_declaration_order_not_artifact_order():
    req = _artifact(id="r", name="requirements.md", type="requirements")
    plan = _artifact(id="p", name="plan.md", type="plan", path="task-1/plan-v1.md")
    step = _step(context=["plan", "requirements"])
    result = select_context(step=step, task=_task(), artifacts=[req, plan])
    assert [e.type for e in result.entries] == ["plan", "requirements"]
    assert result.artifacts == (plan, req)


def test_artifact_from_another_run_of_the_same_task_is_selected():
    # Task scope: the version chain spans Runs -- this is the rework case.
    other_run = _artifact(id="a-x", run_id="run-9")
    result = select_context(
        step=_step(context=["requirements"]), task=_task(), artifacts=[other_run]
    )
    (entry,) = result.entries
    assert entry.artifacts == (other_run,)


def test_selection_is_deterministic_across_shuffled_input():
    arts = [
        _artifact(id="a1", name="a.md", type="plan", version=1, path="task-1/a-v1.md"),
        _artifact(id="a2", name="a.md", type="plan", version=2, path="task-1/a-v2.md"),
        _artifact(id="b1", name="b.md", type="plan", version=1, path="task-1/b-v1.md"),
    ]
    step = _step(context=["plan"])
    forward = select_context(step=step, task=_task(), artifacts=arts)
    backward = select_context(step=step, task=_task(), artifacts=list(reversed(arts)))
    assert forward == backward


def test_selection_is_order_independent_even_for_a_duplicated_name_version_pair():
    # Two artifacts sharing (name, version) but differing in id/path -- a state
    # the store's UNIQUE (task_id, name, version) forbids, but select_context
    # accepts any Iterable[Artifact] and promises an unconditional purity
    # property. The (version, id) comparison key keeps the result total.
    left = _artifact(id="a-left", name="a.md", type="plan", path="task-1/a-left.md")
    right = _artifact(id="a-right", name="a.md", type="plan", path="task-1/a-right.md")
    step = _step(context=["plan"])
    forward = select_context(step=step, task=_task(), artifacts=[left, right])
    backward = select_context(step=step, task=_task(), artifacts=[right, left])
    assert forward == backward
    assert forward.entries[0].artifacts == (right,)  # higher id wins the tie


def test_selection_accepts_a_one_shot_iterable_and_does_not_mutate_inputs():
    arts = [_artifact()]
    step = _step(context=["requirements"])
    result = select_context(step=step, task=_task(), artifacts=(a for a in arts))
    (entry,) = result.entries
    assert entry.artifacts == (_artifact(),)
    assert arts == [_artifact()]
    assert step.context == ("requirements",)


def test_type_matching_is_exact_not_a_substring_or_case_match():
    cased = _artifact(id="c", type="Requirements")
    prefixed = _artifact(
        id="p", name="rr.md", type="requirements-notes", path="task-1/rr.md"
    )
    result = select_context(
        step=_step(context=["requirements"]),
        task=_task(),
        artifacts=[cased, prefixed],
    )
    (entry,) = result.entries
    assert entry.artifacts == ()
    assert result.unresolved == ("requirements",)


# --- rejections (ValueError, message names the cause) ------------------------


def test_non_workflowstep_step_rejected():
    with pytest.raises(ValueError, match="step must be a WorkflowStep"):
        select_context(step="nope", task=_task(), artifacts=[])


def test_non_task_task_rejected():
    with pytest.raises(ValueError, match="task must be a Task"):
        select_context(step=_step(), task="nope", artifacts=[])


def test_artifacts_given_as_a_str_rejected():
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        select_context(step=_step(), task=_task(), artifacts="requirements.md")


def test_artifacts_given_as_none_rejected():
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        select_context(step=_step(), task=_task(), artifacts=None)


def test_non_artifact_element_rejected():
    with pytest.raises(ValueError, match="must contain Artifact"):
        select_context(step=_step(), task=_task(), artifacts=[_artifact(), "nope"])


def test_artifact_from_another_task_rejected():
    foreign = _artifact(id="a-x", task_id="other-task")
    with pytest.raises(ValueError) as exc:
        select_context(
            step=_step(context=["requirements"]),
            task=_task(id="task-1"),
            artifacts=[foreign],
        )
    msg = str(exc.value)
    assert "a-x" in msg and "other-task" in msg and "task-1" in msg


# --- ContextEntry / ContextSelection construction guards --------------------


def test_context_entry_rejects_bad_fields():
    with pytest.raises(ValueError, match="type must be a non-empty string"):
        ContextEntry(type="  ")
    with pytest.raises(ValueError, match="artifacts must contain Artifact"):
        ContextEntry(type="requirements", artifacts=("nope",))
    with pytest.raises(ValueError, match="artifacts must be an iterable"):
        ContextEntry(type="requirements", artifacts="x")


def test_context_selection_rejects_non_entry_members():
    with pytest.raises(ValueError, match="entries must contain ContextEntry"):
        ContextSelection(entries=("nope",))


# --- against the real reference definition ---------------------------------


def test_reference_implementation_step_declares_the_expected_context():
    workflow = load_workflow(REFERENCE)
    step = workflow.find_step("implementation")
    result = select_context(step=step, task=_task(), artifacts=[])
    assert [e.type for e in result.entries] == ["requirements", "plan", "review"]
    assert result.unresolved == ("requirements", "plan", "review")


# --- end-to-end against real storage (the rework case) --------------------


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def conn(ws):
    with contextlib.closing(store.open_store(ws)) as connection:
        yield connection


def test_end_to_end_rework_run_receives_prior_review_and_latest_plan(conn, ws):
    from skillflow import artifacts as artifact_store

    reference = load_workflow(REFERENCE)
    implementation_step = reference.find_step("implementation")

    task = create_task(conn, title="Change something")

    def _run(run_id, step_id, **over):
        run = DomainRun(
            id=run_id,
            task_id=task.id,
            status=RunStatus.RUNNING,
            created_at=datetime.now(UTC),
            step_id=step_id,
            trigger_reason=over.pop("trigger_reason", TRIGGER_REASON_INITIAL),
            **over,
        )
        with conn:
            store.insert_run(conn, run)
        return run

    def _complete(run):
        with conn:
            store.update_run(
                conn,
                dataclasses.replace(
                    run, status=RunStatus.COMPLETED, completed_at=datetime.now(UTC)
                ),
            )

    req_run = _run("run-req", "requirements")
    artifact_store.create_artifact(
        conn,
        ws,
        run_id=req_run.id,
        name="requirements.md",
        type="requirements",
        content="reqs",
    )
    _complete(req_run)

    dec_run = _run(
        "run-dec",
        "decomposition",
        triggered_by_run_id=req_run.id,
        trigger_reason="ready",
    )
    artifact_store.create_artifact(
        conn, ws, run_id=dec_run.id, name="plan.md", type="plan", content="plan v1"
    )
    _complete(dec_run)

    impl_run = _run(
        "run-impl",
        "implementation",
        triggered_by_run_id=dec_run.id,
        trigger_reason="ready",
    )
    _complete(impl_run)

    review_run = _run(
        "run-review",
        "review",
        triggered_by_run_id=impl_run.id,
        trigger_reason="ready",
    )
    artifact_store.create_artifact(
        conn,
        ws,
        run_id=review_run.id,
        name="review.md",
        type="review",
        content="changes requested",
    )
    _complete(review_run)

    # A second decomposition-less rework: implementation runs again after
    # review/changes_requested and produces a new plan version.
    rework_run = _run(
        "run-rework",
        "implementation",
        triggered_by_run_id=review_run.id,
        trigger_reason="changes_requested",
    )
    artifact_store.create_artifact(
        conn, ws, run_id=rework_run.id, name="plan.md", type="plan", content="plan v2"
    )

    result = select_context(
        step=implementation_step,
        task=task,
        artifacts=store.list_artifacts_for_task(conn, task.id),
    )

    by_type = {e.type: e.artifacts for e in result.entries}
    assert [a.name for a in by_type["requirements"]] == ["requirements.md"]
    assert [(a.name, a.version) for a in by_type["plan"]] == [("plan.md", 2)]
    assert [(a.name, a.version) for a in by_type["review"]] == [("review.md", 1)]
    assert result.unresolved == ()


# --- select_trigger_context: the skill-Run half (SF-32) -----------------------


def test_trigger_context_groups_by_sorted_type():
    review = _artifact(
        id="r", name="review.md", type="review", path="task-1/review-v1.md"
    )
    req = _artifact(id="q", name="requirements.md", type="requirements")
    result = select_trigger_context(task=_task(), artifacts=[review, req])
    assert [e.type for e in result.entries] == ["requirements", "review"]
    assert result.artifacts == (req, review)
    assert result.unresolved == ()


def test_trigger_context_resolves_latest_version_per_name():
    v1 = _artifact(
        id="v1",
        name="review.md",
        type="review",
        version=1,
        path="task-1/review-v1.md",
    )
    v2 = _artifact(
        id="v2",
        name="review.md",
        type="review",
        version=2,
        path="task-1/review-v2.md",
    )
    result = select_trigger_context(task=_task(), artifacts=[v1, v2])
    (entry,) = result.entries
    assert entry.type == "review"
    assert entry.artifacts == (v2,)


def test_trigger_context_version_tiebreak_prefers_higher_id():
    left = _artifact(id="a-left", name="a.md", type="plan", path="task-1/a-left.md")
    right = _artifact(id="a-right", name="a.md", type="plan", path="task-1/a-right.md")
    forward = select_trigger_context(task=_task(), artifacts=[left, right])
    backward = select_trigger_context(task=_task(), artifacts=[right, left])
    assert forward == backward
    assert forward.entries[0].artifacts == (right,)


def test_trigger_context_is_order_independent():
    arts = [
        _artifact(id="a1", name="a.md", type="plan", path="task-1/a-v1.md"),
        _artifact(id="r1", name="review.md", type="review", path="task-1/review-v1.md"),
        _artifact(id="b1", name="b.md", type="plan", version=2, path="task-1/b-v2.md"),
    ]
    forward = select_trigger_context(task=_task(), artifacts=arts)
    backward = select_trigger_context(task=_task(), artifacts=list(reversed(arts)))
    assert forward == backward
    assert [e.type for e in forward.entries] == ["plan", "review"]


def test_trigger_context_with_no_artifacts_is_empty():
    result = select_trigger_context(task=_task(), artifacts=[])
    assert result.entries == ()
    assert result.artifacts == ()
    assert result.unresolved == ()


def test_trigger_context_accepts_a_one_shot_iterable():
    arts = [_artifact()]
    result = select_trigger_context(task=_task(), artifacts=(a for a in arts))
    (entry,) = result.entries
    assert entry.artifacts == (_artifact(),)
    assert arts == [_artifact()]


def test_trigger_context_rejects_a_non_task():
    with pytest.raises(ValueError, match="must be a Task"):
        select_trigger_context(task="task-1", artifacts=[])


@pytest.mark.parametrize("artifacts", ["requirements.md", None])
def test_trigger_context_rejects_a_non_iterable(artifacts):
    with pytest.raises(ValueError, match="must be an iterable"):
        select_trigger_context(task=_task(), artifacts=artifacts)


def test_trigger_context_rejects_a_non_artifact_element():
    with pytest.raises(ValueError, match="must contain Artifact"):
        select_trigger_context(task=_task(), artifacts=[_artifact(), "nope"])


def test_trigger_context_rejects_a_foreign_task_artifact():
    with pytest.raises(ValueError) as exc:
        select_trigger_context(
            task=_task(), artifacts=[_artifact(task_id="other-task")]
        )
    msg = str(exc.value)
    assert "artifact-1" in msg and "other-task" in msg and "task-1" in msg
