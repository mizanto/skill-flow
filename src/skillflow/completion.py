"""Skill Flow structured completion validation (SF-22).

One deterministic question, answered before ``complete-run`` (SF-23) writes
anything:

    Given the current Workflow step and what Claude reported, what is the
    canonical :class:`skillflow.domain.Outcome` of this Run -- or is there
    legitimately none, or is the report invalid?

:func:`validate_outcome` maps a :class:`CompletionRequest` onto the current
step's declared ``outcomes`` table (SF-A-5 §6.6). The allowed values come from
the step and nothing else; a step that declares no outcome rules declares no
continuation, so a Run on such a step completes without an outcome (SF-A-5
§6.5) and Lifecycle Evaluation resolves it to ``{action: complete}`` with
reason ``"no_outcome"`` (:mod:`skillflow.evaluator`).

Three boundaries, mirroring :mod:`skillflow.evaluator` and
:mod:`skillflow.outputs`:

* **No persistence.** No ``sqlite3``, no ``store`` import. Invalid outcomes
  are rejected, never written: the caller (``complete-run``) calls this
  before its single write, so a rejection leaves the Run ``running``.
* **No I/O.** No filesystem, no clock, no randomness. Every input is an
  explicit field; the output is a function of those fields alone, so calling
  the function twice on equal input yields equal output. The step is never
  mutated.
* **Not an agent.** Nothing here launches Claude Code, creates a session, or
  creates the next Run. The function returns a value object (or raises);
  turning that into a ``Result`` is ``complete-run``'s job.

It is deliberately **not** a lifecycle evaluation: :mod:`skillflow.evaluator`
consumes an already-canonical ``Result``; validating the command input that
*becomes* that ``Result`` is a separate concern. Human Decisions are likewise
out of scope here -- validating a decision against ``step.decisions`` is
``/skillflow:decide``'s contract (SF-A-5 §7.5), so ``step.decisions`` is never
consulted.

:func:`validate_outcome` reads only ``CompletionRequest.decision``.
``CompletionRequest.artifacts`` is carried for ``complete-run`` (SF-23), which
registers the reported durable outputs in the same atomic unit -- and is
ignored here.
"""

from dataclasses import dataclass

from skillflow.domain import Outcome
from skillflow.workflow import WorkflowStep

