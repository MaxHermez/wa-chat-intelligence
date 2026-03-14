"""Smoke tests for wain CLI."""

from typer.testing import CliRunner
from wain.cli import app
from wain import __version__

runner = CliRunner()


class TestCLISmoke:
    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "wa-chat-intelligence" in result.output

    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        # Typer's no_args_is_help exits with code 0 or 2 depending on version
        assert "Usage" in result.output

    def test_config_help(self):
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "config" in result.output.lower()

    def test_raw_without_date_errors(self):
        result = runner.invoke(app, ["query", "--raw"])
        assert result.exit_code == 1
        assert "--raw requires --date" in result.output
