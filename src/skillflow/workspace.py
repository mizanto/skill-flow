"""The ``.skillflow/`` storage convention and its initialisation.

SkillFlow keeps its durable lifecycle state in a ``.skillflow/`` directory at the
root of the target repository (Persistence v0, SF-A-2 §3). This module names the
locations within that directory and provides the deterministic setup that every
later issue builds on: repository-root detection, idempotent directory creation,
and SQLite database initialisation (SF-3, SF-A-6 §SF-003).

It deliberately stops there. No entity table is created here -- the seven tables
that mirror the domain types are built by :mod:`skillflow.store`, which consumes
:func:`connect`. This module does not import :mod:`skillflow.domain`, so that
boundary stays mechanically checkable.

Layout::

    <repo-root>/.skillflow/
    ├── skillflow.db      # lifecycle state, metadata, events (SQLite)
    ├── artifacts/        # durable artifact content
    └── runs/<run-id>/    # per-run diagnostics (output.log), retained mainly on failure

The ``runs/<run-id>/`` subdirectories are created per Run, not here -- there is
no Run yet. Only the ``runs/`` parent is created.

Workflow Definitions (``workflows/<definition-id>.yaml``) are committed project
source at the repository root, not SkillFlow-owned state: they are named by
:attr:`Workspace.workflows_dir` but never created by :func:`init_workspace`.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WORKSPACE_DIR_NAME",
    "DB_FILE_NAME",
    "ARTIFACTS_DIR_NAME",
    "RUNS_DIR_NAME",
    "OUTPUT_LOG_FILE_NAME",
    "WORKFLOWS_DIR_NAME",
    "SCHEMA_VERSION",
    "WorkspaceError",
    "RepositoryRootNotFoundError",
    "WorkspaceLayoutError",
    "SchemaVersionError",
    "Workspace",
    "find_repo_root",
    "init_workspace",
    "connect",
]

WORKSPACE_DIR_NAME = ".skillflow"
DB_FILE_NAME = "skillflow.db"
ARTIFACTS_DIR_NAME = "artifacts"
RUNS_DIR_NAME = "runs"
#: The per-Run diagnostics file (SF-A-2 §7), written primarily on failure.
#: Diagnostic data, not a domain Artifact.
OUTPUT_LOG_FILE_NAME = "output.log"
#: Directory (at the repository root) holding committed Workflow Definition
#: files (``<definition-id>.yaml``). Named here so ``resolve-task`` can map a
#: Task's ``workflow_definition_id`` to a file; never created by
#: :func:`init_workspace`, because definitions are project source, not
#: SkillFlow-owned state.
WORKFLOWS_DIR_NAME = "workflows"

#: The workspace database layout version, stamped into SQLite ``user_version``.
#:
#: SF-3 stamped ``1`` for a database that had the ``.skillflow/`` container and
#: no tables; SF-4 bumped it to ``2`` when it added the seven entity tables
#: (:mod:`skillflow.store`); SF-5 bumped it to ``3`` when it added the invariant
#: constraints (a partial unique index, ``UNIQUE`` on ``results.run_id``, and
#: composite foreign keys); SF-15 bumped it to ``4`` when it added the Artifact
#: version-uniqueness constraint (``artifacts UNIQUE (task_id, name, version)``).
#: An older database keeps its unconstrained tables because ``open_store`` uses
#: ``CREATE TABLE IF NOT EXISTS``, so it is refused here rather than silently
#: under-enforced. The next bump belongs to whoever next changes the table
#: layout that ships.
#:
#: This number tracks the layout *as released*, not per commit. While a layout
#: change is still unmerged, editing its DDL (adding a ``CHECK``, a column) does
#: not warrant a further bump -- ``store.open_store`` uses
#: ``CREATE TABLE IF NOT EXISTS``, so a database built from an earlier state of
#: the same unmerged change keeps the older table definition until it is deleted
#: and rebuilt. Delete ``.skillflow/skillflow.db`` after any such DDL edit.
#:
#: Pre-1.0 there are no migrations: a database whose stamp does not match is
#: refused by :func:`connect` and :func:`init_workspace` with a
#: :class:`SchemaVersionError`, and the remedy is to delete
#: ``.skillflow/skillflow.db`` and re-initialise. Must stay an ``int`` literal --
#: ``PRAGMA user_version`` cannot be parameterised and the value is interpolated.
SCHEMA_VERSION = 4

# A directory is a repository root if it contains either marker. Order carries
# no meaning -- both are tested at every level and a directory holding either (or
# both) is the root. Precedence between repositories is the nearest-ancestor
# rule in find_repo_root, not this tuple.
_MARKERS = (WORKSPACE_DIR_NAME, ".git")


class WorkspaceError(Exception):
    """A ``.skillflow/`` workspace could not be initialised or opened."""


class RepositoryRootNotFoundError(WorkspaceError):
    """No repository root (a ``.skillflow`` or ``.git`` marker) was found.

    Raised by :func:`find_repo_root` when the walk to the filesystem root turns
    up neither marker. The message names the resolved start path.
    """


class WorkspaceLayoutError(WorkspaceError):
    """A path in the ``.skillflow/`` layout exists but is not a directory.

    Raised instead of letting ``mkdir`` fail with a bare ``FileExistsError`` so
    the message can name the offending path.
    """


class SchemaVersionError(WorkspaceError):
    """The database ``user_version`` does not match :data:`SCHEMA_VERSION`.

    Pre-1.0 there is no migration path. The message names both versions, the
    database path, and the remedy (delete the file and re-initialise).
    """


@dataclass(frozen=True, slots=True)
class Workspace:
    """A located, initialised ``.skillflow/`` workspace.

    Holds the repository ``root`` only; every other path is derived from the
    module constants so callers never rebuild the layout by hand. ``root`` is
    resolved on construction, so two ``Workspace`` values naming the same
    directory compare equal regardless of how the path was spelled.
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def path(self) -> Path:
        """The ``.skillflow/`` directory."""
        return self.root / WORKSPACE_DIR_NAME

    @property
    def db_path(self) -> Path:
        """The SQLite database file."""
        return self.path / DB_FILE_NAME

    @property
    def artifacts_dir(self) -> Path:
        """The durable-artifact content directory."""
        return self.path / ARTIFACTS_DIR_NAME

    @property
    def runs_dir(self) -> Path:
        """The parent directory for per-Run diagnostics."""
        return self.path / RUNS_DIR_NAME

    def run_dir(self, run_id: str) -> Path:
        """Return the ``runs/<run-id>/`` diagnostics directory for ``run_id``.

        Pure path derivation, like every other path here. ``run_id`` becomes
        a path component, so a blank id, a separator, a null byte, or a
        directory entry (``.`` / ``..``) is rejected before any I/O --
        the traversal surface of ``artifacts._artifact_name`` minus the
        drive check, since a database id is not user-supplied display text.
        """
        if (
            not isinstance(run_id, str)
            or not run_id.strip()
            or run_id.strip() in {".", ".."}
            or any(char in run_id for char in ("/", "\\", "\x00"))
        ):
            raise ValueError(f"run id must be a plain path component, not {run_id!r}")
        return self.runs_dir / run_id.strip()

    @property
    def workflows_dir(self) -> Path:
        """The repository-root directory of Workflow Definition files."""
        return self.root / WORKFLOWS_DIR_NAME


