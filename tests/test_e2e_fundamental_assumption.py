"""Fundamental-assumption E2E: Review -> research step -> replan -> Review.

SF-42 (SF-A-4 §14-C) on the product reference workflow: Research ->
Decomposition -> Implementation -> Review completing with
``fundamental_assumption_wrong`` -> Research completing with ``replan`` ->
Decomposition -> Implementation -> Review completing with ``approved`` ->
Task ``completed``. The skill-targeted form of this loop (SF-32) stays
covered on the frozen runtime fixture by
``test_e2e_skill_targeted_research.py``.

Each Run is driven through a freshly opened SQLite connection and only
string ids cross the Run boundary (plus the stateless ``Workspace``
checkout handle, which holds no session state) -- the mechanical form of
"one independent Claude Code session per Run". Nothing else in-memory (no
``Task``/``Run``/``RunInput``) may pass from one Run to the next; every
command re-resolves its state from SQLite + workspace files.

The scenario pins the SF-42 contract end to end:

* the ``fundamental_assumption_wrong`` completion evaluates to a Run of the
  ``research`` step -- an ordinary outcome rule targeting an earlier step,
  not a Rework entity and not a skill-targeted Run;
* the re-entered research Run's provenance names the review Run with reason
  ``fundamental_assumption_wrong`` (SF-A-4 §8), and its context is review
  v1, plan v1 and research v1 in declaration order;
* every Run has a step, so ``prepare-artifacts`` reports the missing
  required outputs for each, and writes nothing;
* research completes with ``replan``, validated against the research step's
  own table and persisted as ``Outcome(type="research")``, and evaluates to
  a Decomposition Run;
* the second Decomposition consumes research v2 and review v1 and produces
  plan v2; Implementation consumes plan v2; the final Review consumes
  research v2 and plan v2;
* ``research.md``, ``plan.md`` and ``review.md`` form immutable v1 -> v2
  version chains across Runs;
* the terminal tail: Task ``completed`` and no 9th Run;
* the same walk through the ``skillflow`` CLI entry point, including the
  printed context of the re-entered research Run.
"""

import contextlib
from pathlib import Path

import pytest

from skillflow import store, workspace
from skillflow.artifacts import read_content
from skillflow.cli import main
from skillflow.complete_run import complete_run as complete
from skillflow.completion import ArtifactSubmission, CompletionRequest
from skillflow.domain import RunStatus, TaskStatus
from skillflow.prepare_artifacts import prepare_artifacts as prepare
from skillflow.resolve_task import ResolveTaskError
from skillflow.resolve_task import resolve_task as resolve
from skillflow.service import create_task, register_workflow
from skillflow.workflow import ActionType
from skillflow.workflow_loader import load_workflow

_TEST_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = _TEST_ROOT / "workflows" / "software-change.yaml"

