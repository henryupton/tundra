import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tundra.cli import cli
from tundra.entities import EntityGenerator
from tundra.spec_file_loader import load_spec

SPEC = Path(__file__).parents[1] / "specs" / "docs_site"


def output(result):
    return result.output + (result.stderr if result.stderr_bytes else "")


def test_docs_has_generate_and_serve(cli_runner):
    result = cli_runner.invoke(cli, ["docs", "--help"])

    assert result.exit_code == 0
    assert "generate" in result.output
    assert "serve" in result.output


class TestGenerate:
    def test_writes_a_site_from_the_spec_alone(self, cli_runner, tmp_path, mocker):
        connector = mocker.patch("tundra.snowflake_connector.SnowflakeConnector")
        target = tmp_path / "site"

        result = cli_runner.invoke(
            cli, ["docs", "generate", "--spec", str(SPEC), "--target-dir", str(target)]
        )

        assert result.exit_code == 0, output(result)
        catalogue = json.loads((target / "catalogue.json").read_text())
        assert catalogue["metadata"]["live"] is False
        assert catalogue["metadata"]["spec"] == "docs_site"
        assert "role:loader" in catalogue["nodes"]
        assert (target / "index.html").is_file()
        assert str(target / "index.html") in result.output
        connector.assert_not_called()

    def test_target_dir_defaults_to_target(self, cli_runner, tmp_path, pushd):
        pushd(tmp_path)

        result = cli_runner.invoke(cli, ["docs", "generate", "--spec", str(SPEC)])

        assert result.exit_code == 0, output(result)
        assert (tmp_path / "target" / "index.html").is_file()

    def test_a_spec_file_is_named_without_its_extension(self, cli_runner, tmp_path):
        spec = tmp_path / "account.yml"
        spec.write_text("roles:\n  - reader:\n      member_of: [loader]\n")

        result = cli_runner.invoke(
            cli,
            [
                "docs",
                "generate",
                "--spec",
                str(spec),
                "--target-dir",
                str(tmp_path / "site"),
            ],
        )

        assert result.exit_code == 0, output(result)
        catalogue = json.loads((tmp_path / "site" / "catalogue.json").read_text())
        assert catalogue["metadata"]["spec"] == "account"

    def test_a_bad_spec_fails_with_tundras_error(self, cli_runner, tmp_path):
        spec = tmp_path / "spec.yml"
        spec.write_text("roles:\n  - reader:\n      warehouses: [missing_wh]\n")

        result = cli_runner.invoke(
            cli,
            [
                "docs",
                "generate",
                "--spec",
                str(spec),
                "--target-dir",
                str(tmp_path / "site"),
            ],
        )

        assert result.exit_code == 1
        assert "missing_wh" in output(result)
        assert not (tmp_path / "site").exists()

    def test_family_rules_colour_matching_roles(self, cli_runner, tmp_path):
        result = cli_runner.invoke(
            cli,
            [
                "docs",
                "generate",
                "--spec",
                str(SPEC),
                "--target-dir",
                str(tmp_path),
                "--family",
                "Loading roles=load*",
            ],
        )

        assert result.exit_code == 0, output(result)
        catalogue = json.loads((tmp_path / "catalogue.json").read_text())
        assert catalogue["nodes"]["role:loader"]["family"] == "loading-roles"

    def test_a_family_rule_needs_an_equals_sign(self, cli_runner, tmp_path):
        result = cli_runner.invoke(
            cli,
            [
                "docs",
                "generate",
                "--spec",
                str(SPEC),
                "--target-dir",
                str(tmp_path),
                "--family",
                "load*",
            ],
        )

        assert result.exit_code == 2
        assert "--family" in output(result)

    def test_live_overlays_what_the_loader_read(self, cli_runner, tmp_path, mocker):
        spec = load_spec(str(SPEC))

        class Conn:
            def show_roles(self):
                return {}

        loader = SimpleNamespace(
            spec=spec,
            entities=EntityGenerator(spec=spec).inspect_entities(),
            conn=Conn(),
            grants_to_role={},
            roles_granted_to_user={},
            missing_entities={},
        )
        load_live = mocker.patch("tundra.cli.docs.load_live", return_value=loader)

        result = cli_runner.invoke(
            cli,
            [
                "docs",
                "generate",
                "--spec",
                str(SPEC),
                "--target-dir",
                str(tmp_path),
                "--live",
            ],
        )

        assert result.exit_code == 0, output(result)
        load_live.assert_called_once_with(str(SPEC), sql_only=False)
        catalogue = json.loads((tmp_path / "catalogue.json").read_text())
        assert catalogue["metadata"]["live"] is True
        member = [
            e
            for e in catalogue["edges"]
            if (e["source"], e["target"]) == ("user:etl_bot", "role:loader")
        ]
        assert [e["status"] for e in member] == ["spec_only"]


class FakeServer:
    """A server that stops as Ctrl-C stops the real one."""

    started: list = []

    def __init__(self, target, host, port):
        self.target, self.server_address = target, (host, port)
        self.closed = False
        FakeServer.started.append(self)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


@pytest.fixture
def built(cli_runner, tmp_path):
    result = cli_runner.invoke(
        cli, ["docs", "generate", "--spec", str(SPEC), "--target-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, output(result)
    return tmp_path


class TestServe:
    def test_serves_the_site_and_opens_it(self, cli_runner, built, mocker):
        FakeServer.started = []
        mocker.patch("tundra.docs.site.make_server", side_effect=FakeServer)
        browser = mocker.patch("webbrowser.open")

        result = cli_runner.invoke(
            cli, ["docs", "serve", "--target-dir", str(built), "--port", "8123"]
        )

        assert result.exit_code == 0, output(result)
        (server,) = FakeServer.started
        assert Path(server.target) == built
        assert server.server_address == ("127.0.0.1", 8123)
        assert server.closed
        browser.assert_called_once_with("http://127.0.0.1:8123/")
        assert "http://127.0.0.1:8123/" in result.output

    def test_no_browser_leaves_the_browser_alone(self, cli_runner, built, mocker):
        mocker.patch("tundra.docs.site.make_server", side_effect=FakeServer)
        browser = mocker.patch("webbrowser.open")

        result = cli_runner.invoke(
            cli, ["docs", "serve", "--target-dir", str(built), "--no-browser"]
        )

        assert result.exit_code == 0, output(result)
        browser.assert_not_called()
        assert "http://127.0.0.1:8080/" in result.output

    def test_a_target_with_no_site_says_to_generate_one(
        self, cli_runner, tmp_path, mocker
    ):
        make_server = mocker.patch("tundra.docs.site.make_server")

        result = cli_runner.invoke(
            cli, ["docs", "serve", "--target-dir", str(tmp_path)]
        )

        assert result.exit_code == 1
        assert "tundra docs generate" in output(result)
        make_server.assert_not_called()