def find_repo_root(start: Path | None = None) -> Path:
    """Return the nearest ancestor of ``start`` that looks like a repository root.

    Walks upward from ``start`` (default: the current working directory,
    resolved). A directory qualifies if it contains either ``.skillflow`` or
    ``.git``; ``.git`` is matched by existence, not ``is_dir()``, so git
    worktrees and submodules (where ``.git`` is a file) work. This is
    nearest-ancestor semantics -- the same rule git uses -- so a ``.skillflow/``
    nested inside a git repository (e.g. a submodule) resolves to itself, by
    design.

    Raises :class:`RepositoryRootNotFoundError` if no ancestor qualifies, and
    ``NotADirectoryError`` if ``start`` is not an existing directory.
    """
    start = (start or Path.cwd()).resolve()
    if not start.is_dir():
        raise NotADirectoryError(f"start path is not a directory: {start}")
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _MARKERS):
            return directory
    raise RepositoryRootNotFoundError(
        f"no .skillflow or .git marker found in {start} or any parent directory"
    )


def _ensure_dir(path: Path) -> None:
    """Create ``path`` (and parents) if absent; no-op if it is already a directory.

    Raises :class:`WorkspaceLayoutError` if ``path`` exists as a non-directory.
    """
    if path.exists() and not path.is_dir():
        raise WorkspaceLayoutError(f"expected a directory, found a file: {path}")
    path.mkdir(parents=True, exist_ok=True)


