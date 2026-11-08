# `-v` and `-vv` only set the log level. `run` prints the statements that are already
# granted only with --print-skipped: as [SKIPPED] when it applies, and as [PENDING]
# with --dry. Runs the roles scenario against the fake account, whose plan has both.

import logging
import re
from collections import Counter

import pytest
from click.testing import CliRunner

import test_golden_scenarios as golden
from tundra.cli import cli
from tundra.logger import GLOBAL_LOGGER
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader

SPEC = golden.GOLDEN / "roles.yml"
STATEMENT = re.compile(r"^(?P<prefix>\+ |  )?\[(?P<status>[A-Z]+)\] (?P<sql>.*);$")
VERBOSITY = [[], ["-v"], ["-vv"]]


@pytest.fixture(autouse=True)
def one_worker(monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_MAX_WORKERS", "1")


@pytest.fixture(autouse=True)
def log_level():
    level = GLOBAL_LOGGER.level
    yield
    GLOBAL_LOGGER.setLevel(level)


@pytest.fixture
def plan(mocker):
    """The scenario's new and already-granted statements, from a fresh account."""
    golden.roles_account().install(mocker)
    loader = SnowflakeSpecLoader(str(SPEC), conn=SnowflakeConnector(), max_workers=1)
    commands = loader.generate_permission_queries()
    new = Counter(c["sql"] for c in commands if not c["already_granted"])
    granted = Counter(c["sql"] for c in commands if c["already_granted"])
    assert new and granted
    return new, granted


def invoke(mocker, verbosity, *args):
    golden.roles_account().install(mocker)
    result = CliRunner().invoke(cli, [*verbosity, "run", *args, str(SPEC)])
    assert result.exit_code == 0, result.output
    return result.output


def run(mocker, verbosity, *args):
    output = invoke(mocker, verbosity, *args)
    return [m for m in map(STATEMENT.match, output.splitlines()) if m]


def prefixed(lines, prefix, status=None):
    return sqls([m for m in lines if m["prefix"] == prefix], status)


def sqls(lines, status=None):
    return Counter(m["sql"] for m in lines if status is None or m["status"] == status)


@pytest.mark.parametrize("verbosity", VERBOSITY)
def test_a_dry_run_prints_only_new_statements_without_the_flag(mocker, plan, verbosity):
    new, _ = plan

    lines = run(mocker, verbosity, "--dry")

    assert sqls(lines, "PENDING") == new
    assert sqls(lines) == new


@pytest.mark.parametrize("verbosity", VERBOSITY)
def test_a_dry_run_prints_already_granted_statements_with_the_flag(
    mocker, plan, verbosity
):
    new, granted = plan

    lines = run(mocker, verbosity, "--dry", "--print-skipped")

    assert sqls(lines, "PENDING") == new + granted
    assert sqls(lines) == new + granted


@pytest.mark.parametrize("verbosity", VERBOSITY)
def test_an_applying_run_prints_no_skipped_statements_without_the_flag(
    mocker, plan, verbosity
):
    new, _ = plan

    lines = run(mocker, verbosity)

    assert sqls(lines, "SKIPPED") == Counter()
    assert sqls(lines) == new


@pytest.mark.parametrize("verbosity", VERBOSITY)
def test_an_applying_run_prints_skipped_statements_with_the_flag(
    mocker, plan, verbosity
):
    new, granted = plan

    lines = run(mocker, verbosity, "--print-skipped")

    assert sqls(lines, "SKIPPED") == granted
    assert sqls(lines) - sqls(lines, "SKIPPED") == new


def test_diff_prefixes_new_and_already_granted_statements(mocker, plan):
    new, granted = plan

    full = run(mocker, [], "--dry", "--diff", "--print-skipped")
    new_only = run(mocker, ["-v"], "--dry", "--diff")

    assert prefixed(full, "+ ") == new
    assert prefixed(full, "  ", "PENDING") == granted
    assert {m["prefix"] for m in new_only} == {"+ "}
    assert sqls(new_only) == new


def test_diff_prefixes_on_an_applying_run(mocker, plan):
    new, granted = plan

    lines = run(mocker, [], "--diff", "--print-skipped")

    assert prefixed(lines, "+ ") == new
    assert prefixed(lines, "  ", "SKIPPED") == granted
    assert sqls(lines) == new + granted


FULL_DIFF = (
    "SQL Commands generated for given spec file"
    " (Full diff with both new and already granted commands):"
)
PLAIN = "SQL Commands generated for given spec file:"


@pytest.mark.parametrize(
    "args,header",
    [
        (["--dry", "--diff", "--print-skipped"], FULL_DIFF),
        (["--diff", "--print-skipped"], FULL_DIFF),
        (["--dry", "--diff"], PLAIN),
        (["--diff"], PLAIN),
        (["--dry", "--print-skipped"], PLAIN),
    ],
)
def test_the_full_diff_header_needs_both_flags(mocker, plan, args, header):
    lines = invoke(mocker, ["-v"], *args).splitlines()

    assert header in lines
    assert ({FULL_DIFF, PLAIN} - {header}).isdisjoint(lines)


@pytest.mark.parametrize(
    "verbosity,level",
    [([], logging.WARNING), (["-v"], logging.INFO), (["-vv"], logging.DEBUG)],
)
def test_verbosity_still_sets_the_log_level(mocker, plan, verbosity, level):
    run(mocker, verbosity, "--dry")

    assert GLOBAL_LOGGER.level == level


def test_help_documents_print_skipped():
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "--print-skipped" in result.output
