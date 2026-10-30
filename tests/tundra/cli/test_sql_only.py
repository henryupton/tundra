import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from tundra.cli import cli
from tundra_test_utils.fake_account import FakeAccount, FakeApi, Grant

USERS_SPEC = str(Path(__file__).parents[1] / "golden" / "users.yml")


def users_account(**options) -> FakeAccount:
    return FakeAccount(
        warehouses=["REPORTING_WH"],
        roles={
            "ANALYST": "SECURITYADMIN",
            "MONITOR": "SECURITYADMIN",
            "STALE_ROLE": "SECURITYADMIN",
        },
        users=["HEIDI", "gitlab-ci"],
        user_roles={"HEIDI": ["ANALYST", "STALE_ROLE"]},
        grants=[Grant("ANALYST", "USAGE", "WAREHOUSE", "REPORTING_WH")],
        **options,
    )


def invoke(*args, env=None, color=False):
    return CliRunner().invoke(cli, list(args), env=env, color=color)


def pending(output: str) -> list:
    return sorted(line for line in output.splitlines() if line.startswith("["))


def summary(output: str) -> str:
    (line,) = [line for line in output.splitlines() if "API-capable listings" in line]
    return line


def test_run_reads_through_the_api(mocker):
    account = users_account()
    account.install(mocker)

    result = invoke("run", "--dry", USERS_SPEC)

    assert result.exit_code == 0, result.output
    assert account.api_calls
    assert not any(q.startswith("SHOW GRANTS TO") for q in account.queries)
    assert re.fullmatch(
        r"  API-capable listings: \d+ through the API, 0 through SQL\.",
        summary(result.output),
    )


@pytest.mark.parametrize(
    "args,env",
    [
        (["--sql-only"], None),
        ([], {"PERMISSION_BOT_SQL_ONLY": "true"}),
    ],
    ids=["--sql-only", "PERMISSION_BOT_SQL_ONLY"],
)
def test_run_sql_only_plans_the_same_without_the_api(mocker, args, env):
    rest = users_account()
    rest.install(mocker)
    expected = pending(invoke("run", "--dry", USERS_SPEC).output)
    assert expected

    account = users_account()
    account.install(mocker)
    result = invoke("run", "--dry", *args, USERS_SPEC, env=env)

    assert result.exit_code == 0, result.output
    assert account.api_calls == []
    assert "SHOW GRANTS TO USER heidi" in account.queries
    assert pending(result.output) == expected
    assert re.fullmatch(
        r"  API-capable listings: 0 through the API, \d+ through SQL \(SQL only\)\.",
        summary(result.output),
    )


@pytest.mark.parametrize(
    "args,env",
    [
        (["--sql-only"], None),
        ([], {"PERMISSION_BOT_SQL_ONLY": "true"}),
    ],
    ids=["--sql-only", "PERMISSION_BOT_SQL_ONLY"],
)
def test_spec_test_sql_only(mocker, args, env):
    account = users_account()
    account.install(mocker)

    result = invoke("spec-test", *args, USERS_SPEC, env=env)

    assert result.exit_code == 0, result.output
    assert account.api_calls == []
    assert "SHOW USERS" in account.queries


def test_spec_test_reads_through_the_api(mocker):
    account = users_account()
    account.install(mocker)

    result = invoke("spec-test", USERS_SPEC)

    assert result.exit_code == 0, result.output
    assert ("users",) in account.api_calls
    assert summary(result.output).endswith(" through the API, 0 through SQL.")


WILDCARD_SPEC = """
version: "1.0"
databases:
  - raw:
      shared: no
roles:
  - reader:
      privileges:
        databases:
          read:
            - raw
        schemas:
          read:
            - raw.*
"""


def wildcard_account(**options) -> FakeAccount:
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.LANDING", "RAW.PUBLIC", "RAW.STAGING"],
        roles={"READER": "SECURITYADMIN"},
        **options,
    )


def test_fallbacks_are_reported_after_generation(mocker, tmp_path):
    # raw.* is expanded while the plan is generated, and its schema listing comes
    # back at the cap of 3, so it's read again through SQL
    spec = tmp_path / "roles.yml"
    spec.write_text(WILDCARD_SPEC)
    account = wildcard_account(rest_list_cap=3)
    account.install(mocker)

    result = invoke("run", "--dry", str(spec))

    assert result.exit_code == 0, result.output
    assert ("schemas", "RAW") in account.api_calls
    line = summary(result.output)
    assert line.endswith(
        " through the API, 1 through SQL (fallbacks: 1 at the row limit)."
    )
    assert result.output.index("Generating permission") < result.output.index(line)


@pytest.mark.parametrize(
    "cap,colour", [(10_000, "\x1b[32m"), (3, "\x1b[33m")], ids=["green", "yellow"]
)
def test_the_summary_is_yellow_after_a_fallback(mocker, tmp_path, cap, colour):
    spec = tmp_path / "roles.yml"
    spec.write_text(WILDCARD_SPEC)
    wildcard_account(rest_list_cap=cap).install(mocker)

    result = invoke("run", "--dry", str(spec), color=True)

    assert result.exit_code == 0, result.output
    assert colour + "  API-capable listings: " in result.output