__all__ = [
    "ArtifactSubmission",
    "CompletionError",
    "CompletionRequest",
    "validate_outcome",
]


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.domain` / :mod:`skillflow.workflow` /
    :mod:`skillflow.evaluator` on purpose: keeping this module's "imports
    only ``dataclasses`` and ``skillflow`` value objects" boundary
    mechanically checkable is worth three lines. A shared helper module no
    issue asks for is the alternative.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


class CompletionError(Exception):
    """The completion request is not valid for the current Workflow step.

    One flat class carrying a ``code``, matching ``ResolveTaskError`` /
    ``PrepareArtifactsError``: ``"InvalidOutcome"`` (SF-A-5 §8's identifier),
    ``"OutcomeRequired"`` and ``"OutcomeNotExpected"``. The latter two are v0
    refinements of §8's ``"InvalidOutcome"`` for the cases it does not name:
    nothing reported where rules exist, and something reported where none
    exist. Every message ends with the concrete next command (AGENTS.md
    principle 13).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactSubmission:
    """One durable output Claude reports at completion (SF-A-5 §5.5/§6.2).

    ``content`` is the artifact body in memory, matching
    ``artifacts.create_artifact``'s parameter: reading a working-tree file is
    the caller's concern, so this module keeps its no-I/O boundary.
    """

    name: str
    type: str
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "name", _require_text(self.name, "ArtifactSubmission.name")
        )
        object.__setattr__(
            self, "type", _require_text(self.type, "ArtifactSubmission.type")
        )
        if not isinstance(self.content, str):
            raise ValueError("ArtifactSubmission.content must be a string")


@dataclass(frozen=True, kw_only=True, slots=True)
class CompletionRequest:
    """What Claude reports at ``/skillflow:complete-run`` (SF-A-5 §6.2).

    ``decision`` is §6.2's ``outcome`` as transported: the outcome key, i.e.
    a key of the current step's ``outcomes`` mapping. ``None`` means "this
    Run reports no lifecycle outcome", legal only on a step that declares
    none.

    ``artifacts`` are the durable outputs the Run produced, carried here so
    ``complete-run`` takes one value object. Two submissions under one name
    in a single completion is a caller error, not a version chain, and is
    rejected.
    """

    decision: str | None = None
    artifacts: tuple[ArtifactSubmission, ...] = ()

    def __post_init__(self) -> None:
        if self.decision is not None:
            object.__setattr__(
                self,
                "decision",
                _require_text(self.decision, "CompletionRequest.decision"),
            )
        if isinstance(self.artifacts, str):
            raise ValueError("CompletionRequest.artifacts must be an iterable")
        try:
            submissions = tuple(self.artifacts)
        except TypeError:
            raise ValueError(
                "CompletionRequest.artifacts must be an iterable"
            ) from None
        for submission in submissions:
            if not isinstance(submission, ArtifactSubmission):
                raise ValueError(
                    "CompletionRequest.artifacts must contain ArtifactSubmission"
                )
        names = [submission.name for submission in submissions]
        if len(set(names)) != len(names):
            raise ValueError("CompletionRequest.artifacts has a duplicate name")
        object.__setattr__(self, "artifacts", submissions)


def validate_outcome(
    *, step: WorkflowStep, request: CompletionRequest
) -> Outcome | None:
    """Return the canonical outcome for ``request`` on ``step``.

    The rule order below is the contract: it fixes error precedence,
    mirroring :func:`skillflow.evaluator.evaluate` and
    :func:`skillflow.outputs.validate_outputs`.

    1. ``step`` is a :class:`WorkflowStep` and ``request`` is a
       :class:`CompletionRequest`, else ``ValueError`` (caller/programming
       error, per the module convention).
    2. ``step.outcomes`` is empty: a reported decision is rejected as
       ``OutcomeNotExpected`` (SkillFlow does not invent a lifecycle meaning
       for an undeclared key, SF-A-4 §11); otherwise return ``None`` -- the
       Run completes without an outcome.
    3. ``request.decision is None`` while the step declares outcomes:
       ``OutcomeRequired``, listing the accepted keys.
    4. ``request.decision`` is not a declared key: ``InvalidOutcome``,
       naming the rejected value and listing the accepted keys.
    5. Return ``Outcome(type=step.id, decision=request.decision)``.
    """
    if not isinstance(step, WorkflowStep):
        raise ValueError("validate_outcome requires a WorkflowStep step")
    if not isinstance(request, CompletionRequest):
        raise ValueError("validate_outcome requires a CompletionRequest request")

    if not step.outcomes:
        if request.decision is not None:
            raise CompletionError(
                "OutcomeNotExpected",
                f"step {step.id!r} declares no outcomes, so outcome "
                f"{request.decision!r} is not expected; complete the Run "
                "without an outcome by running `/skillflow:complete-run` "
                "with no `--outcome` flag",
            )
        return None

    if request.decision is None:
        accepted = ", ".join(repr(k) for k in sorted(step.outcomes))
        raise CompletionError(
            "OutcomeRequired",
            f"step {step.id!r} requires a lifecycle outcome; complete the "
            f"Run with one of: [{accepted}] -- re-run as "
            "`/skillflow:complete-run --outcome <value>`",
        )

    if request.decision not in step.outcomes:
        accepted = ", ".join(repr(k) for k in sorted(step.outcomes))
        raise CompletionError(
            "InvalidOutcome",
            f"step {step.id!r} has no outcome {request.decision!r}; "
            f"accepted: [{accepted}] -- re-run as "
            "`/skillflow:complete-run --outcome <value>`",
        )

    return Outcome(type=step.id, decision=request.decision)
