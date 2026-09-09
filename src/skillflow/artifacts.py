"""Artifact content storage: the SQLite <-> filesystem boundary (SF-15).

An Artifact is the durable context between Runs (SF-A-1 §5). Its *metadata*
lives in SQLite (:mod:`skillflow.store`) and its *content* lives as a file under
``.skillflow/artifacts/`` (:mod:`skillflow.workspace`). This module is the one
place those two stores are written together, which is why it exists separately
from :mod:`skillflow.service` -- that module's docstring pins "reads no files"
as a maintained boundary, and this operation must break it.

Layout::

    .skillflow/artifacts/
    └── <task_id>/
        ├── plan-v1.md
        ├── plan-v2.md
        └── review-v1.md

``Artifact.path`` stores the relative POSIX string ``"<task_id>/plan-v2.md"``:
never absolute (not portable between checkouts) and never an OS-specific
separator. The filename is derived deterministically from the artifact name, so
``plan.md`` at version 2 becomes ``plan-v2.md`` (SF-A-2 §5) and a name without
an extension becomes ``plan-v2``.

Four boundaries are held deliberately:

1. **This is the only module that writes both stores.** It imports
   :mod:`skillflow.domain`, :mod:`skillflow.store` and
   :mod:`skillflow.workspace`, and nothing else in the package.
2. **Nothing existing is ever mutated.** New content is a *new version*: a new
   row, a new version-stamped file, and ``supersedes_id`` pointing at the
   previous one. There is no ``update_artifact`` and no overwrite -- content
   files are created with exclusive ``open(..., "x")``, so an existing file at
   the target path is an error, never a silent rewrite. The version chain is
   keyed by ``(task_id, name)`` and spans Runs (SF-A-1 §7: ``plan.md`` v1 -> v2
   -> v3 is a Task-level progression, and v2 is usually written by a different
   Run than v1).
3. **No Run is created and nothing is launched.** :func:`create_artifact` only
   *reads* the Run it is addressed by. Claude Code is the execution layer; the
   next Run is never created in the current session (SF-A-3).
4. **Git is not involved.** Versioning is the ``version`` column and the
   version-stamped filename. This module imports no VCS and no ``subprocess``.

**Atomicity across the two stores** (SF-A-5 §6.11: filesystem writes sit outside
the SQLite transaction). The write is ordered so neither store can be left
holding half of it: the file is written *inside* ``with conn:``, so a filesystem
failure rolls the database back, and if the commit itself fails the just-written
file is removed. A hard kill between the write and the commit leaves an orphan
file and no row; the retry reuses it when it holds byte-identical content
(SF-36) and otherwise reports drift. A kill *during* the write may leave a
partial file, which the retry likewise reports as drift -- actionable, never
silent. Drift that survives to a later read surfaces from :func:`read_content`
as an actionable :class:`ArtifactStorageError` rather than as a silent empty
string.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from skillflow import store
from skillflow.domain import Artifact, LifecycleEvent, LifecycleEventType
from skillflow.workspace import OUTPUT_LOG_FILE_NAME, Workspace

__all__ = [
    "ArtifactStorageError",
    "create_artifact",
    "content_path",
    "read_content",
    "register_artifact",
    "write_diagnostics",
]


class ArtifactStorageError(Exception):
    """An Artifact's content file could not be written or read.

    One flat class, matching ``store.InvariantViolationError`` /
    ``service.UnknownWorkflowError``: callers distinguish causes by message, and
    every message names the offending path (AGENTS.md principle 13).
    Deliberately **not** a ``ValueError`` (that stays for caller programming
    errors, as in :mod:`skillflow.domain`) and **not** an ``OSError`` subclass
    (a caller catching this should not also swallow unrelated I/O failures).
    """


# Deliberately duplicated from ``service._new_id`` rather than promoted to a
# shared helper: two lines, and this module's import boundary is part of its
# contract. Same rationale ``workflow._require_text`` records for its copy.
def _new_id(prefix: str) -> str:
    """Return ``f"{prefix}-{uuid4().hex}"``. Collisions are not defended against."""
    return f"{prefix}-{uuid4().hex}"


#: Characters that make an artifact name a path rather than a filename.
_FORBIDDEN_IN_NAME = ("/", "\\", "\x00")


def _artifact_name(value: object) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` if it is not a filename.

    ``name`` is both a database key and a path component, so a plain
    ``domain._require_text`` check is not enough: ``../../etc/passwd`` must not
    be reachable. Anything carrying a separator, a null byte, a drive letter, or
    naming a directory entry (``.`` / ``..``) is rejected before any I/O.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact name must be a non-empty string")
    name = value.strip()
    if (
        name in {".", ".."}
        or any(char in name for char in _FORBIDDEN_IN_NAME)
        or PureWindowsPath(name).drive
    ):
        raise ValueError(
            f"artifact name must be a plain filename, not a path: {name!r}"
        )
    return name


def _versioned_filename(name: str, version: int) -> str:
    """Return ``"plan.md"`` at version 2 as ``"plan-v2.md"`` (SF-A-2 §5).

    ``PurePosixPath`` rather than ``Path`` so the split is identical on every
    platform; ``name`` is separator-free by :func:`_artifact_name`.
    """
    stem = PurePosixPath(name).stem
    suffix = PurePosixPath(name).suffix
    return f"{stem}-v{version}{suffix}"


def content_path(workspace: Workspace, artifact: Artifact) -> Path:
    """Return the absolute path of ``artifact``'s content file.

    ``Artifact.path`` is a relative POSIX string, so it is split on ``/`` rather
    than handed to ``Path``: that keeps the stored value portable on Windows.

    :func:`_artifact_name` guards the *write* path, but an ``Artifact`` read
    back from SQLite (the normal path for later Context Selection) carries
    whatever ``path`` the row holds. This is the mirror of that guard on the
    read side: a ``path`` that resolves outside the artifact store is refused
    rather than followed.
    """
    path = workspace.artifacts_dir.joinpath(*artifact.path.split("/"))
    store_root = workspace.artifacts_dir.resolve()
    if not path.resolve().is_relative_to(store_root):
        raise ArtifactStorageError(
            f"artifact {artifact.id!r} has a path outside the artifact store: "
            f"{artifact.path!r}"
        )
    return path


def _write_new_file(
    path: Path, content: str, *, kind: str = "artifact content file"
) -> bool:
    """Create ``path`` and write ``content``. Never overwrites.

    Returns ``True`` when this call created the file, ``False`` when the file
    already existed holding byte-identical content -- a crashed attempt's
    orphan, reused by the retry (SF-36) rather than rewritten.

    ``"x"`` is exclusive creation: an existing file with *different* content
    means the metadata row and the content file have drifted apart, which is
    reported rather than papered over. ``newline="\\n"`` keeps stored content
    byte-identical across platforms. ``kind`` names what is written in the
    error messages (``"diagnostics file"`` for ``output.log``) so a diagnostics
    failure never reports an artifact path.

    The reuse comparison is on bytes -- ``content.encode("utf-8")`` against
    ``path.read_bytes()``. Comparing decoded text would let universal-newline
    translation false-mismatch content containing ``\\r\\n``.
    """
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError as exc:
        try:
            existing = path.read_bytes()
        except OSError as read_exc:
            raise ArtifactStorageError(
                f"could not read existing {kind} {path}: {read_exc}"
            ) from read_exc
        if existing == content.encode("utf-8"):
            return False
        raise ArtifactStorageError(
            f"{kind} already exists: {path}. An existing file at a "
            "new version's path means the metadata and the content store have "
            "drifted; investigate or delete it rather than overwriting."
        ) from exc
    except OSError as exc:
        raise ArtifactStorageError(f"could not write {kind} {path}: {exc}") from exc
    return True


def register_artifact(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    name: str,
    type: str,
    content: str,
) -> tuple[Artifact, Path | None]:
    """Register a new immutable Artifact version without committing.

    Everything :func:`create_artifact` does except the transaction: insert the
    metadata row and the ``artifact.created`` event, and write the content
    file. Returns the Artifact and the path written -- or ``None`` for the
    path when the file already held these exact bytes (a crashed attempt's
    orphan, reused rather than rewritten).

    The caller owns the transaction and the orphan-file cleanup: if the
    caller's commit fails after this returns, it must unlink the returned
    path when it is not ``None``. A ``None`` path must never be unlinked: the
    file predates this attempt and may belong to another committed or
    in-flight attempt. ``store.latest_artifact`` reads through the same
    connection, so several calls in one uncommitted block still version
    correctly.

    Raises ``LookupError`` for an unknown ``run_id``, ``ValueError`` for a name
    that is not a plain filename, a non-string ``content``, a blank ``type``, or
    a ``type`` that contradicts the existing chain, and
    :class:`ArtifactStorageError` if the content file cannot be written.
    """
    name = _artifact_name(name)
    if not isinstance(content, str):
        raise ValueError("artifact content must be a string")

    run = store.get_run(conn, run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id!r}")

    previous = store.latest_artifact(conn, run.task_id, name)
    version = 1 if previous is None else previous.version + 1
    supersedes_id = None if previous is None else previous.id

    artifact = Artifact(
        id=_new_id("artifact"),
        task_id=run.task_id,
        run_id=run.id,
        name=name,
        type=type,
        version=version,
        path=f"{run.task_id}/{_versioned_filename(name, version)}",
        created_at=datetime.now(UTC),
        supersedes_id=supersedes_id,
    )

    # A name carries one artifact type. A typo here would silently split the
    # chain that later Context Selection looks up, so it is a rejection.
    if previous is not None and previous.type != artifact.type:
        raise ValueError(
            f"artifact {name!r} in task {run.task_id!r} is of type "
            f"{previous.type!r}; refusing to create version {version} as type "
            f"{artifact.type!r}"
        )

    event = LifecycleEvent(
        id=_new_id("event"),
        task_id=artifact.task_id,
        run_id=artifact.run_id,
        type=LifecycleEventType.ARTIFACT_CREATED,
        payload={
            "artifact_id": artifact.id,
            "name": artifact.name,
            "type": artifact.type,
            "version": str(artifact.version),
            "path": artifact.path,
        },
        created_at=artifact.created_at,
    )

    path = content_path(workspace, artifact)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactStorageError(
            f"could not create artifact directory {path.parent}: {exc}"
        ) from exc

    store.insert_artifact(conn, artifact)
    store.insert_lifecycle_event(conn, event)
    created = _write_new_file(path, content)
    return artifact, path if created else None


def create_artifact(
    conn: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    name: str,
    type: str,
    content: str,
) -> Artifact:
    """Create a new immutable Artifact version: content on disk, metadata in SQLite.

    Addressed by ``run_id``; ``task_id`` is derived from the Run, which makes
    "Artifacts link to Runs" structural and the cross-parent invariant
    unreachable by construction. If the Task already has an Artifact under
    ``name``, this is the next version of that chain: ``version`` is the
    previous one plus one and ``supersedes_id`` names it. Nothing about the
    previous row or the previous file is touched.

    The Run's status is deliberately not checked: SF-A-2 §9 keeps artifacts from
    failed Runs, and ``complete-run`` registers artifacts while the Run is still
    ``running``.

    A single-artifact wrapper around :func:`register_artifact`: one transaction
    that commits at block exit, with orphan-file cleanup if the commit fails.
    If the content file already holds these exact bytes (a crashed attempt's
    orphan), it is reused and there is nothing to clean up.

    Raises ``LookupError`` for an unknown ``run_id``, ``ValueError`` for a name
    that is not a plain filename, a non-string ``content``, a blank ``type``, or
    a ``type`` that contradicts the existing chain, and
    :class:`ArtifactStorageError` if the content file cannot be written. Every
    rejection leaves both stores unchanged.
    """
    written: Path | None = None
    try:
        with conn:  # commits at block exit; rolls back on exception
            artifact, written = register_artifact(
                conn,
                workspace,
                run_id=run_id,
                name=name,
                type=type,
                content=content,
            )
    except BaseException:
        if written is not None:
            # The write succeeded but the commit did not: drop the orphan file.
            written.unlink(missing_ok=True)
        raise
    return artifact


def write_diagnostics(
    workspace: Workspace, *, run_id: str, content: str
) -> Path | None:
    """Write ``content`` to ``runs/<run-id>/output.log`` (SF-A-2 §7).

    Diagnostic data, not a domain Artifact: no metadata row, no event, no
    version chain. A Run fails once, so the file is created exclusively --
    an existing ``output.log`` with different content reports drift rather
    than overwriting.

    Returns the path written, or ``None`` when the file already held these
    exact bytes (a crashed attempt's orphan, reused rather than rewritten).
    The caller owns the transaction and the orphan-file cleanup: if the
    caller's commit fails after this returns, it must unlink the returned
    path when it is not ``None`` (the same contract as
    :func:`register_artifact`).

    Raises ``ValueError`` for a ``run_id`` that is not a plain path component
    or a non-string ``content``, and :class:`ArtifactStorageError` if the
    directory or file cannot be written.
    """
    if not isinstance(content, str):
        raise ValueError("diagnostics content must be a string")
    run_dir = workspace.run_dir(run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactStorageError(
            f"could not create diagnostics directory {run_dir}: {exc}"
        ) from exc
    path = run_dir / OUTPUT_LOG_FILE_NAME
    created = _write_new_file(path, content, kind="diagnostics file")
    return path if created else None


def read_content(workspace: Workspace, artifact: Artifact) -> str:
    """Return ``artifact``'s content, or raise :class:`ArtifactStorageError`.

    A missing file means the metadata row and the content store have drifted
    (a crash between the commit and the write, or a deleted file). That is
    reported with both the artifact id and the path, never as an empty string.
    """
    path = content_path(workspace, artifact)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ArtifactStorageError(
            f"artifact {artifact.id!r} has no content file at {path}: its metadata "
            "and content have drifted apart"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtifactStorageError(
            f"could not read artifact content file {path}: {exc}"
        ) from exc
