# The connector's readers through snowflake.core, against FakeAccount's REST answers:
# each gives what its SQL gives, and a listing falls back to SQL exactly when the API
# can't be trusted to have given it in full.

import threading
from collections import Counter

import pytest
import snowflake.connector
import urllib3
from snowflake.core.database import Database
from snowflake.core.exceptions import APIError, NotFoundError, RetryTimeoutError
from snowflake.core.role import Grant as RestGrant

from tundra.snowflake_api import LIST_LIMIT, SnowflakeApi
from tundra.snowflake_connector import ApiFallbackError, ReadStats, SnowflakeConnector
from tundra_test_utils.fake_account import (
    FakeAccount,
    FakeApi,
    FutureGrant,
    Grant,
    api_error,
)


def names_account(**options) -> FakeAccount:
    """Names that REST and SHOW GRANTS spell differently, or that only quoting tells apart."""
    return FakeAccount(
        warehouses=["REPORTING_WH", "etl-wh", '"lower_wh"'],
        databases=["RAW", "SNOWFLAKE", '"Mixed-Db"', '"lower_db"'],
        schemas=[
            "RAW.PUBLIC",
            'RAW."Mixed-Case"',
            'RAW."lower_case"',
            'RAW."CamelCase"',
            'RAW."GROUP"',
            "RAW.AGGREGATE",
            'RAW."dot.inside"',
            'RAW."has""quote"',
        ],
        roles={
            "LOADER": "SECURITYADMIN",
            '"ORG-WIDE-VIEWER"': "SECURITYADMIN",
            '"lower_role"': "SECURITYADMIN",
        },
        users=["HEIDI", "jane.doe@example.com", "gitlab-ci", '"lower_user"'],
        user_roles={
            "HEIDI": ["LOADER", '"ORG-WIDE-VIEWER"', '"lower_role"'],
            "jane.doe@example.com": ["LOADER"],
        },
        database_roles={
            "RAW": ["READER", '"Mixed-Reader"', '"lower_reader"'],
            "SNOWFLAKE": ["GOVERNANCE_VIEWER"],
        },
        grants=[
            Grant("LOADER", "USAGE", "WAREHOUSE", '"etl-wh"'),
            Grant("LOADER", "USAGE", "DATABASE", '"Mixed-Db"'),
            Grant("LOADER", "USAGE", "DATABASE", '"lower_db"'),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."Mixed-Case"'),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."lower_case"'),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."GROUP"'),
            Grant("LOADER", "USAGE", "SCHEMA", 'RAW."AGGREGATE"'),
            Grant("LOADER", "SELECT", "TABLE", 'RAW.PUBLIC."has""quote"'),
            Grant("LOADER", "SELECT", "ICEBERG_TABLE", "RAW.PUBLIC.EVENTS"),
            Grant("LOADER", "USAGE", "EXTERNAL_VOLUME", "RAW_VOLUME"),
            Grant("LOADER", "USAGE", "INTEGRATION", "SNOWFLAKE$LOCAL_APPLICATION"),
            Grant("LOADER", "USAGE", "DATABASE_ROLE", 'RAW."Mixed-Reader"'),
            Grant(
                "LOADER",
                "USAGE",
                "APPLICATION_ROLE",
                'SNOWFLAKE."CORTEX-MODEL-ROLE-ALL"',
                granted_by="ACCOUNTADMIN",
            ),
            # The application's own grant, which neither reader records
            Grant(
                "LOADER",
                "USAGE",
                "APPLICATION_ROLE",
                "SNOWFLAKE.AI_OBSERVABILITY_READER",
                granted_by="SNOWFLAKE",
            ),
            Grant("LOADER", "USAGE", "ROLE", '"ORG-WIDE-VIEWER"', grant_option=True),
            Grant("LOADER", "USAGE", "FUNCTION", 'RAW.PUBLIC."UTIL_PARSE(VARCHAR)"'),
            Grant("LOADER", "CREATE DATABASE", "ACCOUNT", "-", granted_by="SYSADMIN"),
            Grant("LOADER", "APPLY TAG", "ACCOUNT", "-", granted_by="Mixed-Grantor"),
            Grant("LOADER", "MONITOR USAGE", "ACCOUNT", "-", granted_by=""),
            Grant('"ORG-WIDE-VIEWER"', "USAGE", "DATABASE", "RAW"),
        ],
        future_grants=[
            FutureGrant("LOADER", "SELECT", "TABLE", "RAW"),
            FutureGrant("LOADER", "USAGE", "SCHEMA", "RAW"),
            FutureGrant("LOADER", "SELECT", "ICEBERG_TABLE", 'RAW."Mixed-Case"'),
            FutureGrant("LOADER", "SELECT", "VIEW", "LAB_RESULTS.AGGREGATE"),
            FutureGrant("LOADER", "SELECT", "SEMANTIC_VIEW", 'RAW."lower_case"'),
            FutureGrant('"ORG-WIDE-VIEWER"', "SELECT", "DYNAMIC_TABLE", "RAW.PUBLIC"),
        ],
        **options,
    )


