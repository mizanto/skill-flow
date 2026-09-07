"""Skill Flow deterministic Lifecycle Evaluation (SF-11).

The operation the MVP turns on: *given the current lifecycle state and what a
Run reported, what should happen next?* (SF-A-4 §1). This module is a single
pure function -- :func:`evaluate` -- over two frozen value objects. It maps the
current step's declared outcome/decision rules (SF-A-4 §7) onto one v0 lifecycle
action, and mutates nothing.

Three boundaries, mirroring :mod:`skillflow.domain` and :mod:`skillflow.workflow`:

* **No persistence.** No ``sqlite3``, no ``store`` import, no ``to_row`` /
  ``from_row``. The caller fetches the ``Task`` / ``Run`` / ``Result`` /
  ``HumanDecision`` (``store.get_result_for_run`` etc.) and hands them in.
* **No I/O.** No filesystem, no clock, no randomness. Every input is an explicit
  field; the output is a function of those fields alone, so calling
  :func:`evaluate` twice on equal input yields equal output.
* **Not an agent.** Nothing here launches Claude Code, creates a session, or
  creates the next Run. :func:`evaluate` returns an *action*; turning that into a
  concrete Run (resolving skill / model / effort / context) is ``resolve-task``
  (SF-A-5 §4.6), and applying the Task-status consequence is the calling command
  (SF-A-5 §6.9 / §7.7).

It is deliberately **not** a workflow engine: one dict lookup keyed by a
decision string. No graph traversal, no history replay, no implicit "next step
in the list", no state machine, no conditions or expressions.

Two documented deviations from SF-A-4's conceptual sketch:

* **Flat :class:`EvaluationOutput`.** SF-A-4 §5 nests ``action.{type, step,
  skill, reason}``. A one-field wrapper dataclass buys nothing in v0 (AGENTS.md
  principle 6), so the fields sit directly on the output.
* **``task.workflow_definition_id == workflow.name`` is not checked.** That the
  persisted definition id *is* the ``Workflow.name`` is a service-layer
  convention (:mod:`skillflow.service`); the evaluator interprets the loaded
  ``Workflow`` it is handed and does not re-derive identity. ``task.status`` is
  likewise not checked -- command preconditions (``resolve-task`` rejects a
  terminal Task, ``decide`` requires ``waiting_for_human``) belong to the
  commands (SF-A-5 §4.3 / §7.4); duplicating them here would create two places
  that can disagree.

A ``human`` decision that itself maps to ``human`` is unreachable rather than
re-checked here: :class:`skillflow.workflow.WorkflowStep` rejects a ``decisions``
rule with ``action: human`` at construction (SF-A-5 §7.7).
"""

from dataclasses import dataclass

from skillflow.domain import HumanDecision, Result, ResultStatus, Run, Task
from skillflow.workflow import ActionType, Workflow

