"""Fundamental-assumption E2E: Implementation -> Review -> research -> replan.

SF-32 (plan ref SF-033, SF-A-4 §14-C): the assumption-failure loop exercised
as one scenario on the real reference workflow. Every Task starts at
``steps[0]``, so the scenario opens with the happy-path prefix (Requirements,
Decomposition, Implementation), then drives the loop under test: Review
completes with ``fundamental_assumption_wrong``, a skill-targeted research
Run is created, research completes with ``replan``, and the lifecycle
continues through Decomposition (plan v2), Implementation, and a final
Review completing with ``approved``.

Each Run is driven through a freshly opened SQLite connection and only
string ids cross the Run boundary (plus the stateless ``Workspace``
checkout handle, which holds no session state) -- the mechanical form of
"one independent Claude Code session per Run". Nothing else in-memory (no
``Task``/``Run``/``RunInput``) may pass from one Run to the next; every
command re-resolves its state from SQLite + workspace files.

The scenario pins the SF-32 contract end to end:

* the ``fundamental_assumption_wrong`` completion evaluates to a research
  Run (``run`` with ``skill="research"``, no step), and the next resolve
  creates it -- an ordinary outcome rule, not a Rework entity;
* the research Run's provenance names the review Run with reason
  ``fundamental_assumption_wrong`` (SF-A-4 §8);
* the research Run resolves the triggering review Run's artifacts as
  context (the v1 review that found the bad assumption);
* the research Run has no declared outputs, so ``prepare-artifacts``
  reports ``StepUnresolved`` and writes nothing;
* research completes with ``replan`` -- validated against the triggering
  review step's table and persisted as ``Outcome(type="review")`` -- and
  evaluates to a Decomposition Run;
* the second Decomposition resolves ``research.md`` v1 (criterion: research
  output becomes available to subsequent resolution) and produces
  ``plan.md`` v2; Implementation and Review consume plan v2, not research;
* ``plan.md`` v1 -> v2 and ``review.md`` v1 -> v2 are immutable version
  chains across Runs;
* the original Runs remain in history with their canonical Results;
* the terminal tail: Task ``completed`` and no 9th Run.
"""

import contextlib
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.domain import RunStatus, TaskStatus
from skillflow.prepare_artifacts import PrepareArtifactsError
from skillflow.prepare_artifacts import prepare_artifacts as prepare
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

_TEST_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = _TEST_ROOT / "workflows" / "software-change.yaml"

#: One row per Run: step id (None for the skill-targeted research Run),
#: outcome type persisted on the Result (the interpreting step: the Run's
#: own step, or the triggering step for the research Run), completion
#: decision, artifact submissions as (name, type, content), expected
#: resolved context types in declaration order, expected unresolved context
#: types, expected declared outputs as (type, required), expected
#: pre-completion missing required output types (unused for the research
#: Run, whose prepare raises StepUnresolved instead), and the expected
#: evaluated next action as (ActionType, step-or-None, skill-or-None,
#: reason).
STEPS = (
    (
        "requirements",
        "requirements",
        "ready",
        (("requirements.md", "requirements", "# requirements\n"),),
        (),
        (),
        (("requirements", True),),
        ("requirements",),
        (ActionType.RUN, "decomposition", None, "ready"),
    ),
    (
        "decomposition",
        "decomposition",
        "ready",
        (("plan.md", "plan", "# plan\n"),),
        ("requirements",),
        # `research` resolves only on the post-research pass below; on the
        # first pass it is simply unresolved.
        ("research",),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", None, "ready"),
    ),
    (
        # Declares outcomes but no outputs: the legal payload is an outcome
        # with zero submissions. Its context declares `review`, which no Run
        # has produced yet on the first pass, so it stays unresolved.
        "implementation",
        "implementation",
        "ready",
        (),
        ("requirements", "plan"),
        ("review",),
        (),
        (),
        (ActionType.RUN, "review", None, "ready"),
    ),
    (
        # The review that finds the bad assumption. The outcome routes to a
        # skill, not a step -- an ordinary outcome rule, not a Rework entity.
        "review",
        "review",
        "fundamental_assumption_wrong",
        (("review.md", "review", "# review: fundamental assumption wrong\n"),),
        ("requirements", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.RUN, None, "research", "fundamental_assumption_wrong"),
    ),
    (
        # The research Run: no step, no declared outputs, no execution
        # parameters. Its context is the triggering review Run's artifacts.
        # It reports `replan`, validated against the review step's table.
        None,
        "review",
        "replan",
        (("research.md", "research", "# research: corrected assumption\n"),),
        ("review",),
        (),
        (),
        (),
        (ActionType.RUN, "decomposition", None, "replan"),
    ),
    (
        # Re-decomposition: `research` now resolves to the findings, and
        # this Run must produce its own `plan` artifact (per-Run output
        # scope), which becomes v2 of the chain.
        "decomposition",
        "decomposition",
        "ready",
        (("plan.md", "plan", "# plan: revised\n"),),
        ("requirements", "research"),
        (),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", None, "ready"),
    ),
    (
        # Sees requirements, plan v2 (latest version), and the v1 review --
        # but not the research output, which only decomposition consumes.
        "implementation",
        "implementation",
        "ready",
        (),
        ("requirements", "plan", "review"),
        (),
        (),
        (),
        (ActionType.RUN, "review", None, "ready"),
    ),
    (
        # v1 exists, but output scope is the current Run: this Run must
        # produce its own `review` artifact, which becomes v2 of the chain.
        "review",
        "review",
        "approved",
        (("review.md", "review", "# review: approved\n"),),
        ("requirements", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.COMPLETE, None, None, "approved"),
    ),
)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    return workspace.init_workspace(tmp_path)