READS = [
    ("show_databases", ()),
    ("show_warehouses", ()),
    ("show_users", ()),
    ("show_schemas", ("raw",)),
    ("show_database_roles", ("raw",)),
    ("show_database_roles", ("snowflake",)),
    ("show_grants_to_role", ("loader",)),
    ("show_grants_to_role", ('"ORG-WIDE-VIEWER"',)),
    ("show_account_grants_to_role", ("loader",)),
    ("show_grants_to_role_with_grant_option", ("loader",)),
    ("show_future_grants_to_role", ("loader",)),
    ("show_future_grants_to_role", ('"ORG-WIDE-VIEWER"',)),
    ("show_roles_granted_to_user", ("heidi",)),
    ("show_roles_granted_to_user", ("jane.doe@example.com",)),
]


@pytest.mark.parametrize(
    "mixed_case", [False, True], ids=["bare-uppercase", "bare-identifiers"]
)
@pytest.mark.parametrize("reader,args", READS)
def test_rest_reads_what_sql_reads(mocker, monkeypatch, reader, args, mixed_case):
    account = names_account(rest_bare_mixed_case=mixed_case)
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    rest = getattr(SnowflakeConnector(), reader)(*args)
    assert account.api_calls and not account.queries

    sql = getattr(SnowflakeConnector(sql_only=True), reader)(*args)
    assert account.queries
    assert rest and rest == sql


def test_rest_names_are_canonical(mocker):
    names_account(rest_bare_mixed_case=True).install(mocker)
    conn = SnowflakeConnector()

    assert conn.show_schemas("raw") == [
        "raw.public",
        'raw."Mixed-Case"',
        'raw."lower_case"',
        'raw."CamelCase"',
        'raw."GROUP"',
        "raw.aggregate",
        'raw."dot.inside"',
        'raw."has""quote"',
    ]
    assert conn.show_databases() == ["raw", '"SNOWFLAKE"', '"Mixed-Db"', '"lower_db"']
    assert conn.show_future_grants_to_role("loader") == {
        "select": {
            "table": {"raw.<table>"},
            "iceberg_table": {'raw."Mixed-Case".<iceberg_table>'},
            "view": {"lab_results.aggregate.<view>"},
            "semantic_view": {'raw."lower_case".<semantic_view>'},
        },
        "usage": {"schema": {"raw.<schema>"}},
    }
    assert conn.show_account_grants_to_role("loader") == {
        "create database": "sysadmin",
        "apply tag": '"Mixed-Grantor"',
        "monitor usage": "",
    }


def test_reads_are_counted_by_transport(mocker):
    names_account().install(mocker)
    conn = SnowflakeConnector()

    conn.show_databases()
    conn.show_grants_to_role("loader")
    conn.sql_only = True
    conn.show_databases()

    assert conn.read_stats.listings == Counter(
        {
            ("show_databases", "api"): 1,
            ("show_grants_to_role", "api"): 1,
            ("show_databases", "sql"): 1,
        }
    )
    assert not conn.read_stats.fallbacks


# Missing objects: a 404 with 002003 is what SQL's "does not exist" is, not a fallback


def test_a_missing_role_raises_as_it_does_in_sql(mocker):
    account = names_account()
    account.install(mocker)

    with pytest.raises(NotFoundError):
        SnowflakeConnector().show_grants_to_role("gone")
    with pytest.raises(Exception, match="does not exist"):
        SnowflakeConnector(sql_only=True).show_grants_to_role("gone")
    assert account.api_calls == [("grants_to_role", "GONE")]


