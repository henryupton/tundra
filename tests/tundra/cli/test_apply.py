# How apply maps what snowflake.core raises for a statement to the outcomes it has
# always printed, against a fake account that answers the adapter's write calls the
# way Snowflake does and the statements' SQL the way SQL does.

import json
import logging
from collections import Counter

import pytest
import urllib3
from pydantic import ValidationError
from snowflake.core._generated import api_client
from snowflake.core.exceptions import APIError, RetryTimeoutError
from snowflake.core.role import Securable

from tundra.cli.permissions import apply_query, print_command, report_writes
from tundra.identifiers import ObjectName
from tundra.operations import (
    FUTURE,
    GRANT,
    QUOTED_BODY,
    QUOTED_PATH,
    REVOKE,
    ROUTES,
    BulkPrivileges,
    PlannedCommand,
    Privileges,
    Route,
    SqlStatement,
)
from tundra.snowflake_api import SnowflakeApi
from tundra.snowflake_connector import (
    API,
    FALLBACK_REASONS,
    SQL,
    STICKY_REASONS,
    WRITE_FALLBACK_REASONS,
    SnowflakeConnector,
    WriteStats,
)
from tundra_test_utils.fake_account import (
    WRITE_CALLS,
    FakeAccount,
    FakeApi,
    FutureGrant,
    api_error,
)
from tundra_test_utils.offline_http import OfflineHttp
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector

LOADER = ObjectName.single("loader")


def account(**options) -> FakeAccount:
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.PUBLIC", "RAW.LANDING", 'RAW."Mixed-Case"'],
        objects={"TABLES": ["RAW.PUBLIC.EVENTS"]},
        roles={
            "LOADER": "SECURITYADMIN",
            "ANALYST": "SECURITYADMIN",
            "LOCKED": "SECURITYADMIN",
        },
        unmanageable_roles={"LOCKED"},
        **options,
    )


@pytest.fixture
def fake(mocker) -> FakeAccount:
    installed = account()
    installed.install(mocker)
    return installed


@pytest.fixture
def conn(fake) -> SnowflakeConnector:
    return SnowflakeConnector()


@pytest.fixture
def quoted_on_api(monkeypatch):
    """Quoted names go through the API, as they will once a dev probe has sent them."""
    for gate in (QUOTED_PATH, QUOTED_BODY):
        monkeypatch.setitem(ROUTES, gate, Route(api=True, evidence="for the test"))


def future_tables(schema: str = "raw.public", role=LOADER) -> PlannedCommand:
    """A statement on one of the API rows: future tables in a schema."""
    return PlannedCommand(
        False,
        BulkPrivileges(GRANT, FUTURE, ("select",), "table", ObjectName(schema), role),
    )


def writes(fake: FakeAccount) -> list:
    return [call for call in fake.api_calls if call[0] in WRITE_CALLS]


def sql_writes(fake: FakeAccount) -> list:
    return [q for q in fake.queries if q.startswith(("GRANT ", "REVOKE "))]


def test_an_api_row_runs_through_the_api(fake, conn):
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert writes(fake) == [
        (
            "grant_future_privileges",
            "LOADER",
            ["SELECT"],
            "table",
            {"database": "RAW", "schema": "PUBLIC"},
        )
    ]
    assert sql_writes(fake) == []
    assert fake.state()["future_grants"] == [
        ("LOADER", "SELECT", "TABLE", ("RAW", "PUBLIC"))
    ]
    assert conn.write_stats.statements == Counter({API: 1})


def test_a_quoted_name_runs_as_sql_until_its_gate_flips(fake, conn):
    command = future_tables('raw."Mixed-Case"')

    apply_query(conn, command)

    assert command["run_status"] is True
    assert writes(fake) == []
    assert sql_writes(fake) == [command["sql"]]
    assert conn.write_stats.statements == Counter({SQL: 1})