@pytest.mark.parametrize(
    "args", [["run", "--dry"], ["spec-test"]], ids=["run", "spec-test"]
)
def test_strict_mode_fails_a_load_that_falls_back(mocker, args):
    users_account(rest_list_cap=1).install(mocker)

    result = invoke(*args, USERS_SPEC, env={"PERMISSION_BOT_API_STRICT": "1"})

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "PERMISSION_BOT_API_STRICT is set" in result.output


def test_strict_mode_fails_a_plan_that_falls_back(mocker, tmp_path):
    spec = tmp_path / "roles.yml"
    spec.write_text(WILDCARD_SPEC)
    wildcard_account(rest_list_cap=3).install(mocker)

    result = invoke("run", "--dry", str(spec), env={"PERMISSION_BOT_API_STRICT": "1"})

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.output.index("Generating permission") < result.output.index(
        "show_schemas would read through SQL instead of the API (at the row limit)"
    )


@pytest.mark.parametrize(
    "command,timeout",
    [
        # A run's writes have the deadline too
        (
            "run",
            "A listing or statement with no answer after PERMISSION_BOT_API_TIMEOUT"
            " seconds (default 120) goes through SQL, and so does the rest of the run.",
        ),
        (
            "spec-test",
            "A listing with no answer after PERMISSION_BOT_API_TIMEOUT seconds (default"
            " 120) is read through SQL, and so is the rest of the run.",
        ),
    ],
)
def test_help_documents_the_transport_switches(command, timeout):
    result = invoke(command, "--help")

    assert result.exit_code == 0
    for text in (
        "--sql-only",
        "PERMISSION_BOT_SQL_ONLY",
        "PERMISSION_BOT_API_STRICT",
        "PERMISSION_BOT_API_TIMEOUT",
    ):
        assert text in result.output
    assert timeout in " ".join(result.output.split())
    # It points nowhere the reader can't follow: the README has no list of API rows yet
    assert "README" not in result.output


# PERMISSION_BOT_API_TIMEOUT

LISTINGS = (
    "databases",
    "warehouses",
    "users",
    "schemas",
    "database_roles",
    "grants_to_role",
    "future_grants_to_role",
    "grants_to_user",
)

SHORT_DEADLINE = {"PERMISSION_BOT_API_TIMEOUT": "0.01"}


@pytest.fixture
def stalled_api(mocker, stall):
    """Every REST listing held open, as it would be over a stalled connection."""
    held = stall()
    for listing in LISTINGS:
        mocker.patch.object(FakeApi, listing, side_effect=held)
    return held


def test_run_reads_through_sql_after_a_stall(mocker, stalled_api):
    account = users_account()
    account.install(mocker)
    expected = pending(invoke("run", "--dry", "--sql-only", USERS_SPEC).output)

    result = invoke("run", "--dry", USERS_SPEC, env=SHORT_DEADLINE)

    assert result.exit_code == 0, result.output
    assert stalled_api.wait_for(1)
    assert pending(result.output) == expected
    assert re.fullmatch(
        r"  API-capable listings: 0 through the API, \d+ through SQL \(fallbacks: \d+"
        r" missed deadline, then SQL for the rest of the run\)\.",
        summary(result.output),
    )


def test_spec_test_reads_through_sql_after_a_stall(mocker, stalled_api):
    users_account().install(mocker)

    result = invoke("spec-test", USERS_SPEC, env=SHORT_DEADLINE)

    assert result.exit_code == 0, result.output
    assert re.fullmatch(
        r"  API-capable listings: 0 through the API, \d+ through SQL \(fallbacks: \d+"
        r" missed deadline, then SQL for the rest of the run\)\.",
        summary(result.output),
    )


@pytest.mark.parametrize(
    "args", [["run", "--dry"], ["spec-test"]], ids=["run", "spec-test"]
)
def test_strict_mode_fails_a_load_that_stalls(mocker, stalled_api, args):
    users_account().install(mocker)

    result = invoke(
        *args, USERS_SPEC, env={**SHORT_DEADLINE, "PERMISSION_BOT_API_STRICT": "1"}
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    # It names the setting that gives listings longer
    assert (
        "would read through SQL instead of the API (missed deadline of 0.01 s, which"
        " PERMISSION_BOT_API_TIMEOUT sets), and PERMISSION_BOT_API_STRICT is set"
    ) in " ".join(result.output.split())


@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan"])
@pytest.mark.parametrize(
    "args", [["run", "--dry"], ["spec-test"]], ids=["run", "spec-test"]
)
def test_a_bad_timeout_fails_with_one_red_line_before_any_read(mocker, args, value):
    account = users_account()
    account.install(mocker)

    result = invoke(
        *args, USERS_SPEC, env={"PERMISSION_BOT_API_TIMEOUT": value}, color=True
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert [line for line in result.output.splitlines() if "\x1b[31m" in line] == [
        "\x1b[31mPERMISSION_BOT_API_TIMEOUT must be a positive number of seconds,"
        f" not {value!r}\x1b[0m"
    ]
    assert account.queries == [] and account.api_calls == []
