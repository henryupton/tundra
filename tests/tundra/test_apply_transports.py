# Plans applied through `tundra run` on each transport, against the fake account. On
# rest, the statements ROUTES sends to the API go through FakeApi's write calls and the
# rest run as SQL. On sql, every statement runs as SQL. Both have to end in the state
# that running the plan's SQL one statement at a time gives, and print the same
# [SUCCESS], [ERROR] and [VANISHED] lines.

import re

import pytest
from click.testing import CliRunner

import test_golden_scenarios as golden
from tundra.cli import cli
from tundra.operations import api_capable, routes_to_api
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra_test_utils.fake_account import (
    WRITE_CALLS,
    FakeAccount,
    FakeApi,
    FutureGrant,
)

SCENARIOS = [
    ("roles", golden.roles_account),
    ("users", golden.users_account),
    ("wildcards", golden.wildcards_account),
]


@pytest.fixture(autouse=True)
def one_worker(monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_MAX_WORKERS", "1")


def run(spec, *args):
    return CliRunner().invoke(cli, ["run", *args, str(spec)])


def statuses(output: str) -> list:
    return sorted(line for line in output.splitlines() if line.startswith("["))


def summary(output: str) -> list:
    return [line for line in output.splitlines() if "API-capable statements" in line]


def api_writes(account: FakeAccount) -> list:
    return [call for call in account.api_calls if call[0] in WRITE_CALLS]


def plan(spec, account: FakeAccount, mocker) -> list:
    """The statements tundra plans for a fresh copy of the account, still pending."""
    account.install(mocker)
    loader = SnowflakeSpecLoader(str(spec), conn=SnowflakeConnector(), max_workers=1)
    return [c for c in loader.generate_permission_queries() if not c["already_granted"]]


def applied_as_sql(account: FakeAccount, statements: list) -> dict:
    """The account after each statement's SQL, run in the plan's order."""
    for sql in statements:
        account.run_query(sql)
    return account.state()


@pytest.mark.parametrize("name,account", SCENARIOS)
def test_a_plan_applies_the_same_on_either_transport(
    mocker, read_transport, name, account
):
    spec = golden.GOLDEN / f"{name}.yml"
    pending = plan(spec, account(), mocker)
    fake = account()
    fake.install(mocker)

    result = run(spec)

    assert result.exit_code == 0, result.output
    assert statuses(result.output) == sorted(f"[SUCCESS] {c['sql']};" for c in pending)
    assert fake.state() == applied_as_sql(account(), [c["sql"] for c in pending])

    capable = [c for c in pending if api_capable(c.operation)]
    routed = [c for c in pending if routes_to_api(c.operation)]
    # The users spec plans no statement on an API row yet
    assert bool(routed) is (name != "users")
    if read_transport == "rest":
        assert len(api_writes(fake)) == len(routed)
        assert summary(result.output) == [
            f"  API-capable statements: {len(routed)} through the API,"
            f" {len(capable) - len(routed)} through SQL."
        ]
    else:
        assert api_writes(fake) == []
        assert summary(result.output) == [
            f"  API-capable statements: 0 through the API, {len(capable)} through SQL"
            " (SQL only)."
        ]


@pytest.mark.parametrize(
    "name,account", [scenario for scenario in SCENARIOS if scenario[0] != "users"]
)
def test_a_stalled_write_sends_the_rest_of_the_apply_through_sql(
    mocker, monkeypatch, caplog, stall, read_transport, name, account
):
    spec = golden.GOLDEN / f"{name}.yml"
    pending = plan(spec, account(), mocker)
    expected = applied_as_sql(account(), [c["sql"] for c in pending])
    capable = [c for c in pending if api_capable(c.operation)]
    fake = account()
    fake.install(mocker)
    # Every write the API is sent is held, and made once it's released
    held = stall()
    for call in WRITE_CALLS:
        mocker.patch.object(FakeApi, call, held.then(getattr(FakeApi, call)))
    # The listings keep the default deadline, so only the writes miss theirs
    generate = SnowflakeSpecLoader.generate_permission_queries

    def generate_then_shorten(self, *args, **kwargs):
        commands = generate(self, *args, **kwargs)
        monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "0.01")
        return commands

    mocker.patch.object(
        SnowflakeSpecLoader, "generate_permission_queries", generate_then_shorten
    )

    result = run(spec)

    assert result.exit_code == 0, result.output
    assert statuses(result.output) == sorted(f"[SUCCESS] {c['sql']};" for c in pending)
    assert fake.state() == expected
    stalls = [
        record.getMessage()
        for record in caplog.records
        if "(missed deadline)" in record.getMessage()
    ]
    if read_transport == "rest":
        # Strict mode is on for the reads, and doesn't stop the apply
        assert held.wait_for(1)
        assert len(held.threads) == 1
        (warning,) = stalls
        assert "the API request may still be applied" in warning
        assert summary(result.output) == [
            f"  API-capable statements: 0 through the API, {len(capable)} through SQL"
            " (fallbacks: 1 missed deadline, then SQL for the rest of the run)."
        ]
    else:
        assert held.threads == []
        assert stalls == []
        assert summary(result.output) == [
            f"  API-capable statements: 0 through the API, {len(capable)} through SQL"
            " (SQL only)."
        ]

    # The held request lands once it's released, and repeats what its SQL did
    held.released.set()
    for thread in held.threads:
        thread.join(5)
    assert len(api_writes(fake)) == len(held.threads)
    assert fake.state() == expected