@pytest.mark.parametrize("sql_only", [False, True], ids=["rest", "sql"])
def test_a_missing_role_holds_no_future_grants(mocker, monkeypatch, sql_only):
    names_account().install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    conn = SnowflakeConnector(sql_only=sql_only)

    assert conn.show_future_grants_to_role("gone") == {}
    assert not conn.read_stats.fallbacks


def test_a_database_gone_from_a_rest_listing_is_gone(mocker):
    names_account().install(mocker)
    mocker.patch.object(
        FakeApi,
        "schemas",
        side_effect=api_error(
            404, "Database 'GONE' does not exist or not authorized.", "002003"
        ),
    )

    assert not SnowflakeConnector().object_exists("schema", "GONE.PUBLIC")


def test_a_404_for_a_missing_object_is_read_by_its_error_code(mocker):
    names_account().install(mocker)
    mocker.patch.object(
        FakeApi, "schemas", side_effect=api_error(404, "Not found.", "002003")
    )

    assert not SnowflakeConnector().object_exists("schema", "GONE.PUBLIC")


def test_a_lookup_failing_otherwise_leaves_the_object_present(mocker):
    names_account().install(mocker)
    mocker.patch.object(FakeApi, "schemas", side_effect=api_error(403, "Forbidden"))
    mocker.patch.object(
        SnowflakeConnector, "run_query", side_effect=Exception("warehouse suspended")
    )

    assert SnowflakeConnector().object_exists("schema", "RAW.PUBLIC")


# Fallback triggers: each one reads the listing again through SQL


def invalid_model(*args):
    return [Database.from_dict({"name": "not-an-identifier"})]


def unreadable_body(body):
    def side_effect(*args):
        return api_error(502, body=body)

    return side_effect


def raises(error):
    def side_effect(*args):
        raise error

    return side_effect


TRIGGERS = [
    pytest.param("validation", invalid_model, False, id="ValidationError"),
    pytest.param(
        "api error",
        raises(api_error(403, "Insufficient privileges", "003001")),
        False,
        id="403",
    ),
    pytest.param(
        "api error",
        raises(api_error(401, "Session expired", "390114")),
        False,
        id="401",
    ),
    pytest.param(
        "api error",
        raises(api_error(404, "Unknown route", "390400")),
        False,
        id="404 without 002003",
    ),
    pytest.param(
        "api error", raises(api_error(409, "Conflict", "000000")), False, id="409"
    ),
    pytest.param(
        "api error", raises(api_error(500, "Internal error", "000603")), False, id="500"
    ),
    pytest.param(
        "non-json", unreadable_body(b"<html>Bad Gateway</html>"), False, id="not JSON"
    ),
    pytest.param(
        "non-json",
        unreadable_body(b"<html>Passerelle d\xe9faillante</html>"),
        False,
        id="not UTF-8",
    ),
    pytest.param(
        "non-json", unreadable_body(b'"Bad Gateway"'), False, id="JSON string"
    ),
    pytest.param(
        "non-json", unreadable_body(b'{"message": null}'), False, id="null message"
    ),
    pytest.param(
        "transport",
        raises(urllib3.exceptions.MaxRetryError(None, "/api/v2/databases")),
        True,
        id="MaxRetryError",
    ),
    pytest.param(
        "transport",
        raises(urllib3.exceptions.ProtocolError("aborted")),
        True,
        id="ProtocolError",
    ),
    pytest.param(
        "transport", raises(ConnectionResetError("reset")), True, id="OSError"
    ),
    pytest.param(
        "transport",
        raises(APIError(root=None, status=0, reason="SSLError")),
        True,
        id="SSL as status 0",
    ),
    pytest.param(
        "transport",
        raises(snowflake.connector.errors.OperationalError("connection lost")),
        True,
        id="connector error",
    ),
    pytest.param(
        "timeout",
        raises(RetryTimeoutError("gave up after 600 s")),
        True,
        id="RetryTimeoutError",
    ),
]


@pytest.mark.parametrize("reason,side_effect,sticky", TRIGGERS)
def test_a_failed_listing_is_read_through_sql(mocker, reason, side_effect, sticky):
    account = names_account()
    account.install(mocker)
    mocker.patch.object(FakeApi, "databases", side_effect=side_effect)
    conn = SnowflakeConnector()

    assert conn.show_databases() == ["raw", '"SNOWFLAKE"', '"Mixed-Db"', '"lower_db"']
    assert account.queries == ["SHOW DATABASES"]
    assert conn.read_stats.fallbacks == Counter({("show_databases", reason): 1})

    # A transport failure sends the rest of the run through SQL, without trying
    conn.show_warehouses()
    assert (account.api_calls == []) is sticky
    assert ("SHOW WAREHOUSES" in account.queries) is sticky
    assert conn.read_stats.fallbacks == Counter({("show_databases", reason): 1})