__all__ = ["EvaluationError", "EvaluationInput", "EvaluationOutput", "evaluate"]


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.domain` / :mod:`skillflow.workflow` on
    purpose: keeping this module's "imports only ``dataclasses`` and
    ``skillflow`` value objects" boundary mechanically checkable is worth three
    lines. A shared helper module no issue asks for is the alternative.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


class EvaluationError(Exception):
    """A lifecycle action cannot be determined from the given state.

    One flat class, matching ``store.InvariantViolationError`` and
    ``service.UnknownWorkflowError``: callers distinguish causes by message.
    Deliberately not a ``ValueError`` subclass -- a ``ValueError`` from this
    module is a caller/programming error (bad types, mismatched ids), an
    ``EvaluationError`` is a lifecycle state with no v0 rule.
    """


@dataclass(frozen=True, kw_only=True, slots=True)
class EvaluationInput:
    """The current lifecycle state handed to :func:`evaluate` (SF-A-4 §4).

    ``result`` is required even on the decision path: at ``decide`` time the
    current Run's canonical Result already exists, and SF-A-4 §4 lists
    ``human_decision`` as the only optional member.

    ``__post_init__`` type-checks each field and enforces *linkage* only --
    ``current_run`` belongs to ``task``, ``result`` and any ``human_decision``
    belong to ``current_run``. A mismatch is a caller error, so it raises
    ``ValueError``; a lifecycle state with no rule raises :class:`EvaluationError`
    from :func:`evaluate`. Previous Runs, previous Results, artifacts and
    lifecycle events are deliberately absent -- "the evaluator uses the current
    lifecycle state rather than replaying the entire history" (SF-A-4 §4).
    """

    task: Task
    workflow: Workflow
    current_run: Run
    result: Result
    human_decision: HumanDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, Task):
            raise ValueError("EvaluationInput.task must be a Task")
        if not isinstance(self.workflow, Workflow):
            raise ValueError("EvaluationInput.workflow must be a Workflow")
        if not isinstance(self.current_run, Run):
            raise ValueError("EvaluationInput.current_run must be a Run")
        if not isinstance(self.result, Result):
            raise ValueError("EvaluationInput.result must be a Result")
        if self.human_decision is not None and not isinstance(
            self.human_decision, HumanDecision
        ):
            raise ValueError(
                "EvaluationInput.human_decision must be a HumanDecision or None"
            )

        if self.current_run.task_id != self.task.id:
            raise ValueError(
                "EvaluationInput.current_run.task_id does not match task.id"
            )
        if self.result.run_id != self.current_run.id:
            raise ValueError(
                "EvaluationInput.result.run_id does not match current_run.id"
            )
        if self.human_decision is not None:
            if self.human_decision.run_id != self.current_run.id:
                raise ValueError(
                    "EvaluationInput.human_decision.run_id does not match "
                    "current_run.id"
                )
            if self.human_decision.task_id != self.task.id:
                raise ValueError(
                    "EvaluationInput.human_decision.task_id does not match task.id"
                )


@dataclass(frozen=True, kw_only=True, slots=True)
class EvaluationOutput:
    """The next lifecycle action (SF-A-4 §5), flattened -- see the module docstring.

    ``action`` is reused from :mod:`skillflow.workflow`, not redefined.
    ``reason`` is the outcome-decision or Human-Decision string that selected the
    rule (``"approved"``, ``"changes_requested"``, ``"request_changes"``); it is
    what SF-A-5 §4.7 stores as the next Run's ``trigger_reason`` and is required
    because an evaluated action always has a cause (the only reasonless
    ``trigger_reason`` is ``TRIGGER_REASON_INITIAL``, which initial resolution
    -- SF-12 -- produces, never the evaluator).

    ``__post_init__`` repeats ``OutcomeRule``'s target invariant: ``run`` carries
    exactly one of ``step`` / ``skill``; ``human`` / ``complete`` / ``cancel``
    carry neither. The output deliberately does **not** carry the resolved
    ``WorkflowStep``, model, effort or selected context -- that is
    ``resolve-task``'s job.
    """

    action: ActionType
    reason: str
    step: str | None = None
    skill: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ActionType(self.action))
        object.__setattr__(
            self, "reason", _require_text(self.reason, "EvaluationOutput.reason")
        )
        if self.step is not None:
            object.__setattr__(
                self, "step", _require_text(self.step, "EvaluationOutput.step")
            )
        if self.skill is not None:
            object.__setattr__(
                self, "skill", _require_text(self.skill, "EvaluationOutput.skill")
            )

        if self.action is ActionType.RUN:
            if (self.step is None) == (self.skill is None):
                raise ValueError(
                    "EvaluationOutput with action 'run' requires exactly one of "
                    "'step' or 'skill'"
                )
        elif self.step is not None or self.skill is not None:
            raise ValueError(
                f"EvaluationOutput with action '{self.action}' must not carry a "
                "'step' or 'skill'"
            )


def evaluate(evaluation: EvaluationInput) -> EvaluationOutput:
    """Return the next lifecycle action for ``evaluation`` (SF-A-4 §7).

    The rule order below is the contract: it fixes error precedence.

    1. A non-``completed`` Result has no v0 lifecycle rule -- failure handling is
       SF-35. ``EvaluationError``.
    2. A skill-targeted Run (SF-A-4 §9) has no step and therefore no outcome
       rules. ``EvaluationError``.
    3. The step named by the Run is absent from the Workflow (the definition file
       changed under a live Task). ``EvaluationError``.
    4. Pick the rule table: ``human_decision`` -> ``step.decisions`` keyed by the
       decision; otherwise ``step.outcomes`` keyed by ``result.outcome.decision``
       (a ``None`` outcome has no lifecycle meaning -- ``EvaluationError``).
    5. No rule for that key -- reject rather than invent a transition (SF-A-4
       §11). ``EvaluationError`` listing the accepted keys.
    6. Return the rule's action verbatim.
    """
    run = evaluation.current_run
    result = evaluation.result

    if result.status is not ResultStatus.COMPLETED:
        raise EvaluationError(
            f"run {run.id!r} produced a {result.status.value!r} Result; v0 "
            "Lifecycle Evaluation only handles a 'completed' Result "
            "(failed-Run handling is SF-35)"
        )

    if run.step_id is None:
        raise EvaluationError(
            f"run {run.id!r} has no workflow step, so it has no outcome rules; "
            "a skill-targeted Run's lifecycle meaning is not defined in v0 "
            "(SF-A-4 §9)"
        )

    step = evaluation.workflow.find_step(run.step_id)
    if step is None:
        raise EvaluationError(
            f"run {run.id!r} names step {run.step_id!r}, absent from workflow "
            f"{evaluation.workflow.name!r}"
        )

    if evaluation.human_decision is not None:
        key = evaluation.human_decision.decision
        table = step.decisions
        kind = "human decision"
    else:
        if result.outcome is None:
            if step.outcomes:
                detail = f"step {step.id!r} declares outcome rules"
            else:
                detail = f"step {step.id!r} declares no outcome rules"
            raise EvaluationError(
                f"run {run.id!r} produced a Result with no outcome; {detail} "
                "and no v0 rule gives an outcome-less Result a lifecycle meaning "
                "(the outcome-less-Result policy is SF-22)"
            )
        key = result.outcome.decision
        table = step.outcomes
        kind = "outcome"

    rule = table.get(key)
    if rule is None:
        accepted = ", ".join(repr(k) for k in sorted(table))
        raise EvaluationError(
            f"step {step.id!r} has no rule for {kind} {key!r}; accepted: [{accepted}]"
        )

    return EvaluationOutput(
        action=rule.action, reason=key, step=rule.step, skill=rule.skill
    )