EDITS = [
    pytest.param(
        Privileges(GRANT, ("usage",), "database", ObjectName.single("raw"), LOADER),
        False,
        id="API row",
    ),
    pytest.param(
        Privileges(GRANT, ("usage",), "schema", ObjectName("raw.public"), LOADER),
        False,
        id="SQL row",
    ),
    pytest.param(
        Privileges(GRANT, ("usage",), "database", ObjectName.single("raw"), LOADER),
        True,
        id="SQL only",
    ),
]


@pytest.mark.parametrize("operation,sql_only", EDITS)
def test_a_command_whose_sql_a_caller_changed_runs_that_sql(
    fake, conn, capsys, operation, sql_only
):
    conn.sql_only = sql_only
    command = PlannedCommand(False, operation)
    command["sql"] = operation.sql.replace("ROLE loader", "ROLE analyst")

    apply_query(conn, command)
    print_command(command, diff=False)

    assert capsys.readouterr().out == f"[SUCCESS] {command['sql']};\n"
    assert writes(fake) == []
    assert sql_writes(fake) == [command["sql"]]
    assert {grant[0] for grant in fake.state()["grants"]} == {"ANALYST"}


def test_a_dropped_object_is_vanished_from_a_lowercased_message(
    fake, conn, quoted_on_api
):
    fake.schemas.remove('RAW."Mixed-Case"')
    command = future_tables('raw."Mixed-Case"')

    apply_query(conn, command)

    assert command["run_status"] is False
    assert command["vanished"] is True
    assert command["error"] == (
        "002003 (404): schema 'raw.mixed-case' does not exist or not authorized."
        " your primary role securityadmin must have at least one privilege granted on"
        " schema raw.mixed-case."
    )
    # The lookup listed the schema's database, and only one write was sent
    assert ("schemas", "RAW") in fake.api_calls
    assert len(writes(fake)) == 1


def test_a_dropped_database_vanishes_a_statement_in_one_of_its_schemas(fake, conn):
    fake.databases.remove("RAW")
    fake.schemas[:] = [s for s in fake.schemas if not s.startswith("RAW.")]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is False
    assert command["vanished"] is True
    assert command["error"].startswith(
        "002003 (404): database 'raw' does not exist or not authorized."
    )
    assert ("databases",) in fake.api_calls
    assert len(writes(fake)) == 1


def test_a_revoke_in_a_dropped_schema_is_vanished(fake, conn):
    fake.future_grants.append(FutureGrant("LOADER", "SELECT", "TABLE", "RAW.LANDING"))
    fake.schemas.remove("RAW.LANDING")
    command = PlannedCommand(
        False,
        BulkPrivileges(
            REVOKE, FUTURE, ("select",), "table", ObjectName("raw.landing"), LOADER
        ),
    )

    apply_query(conn, command)

    assert command["run_status"] is False
    assert command["vanished"] is True
    assert [call[0] for call in writes(fake)] == ["revoke_future_privileges"]


def test_a_recreated_object_is_retried_once_by_its_own_name(fake, conn, quoted_on_api):
    # The 404 lowercases the name, so only the operation's own name, raw."Mixed-Case",
    # finds the schema again: raw."mixed-case" would read as gone, and vanish it
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, "schema 'raw.mixed-case' does not exist or not authorized.")
    ]
    command = future_tables('raw."Mixed-Case"')

    apply_query(conn, command)

    assert command["run_status"] is True
    assert not command.get("vanished")
    assert len(writes(fake)) == 2
    assert fake.state()["future_grants"] == [
        ("LOADER", "SELECT", "TABLE", ("RAW", "Mixed-Case"))
    ]


def test_a_retry_that_fails_again_is_an_error(fake, conn):
    gone = "schema 'raw.public' does not exist or not authorized."
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, gone),
        api_error(404, gone),
    ]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is False
    assert not command.get("vanished")
    assert command["error"] == f"002003 (404): {gone}"


