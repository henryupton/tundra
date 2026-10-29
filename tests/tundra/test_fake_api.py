# FakeApi spells names the way the dev probe's REST rows do. tundra's decoder reads a
# quoted part and a bare one alike, so a fake that drifted (quoting every part, say)
# would leave the REST tests passing, and its bare parts are what catch ObjectName() or
# ObjectName.single() used on a REST field.

import pytest

from tundra_test_utils.fake_account import FakeAccount, FakeApi, FutureGrant, Grant

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
