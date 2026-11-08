# End-to-end plans for whole specs against a fake account, pinned line for line.
#
# Each scenario is a spec file in tests/tundra/golden/ and the state of the account it
# runs against. The spec loader and grants generator run unmodified, and the fake answers
# their queries the way Snowflake does (see tundra_test_utils.fake_account). The expected
# plan is a `.expected` file next to the spec, in the same `[PENDING]` / `[SKIPPED]` form
# the dry-run prints.
#
# These pin tundra's output, so any change to what a spec plans shows up as a diff
# against its expected file.
#
# To rewrite an expected file after an intended change, run with TUNDRA_UPDATE_GOLDEN=1
# and review the diff.


import os
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from tundra.cli import cli
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra_test_utils.fake_account import FakeAccount, FutureGrant, Grant

GOLDEN = Path(__file__).parent / "golden"


def plan_lines(spec: Path, account: FakeAccount, mocker, **options) -> list:
    account.install(mocker)
    loader = SnowflakeSpecLoader(
        str(spec), conn=SnowflakeConnector(), max_workers=1, **options
    )
    commands = loader.generate_permission_queries()
    return [
        f"[{'SKIPPED' if command['already_granted'] else 'PENDING'}] {command['sql']}"
        for command in commands
    ]


def assert_plan(name: str, account: FakeAccount, mocker, **options) -> None:
    actual = plan_lines(GOLDEN / f"{name}.yml", account, mocker, **options)
    expected_file = GOLDEN / f"{name}.expected"
    if os.getenv("TUNDRA_UPDATE_GOLDEN"):
        expected_file.write_text("\n".join(actual) + "\n")
    # Tundra walks some sets while planning, so the order of the plan changes with
    # PYTHONHASHSEED. The lines don't, and they're what's pinned.
    assert sorted(actual) == sorted(expected_file.read_text().splitlines())


def roles_account() -> FakeAccount:
    return FakeAccount(
        warehouses=["REPORTING_WH", "etl-wh"],
        integrations=["S3_INTEGRATION"],
        external_volumes=["ICEBERG_VOLUME"],
        databases=["ANALYTICS", "RAW", "PARTNER_SHARE", "SNOWFLAKE"],
        schemas=[
            "ANALYTICS.MARTS",
            "ANALYTICS.STAGING",
            "ANALYTICS.INFORMATION_SCHEMA",
            "RAW.PUBLIC",
            "RAW.LOADING_ORDERS",
            "RAW.LOADING_CONTACT",
            'RAW."Mixed-Case"',
            "RAW.ARCHIVE",
            "RAW.INFORMATION_SCHEMA",
            "PARTNER_SHARE.PUBLIC",
        ],
        objects={
            "TABLES": [
                "ANALYTICS.MARTS.ORDERS",
                "RAW.PUBLIC.EVENTS",
                "RAW.LOADING_ORDERS.CHARGES",
                "RAW.ARCHIVE.OLD_EVENTS",
            ],
            "VIEWS": ['ANALYTICS.MARTS."Customer View"'],
        },
        roles={
            "LOADER": "SECURITYADMIN",
            "MONITOR": "SECURITYADMIN",
            "TRANSFORMER": "SECURITYADMIN",
            "STALE_ROLE": "SECURITYADMIN",
        },
        database_roles={"ANALYTICS": ["READER"]},
        applications={"SNOWFLAKE": ["CORTEX_USER"]},
        grants=[
            # Held and still in the spec
            Grant("LOADER", "USAGE", "WAREHOUSE", "REPORTING_WH"),
            Grant("LOADER", "USAGE", "DATABASE", "ANALYTICS"),
            Grant("LOADER", "USAGE", "SCHEMA", "ANALYTICS.MARTS"),
            Grant("LOADER", "SELECT", "TABLE", "ANALYTICS.MARTS.ORDERS"),
            Grant("LOADER", "USAGE", "ROLE", "MONITOR"),
            Grant("LOADER", "USAGE", "DATABASE_ROLE", "ANALYTICS.READER"),
            Grant("LOADER", "APPLY TAG", "ACCOUNT", "-"),
            Grant("TRANSFORMER", "OWNERSHIP", "DATABASE", "ANALYTICS"),
            # Held and no longer in the spec
            Grant("LOADER", "USAGE", "SCHEMA", "RAW.ARCHIVE"),
            Grant("LOADER", "SELECT", "TABLE", "RAW.ARCHIVE.OLD_EVENTS"),
            Grant("LOADER", "USAGE", "ROLE", "STALE_ROLE"),
            Grant("LOADER", "CREATE DATABASE", "ACCOUNT", "-"),
            # Snowflake's own grant on a system role: empty grantor, never revoked
            Grant("LOADER", "MONITOR USAGE", "ACCOUNT", "-", granted_by=""),
        ],
        future_grants=[
            FutureGrant("LOADER", "USAGE", "SCHEMA", "ANALYTICS"),
            FutureGrant("LOADER", "SELECT", "TABLE", "ANALYTICS.MARTS"),
            FutureGrant("LOADER", "SELECT", "TABLE", "RAW.ARCHIVE"),
        ],
    )


