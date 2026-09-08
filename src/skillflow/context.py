"""Skill Flow deterministic Context Selection (SF-17).

Skill Flow connects **independent bounded Runs**: each new Run starts in a fresh
Claude Code session with no memory of the previous one, so the only thing
carrying knowledge forward is a deliberately chosen set of durable Artifacts
(SF-A-1 §5, §9). One deterministic question, which SF-20 (``RunInput``) and SF-21
(``resolve-task``) will ask and this module answers:

    Given a Workflow step and a Task's artifact metadata, which artifacts does
    that step declare it needs, each resolved to its latest version?

The answer has two halves, matching SF-A-1 §9's two MVP mechanisms:

* **Explicit declaration** -- ``WorkflowStep.context`` is a flat list of the
  artifact *types* the step consumes. It is the source of truth for "only
  required artifacts are selected"; without it any selection rule is a guess.
* **Deterministic version resolution** -- for each declared type, every distinct
  artifact *name* of that type is resolved to its chain head (highest
  ``version``), and the survivors are ordered by name. Superseded versions are
  never selected. Determinism here is order-independence: the result depends on
  the *set* of inputs, not the sequence they arrive in.

Boundaries, mirroring :mod:`skillflow.outputs` and :mod:`skillflow.evaluator`:

* **Task scope.** Unlike ``outputs.py``'s per-Run match, selection ranges over
  every artifact the Task has produced, across Runs -- the version chain is keyed
  by ``(task_id, name)`` and spans Runs (SF-A-1 §7), which is the whole point: an
  ``implementation`` Run after ``changes_requested`` must receive the previous
  Run's ``review`` (SF-A-1 §5). An artifact belonging to another Task is a
  caller error (``ValueError``); an artifact from another *Run* of the same Task
  is selected.
* **Metadata only -- no content.** Selection returns artifact metadata
  references (SF-A-5 §4.8's ``context.artifacts: [artifact_reference]``), never
  inlined bytes. Reading a file stays ``artifacts.read_content`` at the caller's
  choice.
* **No I/O, no LLM.** This module imports ``dataclasses``, ``collections.abc``
  and ``skillflow`` value objects, and nothing else: no ``sqlite3``, no
  ``pathlib``, no clock, no randomness, no model call -- the same boundary
  ``evaluator`` / ``outputs`` hold, pinned by an AST import-boundary test. It
  selects; it never creates a Run and never launches anything.

No exception class is defined here (mirroring ``outputs.py``): an unresolved type
is a normal, reportable outcome, not an error. A caller error -- bad types, a
foreign artifact -- raises ``ValueError``, matching ``evaluator``'s convention.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from skillflow.domain import Artifact, Task
from skillflow.workflow import WorkflowStep

__all__ = ["ContextEntry", "ContextSelection", "select_context"]


def _require_sequence(value: object, field_name: str) -> tuple:
    """Return ``value`` as a tuple, or raise ``ValueError`` if it is not iterable.

    Duplicated from :mod:`skillflow.outputs` / :mod:`skillflow.workflow` on
    purpose: keeping this module's "imports only ``dataclasses``,
    ``collections`` and ``skillflow`` value objects" boundary mechanically
    checkable is worth three lines. A ``str`` is refused (iterating it yields
    characters, never the intended elements); a non-iterable, notably ``None``,
    is refused too.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field_name} must be an iterable")
    return tuple(value)


@dataclass(frozen=True, kw_only=True, slots=True)
class ContextEntry:
    """One declared context type and the artifacts resolved for it.

    ``artifacts`` is the chain head of each distinct name of ``type`` the Task
    has produced, ordered by name. It is empty when the Task has produced no
    artifact of that type -- a legitimate state (a first-pass ``implementation``
    Run has no ``review``), reported through :attr:`ContextSelection.unresolved`.
    """

    type: str
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("ContextEntry.type must be a non-empty string")
        artifacts = _require_sequence(self.artifacts, "ContextEntry.artifacts")
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise ValueError("ContextEntry.artifacts must contain Artifact")
        object.__setattr__(self, "artifacts", artifacts)

    @property
    def resolved(self) -> bool:
        """``True`` iff at least one artifact was resolved for this type."""
        return bool(self.artifacts)


