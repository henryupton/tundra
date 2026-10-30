# A statement apply sends through snowflake.core runs under the same deadline as a REST
# listing, and one with no answer by then runs once as its SQL, as does the rest of the
# run. The request can't be called back, so it may still land, and every stall here is a
# Stall the stall fixture releases at teardown, so no watched thread outlives its test.

import logging
import threading
from collections import Counter

import pytest
import urllib3
from snowflake.core._generated import api_client
from snowflake.core.exceptions import RetryTimeoutError
from test_apply import LOADER, Clock, account, future_tables, sql_writes, writes

from tundra.cli.permissions import apply_query, report_writes
from tundra.concurrency import DeadlineExceeded, ThreadStartError, call_with_deadline
from tundra.identifiers import ObjectName
from tundra.operations import (
    FUTURE,
    GRANT,
    REVOKE,
    BulkPrivileges,
    PlannedCommand,
    Privileges,
    SqlStatement,
)
from tundra.snowflake_api import SnowflakeApi
from tundra.snowflake_connector import (
    API,
    FALLBACK_REASONS,
    SQL,
    WRITE_FALLBACK_REASONS,
    ApiFallbackError,
    InvalidSettingError,
    SnowflakeConnector,
)
from tundra_test_utils.fake_account import FakeApi, FutureGrant, api_error
from tundra_test_utils.offline_http import OfflineHttp
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector
from tundra_test_utils.stall import refuse_watched_threads

FUTURE_GRANTS = "/api/v2/roles/LOADER/future-grants"
OK = {"status": "Statement executed successfully."}

STALLED_SUMMARY = (
    "  API-capable statements: 0 through the API, {} through SQL (fallbacks: 1 missed"
    " deadline, then SQL for the rest of the run)."
)


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "0.01")


@pytest.fixture
def fake(mocker):
    installed = account()
    installed.install(mocker)
    return installed


@pytest.fixture
def conn(fake):
    return SnowflakeConnector()


def hold(mocker, held, call="grant_future_privileges"):
    """FakeApi's `call` held open by `held`, and made once it's released."""
    mocker.patch.object(FakeApi, call, held.then(getattr(FakeApi, call)))


def database_usage() -> PlannedCommand:
    """A statement on another API row, USAGE on a database."""
    return PlannedCommand(
        False,
        Privileges(GRANT, ("usage",), "database", ObjectName.single("raw"), LOADER),
    )