def test_a_model_tundra_cant_read_is_read_through_sql(mocker):
    account = names_account()
    account.install(mocker)
    wrong_placeholder = RestGrant.from_dict(
        {
            "securable": {"database": "RAW", "name": '"<VIEW>"'},
            "securable_type": "TABLE",
            "privileges": ["SELECT"],
        }
    )
    mocker.patch.object(
        FakeApi, "future_grants_to_role", return_value=[wrong_placeholder]
    )
    conn = SnowflakeConnector()

    assert conn.show_future_grants_to_role("loader") == SnowflakeConnector(
        sql_only=True
    ).show_future_grants_to_role("loader")
    assert conn.read_stats.fallbacks == Counter(
        {("show_future_grants_to_role", "validation"): 1}
    )


def test_an_error_that_isnt_a_fallback_is_raised(mocker):
    names_account().install(mocker)
    mocker.patch.object(FakeApi, "databases", side_effect=TypeError("a tundra bug"))

    with pytest.raises(TypeError):
        SnowflakeConnector().show_databases()


def test_fallbacks_are_counted_per_reader(mocker):
    names_account(rest_list_cap=2).install(mocker)
    mocker.patch.object(FakeApi, "warehouses", side_effect=invalid_model)
    conn = SnowflakeConnector()

    conn.show_databases()
    conn.show_schemas("raw")
    conn.show_warehouses()
    conn.show_database_roles("snowflake")

    assert conn.read_stats.fallbacks == Counter(
        {
            ("show_databases", "limit"): 1,
            ("show_schemas", "limit"): 1,
            ("show_warehouses", "validation"): 1,
        }
    )
    assert conn.read_stats.by_reason() == Counter({"limit": 2, "validation": 1})
    assert conn.read_stats.by_transport() == Counter({"sql": 3, "api": 1})


# The row limit: a listing that comes back that long may have left rows out


def capped_state(reader, count):
    if reader in ("show_databases", "show_warehouses", "show_users"):
        return {reader[len("show_") :]: [f"N{i}" for i in range(count)]}
    if reader == "show_schemas":
        return {"schemas": [f"RAW.S{i}" for i in range(count)]}
    if reader == "show_database_roles":
        return {"database_roles": {"RAW": [f"R{i}" for i in range(count)]}}
    if reader == "show_roles_granted_to_user":
        return {"user_roles": {"HEIDI": [f"R{i}" for i in range(count)]}}
    if reader == "show_future_grants_to_role":
        return {
            "future_grants": [
                FutureGrant("LOADER", "SELECT", "TABLE", f"RAW.S{i}")
                for i in range(count)
            ]
        }
    if reader == "show_account_grants_to_role":
        return {
            "grants": [
                Grant("LOADER", f"PRIVILEGE {i}", "ACCOUNT", "-") for i in range(count)
            ]
        }
    return {
        "grants": [
            Grant("LOADER", "SELECT", "TABLE", f"RAW.PUBLIC.T{i}") for i in range(count)
        ]
    }


CAPPED_READS = [
    ("show_databases", ()),
    ("show_warehouses", ()),
    ("show_users", ()),
    ("show_schemas", ("raw",)),
    ("show_database_roles", ("raw",)),
    ("show_grants_to_role", ("loader",)),
    ("show_account_grants_to_role", ("loader",)),
    ("show_grants_to_role_with_grant_option", ("loader",)),
    ("show_future_grants_to_role", ("loader",)),
    ("show_roles_granted_to_user", ("heidi",)),
]


@pytest.mark.parametrize(
    "held,falls_back",
    [(2, False), (3, True), (4, True)],
    ids=["one under", "at the limit", "one over"],
)
@pytest.mark.parametrize("reader,args", CAPPED_READS)
def test_a_listing_at_the_limit_is_read_through_sql(
    mocker, reader, args, held, falls_back
):
    account = FakeAccount(
        roles={"LOADER": "SECURITYADMIN"},
        rest_list_cap=3,
        **capped_state(reader, held),
    )
    account.install(mocker)
    conn = SnowflakeConnector()

    result = getattr(conn, reader)(*args)

    assert len(account.api_calls) == 1
    assert bool(account.queries) is falls_back
    assert conn.read_stats.fallbacks == (
        Counter({(reader, "limit"): 1}) if falls_back else Counter()
    )
    assert result == getattr(SnowflakeConnector(sql_only=True), reader)(*args)


