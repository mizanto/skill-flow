"""Skill Flow ``RunInput`` resolution (SF-19).

One deterministic question, asked by ``resolve-task`` (SF-A-5 §4.1, §4.8) and
answered here:

    Given a Task, the ``running`` Run created for it, the resolved Workflow step,
    and the Task's artifact metadata, what exactly does the new Claude Code
    session receive?

The answer is :class:`RunInput` -- SF-A-5 §4.8's shape, projected flat. It is the
last piece of the first vertical slice: :func:`skillflow.evaluator.evaluate` /
``resolve_initial_action`` pick the action, :func:`skillflow.service.create_run`
persists the ``running`` Run, :func:`skillflow.context.select_context` resolves
the step's declared artifacts, and this module assembles the three into the one
value object handed to the session.

A skill-targeted Run (SF-A-4 §9) takes the second half:
:func:`resolve_skill_run_input` (SF-32, gap-fill) projects the action's skill
with no step-derived parameters and the triggering Run's artifacts as context.
Which half applies is ``resolve-task``'s branch on the resolving action --
or, for a Run rebuilt from stored state (``assignment``, SF-44),
:func:`resolve_stored_run_input`'s branch on the stored ``run.step_id``.

Two boundaries:

* **Projection only -- no history.** ``RunInput`` holds neither the ``Run`` nor
  the ``WorkflowStep``, only the fields §4.8 lists. ``Run`` carries
  ``transcript_ref``, ``triggered_by_run_id``, ``trigger_reason`` and
  status/timestamps; ``transcript_ref`` is precisely the transcript SF-A-5 §3.4
  forbids passing automatically. ``WorkflowStep`` carries ``outcomes`` /
  ``decisions``, which are lifecycle *routing* and have no business inside a
  bounded Run. Projecting rather than holding makes "no accumulated conversation
  history" (SF-19's acceptance criterion) a **structural** property -- there is
  no field that could carry it -- instead of a downstream convention.
  :class:`~skillflow.context.ContextSelection` *is* held whole: it is an existing
  abstraction carrying exactly the right data (declaration-ordered entries,
  ``.artifacts``, and the ``.unresolved`` diagnostic a caller cannot cheaply
  recompute), and flattening it would force the caller to run ``select_context``
  twice.
  Artifacts stay metadata references (SF-A-5 §4.8's ``context.artifacts:
  [artifact_reference]``); content is never inlined.
* **No I/O, no LLM.** This module imports ``dataclasses``, ``collections.abc``
  and ``skillflow`` value objects, and nothing else: no ``sqlite3``, no
  ``pathlib``, no clock, no randomness, no model call -- the same boundary
  :mod:`skillflow.context` / :mod:`skillflow.outputs` / :mod:`skillflow.evaluator`
  hold, pinned by an AST import-boundary test. It projects; it never creates a
  Run, never validates lifecycle state, and never launches anything.

``instructions`` is ``Run.instructions`` verbatim. ``WorkflowStep`` has no such
field and gains none here: adding it would be a workflow-schema change (SF-6's
territory) and a step toward the generic DSL AGENTS.md forbids. Whoever composes
instruction text does so at ``create_run`` time; this module only projects what
the Run already stores.

Deliberately **not** checked here: ``run.status`` (whether the Run is ``running``
is ``resolve-task``'s precondition, SF-A-5 §4.3/§4.9 -- ``validate_outputs`` sets
the same precedent) and ``task.workflow_definition_id`` (``WorkflowStep`` carries
no workflow identity, and "``Workflow.name`` *is* the ``WorkflowDefinition.id``"
is ``service.py``'s persistence convention; ``evaluator.evaluate`` does not
cross-check it either).

No exception class and no enum is defined here: a caller error (bad types,
mismatched ids) raises ``ValueError``, matching ``select_context`` /
``validate_outputs``; the serialization of a ``RunInput`` is an implementation
detail of the command that renders it (SF-A-5 §4.8) and belongs to SF-21.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from skillflow.context import (
    ContextSelection,
    select_context,
    select_trigger_context,
)
from skillflow.domain import Artifact, Run, Task
from skillflow.workflow import ExpectedOutput, WorkflowStep

__all__ = [
    "RunInput",
    "resolve_run_input",
    "resolve_skill_run_input",
    "resolve_stored_run_input",
]


def _require_text(value: object, field_name: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is blank.

    Duplicated from :mod:`skillflow.workflow` / :mod:`skillflow.domain` on
    purpose: keeping this module's "imports only ``dataclasses``, ``collections``
    and ``skillflow`` value objects" boundary mechanically checkable is worth
    three lines, as ``context`` / ``outputs`` already state for
    ``_require_sequence``.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_body(value: object, field_name: str) -> None:
    """Type-check a free-text body without normalising it.

    Mirrors ``domain._require_body``: bodies are content, so they are neither
    stripped nor required to be non-empty -- but they must be strings, or a
    ``None`` renders into the ``RunInput`` a session reads (SF-A-5 §4.8).
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")


