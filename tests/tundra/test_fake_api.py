# FakeApi spells names the way the dev probe's REST rows do. tundra's decoder reads a
# quoted part and a bare one alike, so a fake that drifted (quoting every part, say)
# would leave the REST tests passing, and its bare parts are what catch ObjectName() or
# ObjectName.single() used on a REST field.

import re

import pytest
from snowflake.core.exceptions import APIError, NotFoundError

from tundra.identifiers import ObjectName
from tundra.operations import (
    ALL,
    FUTURE,
    GRANT,
    REVOKE,
    BulkPrivileges,
    Privileges,
    RoleMembership,
    UserTag,
)
from tundra_test_utils.fake_account import (
    WRITE_CALLS,
    FakeAccount,
    FakeApi,
    FutureGrant,
    Grant,
    SnowflakeError,
    api_error,
)

# (the grant in the fake's state, the REST row the probe recorded for it, created_on
# left out). The account row carries the fake's locator where the probe had its own.
GRANTS = [
    (
        Grant(
            "LOADER",
            "CREATE EVENT TABLE",
            "SCHEMA",
            'LAB_RESULTS."AGGREGATE"',
            granted_by="SYSADMIN",
        ),
        {
            "securable": {"database": "LAB_RESULTS", "name": "AGGREGATE"},
            "securable_type": "SCHEMA",
            "grant_option": False,
            "privileges": ["CREATE EVENT TABLE"],
            "granted_by": "SYSADMIN",
        },
    ),
    (
        Grant(
            "LOADER",
            "OWNERSHIP",
            "ROLE",
            '"ORG-WIDE-DEV-VIEWER"',
            granted_by="SECURITY_OFFICER",
            grant_option=True,
        ),
        {
            "securable": {"name": '"ORG-WIDE-DEV-VIEWER"'},
            "securable_type": "ROLE",
            "grant_option": True,
            "privileges": ["OWNERSHIP"],
            "granted_by": "SECURITY_OFFICER",
        },
    ),
    (
        Grant(
            "LOADER",
            "SELECT",
            "ICEBERG_TABLE",
            "RAW.WIDGET_CATALOG_ITEMS.ARCHIVED_SNAPSHOT",
            granted_by="SYSADMIN",
        ),
        {
            "securable": {
                "database": "RAW",
                "schema": "WIDGET_CATALOG_ITEMS",
                "name": "ARCHIVED_SNAPSHOT",
            },
            "securable_type": "ICEBERG TABLE",
            "grant_option": False,
            "privileges": ["SELECT"],
            "granted_by": "SYSADMIN",
        },
    ),
    (
        Grant(
            "LOADER",
            "USAGE",
            "DATABASE_ROLE",
            "SNOWFLAKE.GOVERNANCE_VIEWER",
            granted_by="",
        ),
        {
            "securable": {"database": "SNOWFLAKE", "name": "GOVERNANCE_VIEWER"},
            "securable_type": "DATABASE ROLE",
            "grant_option": False,
            "privileges": ["USAGE"],
            "granted_by": "",
        },
    ),
    (
        Grant(
            "LOADER",
            "USAGE",
            "APPLICATION_ROLE",
            'SNOWFLAKE."CORTEX-MODEL-ROLE-ALL"',
            granted_by="ACCOUNTADMIN",
        ),
        {
            "securable": {"database": "SNOWFLAKE", "name": '"CORTEX-MODEL-ROLE-ALL"'},
            "securable_type": "APPLICATION ROLE",
            "grant_option": False,
            "privileges": ["USAGE"],
            "granted_by": "ACCOUNTADMIN",
        },
    ),
    (
        Grant(
            "LOADER",
            "USAGE",
            "EXTERNAL_VOLUME",
            "DATA_LAKE_ICEBERG_VOL",
            granted_by="ACCOUNTADMIN",
        ),
        {
            "securable": {"name": "DATA_LAKE_ICEBERG_VOL"},
            "securable_type": "EXTERNAL VOLUME",
            "grant_option": False,
            "privileges": ["USAGE"],
            "granted_by": "ACCOUNTADMIN",
        },
    ),
    (
        Grant(
            "LOADER",
            "APPLY AGGREGATION POLICY",
            "ACCOUNT",
            "-",
            granted_by="",
            grant_option=True,
        ),
        {
            "securable": {"name": "AB12345"},
            "securable_type": "ACCOUNT",
            "grant_option": True,
            "privileges": ["APPLY AGGREGATION POLICY"],
            "granted_by": "",
        },
    ),
]

