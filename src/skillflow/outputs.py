"""Skill Flow deterministic expected-output validation (SF-16).

One deterministic question, asked by two later commands and answered here:

    Given a Workflow step and the artifacts a Run has registered, which declared
    outputs (``WorkflowStep.outputs``) are satisfied, and which *required* ones
    are missing?

``prepare-artifacts`` (SF-A-5 §5.4) renders the ✓/✗ report from the answer;
``complete-run`` (SF-A-5 §6.4) rejects completion when a required output is
missing and leaves the Run ``running``. This module produces only the verdict --
neither command exists yet, and neither the report nor the ``running`` guarantee
belongs here.

Two boundaries fix the semantics:

* **Match scope is the current Run.** An ``ExpectedOutput`` is satisfied only by
  an ``Artifact`` whose ``run_id == run.id``. The version chain is keyed by
  ``(task_id, name)`` and spans Runs (SF-A-1 §7), so a Task-level match would let
  a second ``review`` Run after rework pass validation on ``review.md`` v1
  produced by the *first* review Run -- producing nothing durable. Each bounded
  Run must produce its own declared outputs. This is *enforced*, not silently
  filtered: an artifact from another Run in the input is a ``ValueError``.

* **Metadata only -- no filesystem.** This module imports ``dataclasses``,
  ``collections.abc`` and ``skillflow`` value objects, and nothing else: no
  ``sqlite3``, no ``pathlib``, no clock, no randomness -- the same boundary
  :mod:`skillflow.evaluator` holds, pinned by an AST import-boundary test.
  Content/metadata drift is SF-15's concern and surfaces from
  ``artifacts.read_content`` as ``ArtifactStorageError``.

**Matching is by ``type``, exactly.** ``ExpectedOutput.type`` and
``Artifact.type`` are both stripped at construction; comparison is ``==`` on
identifiers, never fuzzy and never on ``name`` (``Artifact.name`` is validated as
a plain filename by ``artifacts._artifact_name`` at creation time -- this module
does not re-check it or compare it to anything).

**The v0 constraint vocabulary is exactly ``{type, required}``.** "Basic
type/name/constraint checks" (SF-A-5 §6.4) is satisfied by type matching plus the
required/optional gate. Size, format, content or naming-pattern constraints would
mean extending ``ExpectedOutput``, which no specification asks for and which is
the first step toward the generic workflow DSL AGENTS.md forbids.

No exception class is defined here. ``RequiredArtifactsMissing`` (SF-A-5 §6.4) is
a *command* rejection and belongs to ``complete-run``, where the Run is left
``running``; defining it now, unused, would be speculative. A caller error (bad
types, mismatched ids, a foreign artifact) raises ``ValueError``, matching
``evaluator``'s convention; a missing output is a normal, reportable outcome, not
an exception.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from skillflow.domain import Artifact, Run
from skillflow.workflow import WorkflowStep

__all__ = ["OutputCheck", "OutputValidation", "validate_outputs"]


def _require_sequence(value: object, field_name: str) -> tuple:
    """Return ``value`` as a tuple, or raise ``ValueError`` if it is not iterable.

    Duplicated from :mod:`skillflow.workflow` on purpose: keeping this module's
    "imports only ``dataclasses``, ``collections`` and ``skillflow`` value
    objects" boundary mechanically checkable is worth three lines, and matches
    how ``domain`` / ``workflow`` / ``evaluator`` already stay independent. A
    ``str`` is refused (iterating it yields characters, never the intended
    elements); a non-iterable, notably ``None``, is refused too.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field_name} must be an iterable")
    return tuple(value)


@dataclass(frozen=True, kw_only=True, slots=True)
class OutputCheck:
    """One declared ``ExpectedOutput`` and the current Run's artifacts matching it.

    ``artifacts`` is a tuple, not a single ``Artifact | None``: a Run may register
    two versions of the same type (``plan.md`` v1 then v2), and picking one would
    be an arbitrary tie-break this issue has no reason to make. Order is the
    caller's input order.
    """

    type: str
    required: bool
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("OutputCheck.type must be a non-empty string")
        if not isinstance(self.required, bool):
            raise ValueError("OutputCheck.required must be a bool")
        artifacts = _require_sequence(self.artifacts, "OutputCheck.artifacts")
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise ValueError("OutputCheck.artifacts must contain Artifact")
        object.__setattr__(self, "artifacts", artifacts)

    @property
    def satisfied(self) -> bool:
        """``True`` iff at least one artifact of this type was registered."""
        return bool(self.artifacts)