#: One row per Run: step id, completion decision, artifact submissions as
#: (name, type, content), expected resolved context types in declaration
#: order, expected unresolved context types, expected declared outputs as
#: (type, required), expected pre-completion missing required output types,
#: and the expected evaluated next action as (ActionType, step-or-None,
#: reason). The persisted outcome type is always the Run's own step id.
STEPS = (
    (
        "research",
        "ready",
        (("research.md", "research", "# research\n"),),
        (),
        ("review", "plan", "research"),
        (("research", True),),
        ("research",),
        (ActionType.RUN, "decomposition", "ready"),
    ),
    (
        "decomposition",
        "ready",
        (("plan.md", "plan", "# plan\n"),),
        ("research",),
        ("review",),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", "ready"),
    ),
    (
        # Declares outcomes but no outputs: the legal payload is an outcome
        # with zero submissions.
        "implementation",
        "ready",
        (),
        ("plan",),
        ("review",),
        (),
        (),
        (ActionType.RUN, "review", "ready"),
    ),
    (
        # The review that finds the bad assumption. The outcome routes back
        # to the research step -- an ordinary outcome rule.
        "review",
        "fundamental_assumption_wrong",
        (("review.md", "review", "# review: fundamental assumption wrong\n"),),
        ("research", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.RUN, "research", "fundamental_assumption_wrong"),
    ),
    (
        # The re-entered research Run: every declared context type resolves
        # now. It must produce its own `research` artifact (per-Run output
        # scope), which becomes v2 of the chain.
        "research",
        "replan",
        (("research.md", "research", "# research: corrected assumption\n"),),
        ("review", "plan", "research"),
        (),
        (("research", True),),
        ("research",),
        (ActionType.RUN, "decomposition", "replan"),
    ),
    (
        # Re-decomposition: research v2 and the v1 review; produces plan v2.
        "decomposition",
        "ready",
        (("plan.md", "plan", "# plan: revised\n"),),
        ("research", "review"),
        (),
        (("plan", True),),
        ("plan",),
        (ActionType.RUN, "implementation", "ready"),
    ),
    (
        "implementation",
        "ready",
        (),
        ("plan", "review"),
        (),
        (),
        (),
        (ActionType.RUN, "review", "ready"),
    ),
    (
        # v1 exists, but output scope is the current Run: this Run must
        # produce its own `review` artifact, which becomes v2 of the chain.
        "review",
        "approved",
        (("review.md", "review", "# review: approved\n"),),
        ("research", "plan"),
        (),
        (("review", True),),
        ("review",),
        (ActionType.COMPLETE, None, "approved"),
    ),
)

STEP_IDS = [row[0] for row in STEPS]

#: The skill each step's Run is printed with by `resolve-task`.
SKILLS = {
    "research": "skillflow:research",
    "decomposition": "skillflow:decomposition",
    "implementation": "skillflow:implementation",
    "review": "skillflow:code-review",
}


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


def _new_task(ws):
    with _session(ws) as conn:
        register_workflow(conn, load_workflow(REFERENCE))
        task = create_task(
            conn, title="Ship it", workflow_definition_id="software-change"
        )
        return task.id


