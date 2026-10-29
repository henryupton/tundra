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


def test_roles(mocker):
    assert_plan("roles", roles_account(), mocker)


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


def test_users(mocker):
    assert_plan("users", users_account(), mocker)


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


def test_wildcards_shares_and_ownership(mocker):
    assert_plan("wildcards", wildcards_account(), mocker)


def test_external_volume_grant_reported_with_an_underscore_is_the_same_grant(mocker):
    account = wildcards_account()
    account.grants = [
        replace(grant, on="EXTERNAL_VOLUME") if grant.on == "EXTERNAL VOLUME" else grant
        for grant in account.grants
    ]
    assert_plan("wildcards", account, mocker)


def test_missing_objects_are_left_out_when_ignored(mocker):
    account = FakeAccount(
        warehouses=["REPORTING_WH"],
        databases=["RAW"],
        schemas=["RAW.PUBLIC"],
        objects={"TABLES": ["RAW.PUBLIC.EVENTS"]},
        roles={"READER": "SECURITYADMIN"},
    )
    assert_plan("missing_objects", account, mocker, ignore_missing_objects=True)


def test_keyword_named_schema_is_left_alone_when_already_granted(mocker):
    # Regression for lab_results.aggregate: every run re-granted the schema and then
    # revoked its future grants on the quoted form.
    account = FakeAccount(
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
    assert_plan("keyword_schema", account, mocker)


def test_reserved_word_schema_resolves_to_the_stored_object(mocker):
    account = FakeAccount(
        databases=["RAW"],
        schemas=["RAW.GROUP"],
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."GROUP"'),
        ],
    )
    assert_plan("reserved_schema", account, mocker)


def test_unquoted_mixed_case_schema_resolves_to_uppercase(mocker):
    account = FakeAccount(
        databases=["RAW"],
        schemas=["RAW.MIXEDCASE"],
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", "RAW.MIXEDCASE"),
        ],
    )
    assert_plan("mixed_case_schema", account, mocker)
