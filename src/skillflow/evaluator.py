"""Skill Flow deterministic Lifecycle Evaluation (SF-11, SF-12).

The operation the MVP turns on: *given the current lifecycle state and what a
Run reported, what should happen next?* (SF-A-4 §1). This module is two pure
functions over frozen value objects. Each returns one v0 lifecycle action and
mutates nothing:

* :func:`evaluate` answers *a Run finished, what now?*, mapping the current
  step's declared outcome/decision rules (SF-A-4 §7) onto an action. The one
  exception is a failed Result, which retries the same step or skill without
  consulting any table (SF-35; see :func:`evaluate` rule 1).
* :func:`resolve_initial_action` answers *a Task exists and nothing has run yet,
  what first?* (SF-A-5 §4.5) -- the one case :func:`evaluate` cannot, because
  there is no previous Run, no Result, and so no outcome rule to look up. It
  *requires* the Task's assigned ``Workflow`` and never chooses one: an
  unassigned Task raises :class:`WorkflowSelectionRequiredError` so the caller
  asks the user. Workflow selection stays separate from Lifecycle Evaluation
  (SF-A-5 §4.4), and persisting a selection remains
  ``service.assign_workflow``'s job.

Three boundaries, mirroring :mod:`skillflow.domain` and :mod:`skillflow.workflow`:

* **No persistence.** No ``sqlite3``, no ``store`` import, no ``to_row`` /
  ``from_row``. The caller fetches the ``Task`` / ``Run`` / ``Result`` /
  ``HumanDecision`` (``store.get_result_for_run`` etc.) and hands them in.
* **No I/O.** No filesystem, no clock, no randomness. Every input is an explicit
  field; the output is a function of those fields alone, so calling either
  function twice on equal input yields equal output.
* **Not an agent.** Nothing here launches Claude Code, creates a session, or
  creates the next Run. Both functions return an *action*; turning that into a
  concrete Run (resolving skill / model / effort / context) is ``resolve-task``
  (SF-A-5 §4.6), and applying the Task-status consequence is the calling command
  (SF-A-5 §6.9 / §7.7).

It is deliberately **not** a workflow engine: one dict lookup keyed by a
decision string, and an initial step that is literally ``steps[0]``. No graph
traversal, no history replay, no implicit "next step in the list", no entry
condition or "start" marker, no state machine, no expressions.

Three documented deviations from SF-A-4's conceptual sketch:

* **Flat :class:`EvaluationOutput`.** SF-A-4 §5 nests ``action.{type, step,
  skill, reason}``. A one-field wrapper dataclass buys nothing in v0 (AGENTS.md
  principle 6), so the fields sit directly on the output.
* **``task.workflow_definition_id == workflow.name`` is not checked by
  :func:`evaluate`.** That the persisted definition id *is* the
  ``Workflow.name`` is a service-layer convention (:mod:`skillflow.service`);
  :func:`evaluate` interprets the loaded ``Workflow`` it is handed and does not
  re-derive identity. :func:`resolve_initial_action` does check it, for a
  different reason: there the Workflow *is* the choice being applied, so
  accepting one the Task never selected would be the guess SF-12 forbids.
  ``task.status`` is checked by neither -- command preconditions
  (``resolve-task`` rejects a terminal Task, ``decide`` requires
  ``waiting_for_human``) belong to the commands (SF-A-5 §4.3 / §7.4);
  duplicating them here would create two places that can disagree.
* **Initial resolution lives here.** SF-A-4 scopes this module to
  outcome-to-action evaluation, and SF-A-5 §4.5 does not say where the first
  step is resolved. Both operations are the same kind of thing -- a pure,
  deterministic map from current lifecycle state onto one
  :class:`EvaluationOutput` -- so a second module would import this one and add
  ~40 lines of packaging for nothing (AGENTS.md principle 1).

A ``human`` decision that itself maps to ``human`` is unreachable rather than
re-checked here: :class:`skillflow.workflow.WorkflowStep` rejects a ``decisions``
rule with ``action: human`` at construction (SF-A-5 §7.7).
"""

from dataclasses import dataclass

from skillflow.domain import (
    TRIGGER_REASON_INITIAL,
    HumanDecision,
    Result,
    ResultStatus,
    Run,
    Task,
)
from skillflow.workflow import ActionType, Workflow

__all__ = [
    "EvaluationError",
    "EvaluationInput",
    "EvaluationOutput",
    "REASON_NO_OUTCOME",
    "REASON_RUN_FAILED",
    "WorkflowSelectionRequiredError",
    "evaluate",
    "resolve_initial_action",
]

#: The ``reason`` for the one action not selected by an outcome or decision
#: rule: a completed Run on a step that declares no outcome rules (SF-22).
REASON_NO_OUTCOME = "no_outcome"