def _require_sequence(value: object, field_name: str) -> tuple:
    """Return ``value`` as a tuple, or raise ``ValueError`` if it is not iterable.

    Duplicated from :mod:`skillflow.context` / :mod:`skillflow.outputs` for the
    same reason as :func:`_require_text`. A ``str`` is refused (iterating it
    yields characters, never the intended elements); a non-iterable, notably
    ``None``, is refused too.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field_name} must be an iterable")
    return tuple(value)


@dataclass(frozen=True, kw_only=True, slots=True)
class RunInput:
    """Everything a new bounded Run receives (SF-A-5 §4.8), projected flat.

    Field-for-field, §4.8's ``task.id/title/description`` -> ``task_id`` /
    ``task_title`` / ``task_description``; ``run.id`` -> ``run_id``;
    ``step.id/skill/model/effort`` -> ``step_id`` / ``skill`` / ``model`` /
    ``effort``; ``instructions`` -> ``instructions`` (from ``Run``);
    ``context.artifacts`` -> ``context`` (a ``ContextSelection``); ``outputs`` ->
    ``outputs`` (the step's ``ExpectedOutput`` declarations).

    ``step_id`` is ``None`` for a skill-targeted Run (SF-32, gap-fill for
    SF-A-4 §9): such a Run has no workflow step, so ``skill`` comes from the
    resolving action and ``model`` / ``effort`` / ``outputs`` carry nothing.
    A non-``None`` ``step_id`` is still required to be non-blank.

    No derived properties: ``run_input.context.artifacts`` and
    ``run_input.context.unresolved`` already read well, and aliases would be the
    speculative surface AGENTS.md principles 1 and 6 rule out.
    """

    task_id: str
    task_title: str
    task_description: str
    run_id: str
    step_id: str | None = None
    skill: str
    model: str | None = None
    effort: str | None = None
    instructions: str | None = None
    #: A frozen, slotted instance evaluated once at class creation and shared by
    #: every default -- the pattern ``workflow._EMPTY_MAP`` uses.
    context: ContextSelection = ContextSelection()
    outputs: tuple[ExpectedOutput, ...] = ()

    def __post_init__(self) -> None:
        for name in ("task_id", "task_title", "run_id", "skill"):
            cleaned = _require_text(getattr(self, name), f"RunInput.{name}")
            object.__setattr__(self, name, cleaned)
        if self.step_id is not None:
            object.__setattr__(
                self, "step_id", _require_text(self.step_id, "RunInput.step_id")
            )
        for name in ("model", "effort"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_text(value, f"RunInput.{name}"))

        _require_body(self.task_description, "RunInput.task_description")
        if self.instructions is not None:
            _require_body(self.instructions, "RunInput.instructions")

        if not isinstance(self.context, ContextSelection):
            raise ValueError("RunInput.context must be a ContextSelection")

        outputs = _require_sequence(self.outputs, "RunInput.outputs")
        for output in outputs:
            if not isinstance(output, ExpectedOutput):
                raise ValueError("RunInput.outputs must contain ExpectedOutput")
        object.__setattr__(self, "outputs", outputs)


def resolve_run_input(
    *, task: Task, run: Run, step: WorkflowStep, artifacts: Iterable[Artifact]
) -> RunInput:
    """Return the :class:`RunInput` for ``run``, the Run ``step`` was resolved to.

    The rule order below is the contract: it fixes error precedence (mirroring
    ``select_context`` / ``validate_outputs``).

    1. ``task`` is a ``Task``, ``run`` is a ``Run`` and ``step`` is a
       ``WorkflowStep``, else ``ValueError``.
    2. ``run.task_id == task.id``, else ``ValueError`` naming both ids.
    3. ``run.step_id is None`` -> ``ValueError``. A skill-targeted Run (SF-A-4 §9)
       has no workflow step, so it has no step-derived execution parameters --
       ``skill`` / ``model`` / ``effort`` / ``outputs`` all come from one.
    4. ``run.step_id != step.id`` -> ``ValueError`` naming both ids. Projecting
       one step's parameters onto another step's Run is a caller error.
    5. Delegate context wholly to :func:`skillflow.context.select_context`. Its
       own rules (``artifacts`` an iterable and not a ``str``; every element an
       ``Artifact``; every artifact's ``task_id == task.id``) apply unchanged and
       their ``ValueError``s propagate -- ``artifacts`` is deliberately **not**
       re-validated here, and no new selection rule is introduced.
    6. Build the ``RunInput`` with ``outputs=step.outputs`` and
       ``instructions=run.instructions``.

    Resolving ``run.step_id`` -> ``WorkflowStep`` (``Workflow.find_step``) is the
    caller's job, exactly as it already is for ``select_context`` and
    ``validate_outputs``. A declared context type the Task never produced is not
    an error: it appears in ``run_input.context.unresolved``.
    """
    if not isinstance(task, Task):
        raise ValueError("resolve_run_input() task must be a Task")
    if not isinstance(run, Run):
        raise ValueError("resolve_run_input() run must be a Run")
    if not isinstance(step, WorkflowStep):
        raise ValueError("resolve_run_input() step must be a WorkflowStep")

    if run.task_id != task.id:
        raise ValueError(
            f"run {run.id!r} belongs to task {run.task_id!r}, not task {task.id!r}; "
            "resolve_run_input() projects one Task's Run"
        )
    if run.step_id is None:
        raise ValueError(
            f"run {run.id!r} has no workflow step, so it has no step-derived "
            "execution parameters; a skill-targeted Run (SF-A-4 §9) cannot be "
            "projected onto a step"
        )
    if run.step_id != step.id:
        raise ValueError(
            f"run {run.id!r} targets step {run.step_id!r}, not {step.id!r}; "
            "resolve_run_input() projects a step onto its own Run"
        )

    return RunInput(
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        run_id=run.id,
        step_id=step.id,
        skill=step.skill,
        model=step.model,
        effort=step.effort,
        instructions=run.instructions,
        context=select_context(step=step, task=task, artifacts=artifacts),
        outputs=step.outputs,
    )


def resolve_skill_run_input(
    *, task: Task, run: Run, skill: str, artifacts: Iterable[Artifact]
) -> RunInput:
    """Return the :class:`RunInput` for ``run``, a skill-targeted Run.

    The skill-Run half of ``RunInput`` resolution (SF-32, gap-fill for
    SF-A-4 §9): ``run`` was created from a skill-targeted action and has no
    workflow step, so there are no step-derived execution parameters.
    ``skill`` comes from the resolving action itself, ``model`` / ``effort``
    are ``None``, ``outputs`` is ``()``, and context is the triggering Run's
    artifacts via :func:`skillflow.context.select_trigger_context`.

    The rule order below is the contract: it fixes error precedence
    (mirroring :func:`resolve_run_input`).

    1. ``task`` is a ``Task``, ``run`` is a ``Run`` and ``skill`` is a
       non-blank string, else ``ValueError``.
    2. ``run.task_id == task.id``, else ``ValueError`` naming both ids.
    3. ``run.step_id is not None`` -> ``ValueError``. A step-targeted Run is
       projected by :func:`resolve_run_input`, not here.
    4. Delegate context wholly to
       :func:`skillflow.context.select_trigger_context`. Its own rules apply
       unchanged and their ``ValueError``s propagate -- ``artifacts`` is
       deliberately **not** re-validated here, and no new selection rule is
       introduced.
    5. Build the ``RunInput`` with ``step_id=None``, ``skill=skill``,
       ``model=None``, ``effort=None``, ``outputs=()`` and
       ``instructions=run.instructions``.

    Which half applies -- this function or :func:`resolve_run_input` -- is
    the caller's (``resolve-task``'s) branch on the resolving action, never
    inferred from the Run here beyond rule 3's guard.
    """
    if not isinstance(task, Task):
        raise ValueError("resolve_skill_run_input() task must be a Task")
    if not isinstance(run, Run):
        raise ValueError("resolve_skill_run_input() run must be a Run")
    if not isinstance(skill, str) or not skill.strip():
        raise ValueError("resolve_skill_run_input() skill must be a non-empty string")

    if run.task_id != task.id:
        raise ValueError(
            f"run {run.id!r} belongs to task {run.task_id!r}, not task {task.id!r}; "
            "resolve_skill_run_input() projects one Task's Run"
        )
    if run.step_id is not None:
        raise ValueError(
            f"run {run.id!r} targets step {run.step_id!r}; a step-targeted Run "
            "is projected by resolve_run_input(), not resolve_skill_run_input()"
        )

    return RunInput(
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        run_id=run.id,
        step_id=None,
        skill=skill,
        model=None,
        effort=None,
        instructions=run.instructions,
        context=select_trigger_context(task=task, artifacts=artifacts),
        outputs=(),
    )


def resolve_stored_run_input(
    *,
    task: Task,
    run: Run,
    step: WorkflowStep | None,
    skill: str | None,
    artifacts: Iterable[Artifact],
) -> RunInput:
    """Return the :class:`RunInput` for ``run`` rebuilt from stored state.

    The one shared projection both ``resolve-task`` (SF-20, after creating
    the Run) and ``assignment`` (SF-44, for the workspace's running Run) call,
    so a rebuilt Assignment is the creation-time ``RunInput`` by construction.
    The caller loads whatever the stored Run needs -- the ``WorkflowStep``
    for a step Run, the ``run.created`` skill for a skill-targeted Run, and
    the artifact set (the Task's, or the triggering Run's) -- and this
    function only picks the half and delegates. Loading stays with the
    caller because each command rejects differently when its half cannot be
    loaded (``resolve-task`` carries ``ResolveTaskError`` hints,
    ``assignment`` carries ``AssignmentError`` hints).

    The rule order below is the contract: it fixes error precedence
    (mirroring :func:`resolve_run_input`).

    1. ``task`` is a ``Task`` and ``run`` is a ``Run``, else ``ValueError``.
    2. ``run.step_id is not None`` -> ``step`` must be a ``WorkflowStep``
       with ``step.id == run.step_id``, else ``ValueError``; delegate to
       :func:`resolve_run_input` (``skill`` is ignored on this half).
    3. ``run.step_id is None`` -> ``skill`` must be a non-blank string, else
       ``ValueError``; delegate to :func:`resolve_skill_run_input` (``step``
       is ignored on this half).

    The branch key is the stored ``run.step_id``, never which argument was
    passed: a caller handing a step for a skill Run (or vice versa) is a
    caller error, reported before any projection runs.
    """
    if not isinstance(task, Task):
        raise ValueError("resolve_stored_run_input() task must be a Task")
    if not isinstance(run, Run):
        raise ValueError("resolve_stored_run_input() run must be a Run")

    if run.step_id is not None:
        if not isinstance(step, WorkflowStep):
            raise ValueError(
                f"run {run.id!r} targets step {run.step_id!r}, so a WorkflowStep "
                "is required; resolve_stored_run_input() projects a stored "
                "step Run onto its own step"
            )
        return resolve_run_input(task=task, run=run, step=step, artifacts=artifacts)

    if not isinstance(skill, str) or not skill.strip():
        raise ValueError(
            f"run {run.id!r} has no workflow step, so its skill is required; "
            "resolve_stored_run_input() projects a stored skill Run from its "
            "recorded skill"
        )
    return resolve_skill_run_input(task=task, run=run, skill=skill, artifacts=artifacts)
