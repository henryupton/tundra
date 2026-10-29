# What SnowflakeApi asks snowflake.core for, pinned at the HTTP layer: a real Root on a
# mock connection, with each request stopped at SFPoolManager._send and answered with
# bodies in the shape the dev probe recorded. Nothing reaches the network.

import json
import ssl
from collections import Counter

import pytest
import urllib3
from snowflake.core.exceptions import NotFoundError

from tundra.identifiers import ObjectName
from tundra.snowflake_api import SnowflakeApi
from tundra.snowflake_connector import SnowflakeConnector, _BufferedResult, _is_missing
from tundra_test_utils.offline_http import OfflineHttp
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector

GRANTS = [
    {
        "securable": {"database": "LAB_RESULTS", "name": "AGGREGATE"},
        "securable_type": "SCHEMA",
        "grant_option": False,
        "privileges": ["USAGE"],
        "granted_by": "SYSADMIN",
        "created_on": "2026-01-01 00:00:00.000000+00:00",
    },
    {
        "securable": {
            "database": "RAW",
            "schema": "PUBLIC",
            "name": '"UTIL_PARSE(VARCHAR)"',
        },
        "securable_type": "FUNCTION",
        "grant_option": True,
        "privileges": ["OWNERSHIP"],
        "granted_by": "SYSADMIN",
    },
    {
        "securable": {"database": "SNOWFLAKE", "name": '"CORTEX-MODEL-ROLE-ALL"'},
        "securable_type": "APPLICATION ROLE",
        "grant_option": False,
        "privileges": ["USAGE"],
        "granted_by": "ACCOUNTADMIN",
    },
    {
        "securable": {"name": '"ORG-WIDE-VIEWER"'},
        "securable_type": "ROLE",
        "grant_option": True,
        "privileges": ["OWNERSHIP"],
        "granted_by": "SECURITY_OFFICER",
    },
    {
        "securable": {"name": "RAW_VOLUME"},
        "securable_type": "EXTERNAL VOLUME",
        "grant_option": False,
        "privileges": ["USAGE"],
        "granted_by": "ACCOUNTADMIN",
    },
    {
        "securable": {"name": "AB12345"},
        "securable_type": "ACCOUNT",
        "grant_option": True,
        "privileges": ["CREATE DATABASE"],
        "granted_by": "",
    },
]

FUTURE_GRANTS = [
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
    {
        "securable": {"database": "SCRATCH", "name": '"<DYNAMIC_TABLE>"'},
        "securable_type": "DYNAMIC TABLE",
        "grant_option": False,
        "privileges": ["MONITOR"],
    },
]

NOT_FOUND = {
    "message": "Role 'gone' does not exist or not authorized.",
    "error_code": "002003",
    "request_id": "offline",
}


@pytest.fixture
def http(monkeypatch):
    capture = OfflineHttp()
    capture.install(monkeypatch)
    return capture


@pytest.fixture
def api(http):
    return SnowflakeApi(http.root)


# (method, its argument as tundra spells it for a path, path sent, showLimit, body)
REQUESTS = [
    ("grants_to_role", "loader", "/api/v2/roles/LOADER/grants", ["10000"], GRANTS),
    (
        "grants_to_role",
        '"ORG-WIDE-VIEWER"',
        "/api/v2/roles/%22ORG-WIDE-VIEWER%22/grants",
        ["10000"],
        GRANTS,
    ),
    (
        "grants_to_role",
        '"GROUP"',
        "/api/v2/roles/%22GROUP%22/grants",
        ["10000"],
        GRANTS,
    ),
    (
        "future_grants_to_role",
        "loader",
        "/api/v2/roles/LOADER/future-grants",
        ["10000"],
        FUTURE_GRANTS,
    ),
    (
        "grants_to_user",
        "jane.doe@example.com",
        "/api/v2/users/%22jane.doe%40example.com%22/grants",
        None,
        [{"securable": {"name": "LOADER"}, "securable_type": "ROLE"}],
    ),
    ("databases", None, "/api/v2/databases", ["10000"], [{"name": "RAW"}]),
    ("warehouses", None, "/api/v2/warehouses", None, [{"name": '"etl-wh"'}]),
    ("users", None, "/api/v2/users", ["10000"], [{"name": "gitlab-ci"}]),
    (
        "schemas",
        "raw",
        "/api/v2/databases/RAW/schemas",
        ["10000"],
        [{"name": '"Mixed-Case"'}, {"name": "AGGREGATE"}],
    ),
    (
        "schemas",
        '"my-db"',
        "/api/v2/databases/%22my-db%22/schemas",
        ["10000"],
        [{"name": "PUBLIC"}],
    ),
    (
        "database_roles",
        "snowflake",
        "/api/v2/databases/SNOWFLAKE/database-roles",
        ["10000"],
        [{"name": "GOVERNANCE_VIEWER"}],
    ),
]