#: The ``reason`` for a retry after a failed Run (SF-35): the failed
#: assignment is re-issued as a new Run on the same step or skill. Like
#: ``REASON_NO_OUTCOME``, a synthetic v0 reason rather than a workflow-table
#: key, and what ``resolve-task`` stores as the retry Run's
#: ``trigger_reason`` (SF-A-5 §4.7).
REASON_RUN_FAILED = "run_failed"


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


class WorkflowSelectionRequiredError(Exception):
    """Initial resolution was asked for a Task with no Workflow Definition.

    SkillFlow never selects a Workflow: the user does, and
    ``service.assign_workflow`` records the choice (SF-A-5 §4.4). This is the
    signal ``resolve-task`` reacts to by prompting.

    Deliberately **not** an :class:`EvaluationError` subclass (nor a
    ``ValueError``): a caller writing ``except EvaluationError`` is handling
    "this lifecycle state has no v0 rule" and must not swallow "ask the user
    which Workflow this Task follows". Flat, like ``EvaluationError`` itself.
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

    ``current_skill`` is the skill the current skill-targeted Run executed,
    recovered by the caller from its ``run.created`` payload (the ``Run`` row
    carries no skill column). It is consulted only on the failed skill-Run
    path (SF-35); anywhere else it is ignored, deliberately leniently -- a
    strict coupling of an optional input to one lifecycle state would force a
    future rule that consults the skill elsewhere to loosen validation first,
    and validation must not preclude future rules.
    """

    task: Task
    workflow: Workflow
    current_run: Run
    result: Result
    human_decision: HumanDecision | None = None
    current_skill: str | None = None

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
        if self.current_skill is not None:
            object.__setattr__(
                self,
                "current_skill",
                _require_text(self.current_skill, "EvaluationInput.current_skill"),
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
    because an action always has a cause. The causes that are not an outcome or
    a decision are ``TRIGGER_REASON_INITIAL``, produced by
    :func:`resolve_initial_action` -- never by :func:`evaluate` --,
    ``REASON_NO_OUTCOME``, produced by :func:`evaluate` for an outcome-less
    Result on a step that declares no outcome rules (SF-22), and
    ``REASON_RUN_FAILED``, produced by :func:`evaluate` for a failed Result,
    which retries the same step or skill (SF-35).

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

    1. A ``failed`` Result retries the same assignment (SF-35) -- the one
       non-table rule, and it fires first: no outcome/decision table is ever
       consulted, and any ``human_decision`` or ``outcome`` on the Result is
       ignored. A step Run retries its step (absent from the Workflow --
       the definition changed under a live Task -- has no v0 rule.
       ``EvaluationError``); a skill-targeted Run retries ``current_skill``
       (absent -- the caller resolves it from the ``run.created`` payload --
       is a caller error. ``ValueError``). Returns ``run`` with reason
       ``REASON_RUN_FAILED``.
    2. A skill-targeted Run (SF-A-4 §9) has no step of its own: its outcome
       names the interpreting step -- ``Outcome.type``, persisted from the
       triggering step by ``complete-run`` (SF-32 gap-fill). No outcome, or a
       type naming no step, has no v0 rule. ``EvaluationError``.
    3. The step named by the Run is absent from the Workflow (the definition file
       changed under a live Task). ``EvaluationError``.
    4. Pick the rule table: ``human_decision`` -> ``step.decisions`` keyed by the
       decision; otherwise ``step.outcomes`` keyed by ``result.outcome.decision``.
       A ``None`` outcome on a step that declares no outcome rules is terminal
       -- ``complete`` with reason ``REASON_NO_OUTCOME`` (SF-22); a ``None``
       outcome on a step that declares outcome rules has no v0 rule --
       ``EvaluationError``. (A skill Run never reaches the ``None`` branches --
       rule 2 rejects it first.)
    5. No rule for that key -- reject rather than invent a transition (SF-A-4
       §11). ``EvaluationError`` listing the accepted keys.
    6. A skill-targeted Run resolving to another skill-targeted Run (SF-32,
       approver decision 5): a skill Run resolves to a step, ``human``,
       ``complete``, or ``cancel``. ``EvaluationError``.
    7. Return the rule's action verbatim.
    """
    run = evaluation.current_run
    result = evaluation.result

    if result.status is ResultStatus.FAILED:
        # The failed assignment is re-issued as a new Run; the failed Run
        # itself is never resumed (SF-A-1 §4). A skill Run retries its
        # skill -- the triggering step's outcome that launched it still
        # stands, so re-running the triggering step would answer the wrong
        # question.
        if run.step_id is not None:
            if evaluation.workflow.find_step(run.step_id) is None:
                raise EvaluationError(
                    f"run {run.id!r} names step {run.step_id!r}, absent from "
                    f"workflow {evaluation.workflow.name!r}; the failed Run "
                    "cannot retry a step the definition no longer declares"
                )
            return EvaluationOutput(
                action=ActionType.RUN,
                reason=REASON_RUN_FAILED,
                step=run.step_id,
            )
        if evaluation.current_skill is None:
            raise ValueError(
                f"run {run.id!r} is skill-targeted and failed, so evaluate() "
                "needs current_skill (resolved by the caller from the Run's "
                "'run.created' payload) to retry it"
            )
        return EvaluationOutput(
            action=ActionType.RUN,
            reason=REASON_RUN_FAILED,
            skill=evaluation.current_skill,
        )

    if run.step_id is None:
        if result.outcome is None:
            raise EvaluationError(
                f"run {run.id!r} has no workflow step and reported no outcome, "
                "so no outcome table can interpret it; a skill-targeted Run "
                "advances the lifecycle only with an outcome (SF-32)"
            )
        step = evaluation.workflow.find_step(result.outcome.type)
        if step is None:
            raise EvaluationError(
                f"run {run.id!r} names step {result.outcome.type!r} in its "
                f"outcome, absent from workflow {evaluation.workflow.name!r}"
            )
    else:
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
            if not step.outcomes:
                # A step that declares no outcome rules declares no
                # continuation: the workflow says nothing follows it, so the
                # Task's lifecycle ends (SF-A-5 §6.5 permits the outcome-less
                # Result; SF-22 gives it this meaning). Inferring "the next
                # step in the list" would put sequencing logic in the
                # evaluator, which SF-11 rejected.
                return EvaluationOutput(
                    action=ActionType.COMPLETE, reason=REASON_NO_OUTCOME
                )
            accepted = ", ".join(repr(k) for k in sorted(step.outcomes))
            raise EvaluationError(
                f"run {run.id!r} produced a Result with no outcome, but step "
                f"{step.id!r} declares outcome rules; complete the Run with one "
                f"of: [{accepted}]"
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

    if run.step_id is None and rule.action is ActionType.RUN and rule.skill is not None:
        raise EvaluationError(
            f"run {run.id!r} is skill-targeted, so {kind} {key!r} must not "
            f"target another skill (skill {rule.skill!r}); a skill-targeted "
            "Run resolves to a step, human, complete, or cancel"
        )

    return EvaluationOutput(
        action=rule.action, reason=key, step=rule.step, skill=rule.skill
    )


def resolve_initial_action(task: Task, workflow: Workflow) -> EvaluationOutput:
    """Return the first lifecycle action for ``task`` (SF-A-5 §4.5, SF-12).

    The Task has no lifecycle history yet, so there is no outcome to map: the
    action is always ``run`` targeting the assigned Workflow's normal starting
    step, with ``reason`` ``"initial"`` -- the ``trigger_reason`` SF-A-5 §4.7
    gives an initial Run.

    The rule order below is the contract: it fixes error precedence.

    1. ``task`` is a ``Task`` and ``workflow`` is a ``Workflow``, else
       ``ValueError`` (the convention of ``EvaluationInput.__post_init__``).
    2. The Task has no ``workflow_definition_id`` ->
       :class:`WorkflowSelectionRequiredError`. SkillFlow never picks one, and
       being handed a ``Workflow`` object is not the user having selected it.
    3. ``workflow`` is not the definition the Task is bound to -> ``ValueError``
       naming both ids. Resolving against an unselected Workflow is the guess
       SF-12 forbids. The v0 identity rule is ``WorkflowDefinition.id ==
       Workflow.name`` (:mod:`skillflow.service`); both sides are stripped at
       construction, and the comparison is exact -- ids are identifiers, not
       display text.
    4. Return ``run`` targeting ``workflow.initial_step`` (``steps[0]``), never
       a step inferred from the Task, its title, or a naming convention.

    Two deliberate non-checks:

    * **Task status.** ``active`` / ``waiting_for_human`` / ``completed`` /
      ``cancelled`` all resolve identically here; command preconditions are
      ``resolve-task``'s (SF-A-5 §4.3), exactly as for :func:`evaluate`.
    * **Previous Runs.** This module has no persistence access. Choosing between
      initial resolution and :func:`evaluate` -- i.e. deciding that a Task has
      no history -- is ``resolve-task``'s job (SF-A-5 §4.5 / §4.6).
    """
    if not isinstance(task, Task):
        raise ValueError("resolve_initial_action() task must be a Task")
    if not isinstance(workflow, Workflow):
        raise ValueError("resolve_initial_action() workflow must be a Workflow")

    if task.workflow_definition_id is None:
        raise WorkflowSelectionRequiredError(
            f"task {task.id!r} has no Workflow Definition; SkillFlow never "
            "selects one -- have the user choose a Workflow and record it with "
            "service.assign_workflow() before resolving the initial step"
        )
    if workflow.name != task.workflow_definition_id:
        raise ValueError(
            f"workflow {workflow.name!r} is not the Workflow assigned to task "
            f"{task.id!r} ({task.workflow_definition_id!r})"
        )

    return EvaluationOutput(
        action=ActionType.RUN,
        step=workflow.initial_step.id,
        reason=TRIGGER_REASON_INITIAL,
    )
