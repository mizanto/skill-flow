"""Tests for ``skillflow.workspace``.

Two kinds of test, matching ``test_domain.py``:

* **Change-detectors** for cross-issue contracts -- the layout constants, the
  public surface, and the workspace/store boundary ("no entity tables created
  here", "no third-party imports", "no domain import").
* **Behaviour tests** -- root detection, idempotent initialisation, and SQLite
  setup, one per documented rule and edge case.
"""

import ast
import sqlite3
from pathlib import Path

import pytest

from skillflow import workspace
from skillflow.workspace import (
    RepositoryRootNotFoundError,
    SchemaVersionError,
    Workspace,
    WorkspaceError,
    WorkspaceLayoutError,
    connect,
    find_repo_root,
    init_workspace,
)

# --- contract change-detectors ------------------------------------------------


def test_workspace_convention_constants():
    # The .skillflow layout is a cross-issue contract (SF-3 and later depend on
    # it), so assert exact values as a deliberate change-detector.
    assert workspace.WORKSPACE_DIR_NAME == ".skillflow"
    assert workspace.DB_FILE_NAME == "skillflow.db"
    assert workspace.ARTIFACTS_DIR_NAME == "artifacts"
    assert workspace.RUNS_DIR_NAME == "runs"
    assert workspace.OUTPUT_LOG_FILE_NAME == "output.log"


def test_public_surface():
    assert set(workspace.__all__) == {
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
    }