@dataclass(frozen=True, kw_only=True, slots=True)
class OutputValidation:
    """The per-step verdict: one ``OutputCheck`` per declared output, in order.

    Only two derived properties. ``satisfied`` / ``missing_optional`` are one-line
    comprehensions at the call site (AGENTS.md principle 1); they are not added.
    """

    checks: tuple[OutputCheck, ...] = ()

    def __post_init__(self) -> None:
        checks = _require_sequence(self.checks, "OutputValidation.checks")
        for check in checks:
            if not isinstance(check, OutputCheck):
                raise ValueError("OutputValidation.checks must contain OutputCheck")
        object.__setattr__(self, "checks", checks)

    @property
    def missing_required(self) -> tuple[OutputCheck, ...]:
        """The required checks with no matching artifact, in declaration order."""
        return tuple(c for c in self.checks if c.required and not c.satisfied)

    @property
    def is_complete(self) -> bool:
        """``True`` iff no required output is missing (optional ones never block)."""
        return not self.missing_required


def validate_outputs(
    *, step: WorkflowStep, run: Run, artifacts: Iterable[Artifact]
) -> OutputValidation:
    """Return the expected-output verdict for ``step`` against ``run``'s artifacts.

    The rule order below is the contract: it fixes error precedence (mirroring
    ``evaluate`` / ``resolve_initial_action``).

    1. ``step`` is a ``WorkflowStep`` and ``run`` is a ``Run``, else ``ValueError``.
    2. ``artifacts`` is an iterable and not a ``str``, else ``ValueError``; every
       element is an ``Artifact``, else ``ValueError``.
    3. ``run.step_id is None`` -> ``ValueError``. A skill-targeted Run (SF-A-4 §9)
       has no workflow step and therefore no declared outputs; validating one
       against a step is a caller error.
    4. ``run.step_id != step.id`` -> ``ValueError`` naming both ids. Validating
       one step's declaration against another step's Run is a caller error.
    5. Every artifact belongs to this Run -- ``run_id == run.id`` **and**
       ``task_id == run.task_id``, else ``ValueError`` naming the artifact and
       both ids. This is where per-Run scope is enforced. The module distrusts
       its input (it accepts hand-built ``Artifact`` value objects, and
       type-checks everything else it is given), so it does not lean on the
       schema's ``(task_id, run_id) -> runs`` foreign key to guarantee the
       pairing -- that only holds for artifacts read back from the store.
    6. For each ``ExpectedOutput`` in ``step.outputs``, in declaration order,
       build an ``OutputCheck`` carrying every artifact whose ``type`` equals the
       expected type, in input order.

    A step with no ``outputs`` yields ``OutputValidation(checks=())``, whose
    ``is_complete`` is ``True`` -- the reference ``implementation`` step's case.
    """
    if not isinstance(step, WorkflowStep):
        raise ValueError("validate_outputs() step must be a WorkflowStep")
    if not isinstance(run, Run):
        raise ValueError("validate_outputs() run must be a Run")

    artifacts = _require_sequence(artifacts, "validate_outputs() artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, Artifact):
            raise ValueError("validate_outputs() artifacts must contain Artifact")

    if run.step_id is None:
        raise ValueError(
            f"run {run.id!r} has no workflow step, so it has no declared outputs; "
            "a skill-targeted Run (SF-A-4 §9) cannot be validated against a step"
        )
    if run.step_id != step.id:
        raise ValueError(
            f"run {run.id!r} targets step {run.step_id!r}, not {step.id!r}; "
            "validate_outputs() checks a step against its own Run"
        )

    for artifact in artifacts:
        if artifact.run_id != run.id or artifact.task_id != run.task_id:
            raise ValueError(
                f"artifact {artifact.id!r} belongs to run {artifact.run_id!r} / "
                f"task {artifact.task_id!r}, not the Run under validation "
                f"({run.id!r} / task {run.task_id!r}); an ExpectedOutput is "
                "satisfied only by the current Run's own artifacts (SF-A-1 §7 -- "
                "the version chain spans Runs)"
            )

    checks = tuple(
        OutputCheck(
            type=output.type,
            required=output.required,
            artifacts=tuple(a for a in artifacts if a.type == output.type),
        )
        for output in step.outputs
    )
    return OutputValidation(checks=checks)