@pytest.mark.parametrize("name,account", SCENARIOS)
def test_a_second_run_has_nothing_left_to_do(mocker, read_transport, name, account):
    spec = golden.GOLDEN / f"{name}.yml"
    fake = account()
    fake.install(mocker)

    assert run(spec).exit_code == 0
    again = run(spec, "--dry")

    # ALTER USER sets a user's properties on every run, as it always has
    assert [
        line
        for line in statuses(again.output)
        if not line.startswith("[PENDING] ALTER USER ") or " TAG " in line
    ] == []
    assert summary(again.output) == []


DROPPED_SPEC = """
version: "1.0"
databases:
  - raw:
      shared: no
roles:
  - loader:
      privileges:
        databases:
          read:
            - raw
        schemas:
          read:
            - raw.landing
            - raw.public
        tables:
          read:
            - raw.landing.*
            - raw.public.*
  - locked:
      privileges:
        databases:
          read:
            - raw
"""


def dropped_account() -> FakeAccount:
    # LOADER's future grant in RAW.OLD isn't in the spec, so the plan revokes it
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.LANDING", "RAW.OLD", "RAW.PUBLIC"],
        objects={"TABLES": ["RAW.LANDING.EVENTS", "RAW.PUBLIC.ORDERS"]},
        roles={"LOADER": "SECURITYADMIN", "LOCKED": "SECURITYADMIN"},
        future_grants=[FutureGrant("LOADER", "SELECT", "TABLE", "RAW.OLD")],
        unmanageable_roles={"LOCKED"},
    )


def test_dropped_and_refused_statements_print_the_same_on_either_transport(
    mocker, read_transport, tmp_path
):
    spec = tmp_path / "roles.yml"
    spec.write_text(DROPPED_SPEC)
    pending = [c["sql"] for c in plan(spec, dropped_account(), mocker)]
    fake = dropped_account()
    fake.install(mocker)

    assert (
        "REVOKE select ON FUTURE tables IN schema raw.old FROM ROLE loader" in pending
    )

    # Something else drops raw.landing and raw.old once the plan is made
    generate = SnowflakeSpecLoader.generate_permission_queries

    def generate_then_drop(self, *args, **kwargs):
        commands = generate(self, *args, **kwargs)
        fake.schemas.remove("RAW.LANDING")
        fake.schemas.remove("RAW.OLD")
        return commands

    mocker.patch.object(
        SnowflakeSpecLoader, "generate_permission_queries", generate_then_drop
    )

    result = run(spec)

    assert result.exit_code == 0, result.output
    expected = sorted(
        (
            "[VANISHED]"
            if "raw.landing" in sql or "raw.old" in sql
            else "[ERROR]" if sql.endswith("ROLE locked") else "[SUCCESS]"
        )
        + f" {sql};"
        for sql in pending
    )
    assert statuses(result.output) == expected
    assert {"[SUCCESS]", "[VANISHED]", "[ERROR]"} == {
        line.split(" ", 1)[0] for line in expected
    }
    error_detail = result.output.splitlines()[
        result.output.splitlines().index(
            "[ERROR] GRANT usage ON database raw TO ROLE locked;"
        )
        + 1
    ]
    if read_transport == "rest":
        # The API's 404s named raw.landing and raw.old in lowercase, and its refusal
        # is one line
        vanished = [c for c in api_writes(fake) if "LANDING" in str(c)]
        assert {c[0] for c in vanished} == {
            "grant_future_privileges",
            "grant_privileges_on_all",
        }
        revoked = [c for c in api_writes(fake) if "OLD" in str(c)]
        assert [c[0] for c in revoked] == ["revoke_future_privileges"]
        assert error_detail == (
            "    003001 (403): Insufficient privileges to operate on role 'LOCKED'."
        )
    else:
        assert api_writes(fake) == []
        assert re.fullmatch(
            "    SQL access control error: Insufficient privileges .*", error_detail
        )


def test_a_dry_run_writes_nothing_and_prints_no_write_summary(mocker, read_transport):
    fake = golden.roles_account()
    fake.install(mocker)
    before = fake.state()

    result = run(golden.GOLDEN / "roles.yml", "--dry")

    assert result.exit_code == 0, result.output
    assert fake.state() == before
    assert api_writes(fake) == []
    assert summary(result.output) == []