def _imported_modules() -> set[str]:
    tree = ast.parse(Path(workspace.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_workspace_module_imports_stdlib_only():
    # Mirrors test_domain_module_imports_stdlib_only: guards the zero-dependency
    # decision so a later SQLAlchemy import fails loudly here.
    allowed = {"sqlite3", "contextlib", "dataclasses", "pathlib"}
    top_level = {name.split(".")[0] for name in _imported_modules()}
    assert top_level <= allowed, f"unexpected imports: {top_level - allowed}"


def test_workspace_module_does_not_import_domain():
    # SF-3 must not import domain entities: entity persistence is SF-4.
    assert not any(name.startswith("skillflow") for name in _imported_modules())


# --- root detection ---------------------------------------------------------


def test_find_repo_root_from_nested_subdir_with_git_directory(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_accepts_git_file(tmp_path):
    # Worktree / submodule form: .git is a file, not a directory.
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n")
    nested = tmp_path / "pkg"
    nested.mkdir()
    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_accepts_skillflow_without_git(tmp_path):
    (tmp_path / ".skillflow").mkdir()
    nested = tmp_path / "pkg"
    nested.mkdir()
    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_returns_nearest_ancestor(tmp_path):
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "sub"
    inner.mkdir()
    (inner / ".skillflow").mkdir()
    deep = inner / "x"
    deep.mkdir()
    assert find_repo_root(deep) == inner


def test_find_repo_root_stops_at_directory_holding_both_markers(tmp_path):
    # An inner directory that has its own marker is the root even though an
    # outer directory is also a repository -- nearest ancestor wins, and a
    # directory holding both markers resolves to itself, not its parent.
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").mkdir()
    (inner / ".skillflow").mkdir()
    assert find_repo_root(inner) == inner


def test_find_repo_root_raises_when_no_marker(tmp_path):
    # tmp_path lives under the system temp root on macOS and Linux CI, which has
    # no .git / .skillflow above it -- so the walk reaches the filesystem root.
    with pytest.raises(RepositoryRootNotFoundError, match=str(tmp_path.resolve())):
        find_repo_root(tmp_path)


def test_find_repo_root_defaults_to_cwd(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "here"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert find_repo_root() == tmp_path.resolve()


def test_find_repo_root_rejects_non_directory_start(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError):
        find_repo_root(a_file)


# --- workspace initialisation ----------------------------------------------


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_init_workspace_creates_layout(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    assert ws.path.is_dir()
    assert ws.artifacts_dir.is_dir()
    assert ws.runs_dir.is_dir()
    assert ws.db_path.is_file()


def test_workspace_paths_are_built_from_constants(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    assert ws.path == tmp_path / ".skillflow"
    assert ws.db_path == tmp_path / ".skillflow" / "skillflow.db"
    assert ws.artifacts_dir == tmp_path / ".skillflow" / "artifacts"
    assert ws.runs_dir == tmp_path / ".skillflow" / "runs"


def test_run_dir_derives_the_per_run_diagnostics_directory(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    assert ws.run_dir("run-abc") == tmp_path / ".skillflow" / "runs" / "run-abc"


@pytest.mark.parametrize(
    "run_id", ["", "   ", "a/b", "a\\b", "a\x00b", ".", "..", None, 123]
)
def test_run_dir_rejects_non_path_components(tmp_path, run_id):
    ws = init_workspace(_repo(tmp_path))
    with pytest.raises(ValueError, match="path component"):
        ws.run_dir(run_id)


def test_workspace_resolves_root_so_equal_directories_compare_equal(tmp_path):
    # Workspace is a frozen value object: two handles to one directory must be
    # equal regardless of how the path was spelled (symlinks, .., relative).
    repo = _repo(tmp_path)
    unresolved = repo / "sub" / ".."
    (repo / "sub").mkdir()
    assert Workspace(root=unresolved) == init_workspace(repo)
    assert Workspace(root=unresolved).root == repo.resolve()


def test_init_workspace_rejects_non_directory_root(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError):
        init_workspace(a_file)


def test_init_workspace_resolves_root_via_find_repo_root(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sub = repo / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    ws = init_workspace()
    assert ws.root == repo.resolve()
    assert ws.path.is_dir()


def test_init_workspace_is_idempotent_and_preserves_content(tmp_path):
    ws1 = init_workspace(_repo(tmp_path))
    marker = ws1.artifacts_dir / "keep.txt"
    marker.write_text("durable content")

    ws2 = init_workspace(tmp_path)

    assert ws2 == ws1
    assert marker.read_text() == "durable content"


def test_init_workspace_recreates_a_deleted_subdir(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    ws.runs_dir.rmdir()
    assert not ws.runs_dir.exists()
    init_workspace(tmp_path)
    assert ws.runs_dir.is_dir()


def test_init_workspace_raises_when_skillflow_is_a_file(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".skillflow").write_text("not a dir")
    with pytest.raises(WorkspaceLayoutError, match=".skillflow"):
        init_workspace(tmp_path)


def test_init_workspace_raises_when_artifacts_is_a_file(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".skillflow").mkdir()
    (tmp_path / ".skillflow" / "artifacts").write_text("not a dir")
    with pytest.raises(WorkspaceLayoutError, match="artifacts"):
        init_workspace(tmp_path)


# --- database -------------------------------------------------------------


def test_db_is_sqlite_and_stamped_with_schema_version(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    # Open with a bare sqlite3 connection, independent of our helper.
    con = sqlite3.connect(ws.db_path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    assert version == workspace.SCHEMA_VERSION


def test_db_journal_mode_is_wal(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    con = sqlite3.connect(ws.db_path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_connect_returns_usable_connection(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    con = connect(ws)
    try:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert con.row_factory is sqlite3.Row
        con.execute("SELECT 1")
    finally:
        con.close()


def test_connect_on_uninitialised_workspace_raises(tmp_path):
    ws = Workspace(root=_repo(tmp_path))
    with pytest.raises(WorkspaceError, match="does not exist"):
        connect(ws)


def test_schema_version_mismatch_is_rejected(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    con = sqlite3.connect(ws.db_path)
    try:
        con.execute(f"PRAGMA user_version = {workspace.SCHEMA_VERSION + 1}")
        con.commit()
    finally:
        con.close()

    for call in (lambda: init_workspace(tmp_path), lambda: connect(ws)):
        with pytest.raises(SchemaVersionError) as exc_info:
            call()
        message = str(exc_info.value)
        assert str(workspace.SCHEMA_VERSION) in message
        assert str(workspace.SCHEMA_VERSION + 1) in message
        assert str(ws.db_path) in message
        assert "delete" in message.lower()


def test_corrupt_database_is_wrapped_in_workspace_error(tmp_path):
    ws = init_workspace(_repo(tmp_path))
    ws.db_path.write_bytes(b"this is not a sqlite database")

    with pytest.raises(WorkspaceError) as init_exc:
        init_workspace(tmp_path)
    # Full phrase, not the bare verb -- both messages end in "re-initialise".
    assert "cannot initialise workspace database" in str(init_exc.value)
    assert str(ws.db_path) in str(init_exc.value)

    with pytest.raises(WorkspaceError) as connect_exc:
        connect(ws)
    message = str(connect_exc.value)
    # Reported as an open failure, not an init failure.
    assert "cannot open workspace database" in message
    assert "re-initialise" in message  # actionable remedy, per AGENTS.md §13


def test_database_path_blocked_by_directory_is_wrapped(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".skillflow").mkdir()
    (repo / ".skillflow" / "skillflow.db").mkdir()
    with pytest.raises(WorkspaceError) as exc:
        init_workspace(repo)
    assert str(repo / ".skillflow" / "skillflow.db") in str(exc.value)


def test_init_workspace_creates_no_entity_tables(tmp_path):
    # Change-detector guarding the workspace/store boundary: init_workspace
    # builds the container, skillflow.store.open_store builds the tables.
    ws = init_workspace(_repo(tmp_path))
    con = connect(ws)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        con.close()
    assert rows == []