def warnings(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def stall_warning(sql: str) -> str:
    return (
        f"{sql} through snowflake.core failed (missed deadline), so it and the rest of"
        " the run go through SQL, though the API request may still be applied: still"
        " running after 0.01 s (PERMISSION_BOT_API_TIMEOUT sets the deadline)"
    )


def test_a_stall_is_worded_as_for_reads():
    assert WRITE_FALLBACK_REASONS["stall"] == FALLBACK_REASONS["stall"]


def test_a_stalled_write_runs_once_as_sql_while_the_request_is_held(
    fake, conn, mocker, stall, short_deadline
):
    held = stall()
    hold(mocker, held)
    failed = mocker.spy(conn, "_fail_api")
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert "error" not in command
    # The sticky failure is set in the one locked place reads set it too
    (reason, cause), _ = failed.call_args
    assert failed.call_count == 1
    assert reason == "stall" and isinstance(cause, DeadlineExceeded)
    # Answered by SQL while the request is still held, not once it gave up
    assert held.wait_for(1)
    assert [thread.is_alive() for thread in held.threads] == [True]
    assert sql_writes(fake) == [command["sql"]]
    assert fake.state()["future_grants"] == [
        ("LOADER", "SELECT", "TABLE", ("RAW", "PUBLIC"))
    ]
    assert conn.write_stats.statements == Counter({SQL: 1})
    assert conn.write_stats.fallbacks == Counter({"stall": 1})
    reason, cause = conn._api_failure
    assert conn._api_failed and reason == "stall"
    assert isinstance(cause, DeadlineExceeded)


def test_the_rest_of_the_run_goes_through_sql_without_a_thread(
    fake, conn, mocker, caplog, stall, short_deadline
):
    held = stall()
    hold(mocker, held)
    first = future_tables()
    apply_query(conn, first)
    started = mocker.spy(threading.Thread, "start")
    rest = [future_tables("raw.landing"), database_usage()]

    for command in rest:
        apply_query(conn, command)

    assert all(command["run_status"] is True for command in rest)
    assert started.call_count == 0
    assert held.wait_for(1)
    assert len(held.threads) == 1
    # The held request hasn't landed, and nothing else reached the API
    assert writes(fake) == []
    assert sql_writes(fake) == [first["sql"]] + [c["sql"] for c in rest]
    assert conn.write_stats.fallbacks == Counter({"stall": 1})
    assert conn.write_stats.summary() == STALLED_SUMMARY.format(3)
    # One warning for the run, naming the statement and the setting
    assert warnings(caplog) == [stall_warning(first["sql"])]


def test_strict_mode_doesnt_stop_an_apply_on_a_stalled_write(
    fake, conn, mocker, monkeypatch, stall, short_deadline
):
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    hold(mocker, stall())
    first, second = future_tables(), future_tables("raw.landing")

    apply_query(conn, first)
    apply_query(conn, second)

    assert first["run_status"] is True and second["run_status"] is True
    assert sql_writes(fake) == [first["sql"], second["sql"]]
    assert conn.write_stats.fallbacks == Counter({"stall": 1})
    # A lookup still answers, through SQL, and a read outside one says why it can't
    assert conn.object_exists("schema", "raw.public")
    with pytest.raises(ApiFallbackError) as error:
        conn.show_databases()
    assert error.value.reason == "stall"
    assert isinstance(error.value.__cause__, DeadlineExceeded)


LATE = [
    pytest.param((), id="lands"),
    pytest.param(urllib3.exceptions.ProtocolError("aborted"), id="transport error"),
    pytest.param(
        RetryTimeoutError("503 Service Unavailable query timeout"), id="retry timeout"
    ),
    pytest.param(api_error(500, "Internal error", "000603"), id="server error"),
]


def revoke_old() -> PlannedCommand:
    return PlannedCommand(
        False,
        BulkPrivileges(
            REVOKE, FUTURE, ("select",), "table", ObjectName("raw.landing"), LOADER
        ),
    )


@pytest.mark.parametrize("outcome", LATE)
@pytest.mark.parametrize(
    "command,call",
    [
        (future_tables, "grant_future_privileges"),
        (revoke_old, "revoke_future_privileges"),
    ],
    ids=["grant", "revoke"],
)
def test_a_late_write_changes_nothing(
    fake,
    conn,
    mocker,
    monkeypatch,
    capfd,
    caplog,
    stall,
    short_deadline,
    command,
    call,
    outcome,
):
    fake.future_grants.append(FutureGrant("LOADER", "SELECT", "TABLE", "RAW.LANDING"))
    held = stall(outcome)
    hold(mocker, held, call)
    command = command()
    apply_query(conn, command)
    outcome_then = dict(command)
    state = fake.state()
    statements = Counter(conn.write_stats.statements)
    fallbacks = Counter(conn.write_stats.fallbacks)
    failure = conn._api_failure
    hooked = []
    monkeypatch.setattr(threading, "excepthook", hooked.append)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="tundra.logger")
    caplog.clear()
    capfd.readouterr()

    assert held.wait_for(1)
    (thread,) = held.threads
    assert thread.is_alive()
    held.released.set()
    thread.join(5)

    assert thread.daemon and not thread.is_alive()
    # A request that lands repeats what its SQL did
    assert len(writes(fake)) == (1 if outcome == () else 0)
    assert fake.state() == state
    assert dict(command) == outcome_then
    assert conn.write_stats.statements == statements
    assert conn.write_stats.fallbacks == fallbacks
    assert conn._api_failure == failure
    assert hooked == []
    assert caplog.records == []
    assert capfd.readouterr() == ("", "")