def init_workspace(root: Path | None = None) -> Workspace:
    """Create (or open) the ``.skillflow/`` workspace under ``root``.

    ``root`` defaults to :func:`find_repo_root`'s result. The directory layout
    and the SQLite database are created if missing and left untouched if
    present: calling this twice in a row is safe and preserves existing content.

    Raises ``NotADirectoryError`` if ``root`` is not an existing directory,
    :class:`WorkspaceLayoutError` if a layout path is blocked by a file,
    :class:`SchemaVersionError` if the database was stamped by a newer SkillFlow,
    and :class:`WorkspaceError` if the database file cannot be opened.
    """
    workspace = Workspace(root=root or find_repo_root())
    if not workspace.root.is_dir():
        raise NotADirectoryError(f"root path is not a directory: {workspace.root}")
    _ensure_dir(workspace.path)
    _ensure_dir(workspace.artifacts_dir)
    _ensure_dir(workspace.runs_dir)
    with closing(_open_db(workspace.db_path, create=True)):
        pass
    return workspace


def connect(workspace: Workspace) -> sqlite3.Connection:
    """Open a connection to an already-initialised workspace database.

    Applies the per-connection pragmas (foreign keys on, a busy timeout),
    verifies the schema version, and sets ``row_factory`` to
    :class:`sqlite3.Row`. The caller owns closing the connection
    (``contextlib.closing``). Transaction and isolation semantics are left at the
    :mod:`sqlite3` default -- :mod:`skillflow.store` and its callers own those.

    Raises :class:`WorkspaceError` if the database file does not exist or cannot
    be opened, and :class:`SchemaVersionError` on a version mismatch.
    """
    if not workspace.db_path.exists():
        raise WorkspaceError(
            f"workspace database does not exist: {workspace.db_path} "
            f"(run init_workspace first)"
        )
    conn = _open_db(workspace.db_path, create=False)
    conn.row_factory = sqlite3.Row
    return conn


def _open_db(db_path: Path, *, create: bool) -> sqlite3.Connection:
    """Open ``db_path``, apply pragmas, and reconcile the schema version.

    Shared by :func:`init_workspace` (``create=True``: the file and a fresh
    ``user_version`` of ``0`` are acceptable and get stamped) and :func:`connect`
    (``create=False``: the database must already carry :data:`SCHEMA_VERSION`).
    Any :class:`sqlite3.Error` while opening is wrapped in
    :class:`WorkspaceError` naming the path, the operation (open vs initialise),
    and the delete-and-re-initialise remedy (AGENTS.md principle 13).
    """
    action = "initialise" if create else "open"
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise WorkspaceError(
            f"cannot {action} workspace database {db_path}: {exc}; "
            f"if it is corrupt, delete {db_path} and re-initialise"
        ) from exc
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if create:
            conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if create and version == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"workspace database {db_path} has schema version {version}, "
                f"expected {SCHEMA_VERSION}; delete {db_path} and re-initialise "
                f"(pre-1.0 there is no migration path)"
            )
    except sqlite3.Error as exc:
        conn.close()
        raise WorkspaceError(
            f"cannot {action} workspace database {db_path}: {exc}; "
            f"if it is corrupt, delete {db_path} and re-initialise"
        ) from exc
    except BaseException:
        conn.close()
        raise
    return conn
