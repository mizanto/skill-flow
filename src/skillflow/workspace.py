"""The ``.skillflow/`` storage convention.

SkillFlow keeps its durable lifecycle state in a ``.skillflow/`` directory at the
root of the target repository (Persistence v0, SF-A-2 §3). This module only
*names* the locations within that directory. Repository-root detection,
directory creation, idempotent setup, and SQLite initialisation are SF-3's
deliverables and are intentionally not implemented here.

Layout::

    <repo-root>/.skillflow/
    ├── skillflow.db      # lifecycle state, metadata, events (SQLite)
    ├── artifacts/        # durable artifact content
    └── runs/<run-id>/    # per-run diagnostics (output.log), retained mainly on failure
"""

WORKSPACE_DIR_NAME = ".skillflow"
DB_FILE_NAME = "skillflow.db"
ARTIFACTS_DIR_NAME = "artifacts"
RUNS_DIR_NAME = "runs"
