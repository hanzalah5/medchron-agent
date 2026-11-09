import pytest

import medchron
from medchron.cli import main


def test_version_is_set() -> None:
    assert medchron.__version__


def test_cli_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "medchron" in capsys.readouterr().out


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert medchron.__version__ in capsys.readouterr().out