def users_account() -> FakeAccount:
    return FakeAccount(
        warehouses=["REPORTING_WH"],
        roles={
            "ANALYST": "SECURITYADMIN",
            "MONITOR": "SECURITYADMIN",
            "STALE_ROLE": "SECURITYADMIN",
        },
        users=["HEIDI", "gitlab-ci"],
        user_roles={"HEIDI": ["ANALYST", "STALE_ROLE"]},
        user_tags={
            "HEIDI": {
                "GOVERNANCE.TAGS.TEAM": "marketing",
                "GOVERNANCE.TAGS.COST_CENTRE": "1234",
            }
        },
        grants=[Grant("ANALYST", "USAGE", "WAREHOUSE", "REPORTING_WH")],
    )


def wildcards_account() -> FakeAccount:
    return FakeAccount(
        external_volumes=["ICEBERG_VOLUME", "OLD_VOLUME"],
        databases=["RAW", "PARTNER_SHARE", "OTHER_DB"],
        schemas=["RAW.PUBLIC", "RAW.STAGING", "RAW.INFORMATION_SCHEMA"],
        objects={
            "TABLES": ["RAW.PUBLIC.EVENTS", "RAW.STAGING.LOADS"],
            "TASKS": ["RAW.PUBLIC.NIGHTLY_LOAD"],
        },
        roles={
            "ENGINEER": "SECURITYADMIN",
            "ANALYST": "SECURITYADMIN",
            "ADMIN_ROLE": "SECURITYADMIN",
        },
        users=["ROBOT"],
        grants=[
            Grant("ENGINEER", "USAGE", "EXTERNAL VOLUME", "OLD_VOLUME"),
            Grant("ENGINEER", "USAGE", "DATABASE", "OTHER_DB"),
            Grant("ENGINEER", "IMPORTED PRIVILEGES", "DATABASE", "PARTNER_SHARE"),
            Grant(
                "ENGINEER", "CREATE SHARE", "ACCOUNT", "-", granted_by="ACCOUNTADMIN"
            ),
            Grant("ENGINEER", "OWNERSHIP", "SCHEMA", "RAW.PUBLIC"),
            Grant("ENGINEER", "OWNERSHIP", "TABLE", "RAW.PUBLIC.EVENTS"),
        ],
        future_grants=[FutureGrant("ENGINEER", "SELECT", "TABLE", "RAW")],
    )


def underscored_volumes_account() -> FakeAccount:
    account = wildcards_account()
    account.grants = [
        replace(grant, on="EXTERNAL_VOLUME") if grant.on == "EXTERNAL VOLUME" else grant
        for grant in account.grants
    ]
    return account


def missing_objects_account() -> FakeAccount:
    return FakeAccount(
        warehouses=["REPORTING_WH"],
        databases=["RAW"],
        schemas=["RAW.PUBLIC"],
        objects={"TABLES": ["RAW.PUBLIC.EVENTS"]},
        roles={"READER": "SECURITYADMIN"},
    )


def keyword_schema_account() -> FakeAccount:
    return FakeAccount(
        databases=["LAB_RESULTS"],
        schemas=["LAB_RESULTS.AGGREGATE"],
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "LAB_RESULTS"),
            Grant("LOADER", "USAGE", "SCHEMA", "LAB_RESULTS.AGGREGATE"),
        ],
        future_grants=[
            FutureGrant("LOADER", "SELECT", "TABLE", "LAB_RESULTS.AGGREGATE"),
            FutureGrant("LOADER", "SELECT", "VIEW", "LAB_RESULTS.AGGREGATE"),
        ],
    )


def reserved_schema_account() -> FakeAccount:
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.GROUP"],
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."GROUP"'),
        ],
    )


def mixed_case_schema_account() -> FakeAccount:
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.MIXEDCASE"],
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", "RAW.MIXEDCASE"),
        ],
    )


