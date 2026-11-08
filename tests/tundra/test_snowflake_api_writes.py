# What each planned operation sends through snowflake.core, pinned at the HTTP layer: a
# real Root on a mock connection, with each request stopped at SFPoolManager._send and
# its JSON body decoded. Nothing reaches the network.
#
# PROBED holds the shapes the first dev write probe saw Snowflake accept: IMPORTED
# PRIVILEGES on SNOWFLAKE through grant_privileges and revoke_privileges, and SELECT on
# future and all tables in a schema, with a bare role, an uppercase scope and a keyword
# schema sent unquoted. CARRIED holds what an API row sends in a probed shape that dev
# hasn't run itself. The rows and gates still on SQL are pinned too, so flipping one
# after the second probe sends what the probe tried.

import pytest
from snowflake.core.exceptions import NotFoundError

from tundra.identifiers import ObjectName
from tundra.operations import (
    ALL,
    FUTURE,
    GRANT,
    REVOKE,
    BulkPrivileges,
    PlannedCommand,
    Privileges,
    RoleMembership,
    SqlStatement,
    UserTag,
    routes_to_api,
)
from tundra.snowflake_api import SnowflakeApi
from tundra.snowflake_connector import API, SQL, SnowflakeConnector
from tundra.table_object_types import ALL_OBJECT_TYPES
from tundra_test_utils.offline_http import OfflineHttp
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector

LOADER = ObjectName.single("loader")
OK = {"status": "Statement executed successfully."}


@pytest.fixture
def http(monkeypatch):
    capture = OfflineHttp()
    capture.install(monkeypatch)
    return capture


@pytest.fixture
def api(http):
    return SnowflakeApi(http.root)


def role_grants(action: str) -> str:
    return "/api/v2/roles/LOADER/grants" + ("" if action == GRANT else ":revoke")


def future_grants(action: str) -> str:
    return "/api/v2/roles/LOADER/future-grants" + ("" if action == GRANT else ":revoke")


