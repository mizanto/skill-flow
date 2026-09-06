import subprocess
import sys

from skillflow import __version__


def test_module_entry_point_starts():
    result = subprocess.run(
        [sys.executable, "-m", "skillflow", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert f"skillflow {__version__}" in result.stdout


def test_module_entry_point_propagates_main_return_value():
    # No args: main() returns 0 through __main__.py's `raise SystemExit(main())`,
    # exercising the return-value propagation path that --version bypasses.
    result = subprocess.run(
        [sys.executable, "-m", "skillflow"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: skillflow" in result.stdout