def test_the_write_deadline_is_the_setting(fake, conn, mocker, monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "30")
    watched = mocker.patch(
        "tundra.snowflake_connector.call_with_deadline", wraps=call_with_deadline
    )

    apply_query(conn, future_tables())
    apply_query(conn, database_usage())

    assert [call.args[1] for call in watched.call_args_list] == [30.0, 30.0]
    assert conn.write_stats.statements == Counter({API: 2})
    assert len(writes(fake)) == 2


def test_the_deadline_is_read_before_the_api_is_built(fake, conn, mocker, monkeypatch):
    built = mocker.patch.object(
        SnowflakeConnector, "api", new_callable=mocker.PropertyMock
    )
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "soon")

    with pytest.raises(InvalidSettingError):
        conn.execute(future_tables().operation)

    built.assert_not_called()


# No thread for SQL


@pytest.mark.parametrize(
    "operation",
    [
        SqlStatement(
            "GRANT OWNERSHIP ON schema raw.public TO ROLE loader COPY CURRENT GRANTS"
        ),
        Privileges(GRANT, ("usage",), "schema", ObjectName("raw.public"), LOADER),
        future_tables('raw."Mixed-Case"').operation,
    ],
    ids=["only SQL", "a row on SQL", "a gate on SQL"],
)
def test_no_thread_starts_for_a_statement_routed_to_sql(fake, conn, mocker, operation):
    started = mocker.spy(threading.Thread, "start")

    assert conn.execute(operation) == (SQL, None)

    assert started.call_count == 0
    assert fake.api_calls == []


@pytest.mark.parametrize("how", ["connector", "env"])
def test_no_thread_starts_for_a_sql_only_write(fake, conn, mocker, monkeypatch, how):
    if how == "connector":
        conn.sql_only = True
    else:
        monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", "true")
    started = mocker.spy(threading.Thread, "start")

    assert conn.execute(future_tables().operation) == (SQL, None)

    assert started.call_count == 0
    assert fake.api_calls == []


@pytest.mark.parametrize(
    "failure",
    ["stall", urllib3.exceptions.ProtocolError("aborted")],
    ids=["after a stall", "after a transport error"],
)
def test_no_thread_starts_for_a_write_after_a_read_fails_for_the_run(
    fake, conn, mocker, stall, short_deadline, failure
):
    mocker.patch.object(
        FakeApi, "databases", side_effect=stall() if failure == "stall" else failure
    )
    conn.show_databases()
    started = mocker.spy(threading.Thread, "start")
    command = future_tables()

    apply_query(conn, command)

    assert command["run_status"] is True
    assert started.call_count == 0
    assert writes(fake) == []
    assert sql_writes(fake) == [command["sql"]]
    # The read fell back, not the write
    assert conn.write_stats.fallbacks == Counter()


def test_no_thread_starts_when_the_api_cant_be_built(mocker):
    # conftest refuses to build snowflake.core when nothing stands in for it
    connector = MockSnowflakeConnector()
    ran: list = []
    connector.run_query = ran.append
    started = mocker.spy(threading.Thread, "start")
    first, second = future_tables(), future_tables("raw.landing")

    apply_query(connector, first)
    apply_query(connector, second)

    assert started.call_count == 0
    assert ran == [first["sql"], second["sql"]]
    assert connector.write_stats.fallbacks == Counter({"transport": 1})


# Lookups after a stall


def test_a_lookup_after_a_stalled_write_reads_through_sql(
    fake, conn, mocker, stall, short_deadline
):
    hold(mocker, stall())
    apply_query(conn, future_tables())
    fake.api_calls.clear()
    fake.queries.clear()
    started = mocker.spy(threading.Thread, "start")

    assert conn.object_exists("schema", "raw.public")
    assert not conn.object_exists("schema", "raw.gone")

    assert started.call_count == 0
    assert fake.api_calls == []
    assert fake.queries == ["SHOW TERSE SCHEMAS IN DATABASE raw"] * 2