def test_a_retry_is_counted_by_the_transport_it_ended_on(fake, conn, mocker):
    # The 404's lookup finds the API not answering, so the retry runs as SQL
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, "schema 'raw.public' does not exist or not authorized.")
    ]
    mocker.patch.object(
        FakeApi,
        "schemas",
        side_effect=urllib3.exceptions.MaxRetryError(None, "/api/v2/databases"),
    )
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert len(writes(fake)) == 1
    assert sql_writes(fake) == [command["sql"]]
    assert conn.write_stats.statements == Counter({SQL: 1})


@pytest.mark.parametrize(
    "message",
    [
        "Role 'loader' does not exist or not authorized.",
        # A part the statement isn't on
        "schema 'raw.other' does not exist or not authorized.",
        "database 'other' does not exist or not authorized.",
        # Wording tundra doesn't know
        "Object 'RAW.PUBLIC' not found.",
    ],
    ids=["role", "another schema", "another database", "other wording"],
)
def test_a_404_that_doesnt_name_the_target_gets_sqls_answer(
    fake, conn, caplog, message
):
    fake.api_write_errors["grant_future_privileges"] = [api_error(404, message)]
    command = future_tables()

    apply_query(conn, command)

    # SQL finds the schema there and the role too, so the statement runs
    assert command["run_status"] is True
    assert not command.get("vanished")
    assert len(writes(fake)) == 1
    assert sql_writes(fake) == [command["sql"]]
    assert ("schemas", "RAW") not in fake.api_calls
    assert conn.write_stats.fallbacks == Counter({"unmatched": 1})
    assert conn.write_stats.statements == Counter({SQL: 1})
    assert not conn._api_failed
    (warning,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning.getMessage().startswith(
        f"{command['sql']} through snowflake.core failed (unrecognised 404), so it"
        " runs as SQL: 002003 (404): "
    )


def test_an_unmatched_404_for_a_dropped_schema_is_vanished_by_sql(fake, conn):
    fake.schemas.remove("RAW.LANDING")
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, "Object 'RAW.LANDING' not found.")
    ]
    command = future_tables("raw.landing")

    apply_query(conn, command)

    assert command["run_status"] is False
    assert command["vanished"] is True
    assert sql_writes(fake) == [command["sql"]]


def test_a_statement_the_api_couldnt_answer_is_retried_as_sql(
    fake, conn, monkeypatch, caplog
):
    # The API's 404 doesn't name the schema, and SQL finds it gone and then back
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, "Object 'RAW.LANDING' not found.")
    ] * 2
    command = future_tables("raw.landing")
    run_query = conn.run_query
    ran = []

    def recreated(query):
        if query.startswith("GRANT "):
            ran.append(query)
            if len(ran) == 1:
                raise Exception(
                    "002003 (02000): SQL compilation error:\n"
                    "Schema 'RAW.LANDING' does not exist or not authorized."
                )
        return run_query(query)

    monkeypatch.setattr(conn, "run_query", recreated)

    apply_query(conn, command)

    assert command["run_status"] is True
    assert not command.get("vanished")
    # One request, one fallback and one warning for the statement
    assert len(writes(fake)) == 1
    assert ran == [command["sql"]] * 2
    assert conn.write_stats.statements == Counter({SQL: 1})
    assert conn.write_stats.fallbacks == Counter({"unmatched": 1})
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


@pytest.mark.parametrize("sql_only", [False, True], ids=["api", "sql"])
def test_a_missing_spec_role_is_an_error_on_both_transports(fake, conn, sql_only):
    conn.sql_only = sql_only
    del fake.roles["LOADER"]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is False
    assert not command.get("vanished")
    # SQL's answer, in its uppercase, wins after the API's 404 for the role
    assert "Role 'LOADER' does not exist or not authorized." in command["error"]
    assert [call[0] for call in writes(fake)] == (
        [] if sql_only else ["grant_future_privileges"]
    )
    assert sql_writes(fake) == [command["sql"]]
    assert conn.write_stats.fallbacks == (
        Counter() if sql_only else Counter({"unmatched": 1})
    )