def test_fundamental_assumption_loop_to_approved(ws, workflows):
    task_id = _new_task(ws)

    run_ids = []
    for index, (
        step_id,
        decision,
        submissions,
        context_types,
        unresolved,
        outputs,
        missing,
        (action, next_step, reason),
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

            events_before = len(store.list_lifecycle_events_for_task(conn, task_id))
            report = prepare(conn, ws, task_id=task_id)
            assert report.run_id == run_input.run_id
            assert report.step_id == step_id
            actual = tuple(c.type for c in report.validation.missing_required)
            assert actual == missing
            # prepare-artifacts is a read-only inspection: no rows, no events.
            assert (
                len(store.list_lifecycle_events_for_task(conn, task_id))
                == events_before
            )

            ctx = [(a.type, a.version, a.run_id) for a in run_input.context.artifacts]
            if index == 4:
                # The re-entered research Run: the step's own execution
                # parameters, and review v1, plan v1, research v1 as context.
                assert run_input.skill == "skillflow:research"
                assert run_input.model == "opus"
                assert run_input.effort == "high"
                assert run_input.instructions is None
                assert ctx == [
                    ("review", 1, run_ids[3]),
                    ("plan", 1, run_ids[1]),
                    ("research", 1, run_ids[0]),
                ]
                # Sequential integrity across the loop: a second resolve
                # while this Run is running is rejected; the Run itself is
                # untouched.
                with pytest.raises(ResolveTaskError) as exc_info:
                    resolve(conn, ws, task_id=task_id)
                assert exc_info.value.code == "ActiveRunExists"

            if index == 5:
                # Re-decomposition sees the corrected research and the review.
                assert ctx == [
                    ("research", 2, run_ids[4]),
                    ("review", 1, run_ids[3]),
                ]

            if index == 6:
                # The second implementation sees plan v2 and the v1 review.
                assert ctx == [("plan", 2, run_ids[5]), ("review", 1, run_ids[3])]

            if index == 7:
                assert ctx == [("research", 2, run_ids[4]), ("plan", 2, run_ids[5])]

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
            assert done.action.skill is None
            assert done.action.reason == reason
            assert done.run.status is RunStatus.COMPLETED
            assert done.result.run_id == run_input.run_id
            assert done.result.outcome is not None
            assert (
                done.result.outcome.type,
                done.result.outcome.decision,
            ) == (step_id, decision)
            run_ids.append(run_input.run_id)
            # Only `task_id` / `run_ids` (plain strings) leave the session.

    # Terminal state, observed through yet another fresh connection.
    with _session(ws) as conn:
        task = store.get_task(conn, task_id)
        assert task.status is TaskStatus.COMPLETED

        runs = store.list_runs_for_task(conn, task_id)
        assert [run.id for run in runs] == run_ids
        assert all(run.status is RunStatus.COMPLETED for run in runs)
        assert [run.step_id for run in runs] == STEP_IDS
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
        assert len(expected_reasons) == len(runs) - 1
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
            ("research.md", "research", 1),
            ("plan.md", "plan", 1),
            ("review.md", "review", 1),
            ("research.md", "research", 2),
            ("plan.md", "plan", 2),
            ("review.md", "review", 2),
        ]
        assert [read_content(ws, a) for a in artifacts] == [
            "# research\n",
            "# plan\n",
            "# review: fundamental assumption wrong\n",
            "# research: corrected assumption\n",
            "# plan: revised\n",
            "# review: approved\n",
        ]
        for v1, v2, (run_v1, run_v2) in (
            (artifacts[0], artifacts[3], (0, 4)),
            (artifacts[1], artifacts[4], (1, 5)),
            (artifacts[2], artifacts[5], (3, 7)),
        ):
            assert v1.supersedes_id is None
            assert v2.supersedes_id == v1.id
            assert v1.run_id == run_ids[run_v1]
            assert v2.run_id == run_ids[run_v2]

        # No 9th Run: a terminal Task takes no further Runs.
        with pytest.raises(ResolveTaskError) as exc_info:
            resolve(conn, ws, task_id=task_id)
        assert exc_info.value.code == "TaskAlreadyCompleted"


def test_fundamental_assumption_cli_walk(ws, workflows, monkeypatch, capsys):
    # The same walk through the `skillflow` entry point, as a user would
    # type it: resolve-task, write the durable files, complete-run.
    task_id = _new_task(ws)
    monkeypatch.chdir(ws.root)

    for index, (step_id, decision, submissions, *_, next_action) in enumerate(STEPS):
        assert main(["resolve-task", task_id]) == 0
        resolved = capsys.readouterr().out
        skill = SKILLS[step_id]
        assert f"step {step_id!r} via skill {skill!r}" in resolved

        if index == 4:
            # The re-entered research Run prints review v1, plan v1 and
            # research v1 as its context, in declaration order, each with
            # its repo-relative content path (SF-44).
            store_prefix = f".skillflow/artifacts/{task_id}"
            assert (
                "Context:\n"
                f"  - review.md (review v1): {store_prefix}/review-v1.md\n"
                f"  - plan.md (plan v1): {store_prefix}/plan-v1.md\n"
                f"  - research.md (research v1): {store_prefix}/research-v1.md\n"
            ) in resolved

        args = ["complete-run", "--outcome", decision]
        for name, type_, body in submissions:
            (ws.root / name).write_text(body, encoding="utf-8")
            args += ["--artifact", f"{name}:{type_}:{name}"]
        assert main(args) == 0
        completed = capsys.readouterr().out
        action, next_step, _ = next_action
        if action is ActionType.RUN:
            assert f"Next action:\nRun {next_step}." in completed
        else:
            assert "Task status:\ncompleted" in completed

    with _session(ws) as conn:
        assert store.get_task(conn, task_id).status is TaskStatus.COMPLETED
        runs = store.list_runs_for_task(conn, task_id)
        assert [run.step_id for run in runs] == STEP_IDS
