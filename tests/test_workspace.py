from skillflow import workspace


def test_workspace_convention_constants():
    # The .skillflow layout is a cross-issue contract (SF-3 and later depend on
    # it), so assert exact values as a deliberate change-detector.
    assert workspace.WORKSPACE_DIR_NAME == ".skillflow"
    assert workspace.DB_FILE_NAME == "skillflow.db"
    assert workspace.ARTIFACTS_DIR_NAME == "artifacts"
    assert workspace.RUNS_DIR_NAME == "runs"