@pytest.mark.parametrize(
    "held,falls_back",
    [(LIST_LIMIT - 1, False), (LIST_LIMIT, True), (LIST_LIMIT + 1, True)],
    ids=["one under", "at the limit", "one over"],
)
def test_the_limit_is_ten_thousand_rows(mocker, held, falls_back):
    account = FakeAccount(
        roles={"LOADER": "SECURITYADMIN"},
        **capped_state("show_grants_to_role", held),
    )
    account.install(mocker)

    grants = SnowflakeConnector().show_grants_to_role("loader")

    assert account.queries == (["SHOW GRANTS TO ROLE loader"] if falls_back else [])
    assert len(grants["select"]["table"]) == held
    assert LIST_LIMIT == SnowflakeApi.list_limit == 10_000


# PERMISSION_BOT_API_STRICT


@pytest.mark.parametrize(
    "reason,patch",
    [
        ("limit", {"rest_list_cap": 1}),
        ("validation", {"side_effect": invalid_model}),
        ("api error", {"side_effect": raises(api_error(500, "Internal error"))}),
        ("transport", {"side_effect": raises(ConnectionResetError("reset"))}),
    ],
)
def test_strict_mode_raises_instead_of_falling_back(mocker, monkeypatch, reason, patch):
    account = names_account(rest_list_cap=patch.get("rest_list_cap", LIST_LIMIT))
    account.install(mocker)
    if "side_effect" in patch:
        mocker.patch.object(FakeApi, "databases", side_effect=patch["side_effect"])
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    with pytest.raises(ApiFallbackError, match="PERMISSION_BOT_API_STRICT") as error:
        SnowflakeConnector().show_databases()

    assert (error.value.reader, error.value.reason) == ("show_databases", reason)
    assert account.queries == []


def test_strict_mode_looks_an_object_up_through_sql(mocker, monkeypatch):
    account = names_account(rest_list_cap=1)
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    # A lookup runs during apply, after a statement failed, so raising would stop the
    # run with some statements applied
    assert not SnowflakeConnector().object_exists("database", "GONE")
    assert account.queries == ["SHOW DATABASES"]


def test_strict_mode_leaves_missing_objects_and_sql_only_alone(mocker, monkeypatch):
    names_account().install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    with pytest.raises(NotFoundError):
        SnowflakeConnector().show_grants_to_role("gone")
    assert SnowflakeConnector(sql_only=True).show_databases()


# The transport switch


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_sql_only_environment_variable(mocker, monkeypatch, value):
    account = names_account()
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", value)
    conn = SnowflakeConnector()

    conn.show_databases()
    conn.show_future_grants_to_role("loader")

    assert conn.sql_only
    assert account.api_calls == []
    assert account.queries == ["SHOW DATABASES", "SHOW FUTURE GRANTS TO ROLE loader"]


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_sql_only_environment_variable_off(mocker, monkeypatch, value):
    names_account().install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", value)

    assert not SnowflakeConnector().sql_only


def test_sql_only_can_be_set_on_a_connector(mocker):
    account = names_account()
    account.install(mocker)
    conn = SnowflakeConnector()

    conn.sql_only = True
    conn.show_users()

    assert account.api_calls == []


def test_sql_only_connector_never_builds_the_api(mocker, real_build_api):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    conn = SnowflakeConnector(sql_only=True)
    mocker.patch.object(conn, "run_query")

    conn.show_databases()
    conn.show_grants_to_role("loader")

    create_engine.return_value.raw_connection.assert_not_called()


# SnowflakeConnector.api


