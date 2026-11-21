from typer.testing import CliRunner

from revertbench import __version__
from revertbench.cli import app


def test_version_command_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__