FUTURE_GRANTS = [
    (
        FutureGrant("LOADER", "SELECT", "DYNAMIC_TABLE", "ANALYTICS.DRAFT_ZONE"),
        {
            "securable": {
                "database": "ANALYTICS",
                "schema": "DRAFT_ZONE",
                "name": '"<DYNAMIC_TABLE>"',
            },
            "securable_type": "DYNAMIC TABLE",
            "grant_option": False,
            "privileges": ["SELECT"],
        },
    ),
    (
        FutureGrant("LOADER", "SELECT", "ICEBERG_TABLE", "ANALYTICS.DIVISION"),
        {
            "securable": {
                "database": "ANALYTICS",
                "schema": "DIVISION",
                "name": '"<ICEBERG_TABLE>"',
            },
            "securable_type": "ICEBERG TABLE",
            "grant_option": False,
            "privileges": ["SELECT"],
        },
    ),
    # From the write probe: a future grant in a keyword schema
    (
        FutureGrant("LOADER", "SELECT", "TABLE", 'LAB_RESULTS."AGGREGATE"'),
        {
            "securable": {
                "database": "LAB_RESULTS",
                "schema": "AGGREGATE",
                "name": '"<TABLE>"',
            },
            "securable_type": "TABLE",
            "grant_option": False,
            "privileges": ["SELECT"],
        },
    ),
]


def rows(models):
    return [model.to_dict() for model in models]


@pytest.mark.parametrize(
    "mixed_case", [False, True], ids=["bare-uppercase", "bare-identifiers"]
)
def test_grants_are_spelt_as_the_probe_recorded(mixed_case):
    account = FakeAccount(
        roles={"LOADER": "SECURITYADMIN"},
        grants=[grant for grant, _ in GRANTS],
        future_grants=[grant for grant, _ in FUTURE_GRANTS],
        rest_bare_mixed_case=mixed_case,
    )
    api = FakeApi(account)

    assert rows(api.grants_to_role("LOADER")) == [row for _, row in GRANTS]
    assert rows(api.future_grants_to_role("LOADER")) == [
        row for _, row in FUTURE_GRANTS
    ]


@pytest.mark.parametrize(
    "mixed_case,schemas,role",
    [
        (
            False,
            ["AGGREGATE", "GROUP", '"Mixed-Case"', '"lower_case"', '"CamelCase"'],
            '"lower_role"',
        ),
        (
            True,
            ["AGGREGATE", "GROUP", '"Mixed-Case"', "lower_case", "CamelCase"],
            "lower_role",
        ),
    ],
    ids=["bare-uppercase", "bare-identifiers"],
)
def test_names_are_bare_unless_they_must_be_quoted(mixed_case, schemas, role):
    account = FakeAccount(
        schemas=[
            "RAW.AGGREGATE",
            'RAW."GROUP"',
            'RAW."Mixed-Case"',
            'RAW."lower_case"',
            'RAW."CamelCase"',
        ],
        roles={'"lower_role"': "SECURITYADMIN"},
        user_roles={"HEIDI": ['"lower_role"']},
        rest_bare_mixed_case=mixed_case,
    )
    api = FakeApi(account)

    assert [schema.name for schema in api.schemas("RAW")] == schemas
    assert rows(api.grants_to_user("HEIDI")) == [
        {"securable": {"name": role}, "securable_type": "ROLE"}
    ]


# Writes: each adapter call changes the account the way its statement's SQL does

LOADER = ObjectName.single("loader")


def write_account() -> FakeAccount:
    return FakeAccount(
        warehouses=["REPORTING_WH", "etl-wh"],
        integrations=["S3_INT"],
        external_volumes=["ICEBERG_VOLUME"],
        databases=["RAW", "SNOWFLAKE"],
        schemas=["RAW.PUBLIC", 'RAW."Mixed-Case"'],
        objects={
            "TABLES": ["RAW.PUBLIC.EVENTS", 'RAW."Mixed-Case"."Events.2024"'],
            "VIEWS": ["RAW.PUBLIC.EVENTS_V"],
            "ICEBERG TABLES": ["RAW.PUBLIC.LAKE"],
            "DYNAMIC TABLES": ["RAW.PUBLIC.ROLLUP"],
            "STREAMLITS": ["RAW.PUBLIC.APP"],
            "SEMANTIC VIEWS": ["RAW.PUBLIC.MODEL"],
            "TASKS": ["RAW.PUBLIC.NIGHTLY"],
        },
        roles={
            "LOADER": "SECURITYADMIN",
            "ANALYST": "SECURITYADMIN",
            "org-wide-viewer": "SECURITYADMIN",
        },
        database_roles={"SNOWFLAKE": ["USAGE_VIEWER"]},
        users=["jane.doe@example.com", "JANE"],
        grants=[
            Grant("LOADER", "OPERATE", "WAREHOUSE", "REPORTING_WH"),
            Grant("LOADER", "USAGE", "EXTERNAL VOLUME", "ICEBERG_VOLUME"),
            Grant("LOADER", "EXECUTE TASK", "ACCOUNT", "-"),
            Grant("LOADER", "USAGE", "DATABASE", "SNOWFLAKE"),
            Grant("LOADER", "MONITOR", "DATABASE", "RAW"),
            Grant("LOADER", "CREATE SCHEMA", "DATABASE", "RAW"),
            Grant("LOADER", "SELECT", "TABLE", 'RAW."Mixed-Case"."Events.2024"'),
            Grant("LOADER", "USAGE", "ROLE", '"org-wide-viewer"'),
            Grant("LOADER", "USAGE", "DATABASE_ROLE", "SNOWFLAKE.USAGE_VIEWER"),
        ],
        future_grants=[
            FutureGrant("LOADER", "USAGE", "SCHEMA", "RAW"),
            FutureGrant("LOADER", "SELECT", "SEMANTIC_VIEW", "RAW.PUBLIC"),
        ],
        user_roles={"jane.doe@example.com": ["ANALYST"]},
        user_tags={"JANE": {"GOVERNANCE.TAGS.TEAM": "MARKETING"}},
    )