@pytest.mark.parametrize(
    "status,code",
    [(404, "390400"), (403, "002003"), (400, "002003")],
    ids=["404 with another code", "403 with 002003", "400 with 002003"],
)
def test_only_a_404_with_002003_is_a_missing_object(fake, conn, status, code):
    gone = "schema 'raw.public' does not exist or not authorized."
    fake.api_write_errors["grant_future_privileges"] = [api_error(status, gone, code)]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is False
    assert not command.get("vanished")
    assert command["error"] == f"{code} ({status}): {gone}"
    # No lookup, no retry and no SQL
    assert ("schemas", "RAW") not in fake.api_calls
    assert len(writes(fake)) == 1
    assert sql_writes(fake) == []


# Error bodies snowflake.core's APIError can't be built from, as a gateway may send
UNREADABLE_BODIES = [
    pytest.param(b"<html>Bad Gateway</html>", json.JSONDecodeError, id="not JSON"),
    pytest.param(
        b"<html>Passerelle d\xe9faillante</html>", UnicodeDecodeError, id="not UTF-8"
    ),
    pytest.param(b'"Bad Gateway"', AttributeError, id="a JSON string"),
    pytest.param(b'{"message": null}', AttributeError, id="a null message"),
]


def unreadable(body: bytes, raised: type):
    def error() -> Exception:
        try:
            api_error(502, body=body)
        except raised as error:
            return error
        raise AssertionError("snowflake.core should fail to read the body")

    return error


FALLBACKS = [
    pytest.param(
        "syntax",
        lambda: api_error(
            400, "syntax error line 1 at position 12 unexpected 'on'.", "001003"
        ),
        "syntax error",
        False,
        id="001003",
    ),
    pytest.param(
        "server",
        lambda: api_error(500, "Internal error", "000603"),
        "server error",
        False,
        id="500",
    ),
    pytest.param(
        "server",
        lambda: api_error(502, "Bad gateway", "000000"),
        "server error",
        False,
        id="502",
    ),
    *(
        pytest.param(
            "non-json",
            unreadable(*body.values),
            "non-JSON error",
            False,
            id=f"body {body.id}",
        )
        for body in UNREADABLE_BODIES
    ),
    pytest.param(
        "timeout",
        lambda: RetryTimeoutError("503 Service Unavailable query timeout"),
        "retry timeout",
        True,
        id="RetryTimeoutError",
    ),
    pytest.param(
        "transport",
        lambda: urllib3.exceptions.MaxRetryError(None, "/api/v2/roles/LOADER"),
        "transport error",
        True,
        id="MaxRetryError",
    ),
    pytest.param(
        "transport",
        lambda: urllib3.exceptions.ProtocolError("aborted"),
        "transport error",
        True,
        id="ProtocolError",
    ),
    pytest.param(
        "transport",
        lambda: APIError(root=None, status=0, reason="SSLError"),
        "transport error",
        True,
        id="SSL as status 0",
    ),
]


@pytest.mark.parametrize("reason,error,description,sticky", FALLBACKS)
def test_a_statement_the_api_cant_answer_runs_once_as_its_sql(
    fake, conn, mocker, caplog, reason, error, description, sticky
):
    raised = error()
    fake.api_write_errors["grant_future_privileges"] = [raised]
    failed = mocker.spy(conn, "_fail_api")
    command = future_tables()

    with caplog.at_level(logging.WARNING):
        apply_query(conn, command)

    assert command["run_status"] is True
    assert "error" not in command
    assert sql_writes(fake) == [command["sql"]]
    assert fake.state()["future_grants"] == [
        ("LOADER", "SELECT", "TABLE", ("RAW", "PUBLIC"))
    ]
    assert conn.write_stats.statements == Counter({SQL: 1})
    assert conn.write_stats.fallbacks == Counter({reason: 1})
    assert f"{command['sql']} through snowflake.core failed ({description})" in (
        caplog.text
    )
    # A sticky failure is set in the one locked place reads set it too
    assert failed.call_args_list == ([mocker.call(reason, raised)] if sticky else [])

    # After a transport failure or a timeout the rest of the run goes through SQL
    # without trying
    fake.api_calls.clear()
    apply_query(conn, future_tables("raw.landing"))
    assert (writes(fake) == []) is sticky
    assert conn._api_failed is sticky