@dataclass(frozen=True, kw_only=True, slots=True)
class ContextSelection:
    """The step's selected context: one :class:`ContextEntry` per declared type.

    Entries follow ``WorkflowStep.context`` declaration order. Two derived
    properties: :attr:`artifacts` flattens the selection for a caller that just
    wants the references, and :attr:`unresolved` names the declared types the
    Task cannot satisfy -- the one diagnostic a caller cannot cheaply recompute.
    """

    entries: tuple[ContextEntry, ...] = ()

    def __post_init__(self) -> None:
        entries = _require_sequence(self.entries, "ContextSelection.entries")
        for entry in entries:
            if not isinstance(entry, ContextEntry):
                raise ValueError("ContextSelection.entries must contain ContextEntry")
        object.__setattr__(self, "entries", entries)

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        """Every selected artifact, in declaration order then by name."""
        return tuple(a for entry in self.entries for a in entry.artifacts)

    @property
    def unresolved(self) -> tuple[str, ...]:
        """Declared types with no matching artifact, in declaration order."""
        return tuple(entry.type for entry in self.entries if not entry.resolved)


def _latest_per_name(
    artifacts: tuple[Artifact, ...], declared_type: str
) -> tuple[Artifact, ...]:
    """Chain head of each name of ``declared_type``, ordered by name.

    ``UNIQUE (task_id, name, version)`` (SF-A-2 §5) makes "highest version per
    name" tie-free, so no secondary key is needed.
    """
    heads: dict[str, Artifact] = {}
    for artifact in artifacts:
        if artifact.type != declared_type:
            continue
        current = heads.get(artifact.name)
        if current is None or artifact.version > current.version:
            heads[artifact.name] = artifact
    return tuple(heads[name] for name in sorted(heads))


def select_context(
    *, step: WorkflowStep, task: Task, artifacts: Iterable[Artifact]
) -> ContextSelection:
    """Return the context ``step`` declares, resolved against ``task``'s artifacts.

    The rule order below is the contract: it fixes error precedence (mirroring
    ``evaluate`` / ``validate_outputs``).

    1. ``step`` is a ``WorkflowStep`` and ``task`` is a ``Task``, else
       ``ValueError``.
    2. ``artifacts`` is an iterable and not a ``str``, else ``ValueError``; every
       element is an ``Artifact``, else ``ValueError``.
    3. Every artifact has ``task_id == task.id``, else ``ValueError`` naming the
       artifact and both ids. ``run_id`` is deliberately **not** checked -- Task
       scope is the point.
    4. For each type in ``step.context``, **in declaration order**: keep the
       highest ``version`` per distinct ``name`` among the artifacts of that
       type, ordered by ``name``.
    5. ``step.context == ()`` -> ``ContextSelection(entries=())``.

    A declared type the Task has never produced is not an error: its entry is
    empty and it appears in :attr:`ContextSelection.unresolved`.
    """
    if not isinstance(step, WorkflowStep):
        raise ValueError("select_context() step must be a WorkflowStep")
    if not isinstance(task, Task):
        raise ValueError("select_context() task must be a Task")

    artifacts = _require_sequence(artifacts, "select_context() artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, Artifact):
            raise ValueError("select_context() artifacts must contain Artifact")

    for artifact in artifacts:
        if artifact.task_id != task.id:
            raise ValueError(
                f"artifact {artifact.id!r} belongs to task {artifact.task_id!r}, "
                f"not task {task.id!r}; select_context() resolves a step's "
                "declared context against one Task's artifacts (SF-A-1 §7 -- the "
                "version chain spans Runs of the same Task)"
            )

    entries = tuple(
        ContextEntry(
            type=declared_type,
            artifacts=_latest_per_name(artifacts, declared_type),
        )
        for declared_type in step.context
    )
    return ContextSelection(entries=entries)