WRITES = [
    Privileges(GRANT, ("usage",), "warehouse", ObjectName.single("etl-wh"), LOADER),
    Privileges(
        REVOKE, ("operate",), "warehouse", ObjectName.single("reporting_wh"), LOADER
    ),
    Privileges(GRANT, ("usage",), "integration", ObjectName.single("s3_int"), LOADER),
    Privileges(
        REVOKE,
        ("usage",),
        "external volume",
        ObjectName.single("iceberg_volume"),
        LOADER,
    ),
    Privileges(GRANT, ("apply tag",), "account", None, LOADER),
    Privileges(REVOKE, ("execute task",), "account", None, LOADER),
    Privileges(
        GRANT,
        ("usage", "monitor", "create schema"),
        "database",
        ObjectName.single("raw"),
        LOADER,
    ),
    Privileges(
        REVOKE,
        ("monitor", "create schema"),
        "database",
        ObjectName.single("raw"),
        LOADER,
    ),
    Privileges(
        GRANT,
        ("imported privileges",),
        "database",
        ObjectName.single("snowflake"),
        ObjectName.single("analyst"),
    ),
    Privileges(
        REVOKE,
        ("imported privileges",),
        "database",
        ObjectName.single("snowflake"),
        LOADER,
    ),
    Privileges(GRANT, ("usage",), "schema", ObjectName('raw."Mixed-Case"'), LOADER),
    *[
        Privileges(GRANT, ("select",), kind, ObjectName(f"raw.public.{name}"), LOADER)
        for kind, name in [
            ("table", "events"),
            ("view", "events_v"),
            ("iceberg table", "lake"),
            ("dynamic table", "rollup"),
            ("semantic view", "model"),
        ]
    ],
    Privileges(GRANT, ("usage",), "streamlit", ObjectName("raw.public.app"), LOADER),
    Privileges(GRANT, ("monitor",), "task", ObjectName("raw.public.nightly"), LOADER),
    Privileges(
        REVOKE,
        ("select",),
        "table",
        ObjectName('raw."Mixed-Case"."Events.2024"'),
        LOADER,
    ),
    BulkPrivileges(GRANT, ALL, ("select",), "table", ObjectName("raw"), LOADER),
    BulkPrivileges(
        GRANT,
        ALL,
        ("select", "insert"),
        "table",
        ObjectName('raw."Mixed-Case"'),
        LOADER,
    ),
    BulkPrivileges(
        GRANT, FUTURE, ("select",), "iceberg table", ObjectName("raw.public"), LOADER
    ),
    BulkPrivileges(
        GRANT,
        FUTURE,
        ("usage",),
        "schema",
        ObjectName("raw"),
        ObjectName.single("analyst"),
    ),
    BulkPrivileges(REVOKE, FUTURE, ("usage",), "schema", ObjectName("raw"), LOADER),
    BulkPrivileges(
        REVOKE, FUTURE, ("select",), "semantic view", ObjectName("raw.public"), LOADER
    ),
    RoleMembership(GRANT, ObjectName.single("analyst"), "role", LOADER, "role"),
    RoleMembership(
        REVOKE, ObjectName.single("org-wide-viewer"), "role", LOADER, "role"
    ),
    RoleMembership(
        GRANT,
        ObjectName.role_reference("snowflake.usage_viewer"),
        "database role",
        ObjectName.single("analyst"),
        "role",
    ),
    RoleMembership(
        REVOKE,
        ObjectName.role_reference("snowflake.usage_viewer"),
        "database role",
        LOADER,
        "role",
    ),
    RoleMembership(
        GRANT,
        ObjectName.single("org-wide-viewer"),
        "role",
        ObjectName.single("jane.doe@example.com"),
        "user",
    ),
    RoleMembership(
        REVOKE,
        ObjectName.single("analyst"),
        "role",
        ObjectName.single("jane.doe@example.com"),
        "user",
    ),
    UserTag(
        "set", ObjectName.single("jane"), ObjectName("governance.tags.team"), "O'NEIL"
    ),
    UserTag(
        "set", ObjectName.single("jane"), ObjectName("governance.tags.tier"), "GOLD"
    ),
    UserTag("unset", ObjectName.single("jane"), ObjectName("governance.tags.team")),
]