# Every pinned plan: its spec, the account it's planned against and the loader's
# options. The pinning test runs from this list, so a test that checks something across
# all of them, like test_plan_invariants, sees every plan.
PLANNED = [
    ("roles", roles_account, {}),
    ("users", users_account, {}),
    # Shares and ownership
    ("wildcards", wildcards_account, {}),
    # An external volume grant reported with an underscore is the same grant
    ("wildcards", underscored_volumes_account, {}),
    # Missing objects are left out when ignored
    ("missing_objects", missing_objects_account, {"ignore_missing_objects": True}),
    # A keyword-named schema is left alone when already granted. Regression for
    # lab_results.aggregate: every run re-granted the schema and then revoked its
    # future grants on the quoted form.
    ("keyword_schema", keyword_schema_account, {}),
    # A reserved-word schema resolves to the stored object
    ("reserved_schema", reserved_schema_account, {}),
    # An unquoted mixed-case schema resolves to uppercase
    ("mixed_case_schema", mixed_case_schema_account, {}),
]


@pytest.mark.parametrize(
    "name,account,options",
    PLANNED,
    ids=[account.__name__.removesuffix("_account") for _, account, _ in PLANNED],
)
def test_the_plan_is_pinned(mocker, name, account, options):
    assert_plan(name, account(), mocker, **options)


def test_every_golden_spec_and_account_is_planned():
    assert {name for name, _, _ in PLANNED} == {
        spec.stem for spec in GOLDEN.glob("*.yml")
    }
    # An account left out of PLANNED, or a row dropped from it, would leave a plan
    # pinned nowhere and checked by nothing
    accounts = {
        value
        for key, value in globals().items()
        if key.endswith("_account") and callable(value)
    }
    assert {account for _, account, _ in PLANNED} == accounts


# A run builds one connector, and every phase shares its pool. Between them, these two
# scenarios reach every place that used to build a connector of its own: the loader's
# checks and fetches, member_of "*", schema expansion for schema grants, object listing
# for table and task grants, and apply.
ONE_CONNECTOR_SCENARIOS = [("roles", roles_account), ("wildcards", wildcards_account)]


def connectors_built(account: FakeAccount, mocker) -> list:
    """Install the account, and list every SnowflakeConnector built from then on."""
    account.install(mocker)
    built = []
    mocker.patch.object(
        SnowflakeConnector, "__init__", lambda self, config=None: built.append(self)
    )
    return built


@pytest.mark.parametrize("name,account", ONE_CONNECTOR_SCENARIOS)
def test_planning_builds_one_connector(mocker, name, account):
    built = connectors_built(account(), mocker)

    loader = SnowflakeSpecLoader(str(GOLDEN / f"{name}.yml"), max_workers=1)
    loader.generate_permission_queries()

    assert built == [loader.conn]


def test_loader_checks_called_without_a_connector_use_the_loaders(mocker):
    built = connectors_built(roles_account(), mocker)
    loader = SnowflakeSpecLoader(str(GOLDEN / "roles.yml"), max_workers=1)

    loader.check_entities_on_snowflake_server()
    loader.get_privileges_from_snowflake_server()

    assert built == [loader.conn]


@pytest.mark.parametrize("name,account", ONE_CONNECTOR_SCENARIOS)
def test_cli_run_plans_and_applies_on_one_connector(mocker, monkeypatch, name, account):
    fake = account()
    built = connectors_built(fake, mocker)
    ran_on = set()
    applied = []

    def run_query(conn, query):
        ran_on.add(id(conn))
        if query.startswith(("GRANT ", "REVOKE ", "ALTER ")):
            return None
        return fake.run_query(query)

    # Statements go through the API or SQL as ROUTES says, so each is recorded where
    # the connector runs it, whichever transport takes it from there
    execute = SnowflakeConnector.execute

    def record(conn, operation, **options):
        ran_on.add(id(conn))
        applied.append(operation.sql)
        return execute(conn, operation, **options)

    mocker.patch.object(SnowflakeConnector, "run_query", run_query)
    mocker.patch.object(SnowflakeConnector, "execute", record)
    monkeypatch.setenv("PERMISSION_BOT_MAX_WORKERS", "1")

    result = CliRunner().invoke(cli, ["run", str(GOLDEN / f"{name}.yml")])

    assert result.exit_code == 0, result.output
    assert "[ERROR]" not in result.output
    pending = [
        line.removeprefix("[PENDING] ")
        for line in (GOLDEN / f"{name}.expected").read_text().splitlines()
        if line.startswith("[PENDING] ")
    ]
    assert sorted(applied) == sorted(pending)
    assert len(built) == 1
    assert ran_on == {id(built[0])}