@pytest.mark.parametrize("back", [False, True], ids=["still gone", "recreated"])
def test_a_stalled_write_whose_schema_went_is_settled_through_sql(
    fake, conn, mocker, monkeypatch, stall, short_deadline, back
):
    hold(mocker, stall())
    command = future_tables("raw.landing")
    if not back:
        fake.schemas.remove("RAW.LANDING")
    run_query = conn.run_query
    ran = []

    def recreated(query):
        # When it's back, its SQL finds it gone first and the lookup finds it again
        if query.startswith("GRANT "):
            ran.append(query)
            if back and len(ran) == 1:
                raise Exception(
                    "002003 (02000): SQL compilation error:\n"
                    "Schema 'RAW.LANDING' does not exist or not authorized."
                )
        return run_query(query)

    monkeypatch.setattr(conn, "run_query", recreated)
    started = mocker.spy(threading.Thread, "start")

    apply_query(conn, command)

    # The one request was the stalled one: the lookup and any retry were SQL
    assert started.call_count == 1
    assert command["run_status"] is back
    assert bool(command.get("vanished")) is not back
    assert ran == [command["sql"]] * (2 if back else 1)
    assert writes(fake) == []
    assert ("schemas", "RAW") not in fake.api_calls
    assert "SHOW TERSE SCHEMAS IN DATABASE raw" in fake.queries


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("stall", "missed deadline"),
        (urllib3.exceptions.ProtocolError("aborted"), "transport error"),
    ],
    ids=["stall", "transport error"],
)
def test_a_lookup_that_fails_for_the_run_says_why_later_statements_ran_as_sql(
    fake, conn, mocker, caplog, stall, short_deadline, failure, reason
):
    # The first statement's 404 names its schema, and the lookup for it fails
    fake.api_write_errors["grant_future_privileges"] = [
        api_error(404, "schema 'raw.public' does not exist or not authorized.")
    ]
    mocker.patch.object(
        FakeApi, "schemas", side_effect=stall() if failure == "stall" else failure
    )
    first, second = future_tables(), future_tables("raw.landing")
    secho = mocker.patch("click.secho")

    apply_query(conn, first)
    apply_query(conn, second)
    report_writes(conn)

    assert first["run_status"] is True and second["run_status"] is True
    assert len(writes(fake)) == 1
    assert sql_writes(fake) == [first["sql"], second["sql"]]
    assert conn.write_stats.fallbacks == Counter()
    # The one warning says writes switch too, and the summary names the listing's
    (warning,) = warnings(caplog)
    assert warning.startswith(
        f"show_schemas through snowflake.core failed ({reason}), so the rest of the"
        " run goes through SQL: "
    )
    secho.assert_called_once_with(
        "  API-capable statements: 0 through the API, 2 through SQL (SQL for the rest"
        f" of the run after a listing's {reason}).",
        fg="yellow",
    )


def test_a_write_with_no_thread_to_send_it_runs_as_sql_for_the_rest_of_the_run(
    fake, conn, mocker, caplog
):
    refused = refuse_watched_threads(mocker)
    failed = mocker.spy(conn, "_fail_api")
    first, second = future_tables(), future_tables("raw.landing")

    apply_query(conn, first)
    apply_query(conn, second)

    # Nothing was sent, so SQL runs each statement, and the API isn't tried again
    assert first["run_status"] is True and second["run_status"] is True
    assert writes(fake) == []
    assert sql_writes(fake) == [first["sql"], second["sql"]]
    assert conn.write_stats.statements == Counter({SQL: 2})
    assert conn.write_stats.fallbacks == Counter({"transport": 1})
    (reason, cause), _ = failed.call_args
    assert failed.call_count == 1
    assert reason == "transport" and isinstance(cause, ThreadStartError)
    assert cause.__cause__ is refused
    assert warnings(caplog) == [
        f"{first['sql']} through snowflake.core failed (transport error), so it and"
        " the rest of the run go through SQL: no thread to run the call on: can't"
        " start new thread"
    ]