@pytest.mark.parametrize("operation", WRITES, ids=[w.sql for w in WRITES])
def test_a_write_call_changes_the_account_as_its_sql_does(operation):
    through_api, through_sql = write_account(), write_account()

    operation.send(through_api.api)
    through_sql.run_query(operation.sql)

    assert through_api.state() == through_sql.state()
    assert through_api.state() != write_account().state()
    ((call, *_),) = through_api.api_calls
    assert call in WRITE_CALLS


def test_write_calls_are_recorded_with_their_request_models():
    account = write_account()

    WRITES[0].send(account.api)

    assert account.api_calls == [
        (
            "grant_privileges",
            "LOADER",
            ["USAGE"],
            "warehouse",
            {"name": '"etl-wh"'},
        )
    ]


@pytest.mark.parametrize(
    "operation,sql_error,api_message",
    [
        (
            Privileges(
                GRANT, ("usage",), "schema", ObjectName('raw."Gone-Schema"'), LOADER
            ),
            """Schema 'RAW."Gone-Schema"' does not exist or not authorized.""",
            "schema 'raw.gone-schema' does not exist or not authorized.",
        ),
        (
            BulkPrivileges(
                GRANT, FUTURE, ("select",), "table", ObjectName("gone.public"), LOADER
            ),
            "Database 'GONE' does not exist or not authorized.",
            "database 'gone' does not exist or not authorized.",
        ),
        (
            Privileges(
                GRANT, ("select",), "view", ObjectName("raw.public.gone"), LOADER
            ),
            "View 'RAW.PUBLIC.GONE' does not exist or not authorized.",
            "view 'raw.public.gone' does not exist or not authorized.",
        ),
        (
            BulkPrivileges(
                REVOKE, FUTURE, ("select",), "table", ObjectName("raw.gone"), LOADER
            ),
            "Schema 'RAW.GONE' does not exist or not authorized.",
            "schema 'raw.gone' does not exist or not authorized.",
        ),
        (
            RoleMembership(REVOKE, ObjectName.single("gone"), "role", LOADER, "role"),
            "Role 'GONE' does not exist or not authorized.",
            "role 'gone' does not exist or not authorized.",
        ),
        (
            RoleMembership(
                REVOKE,
                ObjectName.single("gone"),
                "role",
                ObjectName.single("jane"),
                "user",
            ),
            "Role 'GONE' does not exist or not authorized.",
            "role 'gone' does not exist or not authorized.",
        ),
    ],
    ids=[
        "schema",
        "database of a schema",
        "view",
        "revoke future in a schema",
        "revoke a role from a role",
        "revoke a role from a user",
    ],
)
def test_a_missing_object_fails_on_each_transport_as_snowflake_reports_it(
    operation, sql_error, api_message
):
    with pytest.raises(SnowflakeError, match=re.escape(sql_error)):
        write_account().run_query(operation.sql)
    with pytest.raises(NotFoundError) as error:
        operation.send(write_account().api)
    assert error.value.get_request_info()["message"].startswith(api_message)
    assert error.value.get_request_info()["error_code"] == "002003"


def test_an_application_role_through_the_api_gets_the_probes_syntax_error():
    with pytest.raises(APIError) as error:
        write_account().api.grant_role_to_role(
            "LOADER",
            "application role",
            ObjectName.role_reference("snowflake.cortex_user").to_securable(),
        )

    assert error.value.status == 400
    assert error.value.get_request_info()["error_code"] == "001003"


def test_a_queued_error_is_raised_once_and_changes_nothing():
    account = write_account()
    account.api_write_errors["grant_privileges"] = [api_error(500, "Internal", "1")]

    with pytest.raises(APIError):
        WRITES[0].send(account.api)
    assert account.state() == write_account().state()

    WRITES[0].send(account.api)
    assert account.state() != write_account().state()
