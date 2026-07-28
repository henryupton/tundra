import os

from click.testing import CliRunner

import tundra
from tundra.caching_connector import CachingSnowflakeConnector
from tundra.cli import cli
from tundra.snowflake_connector import SnowflakeConnector


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


def test_run_sets_max_workers_env(mocker, monkeypatch):
    # monkeypatch.delenv(raising=False) alone records no undo action when the
    # var is already absent, so the CLI's direct os.environ write below would
    # still leak past teardown. Seed it first so monkeypatch always has a
    # pre-test value snapshotted to restore (or delete) afterwards.
    monkeypatch.setenv("PERMISSION_BOT_MAX_WORKERS", "unset")
    monkeypatch.delenv("PERMISSION_BOT_MAX_WORKERS")
    captured = {}

    def fake_grants(**kwargs):
        captured["workers"] = os.getenv("PERMISSION_BOT_MAX_WORKERS")

    mocker.patch("tundra.cli.permissions.tundra_grants", side_effect=fake_grants)
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "spec.yml", "--dry", "--max-workers", "12"])

    assert result.exit_code == 0
    assert captured["workers"] == "12"


def test_build_connector_plain_when_no_cache(mocker):
    from tundra.cli.permissions import _build_connector

    mocker.patch(
        "tundra.cli.permissions.SnowflakeConnector",
        return_value=mocker.MagicMock(spec=SnowflakeConnector),
    )
    conn = _build_connector(
        no_cache=True, refresh=False, cache_ttl=3600, cache_path=None
    )
    assert not isinstance(conn, CachingSnowflakeConnector)


def test_build_connector_caches_by_default(mocker, tmp_path):
    from tundra.cli.permissions import _build_connector

    mocker.patch(
        "tundra.cli.permissions.SnowflakeConnector",
        return_value=mocker.MagicMock(spec=SnowflakeConnector),
    )
    conn = _build_connector(
        no_cache=False,
        refresh=False,
        cache_ttl=3600,
        cache_path=str(tmp_path / "c.db"),
    )
    assert isinstance(conn, CachingSnowflakeConnector)