@pytest.fixture
def workflows(ws):
    directory = ws.root / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / "software-change.yaml").write_text(
        REFERENCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


@contextlib.contextmanager
def _session(ws):
    """Yield a fresh connection: one simulated Claude Code session."""
    with contextlib.closing(store.open_store(ws)) as conn:
        yield conn


def test_fundamental_assumption_loop_to_approved(ws, workflows):
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id="software-change"
        )
        task_id = task.id

    run_ids = []
    for index, (
        step_id,
        outcome_type,
        decision,
        submissions,
        context_types,
        unresolved,
        outputs,
        missing,
        (action, next_step, next_skill, reason),
    ) in enumerate(STEPS):
        # A new session per Run: only string ids (`task_id`, the
        # collected `run_ids`) and the stateless `ws` handle cross in.
        with _session(ws) as conn:
            run_input = resolve(conn, ws, task_id=task_id)
            assert run_input.step_id == step_id
            assert run_input.task_id == task_id
            seen_types = [a.type for a in run_input.context.artifacts]
            assert seen_types == list(context_types)
            assert run_input.context.unresolved == unresolved
            seen_outputs = [(o.type, o.required) for o in run_input.outputs]
            assert seen_outputs == list(outputs)

            events = store.list_lifecycle_events_for_task(conn, task_id)
            events_before = len(events)
            if step_id is None:
                # The research Run declares no outputs, so there is nothing
                # to inspect: prepare-artifacts reports StepUnresolved and
                # writes nothing.
                with pytest.raises(PrepareArtifactsError) as exc_info:
                    prepare(conn, ws, task_id=task_id)
                assert exc_info.value.code == "StepUnresolved"
                assert run_input.run_id in str(exc_info.value)
                assert (
                    len(store.list_lifecycle_events_for_task(conn, task_id))
                    == events_before
                )
            else:
                report = prepare(conn, ws, task_id=task_id)
                assert report.run_id == run_input.run_id
                assert report.step_id == step_id
                actual = tuple(c.type for c in report.validation.missing_required)
                assert actual == missing
                # prepare-artifacts is a read-only inspection: no rows,
                # no events.
                assert (
                    len(store.list_lifecycle_events_for_task(conn, task_id))
                    == events_before
                )

            if index == 4:
                # The research RunInput: no step, the action's skill, no
                # execution parameters, no declared outputs.
                assert run_input.skill == "research"
                assert run_input.model is None
                assert run_input.effort is None
                assert run_input.instructions is None
                # Its context is the triggering review Run's artifacts: the
                # v1 review that found the bad assumption.
                ctx_artifacts = run_input.context.artifacts
                assert len(ctx_artifacts) == 1
                [review] = ctx_artifacts
                assert review.version == 1
                assert review.run_id == run_ids[3]
                # Sequential integrity across the loop: a second resolve
                # while this Run is running is rejected; the Run itself is
                # untouched.
                with pytest.raises(ResolveTaskError) as exc_info:
                    resolve(conn, ws, task_id=task_id)
                assert exc_info.value.code == "ActiveRunExists"

            if index == 5:
                # Re-decomposition sees the research findings.
                ctx_artifacts = run_input.context.artifacts
                [research] = [a for a in ctx_artifacts if a.type == "research"]
                assert research.version == 1
                assert research.run_id == run_ids[4]

            if index == 6:
                # The second implementation sees plan v2, not research.
                ctx_artifacts = run_input.context.artifacts
                [plan] = [a for a in ctx_artifacts if a.type == "plan"]
                assert plan.version == 2
                assert [a.type for a in ctx_artifacts] == [
                    "requirements",
                    "plan",
                    "review",
                ]

            done = complete(
                conn,
                ws,
                run_id=run_input.run_id,
                request=CompletionRequest(
                    decision=decision,
                    artifacts=tuple(
                        ArtifactSubmission(name=name, type=type, content=body)
                        for name, type, body in submissions
                    ),
                ),
            )
            assert done.action is not None
            assert done.action.action is action
            assert done.action.step == next_step
            assert done.action.skill == next_skill
            assert done.action.reason == reason
            assert done.run.status is RunStatus.COMPLETED
            assert done.result.run_id == run_input.run_id
            assert done.result.outcome is not None
            assert (
                done.result.outcome.type,
                done.result.outcome.decision,
            ) == (outcome_type, decision)
            run_ids.append(run_input.run_id)
            # Only `task_id` / `run_ids` (plain strings) leave the session.

    # Terminal state, observed through yet another fresh connection.
    with _session(ws) as conn:
        task = store.get_task(conn, task_id)
        assert task.status is TaskStatus.COMPLETED

        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert all(run.status is RunStatus.COMPLETED for run in runs)
        assert [run.step_id for run in runs] == [
            "requirements",
            "decomposition",
            "implementation",
            "review",
            None,
            "decomposition",
            "implementation",
            "review",
        ]
        assert runs[0].triggered_by_run_id is None
        assert runs[0].trigger_reason == "initial"
        expected_reasons = (
            "ready",
            "ready",
            "ready",
            "fundamental_assumption_wrong",
            "replan",
            "ready",
            "ready",
        )
        for previous, run, expected in zip(
            runs, runs[1:], expected_reasons, strict=False
        ):
            assert run.triggered_by_run_id == previous.id
            assert run.trigger_reason == expected

        for run in runs:
            result = store.get_result_for_run(conn, run.id)
            assert result is not None
            assert result.run_id == run.id

        artifacts = store.list_artifacts_for_task(conn, task_id)
        assert [(a.name, a.type, a.version) for a in artifacts] == [
            ("requirements.md", "requirements", 1),
            ("plan.md", "plan", 1),
            ("review.md", "review", 1),
            ("research.md", "research", 1),
            ("plan.md", "plan", 2),
            ("review.md", "review", 2),
        ]
        assert [read_content(ws, a) for a in artifacts] == [
            "# requirements\n",
            "# plan\n",
            "# review: fundamental assumption wrong\n",
            "# research: corrected assumption\n",
            "# plan: revised\n",
            "# review: approved\n",
        ]
        plan_v1, plan_v2 = artifacts[1], artifacts[4]
        assert plan_v1.supersedes_id is None
        assert plan_v2.supersedes_id == plan_v1.id
        assert plan_v1.run_id == run_ids[1]
        assert plan_v2.run_id == run_ids[5]
        first_review, second_review = artifacts[2], artifacts[5]
        assert first_review.supersedes_id is None
        assert second_review.supersedes_id == first_review.id
        assert first_review.run_id == run_ids[3]
        assert second_review.run_id == run_ids[7]
        (research,) = [a for a in artifacts if a.type == "research"]
        assert research.supersedes_id is None
        assert research.run_id == run_ids[4]

        # No 9th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"