class WatchedLock:
    """A lock that counts the threads that have asked for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._asked = threading.Semaphore(0)

    def __enter__(self) -> None:
        self._asked.release()
        self._lock.acquire()

    def __exit__(self, *exc) -> None:
        self._lock.release()

    def wait_for(self, threads: int) -> None:
        for _ in range(threads):
            assert self._asked.acquire(timeout=5), "readers didn't ask for the lock"


def build_in_parallel(mocker, target, outcome, threads=8):
    """
    Run `target` on `threads` threads while the first one is building Root, which
    returns or raises `outcome` once every thread is waiting on the API lock.
    """
    lock = WatchedLock()
    mocker.patch.object(SnowflakeConnector, "_api_lock", lock)
    release = threading.Event()

    def build(connection):
        assert release.wait(5)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    root = mocker.patch("snowflake.core.Root", side_effect=build)
    workers = [threading.Thread(target=target) for _ in range(threads)]
    for worker in workers:
        worker.start()
    try:
        lock.wait_for(threads)
    finally:
        release.set()
        for worker in workers:
            worker.join()
    return root


def test_api_is_built_once_over_a_pooled_connection(mocker, real_build_api):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    conn = SnowflakeConnector()
    pooled = create_engine.return_value.raw_connection.return_value

    apis = []
    root = build_in_parallel(
        mocker, lambda: apis.append(conn.api), mocker.sentinel.root
    )

    assert len(apis) == 8
    assert all(api is apis[0] for api in apis)
    assert isinstance(apis[0], SnowflakeApi)
    assert apis[0].root is mocker.sentinel.root
    root.assert_called_once_with(pooled.driver_connection)
    create_engine.return_value.raw_connection.assert_called_once_with()
    # Kept checked out for the run
    assert conn._api_connection is pooled
    pooled.close.assert_not_called()


def test_api_that_fails_to_build_returns_its_connection_and_reads_sql(
    mocker, real_build_api
):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    mocker.patch(
        "snowflake.core.Root",
        side_effect=snowflake.connector.errors.ProgrammingError("login failed"),
    )
    conn = SnowflakeConnector()
    run_query = mocker.patch.object(conn, "run_query")

    conn.show_databases()
    conn.show_warehouses()

    create_engine.return_value.raw_connection.return_value.close.assert_called_once()
    assert [c.args[0] for c in run_query.call_args_list] == [
        "SHOW DATABASES",
        "SHOW WAREHOUSES",
    ]
    assert conn.read_stats.fallbacks == Counter({("show_databases", "transport"): 1})


def test_api_that_fails_to_build_is_tried_once_by_parallel_readers(
    mocker, real_build_api
):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    conn = SnowflakeConnector()
    mocker.patch.object(conn, "run_query")

    # The readers waiting on the lock get the stored error, rather than log in again
    root = build_in_parallel(mocker, conn.show_databases, OSError("unreachable"))

    root.assert_called_once()
    create_engine.return_value.raw_connection.assert_called_once_with()
    assert conn.read_stats.by_transport() == Counter({"sql": 8})
    assert conn.read_stats.fallbacks == Counter({("show_databases", "transport"): 8})


def test_account_schemas_stay_sql(mocker):
    account = names_account()
    account.install(mocker)

    SnowflakeConnector().show_schemas()

    assert account.api_calls == []
    assert account.queries == ["SHOW TERSE SCHEMAS IN ACCOUNT"]


# The summary line


def stats(listings=(), fallbacks=()) -> ReadStats:
    result = ReadStats()
    for reader, transport, count in listings:
        result.listings[reader, transport] += count
    for reader, reason, count in fallbacks:
        result.fallbacks[reader, reason] += count
    return result


@pytest.mark.parametrize(
    "read,sql_only,line",
    [
        (
            stats([("show_databases", "api", 3), ("show_users", "api", 1)]),
            False,
            "  API-capable listings: 4 through the API, 0 through SQL.",
        ),
        (
            stats([("show_databases", "sql", 4)]),
            True,
            "  API-capable listings: 0 through the API, 4 through SQL (SQL only).",
        ),
        (
            stats(
                [("show_grants_to_role", "api", 5), ("show_grants_to_role", "sql", 3)],
                [
                    ("show_grants_to_role", "limit", 2),
                    ("show_schemas", "non-json", 1),
                ],
            ),
            False,
            "  API-capable listings: 5 through the API, 3 through SQL"
            " (fallbacks: 2 at the row limit, 1 non-JSON error).",
        ),
        (
            stats(
                [("show_databases", "api", 1), ("show_users", "sql", 6)],
                [("show_users", "transport", 1)],
            ),
            False,
            "  API-capable listings: 1 through the API, 6 through SQL"
            " (fallbacks: 1 transport error, then SQL for the rest of the run).",
        ),
    ],
)
def test_summary(read, sql_only, line):
    assert read.summary(sql_only=sql_only) == line
