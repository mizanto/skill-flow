"""Skill Flow Human Decision validation (SF-26).

One deterministic question, answered before ``/skillflow:decide`` (SF-27)
writes anything:

    Given the current Workflow step and what the human reported, is this a
    declared decision of that step -- or is the report invalid?

:func:`validate_decision` maps a :class:`DecisionRequest` onto the current
step's declared ``decisions`` table (SF-A-5 §7.5). The allowed values come from
the step and nothing else: SkillFlow defines no global decision enum (SF-A-5
§7.3).

Three boundaries, mirroring :mod:`skillflow.completion` and
:mod:`skillflow.evaluator`:

* **No persistence.** No ``sqlite3``, no ``store`` import. Invalid decisions
  are rejected, never written: the caller (``decide``) calls this before its
  single write, so a rejection leaves the Task ``waiting_for_human`` with the
  Result unchanged.
* **No I/O.** No filesystem, no clock, no randomness. Every input is an
  explicit field; the output is a function of those fields alone, so calling
  the function twice on equal input yields equal output. The step is never
  mutated.
* **Not an agent.** Nothing here launches Claude Code, creates a session, or
  creates the next Run. The function returns a value (or raises); turning that
  into a ``HumanDecision`` is ``decide``'s job.

It is deliberately **not** a lifecycle evaluation:
:mod:`skillflow.evaluator` consumes an already-persisted ``HumanDecision``;
validating the command input that *becomes* that ``HumanDecision`` is a
separate concern. ``step.outcomes`` is likewise never consulted here -- the
two tables are separate, and a key of one is meaningless in the other.

:func:`validate_decision` reads only ``DecisionRequest.decision``.
``DecisionRequest.comment`` is carried for ``decide`` (SF-27), which stores it
on the ``HumanDecision`` -- and is ignored here.
"""

from dataclasses import dataclass

from skillflow.workflow import WorkflowStep

__all__ = [
    "DecisionError",
    "DecisionRequest",
    "validate_decision",
]


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.domain` / :mod:`skillflow.workflow` /
    :mod:`skillflow.evaluator` / :mod:`skillflow.completion` on purpose:
    keeping this module's "imports only ``dataclasses`` and ``skillflow``
    value objects" boundary mechanically checkable is worth three lines. A
    shared helper module no issue asks for is the alternative.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_comment(value: object, field_name: str) -> None:
    """Type-check an optional free-text comment without normalising it.

    A comment is content, mirroring ``domain._require_body`` with
    ``optional=True``: ``None`` or a string (verbatim -- unstripped, and
    permitted to be empty). Anything else is a caller error.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")


class DecisionError(Exception):
    """The reported decision is not valid for the current Workflow step.

    One flat class carrying a ``code``, matching ``CompletionError`` /
    ``CompleteRunError`` / ``ResolveTaskError``: ``"InvalidHumanDecision"``
    (SF-A-5 §7.5/§8's identifier). Every message ends with the concrete next
    command (AGENTS.md principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class DecisionRequest:
    """What the human reports at ``/skillflow:decide`` (SF-A-5 §7.2).

    ``decision`` is §7.2's ``decision`` as transported: a key of the current
    step's ``decisions`` mapping. It is required -- unlike a completion
    outcome, a decision is never legitimately absent, so ``None`` is not a
    case here (a missing decision is a CLI arity error, not a validation
    outcome).

    ``comment`` is §7.2's optional free-text body, carried here so ``decide``
    takes one value object. It is stored verbatim on the ``HumanDecision``,
    exactly like ``HumanDecision.comment``.
    """

    decision: str
    comment: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision",
            _require_text(self.decision, "DecisionRequest.decision"),
        )
        _require_comment(self.comment, "DecisionRequest.comment")


def validate_decision(*, step: WorkflowStep, request: DecisionRequest) -> str:
    """Return the canonical decision string for ``request`` on ``step``.

    The rule order below is the contract: it fixes error precedence,
    mirroring :func:`skillflow.completion.validate_outcome` and
    :func:`skillflow.evaluator.evaluate`.

    1. ``step`` is a :class:`WorkflowStep` and ``request`` is a
       :class:`DecisionRequest`, else ``ValueError`` (caller/programming
       error, per the module convention).
    2. ``request.decision`` is not a declared key: ``InvalidHumanDecision``,
       naming the rejected value and listing the accepted keys. A step that
       declares no ``decisions`` falls out of this rule with an empty
       accepted list (normally unreachable: a step whose outcomes can
       produce the ``human`` action must declare at least one).
    3. Return ``request.decision`` (already stripped at construction).
       Unlike :func:`skillflow.completion.validate_outcome` there is no
       wrapper to build: ``HumanDecision.decision`` is a plain string.
    """
    if not isinstance(step, WorkflowStep):
        raise ValueError("validate_decision requires a WorkflowStep step")
    if not isinstance(request, DecisionRequest):
        raise ValueError("validate_decision requires a DecisionRequest request")

    if request.decision not in step.decisions:
        accepted = ", ".join(repr(k) for k in sorted(step.decisions))
        raise DecisionError(
            "InvalidHumanDecision",
            f"step {step.id!r} has no decision {request.decision!r}; "
            f"accepted: [{accepted}] -- re-run as "
            "`skillflow decide <decision>`",
        )

    return request.decision
