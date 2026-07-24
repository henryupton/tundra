import os

from click.testing import CliRunner

import tundra
from tundra.cli import cli


def test_version(cli_runner):
    cli_version = cli_runner.invoke(cli, ["--version"])

    assert (
        cli_version.output
        == f"tundra {tundra.__version__} - Snowflake permissions with Iceberg table support\n"
    )


def test_run_command(cli_runner):
    cli_run_command = cli_runner.invoke(cli.commands["run"], ["--help"])

    cli_output = cli_run_command.output
    assert (len(cli_output) >= 5) and (cli_output[:5] == "Usage")


def test_load_command(cli_runner):
    cli_spec_test_command = cli_runner.invoke(cli.commands["spec-test"], ["--help"])

    cli_output = cli_spec_test_command.output
    assert (len(cli_output) >= 5) and (cli_output[:5] == "Usage")


def test_run_sets_max_workers_env(mocker):
    captured = {}

    def fake_grants(**kwargs):
        captured["workers"] = os.getenv("PERMISSION_BOT_MAX_WORKERS")

    mocker.patch("tundra.cli.permissions.tundra_grants", side_effect=fake_grants)
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "spec.yml", "--dry", "--max-workers", "12"])

    assert result.exit_code == 0
    assert captured["workers"] == "12"