# Through snowflake.core


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


def test_a_stall_inside_snowflake_core_runs_the_statement_as_sql(
    http, http_conn, caplog, stall, short_deadline
):
    held = stall(OK)
    http.answer(FUTURE_GRANTS, held, method="POST")
    command, after = future_tables(), future_tables("raw.landing")

    apply_query(http_conn, command)
    apply_query(http_conn, after)

    assert command["run_status"] is True and after["run_status"] is True
    assert held.wait_for(1)
    assert [thread.is_alive() for thread in held.threads] == [True]
    assert [(r.method, r.path) for r in http.requests] == [("POST", FUTURE_GRANTS)]
    assert http_conn.ran == [command["sql"], after["sql"]]
    assert http_conn.write_stats.summary() == STALLED_SUMMARY.format(2)
    assert warnings(caplog) == [stall_warning(command["sql"])]

    # Its answer, once it comes, is dropped
    caplog.clear()
    held.released.set()
    held.threads[0].join(5)

    assert not held.threads[0].is_alive()
    assert len(http.requests) == 1
    assert http_conn.ran == [command["sql"], after["sql"]]
    assert http_conn.write_stats.summary() == STALLED_SUMMARY.format(2)
    assert warnings(caplog) == []


class HeldClock(Clock):
    """snowflake.core's clock, with each sleep before a resend held by `held` first."""

    def __init__(self, held) -> None:
        super().__init__()
        self.held = held

    def sleep(self, seconds: float) -> None:
        self.held()
        super().sleep(seconds)


@pytest.mark.parametrize("status", [429, 503, 504])
def test_the_deadline_cuts_a_retry_loop_short(
    http, http_conn, monkeypatch, caplog, stall, short_deadline, status
):
    held = stall()
    monkeypatch.setattr(api_client, "time", HeldClock(held))
    http.answer(
        FUTURE_GRANTS, {"message": "busy", "error_code": "000000"}, status, "POST"
    )
    command = future_tables()
    caplog.set_level(logging.DEBUG, logger="tundra.logger")

    apply_query(http_conn, command)

    # Run as SQL while snowflake.core waits to resend, well inside its 600 s window
    assert held.wait_for(1)
    assert len(http.requests) == 1
    assert command["run_status"] is True
    assert http_conn.ran == [command["sql"]]
    assert http_conn.write_stats.summary() == STALLED_SUMMARY.format(1)

    caplog.clear()
    held.released.set()
    (thread,) = set(held.threads)
    thread.join(5)

    # It goes on resending after tundra stopped waiting, until its window closes, and
    # tundra hears nothing of it
    assert not thread.is_alive()
    assert len(http.requests) > 1
    assert {(r.method, r.path) for r in http.requests} == {("POST", FUTURE_GRANTS)}
    assert http_conn.ran == [command["sql"]]
    assert http_conn.write_stats.summary() == STALLED_SUMMARY.format(1)
    assert [r for r in caplog.records if r.name.startswith("tundra")] == []


def test_a_lookup_with_no_thread_to_run_on_still_finds_a_dropped_schema(
    fake, conn, mocker
):
    fake.schemas.remove("RAW.LANDING")
    grant = FakeApi.grant_future_privileges

    def refuse_threads_then_grant(*args, **kwargs):
        # The write's own thread has started, and the lookup's won't
        refuse_watched_threads(mocker)
        return grant(*args, **kwargs)

    mocker.patch.object(FakeApi, "grant_future_privileges", refuse_threads_then_grant)
    command = future_tables("raw.landing")

    apply_query(conn, command)

    # The lookup reads through SQL, which finds the schema gone, as SQL only would
    assert command["run_status"] is False
    assert command["vanished"] is True
    assert conn.api_failure == "transport"
    assert ("schemas", "RAW") not in fake.api_calls
    assert sql_writes(fake) == []