@pytest.fixture
def http(monkeypatch):
    capture = OfflineHttp()
    capture.install(monkeypatch)
    return capture


@pytest.fixture
def http_conn(http, monkeypatch):
    """A connector over a real Root that OfflineHttp answers, recording the SQL it runs."""
    monkeypatch.setattr(
        SnowflakeConnector, "api", property(lambda self: SnowflakeApi(http.root))
    )
    connector = MockSnowflakeConnector()
    connector.ran = []
    monkeypatch.setattr(connector, "run_query", connector.ran.append, raising=False)
    return connector


class Clock:
    """time for snowflake.core's retry loop, where sleeping moves the clock on."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("status", [429, 503, 504])
def test_a_status_snowflake_core_retries_runs_as_sql_once_its_window_closes(
    http, http_conn, monkeypatch, status
):
    # snowflake.core resends these until its 600 s retry window closes and then raises
    # RetryTimeoutError, never a ServerError
    monkeypatch.setattr(api_client, "time", Clock())
    path = "/api/v2/roles/LOADER/future-grants"
    http.answer(path, {"message": "busy", "error_code": "000000"}, status, "POST")
    command, after = future_tables(), future_tables("raw.landing")

    apply_query(http_conn, command)
    apply_query(http_conn, after)

    assert command["run_status"] is True
    assert after["run_status"] is True
    assert len(http.requests) > 1
    assert {(r.method, r.path) for r in http.requests} == {("POST", path)}
    assert http_conn.ran == [command["sql"], after["sql"]]
    assert http_conn.write_stats.summary() == (
        "  API-capable statements: 0 through the API, 2 through SQL"
        " (fallbacks: 1 retry timeout, then SQL for the rest of the run)."
    )


@pytest.mark.parametrize("body,raised", UNREADABLE_BODIES)
def test_a_gateway_page_snowflake_core_cant_read_runs_as_sql(
    http, http_conn, body, raised
):
    http.answer("/api/v2/roles/LOADER/future-grants", body, 502, "POST")
    command = future_tables()

    apply_query(http_conn, command)

    assert command["run_status"] is True
    assert http_conn.ran == [command["sql"]]
    assert http_conn.write_stats.fallbacks == Counter({"non-json": 1})


def test_an_api_that_cant_be_built_runs_every_write_as_sql():
    # conftest refuses to build snowflake.core when nothing stands in for it
    connector = MockSnowflakeConnector()
    ran: list = []
    connector.run_query = ran.append
    first, second = future_tables(), future_tables("raw.landing")

    apply_query(connector, first)
    apply_query(connector, second)

    assert first["run_status"] is True
    assert second["run_status"] is True
    assert ran == [first["sql"], second["sql"]]
    assert connector.write_stats.fallbacks == Counter({"transport": 1})
    assert connector.write_stats.statements == Counter({SQL: 2})
    assert connector._api_failed is True


def test_the_sql_fallback_can_fail_too(fake, conn):
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(500, "Internal error", "000603")
    ]
    fake.schemas.remove("RAW.PUBLIC")
    command = future_tables()

    apply_query(conn, command)

    # The SQL error decides the outcome, as for any SQL statement
    assert command["run_status"] is False
    assert command["vanished"] is True
    assert command["error"] == (
        "SQL compilation error: Schema 'RAW.PUBLIC' does not exist or not authorized."
    )


def test_another_api_error_prints_as_an_error_line(fake, conn, capsys):
    command = future_tables(role=ObjectName.single("locked"))

    apply_query(conn, command)
    print_command(command, diff=False)

    assert capsys.readouterr().out == (
        "[ERROR] GRANT select ON FUTURE tables IN schema raw.public TO ROLE locked;\n"
        "    003001 (403): Insufficient privileges to operate on role 'LOCKED'.\n"
    )
    assert sql_writes(fake) == []
    assert conn.write_stats.statements == Counter({API: 1})


def test_a_multi_line_api_message_prints_on_one_detail_line(fake, conn, capsys):
    # CD's grep keeps the [ERROR] line and the indented lines under it
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(403, "SQL access control error:\nInsufficient privileges", "003001")
    ]
    command = future_tables()

    apply_query(conn, command)
    print_command(command, diff=False)

    assert capsys.readouterr().out == (
        "[ERROR] GRANT select ON FUTURE tables IN schema raw.public TO ROLE loader;\n"
        "    003001 (403): SQL access control error: Insufficient privileges\n"
    )


def validation_error() -> ValidationError:
    try:
        Securable(name="not an identifier")
    except ValidationError as error:
        return error
    raise AssertionError("snowflake.core should reject the name")


def test_a_request_snowflake_core_rejects_is_an_error_not_a_fallback(fake, conn):
    # SQL wouldn't fix a request tundra built wrongly
    fake.api_write_errors["grant_future_privileges"] = [validation_error()]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is False
    assert "Securable" in command["error"]
    assert sql_writes(fake) == []
    assert conn.write_stats.fallbacks == Counter()
    assert conn._api_failed is False


def test_strict_mode_is_for_reads_and_cant_stop_an_apply(fake, conn, monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(500, "Internal error", "000603")
    ]
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert conn.write_stats.fallbacks == Counter({"server": 1})


def test_a_transport_failure_while_reading_sends_every_write_through_sql(
    fake, conn, mocker
):
    mocker.patch.object(
        FakeApi,
        "databases",
        side_effect=urllib3.exceptions.MaxRetryError(None, "/api/v2/databases"),
    )
    conn.show_databases()
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert writes(fake) == []
    assert sql_writes(fake) == [command["sql"]]
    assert conn.write_stats.fallbacks == Counter()


@pytest.mark.parametrize("how", ["connector", "env"])
def test_sql_only_sends_every_write_through_sql(fake, conn, monkeypatch, how):
    if how == "connector":
        conn.sql_only = True
    else:
        monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", "true")
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert fake.api_calls == []
    assert sql_writes(fake) == [command["sql"]]
    assert conn.write_stats.summary(sql_only=conn.sql_only) == (
        "  API-capable statements: 0 through the API, 1 through SQL (SQL only)."
    )


def test_a_row_still_on_sql_runs_its_sql(fake, conn):
    command = PlannedCommand(
        False,
        Privileges(GRANT, ("usage",), "schema", ObjectName("raw.public"), LOADER),
    )

    apply_query(conn, command)

    assert command["run_status"] is True
    assert writes(fake) == []
    assert sql_writes(fake) == ["GRANT usage ON schema raw.public TO ROLE loader"]
    assert conn.write_stats.statements == Counter({SQL: 1})


def test_statements_only_sql_can_express_arent_counted(fake, conn):
    command = PlannedCommand(
        False,
        SqlStatement(
            "GRANT OWNERSHIP ON schema raw.public TO ROLE loader COPY CURRENT GRANTS"
        ),
    )

    apply_query(conn, command)

    assert command["run_status"] is True
    assert conn.write_stats.statements == Counter()


def test_the_summary_counts_fallbacks_by_reason(fake, conn):
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(400, "syntax error", "001003"),
        api_error(502, "Bad gateway", "000000"),
    ]
    for schema in ("raw.public", "raw.landing", "raw.public"):
        apply_query(conn, future_tables(schema))
    apply_query(
        conn,
        PlannedCommand(
            False,
            Privileges(GRANT, ("usage",), "schema", ObjectName("raw.public"), LOADER),
        ),
    )

    assert conn.write_stats.summary() == (
        "  API-capable statements: 1 through the API, 3 through SQL"
        " (fallbacks: 1 syntax error, 1 server error)."
    )


def test_a_repeated_fallback_is_logged_at_info_with_snowflakes_message(
    fake, conn, caplog
):
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(400, "syntax error line 1 at position 12.", "001003")
    ] * 2
    first, second = future_tables(), future_tables("raw.landing")

    with caplog.at_level(logging.INFO, logger="tundra.logger"):
        apply_query(conn, first)
        apply_query(conn, second)

    logged = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if "through snowflake.core failed" in r.getMessage()
    ]
    assert logged == [
        (
            level,
            f"{command['sql']} through snowflake.core failed (syntax error), so it"
            " runs as SQL: 001003 (400): syntax error line 1 at position 12.",
        )
        for level, command in [(logging.WARNING, first), (logging.INFO, second)]
    ]
    assert conn.write_stats.fallbacks == Counter({"syntax": 2})


def test_the_rest_of_the_run_follows_the_reason_that_sent_it_to_sql():
    stats = WriteStats()
    stats.statements[SQL] += 3
    stats.fell_back("stall")
    stats.fell_back("unmatched")

    assert stats.summary() == (
        "  API-capable statements: 0 through the API, 3 through SQL (fallbacks: 1"
        " unrecognised 404, 1 missed deadline, then SQL for the rest of the run)."
    )


@pytest.mark.parametrize(
    "reasons", [FALLBACK_REASONS, WRITE_FALLBACK_REASONS], ids=["reads", "writes"]
)
def test_the_sticky_reasons_are_summarised_last(reasons):
    assert set(list(reasons)[-len(STICKY_REASONS) :]) == STICKY_REASONS


@pytest.mark.parametrize(
    "fallbacks,api_failure,diverted,summary",
    [
        ({}, None, 0, "1 through the API, 2 through SQL."),
        (
            {},
            "stall",
            2,
            "1 through the API, 2 through SQL (SQL for the rest of the run after a"
            " listing's missed deadline).",
        ),
        (
            {"unmatched": 1},
            "transport",
            1,
            "1 through the API, 2 through SQL (fallbacks: 1 unrecognised 404, then SQL"
            " for the rest of the run after a listing's transport error).",
        ),
        # A statement's own failure is already named
        (
            {"timeout": 1},
            "timeout",
            1,
            "1 through the API, 2 through SQL (fallbacks: 1 retry timeout, then SQL for"
            " the rest of the run).",
        ),
        # The API failed, but no statement it was meant to take ran as SQL for it
        ({}, "stall", 0, "1 through the API, 2 through SQL."),
    ],
    ids=[
        "none",
        "a listing's",
        "a listing's after a fallback",
        "a statement's",
        "a listing's that sent nothing to SQL",
    ],
)
def test_the_summary_names_a_listings_failure_for_the_run(
    fallbacks, api_failure, diverted, summary
):
    stats = WriteStats()
    stats.statements.update({API: 1, SQL: 2})
    stats.fallbacks.update(fallbacks)
    stats.diverted = diverted

    assert stats.summary(api_failure=api_failure) == (
        f"  API-capable statements: {summary}"
    )
    assert stats.any_sent_to_sql is bool(fallbacks or diverted)


def test_a_sql_only_apply_has_a_green_summary(fake, conn, mocker):
    conn.sql_only = True
    secho = mocker.patch("click.secho")

    apply_query(conn, future_tables())
    report_writes(conn)

    secho.assert_called_once_with(
        "  API-capable statements: 0 through the API, 1 through SQL (SQL only).",
        fg="green",
    )
    assert conn.write_stats.diverted == 0