@pytest.mark.parametrize("method,name,path,show_limit,body", REQUESTS)
def test_request(http, api, method, name, path, show_limit, body):
    http.answer(path, body)
    args = () if name is None else (ObjectName.single(name).api_name,)

    models = getattr(api, method)(*args)

    request = http.last
    assert (request.method, request.path) == ("GET", path)
    assert request.query.get("showLimit") == show_limit
    assert list(map(name_of, models)) == list(map(name_of, body))


def name_of(model):
    """The name a model or a row of a response body carries, as REST spells it."""
    if isinstance(model, dict):
        return (model.get("securable") or model)["name"]
    return (getattr(model, "securable", None) or model).name


def test_grants_deserialise_as_snowflake_core_models(http, api):
    http.answer("/api/v2/roles/LOADER/grants", GRANTS)

    keyword_schema, function, *_ = api.grants_to_role("LOADER")

    assert keyword_schema.securable_type == "SCHEMA"
    assert keyword_schema.securable.database == "LAB_RESULTS"
    assert keyword_schema.securable.name == "AGGREGATE"
    assert function.securable.var_schema == "PUBLIC"
    assert function.grant_option is True


def test_the_connector_reads_the_recorded_shapes(http, monkeypatch):
    http.answer("/api/v2/roles/LOADER/grants", GRANTS)
    http.answer("/api/v2/roles/LOADER/future-grants", FUTURE_GRANTS)
    monkeypatch.setattr(
        SnowflakeConnector, "api", property(lambda self: SnowflakeApi(http.root))
    )
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    conn = MockSnowflakeConnector()

    grants = SnowflakeConnector.show_grants_to_role(conn, "loader")
    future = SnowflakeConnector.show_future_grants_to_role(conn, "loader")
    account = SnowflakeConnector.show_account_grants_to_role(conn, "loader")

    assert grants == {
        "usage": {
            "schema": {"lab_results.aggregate"},
            "application role": {'"SNOWFLAKE"."CORTEX-MODEL-ROLE-ALL"'},
            "external volume": {"raw_volume"},
        },
        "ownership": {"role": {'"ORG-WIDE-VIEWER"'}},
    }
    assert future == {
        "select": {"table": {"lab_results.aggregate.<table>"}},
        "monitor": {"dynamic_table": {"scratch.<dynamic_table>"}},
    }
    assert account == {"create database": ""}


def test_a_404_is_a_missing_object(http, api):
    http.answer("/api/v2/roles/GONE/grants", NOT_FOUND, status=404)

    with pytest.raises(NotFoundError) as error:
        api.grants_to_role("GONE")

    assert _is_missing(error.value)
    assert "does not exist or not authorized" in str(error.value)


def test_a_404_with_another_code_isnt_a_missing_object(http, api):
    http.answer(
        "/api/v2/roles/LOADER/grants",
        {"message": "Unknown route", "error_code": "390400"},
        status=404,
    )

    with pytest.raises(NotFoundError) as error:
        api.grants_to_role("LOADER")

    assert not _is_missing(error.value)


def test_an_error_body_that_isnt_json_raises_json_decode_error(http, api):
    http.answer("/api/v2/databases", b"<html>Bad Gateway</html>", status=502)

    with pytest.raises(json.JSONDecodeError):
        api.databases()


def test_an_unknown_request_fails_rather_than_leaving(http, api):
    with pytest.raises(AssertionError, match="No answer"):
        api.warehouses()


def ssl_failure(verify_message: str) -> urllib3.exceptions.MaxRetryError:
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_message = verify_message
    return urllib3.exceptions.MaxRetryError(
        None, "/api/v2/databases", reason=urllib3.exceptions.SSLError(error)
    )


@pytest.mark.parametrize(
    "verify_message",
    [
        "unable to get local issuer certificate",
        # snowflake.core raises this one again as a SnowflakePythonError
        "Hostname mismatch, certificate is not valid for 'my_org-ab12345'",
    ],
    ids=["SSL", "hostname mismatch"],
)
def test_a_transport_failure_inside_snowflake_core_reads_through_sql(
    http, monkeypatch, verify_message
):
    http.answer("/api/v2/databases", ssl_failure(verify_message))
    monkeypatch.setattr(
        SnowflakeConnector, "api", property(lambda self: SnowflakeApi(http.root))
    )
    conn = MockSnowflakeConnector()
    monkeypatch.setattr(
        conn,
        "run_query",
        lambda query: _BufferedResult([{"name": "RAW"}]),
        raising=False,
    )

    assert SnowflakeConnector.show_databases(conn) == ["raw"]
    assert conn.read_stats.fallbacks == Counter({("show_databases", "transport"): 1})
    assert conn._api_failed
