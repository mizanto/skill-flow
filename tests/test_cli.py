import pytest

from skillflow import __version__
from skillflow.cli import main


def test_version_exits_zero_and_prints_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert f"skillflow {__version__}" in capsys.readouterr().out


def test_no_args_returns_zero_and_prints_usage(capsys):
    assert main([]) == 0
    assert "usage: skillflow" in capsys.readouterr().out


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as exc_info:
        main(["definitely-not-a-command"])
    assert exc_info.value.code == 2