# (label, operation, path, body)
PROBED = [
    (
        "IMPORTED PRIVILEGES on SNOWFLAKE",
        Privileges(
            GRANT,
            ("imported privileges",),
            "database",
            ObjectName.single("snowflake"),
            LOADER,
        ),
        role_grants(GRANT),
        {
            "securable": {"name": "SNOWFLAKE"},
            "securable_type": "database",
            "privileges": ["IMPORTED PRIVILEGES"],
        },
    ),
    (
        "IMPORTED PRIVILEGES revoked",
        Privileges(
            REVOKE,
            ("imported privileges",),
            "database",
            ObjectName.single("snowflake"),
            LOADER,
        ),
        role_grants(REVOKE),
        {
            "securable": {"name": "SNOWFLAKE"},
            "securable_type": "database",
            "privileges": ["IMPORTED PRIVILEGES"],
        },
    ),
    (
        "future tables in a schema",
        BulkPrivileges(
            GRANT, FUTURE, ("select",), "table", ObjectName("raw.public"), LOADER
        ),
        future_grants(GRANT),
        {
            "containing_scope": {"database": "RAW", "schema": "PUBLIC"},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "future tables revoked in a schema",
        BulkPrivileges(
            REVOKE, FUTURE, ("select",), "table", ObjectName("raw.public"), LOADER
        ),
        future_grants(REVOKE),
        {
            "containing_scope": {"database": "RAW", "schema": "PUBLIC"},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "future tables in a keyword schema",
        BulkPrivileges(
            GRANT,
            FUTURE,
            ("select",),
            "table",
            ObjectName('raw."AGGREGATE"'),
            LOADER,
        ),
        future_grants(GRANT),
        {
            "containing_scope": {"database": "RAW", "schema": "AGGREGATE"},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "all tables in a schema",
        BulkPrivileges(
            GRANT, ALL, ("select",), "table", ObjectName("raw.public"), LOADER
        ),
        role_grants(GRANT),
        {
            "containing_scope": {"database": "RAW", "schema": "PUBLIC"},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
]

# On an API row, in the shape of IMPORTED PRIVILEGES on a database
CARRIED = [
    (
        "USAGE on a database",
        Privileges(GRANT, ("usage",), "database", ObjectName.single("raw"), LOADER),
        role_grants(GRANT),
        {
            "securable": {"name": "RAW"},
            "securable_type": "database",
            "privileges": ["USAGE"],
        },
    ),
    (
        "USAGE revoked on a database",
        Privileges(REVOKE, ("usage",), "database", ObjectName.single("raw"), LOADER),
        role_grants(REVOKE),
        {
            "securable": {"name": "RAW"},
            "securable_type": "database",
            "privileges": ["USAGE"],
        },
    ),
]

OBJECT_TYPES = [t.name for t in ALL_OBJECT_TYPES]
TEAM_ROLE = ObjectName.single("org-wide-viewer")

# Still on SQL: what each row sends once it's flipped
UNPROBED = [
    (
        "database write bundle",
        Privileges(
            GRANT,
            ("usage", "monitor", "create schema"),
            "database",
            ObjectName.single("raw"),
            LOADER,
        ),
        role_grants(GRANT),
        {
            "securable": {"name": "RAW"},
            "securable_type": "database",
            "privileges": ["USAGE", "MONITOR", "CREATE SCHEMA"],
        },
    ),
    (
        "future tables with a quoted role",
        BulkPrivileges(
            GRANT, FUTURE, ("select",), "table", ObjectName("raw.public"), TEAM_ROLE
        ),
        "/api/v2/roles/%22org-wide-viewer%22/future-grants",
        {
            "containing_scope": {"database": "RAW", "schema": "PUBLIC"},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "USAGE on a database for a quoted role",
        Privileges(GRANT, ("usage",), "database", ObjectName.single("raw"), TEAM_ROLE),
        "/api/v2/roles/%22org-wide-viewer%22/grants",
        {
            "securable": {"name": "RAW"},
            "securable_type": "database",
            "privileges": ["USAGE"],
        },
    ),
    (
        "future tables in a reserved-word schema",
        BulkPrivileges(
            GRANT, FUTURE, ("select",), "table", ObjectName("raw.group"), LOADER
        ),
        future_grants(GRANT),
        {
            "containing_scope": {"database": "RAW", "schema": '"GROUP"'},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "all tables in a mixed-case schema",
        BulkPrivileges(
            GRANT, ALL, ("select",), "table", ObjectName('"MyDb"."MySchema"'), LOADER
        ),
        role_grants(GRANT),
        {
            "containing_scope": {"database": '"MyDb"', "schema": '"MySchema"'},
            "securable_type": "table",
            "privileges": ["SELECT"],
        },
    ),
    (
        "USAGE on a mixed-case database",
        Privileges(GRANT, ("usage",), "database", ObjectName.single('"MyDb"'), LOADER),
        role_grants(GRANT),
        {
            "securable": {"name": '"MyDb"'},
            "securable_type": "database",
            "privileges": ["USAGE"],
        },
    ),
    (
        "table write bundle on future tables in a schema",
        BulkPrivileges(
            GRANT,
            FUTURE,
            ("select", "insert", "update"),
            "table",
            ObjectName("raw.public"),
            LOADER,
        ),
        future_grants(GRANT),
        {
            "containing_scope": {"database": "RAW", "schema": "PUBLIC"},
            "securable_type": "table",
            "privileges": ["SELECT", "INSERT", "UPDATE"],
        },
    ),
    (
        "schema in a database named by a keyword",
        Privileges(GRANT, ("usage",), "schema", ObjectName("raw.group"), LOADER),
        role_grants(GRANT),
        {
            "securable": {"database": "RAW", "name": '"GROUP"'},
            "securable_type": "schema",
            "privileges": ["USAGE"],
        },
    ),
    (
        "warehouse that needs quoting",
        Privileges(
            REVOKE, ("operate",), "warehouse", ObjectName.single("etl-wh"), LOADER
        ),
        role_grants(REVOKE),
        {
            "securable": {"name": '"etl-wh"'},
            "securable_type": "warehouse",
            "privileges": ["OPERATE"],
        },
    ),
    (
        "integration",
        Privileges(
            GRANT, ("usage",), "integration", ObjectName.single("s3_int"), LOADER
        ),
        role_grants(GRANT),
        {
            "securable": {"name": "S3_INT"},
            "securable_type": "integration",
            "privileges": ["USAGE"],
        },
    ),
    (
        "external volume",
        Privileges(
            GRANT,
            ("usage",),
            "external volume",
            ObjectName.single("iceberg_volume"),
            LOADER,
        ),
        role_grants(GRANT),
        {
            "securable": {"name": "ICEBERG_VOLUME"},
            "securable_type": "external volume",
            "privileges": ["USAGE"],
        },
    ),
    (
        "account",
        Privileges(REVOKE, ("execute task",), "account", None, LOADER),
        role_grants(REVOKE),
        {"securable_type": "account", "privileges": ["EXECUTE TASK"]},
    ),
    *[
        (
            f"{kind} named with a dot",
            Privileges(
                GRANT,
                ("select",),
                kind,
                ObjectName('raw."my-schema"."Events.2024"'),
                LOADER,
            ),
            role_grants(GRANT),
            {
                "securable": {
                    "database": "RAW",
                    "schema": '"my-schema"',
                    "name": '"Events.2024"',
                },
                "securable_type": kind,
                "privileges": ["SELECT"],
            },
        )
        for kind in OBJECT_TYPES
    ],
    (
        "future schemas in a database",
        BulkPrivileges(
            REVOKE, FUTURE, ("usage",), "schema", ObjectName.single("raw"), LOADER
        ),
        future_grants(REVOKE),
        {
            "containing_scope": {"database": "RAW"},
            "securable_type": "schema",
            "privileges": ["USAGE"],
        },
    ),
    *[
        (
            f"{on} {kind} in a {scope}",
            BulkPrivileges(
                GRANT,
                on,
                ("select",),
                kind,
                ObjectName("raw.public" if scope == "schema" else "raw"),
                LOADER,
            ),
            role_grants(GRANT) if on == ALL else future_grants(GRANT),
            {
                "containing_scope": (
                    {"database": "RAW", "schema": "PUBLIC"}
                    if scope == "schema"
                    else {"database": "RAW"}
                ),
                "securable_type": kind,
                "privileges": ["SELECT"],
            },
        )
        for on in (FUTURE, ALL)
        for kind in OBJECT_TYPES
        for scope in ("schema", "database")
        if not (kind == "table" and scope == "schema")
    ],
    (
        "account role to a role",
        RoleMembership(GRANT, ObjectName.single("analyst"), "role", LOADER, "role"),
        role_grants(GRANT),
        {"securable": {"name": "ANALYST"}, "securable_type": "role"},
    ),
    (
        "account role revoked from a role",
        RoleMembership(
            REVOKE, ObjectName.single("org-wide-viewer"), "role", LOADER, "role"
        ),
        role_grants(REVOKE),
        {"securable": {"name": '"org-wide-viewer"'}, "securable_type": "role"},
    ),
    (
        "database role to a role",
        RoleMembership(
            GRANT,
            ObjectName.role_reference("snowflake.usage_viewer"),
            "database role",
            LOADER,
            "role",
        ),
        role_grants(GRANT),
        {
            "securable": {"database": "SNOWFLAKE", "name": "USAGE_VIEWER"},
            "securable_type": "database role",
        },
    ),
    (
        "account role to a user, quoted in the path",
        RoleMembership(
            GRANT,
            ObjectName.single("analyst"),
            "role",
            ObjectName.single("jane.doe@example.com"),
            "user",
        ),
        "/api/v2/users/%22jane.doe%40example.com%22/grants",
        {"securable": {"name": "ANALYST"}, "securable_type": "role"},
    ),
    (
        "account role revoked from a user",
        RoleMembership(
            REVOKE,
            ObjectName.single("analyst"),
            "role",
            ObjectName.single("jane.doe@example.com"),
            "user",
        ),
        "/api/v2/users/%22jane.doe%40example.com%22/grants:revoke",
        {"securable": {"name": "ANALYST"}, "securable_type": "role"},
    ),
    (
        "tag set on a user",
        UserTag(
            "set",
            ObjectName.single("jane"),
            ObjectName("governance.tags.team"),
            "O'BRIEN",
        ),
        "/api/v2/users/JANE:set-tags",
        [
            {
                "tag_database": "GOVERNANCE",
                "tag_schema": "TAGS",
                "tag_name": "TEAM",
                "tag_value": "O'BRIEN",
            }
        ],
    ),
    (
        "tag unset from a user",
        UserTag(
            "unset", ObjectName.single("gitlab-ci"), ObjectName('gov."my-tags".team')
        ),
        "/api/v2/users/%22gitlab-ci%22:unset-tags",
        [{"tag_database": "GOV", "tag_schema": '"my-tags"', "tag_name": "TEAM"}],
    ),
]

CONTRACTS = PROBED + CARRIED + UNPROBED


@pytest.mark.parametrize(
    "operation,path,body",
    [contract[1:] for contract in CONTRACTS],
    ids=[contract[0] for contract in CONTRACTS],
)
def test_request(http, api, operation, path, body):
    http.answer(path, OK, method="POST")

    operation.send(api)

    (request,) = http.requests
    assert (request.method, request.path) == ("POST", path)
    # Revokes send no mode, so Snowflake's default RESTRICT applies
    assert request.query == {}
    assert request.body == body


def test_nothing_sends_grant_option_or_a_placeholder():
    for _, _, _, body in CONTRACTS:
        text = str(body)
        assert "grant_option" not in text
        assert "<" not in text


def test_the_probed_shapes_are_what_the_api_rows_send():
    assert all(routes_to_api(operation) for _, operation, _, _ in PROBED + CARRIED)
    assert not any(routes_to_api(operation) for _, operation, _, _ in UNPROBED)


@pytest.fixture
def conn(http, monkeypatch):
    monkeypatch.setattr(
        SnowflakeConnector, "api", property(lambda self: SnowflakeApi(http.root))
    )
    connector = MockSnowflakeConnector()
    ran = []
    monkeypatch.setattr(connector, "run_query", ran.append, raising=False)
    connector.ran = ran
    return connector


@pytest.mark.parametrize(
    "operation,path,body",
    [contract[1:] for contract in PROBED + CARRIED],
    ids=[contract[0] for contract in PROBED + CARRIED],
)
def test_the_connector_sends_an_api_row_through_snowflake_core(
    http, conn, operation, path, body
):
    http.answer(path, OK, method="POST")

    executed = conn.execute(operation)

    assert executed == (API, None)
    assert [(r.path, r.body) for r in http.requests] == [(path, body)]
    assert conn.ran == []


@pytest.mark.parametrize(
    "operation",
    [contract[1] for contract in UNPROBED]
    + [SqlStatement('GRANT APPLICATION ROLE "SNOWFLAKE".cortex_user TO ROLE loader')],
    ids=[contract[0] for contract in UNPROBED] + ["application role"],
)
def test_the_connector_runs_any_other_row_as_its_sql(http, conn, operation):
    executed = conn.execute(operation)

    assert executed == (SQL, None)
    assert conn.ran == [operation.sql]
    assert http.requests == []


def test_a_missing_table_is_named_from_the_operation(http, api):
    # The dev probe's 404 for a table that doesn't exist, with the names made generic.
    # REST lowercases the name, which here is case-sensitive.
    operation = Privileges(
        GRANT, ("select",), "table", ObjectName('raw.public."MixedCase"'), LOADER
    )
    http.answer(
        role_grants(GRANT),
        {
            "message": "table 'raw.public.mixedcase' does not exist or not authorized."
            " your primary role securityadmin must have at least one privilege granted"
            " on table raw.public.mixedcase.",
            "request_id": "offline",
            "error_code": "002003",
            "code": "002003",
        },
        status=404,
        method="POST",
    )

    with pytest.raises(NotFoundError) as error:
        operation.send(api)

    assert SnowflakeConnector.missing_object(error.value, operation) == (
        "table",
        operation.target,
    )
    assert PlannedCommand(False, operation)["sql"] == (
        'GRANT select ON table raw.public."MixedCase" TO ROLE loader'
    )
