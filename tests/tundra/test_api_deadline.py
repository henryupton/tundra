# snowflake.core sends its requests with no socket timeout, so each REST listing runs on a
# watched thread under a deadline, and one with no answer by then is read through SQL
# for the rest of the run. Every stall here is a Stall the stall fixture releases at
# teardown, so no watched thread outlives its test.

import contextvars
import logging
import math
import os
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

import click
import pytest
import test_golden_scenarios as golden
import urllib3
from click.testing import CliRunner
from snowflake.core.exceptions import NotFoundError
from test_rest_reads import TRIGGERS, names_account

import tundra
from tundra.cli import cli
from tundra.cli.permissions import apply_query
from tundra.concurrency import DeadlineExceeded, ThreadStartError, call_with_deadline
from tundra.snowflake_api import SnowflakeApi
from tundra.snowflake_connector import (
    DEFAULT_API_TIMEOUT,
    ApiFallbackError,
    InvalidSettingError,
    ReadStats,
    SnowflakeConnector,
    _BufferedResult,
    _fallback_reason,
    api_timeout,
)
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra_test_utils.fake_account import (
    WRITE_CALLS,
    FakeAccount,
    FakeApi,
    Grant,
    api_error,
)
from tundra_test_utils.offline import OfflineError
from tundra_test_utils.offline_http import OfflineHttp
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector
from tundra_test_utils.stall import WATCHED_THREAD, refuse_watched_threads

DATABASES = ["raw", '"SNOWFLAKE"', '"Mixed-Db"', '"lower_db"']


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "0.01")


def watched(side_effect):
    """`side_effect`, recording the threads it's called on."""
    threads = []

    def call(*args):
        threads.append(threading.current_thread())
        if isinstance(side_effect, BaseException):
            raise side_effect
        return side_effect(*args)

    return call, threads


# call_with_deadline


def test_a_call_that_finishes_in_time_gives_its_result():
    assert call_with_deadline(lambda: 42, 5) == 42


def test_a_call_runs_on_a_daemon_thread_in_the_callers_context():
    request = contextvars.ContextVar("request")
    request.set("listing")
    seen = []

    call_with_deadline(
        lambda: seen.append((threading.current_thread(), request.get())), 5
    )

    ((thread, value),) = seen
    assert thread is not threading.current_thread()
    assert thread.daemon and thread.name == WATCHED_THREAD
    assert value == "listing"


@pytest.mark.parametrize(
    "error",
    [ValueError("bad"), OfflineError("a guard")],
    ids=["Exception", "BaseException"],
)
def test_what_a_call_raises_is_raised_to_the_caller(error):
    def fail():
        raise error

    with pytest.raises(type(error)) as raised:
        call_with_deadline(fail, 5)

    assert raised.value is error


def test_a_call_past_its_deadline_raises_deadline_exceeded(stall):
    held = stall()

    with pytest.raises(DeadlineExceeded, match=r"^still running after 0\.01 s$") as e:
        call_with_deadline(held, 0.01)

    assert e.value.deadline == 0.01
    assert held.wait_for(1)


@pytest.mark.parametrize(
    "deadline,raised",
    [(0, ValueError), (-1, ValueError), (math.nan, ValueError), (None, TypeError)],
    ids=["zero", "negative", "nan", "none"],
)
def test_a_bad_deadline_is_refused_before_the_call_starts(deadline, raised):
    called = []

    with pytest.raises(raised):
        call_with_deadline(lambda: called.append(1), deadline)

    assert called == []
    assert WATCHED_THREAD not in [thread.name for thread in threading.enumerate()]


def test_a_thread_that_cant_start_raises_thread_start_error(mocker):
    refused = refuse_watched_threads(mocker)
    called = []

    with pytest.raises(ThreadStartError) as raised:
        call_with_deadline(lambda: called.append(1), 5)

    # Told apart from a call that raised RuntimeError, and never made
    assert raised.value.__cause__ is refused
    assert str(raised.value) == "no thread to run the call on: can't start new thread"
    assert called == []


def test_a_listing_with_no_thread_to_run_on_reads_through_sql_for_the_run(mocker):
    account = names_account()
    account.install(mocker)
    refused = refuse_watched_threads(mocker)
    failed = mocker.spy(SnowflakeConnector, "_fail_api")
    conn = SnowflakeConnector()

    first = conn.show_databases()
    second = conn.show_databases()

    # Nothing was sent, and the API isn't tried again
    assert first == second and first
    assert account.api_calls == []
    assert len(account.queries) == 2
    assert conn.api_failure == "transport"
    (_, reason, cause), _ = failed.call_args
    assert failed.call_count == 1
    assert reason == "transport" and isinstance(cause, ThreadStartError)
    assert cause.__cause__ is refused
    assert conn.read_stats.fallbacks == Counter({("show_databases", "transport"): 1})


def test_strict_mode_raises_for_a_listing_with_no_thread_to_run_on(mocker, monkeypatch):
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "1")
    account = names_account()
    account.install(mocker)
    refuse_watched_threads(mocker)
    conn = SnowflakeConnector()

    with pytest.raises(ApiFallbackError) as raised:
        conn.show_databases()

    assert raised.value.reason == "transport"
    assert isinstance(raised.value.__cause__, ThreadStartError)
    assert account.api_calls == [] and account.queries == []


def test_a_deadline_past_the_lock_limit_is_waited_on():
    # The call is still running when the wait starts, so the wait is given the deadline
    answer = threading.Event()
    answerer = threading.Timer(0.05, answer.set)
    answerer.start()
    try:
        assert call_with_deadline(lambda: answer.wait(5) and 1, 1e300) == 1
    finally:
        answer.set()
        answerer.join(5)


@pytest.mark.parametrize(
    "outcome",
    [["late"], RuntimeError("late"), OfflineError("late")],
    ids=["result", "Exception", "BaseException"],
)
def test_a_call_past_its_deadline_finishes_without_a_trace(
    monkeypatch, capfd, stall, outcome
):
    held = stall(outcome)
    with pytest.raises(DeadlineExceeded):
        call_with_deadline(held, 0.01)
    assert held.wait_for(1)
    hooked = []
    monkeypatch.setattr(threading, "excepthook", hooked.append)
    capfd.readouterr()

    held.released.set()
    (thread,) = held.threads
    thread.join(5)

    assert not thread.is_alive()
    assert hooked == []
    assert capfd.readouterr() == ("", "")


# A stalled listing


def test_a_listing_with_no_answer_reads_through_sql_for_the_rest_of_the_run(
    mocker, caplog, stall, short_deadline
):
    account = names_account()
    account.install(mocker)
    held = stall()
    mocker.patch.object(FakeApi, "databases", side_effect=held)
    conn = SnowflakeConnector()

    assert conn.show_databases() == DATABASES
    # Answered while the listing is still held, not once it gave up
    assert held.wait_for(1)
    assert [thread.is_alive() for thread in held.threads] == [True]
    assert account.queries == ["SHOW DATABASES"]
    assert conn.read_stats.fallbacks == Counter({("show_databases", "stall"): 1})
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ] == [
        "show_databases through snowflake.core failed (missed deadline), so the rest"
        " of the run goes through SQL: still running after 0.01 s"
        " (PERMISSION_BOT_API_TIMEOUT sets the deadline)"
    ]

    conn.show_warehouses()
    conn.show_grants_to_role("loader")

    assert account.api_calls == []
    assert account.queries[1:] == ["SHOW WAREHOUSES", "SHOW GRANTS TO ROLE loader"]
    assert conn.read_stats.fallbacks == Counter({("show_databases", "stall"): 1})
    assert conn.read_stats.by_transport() == Counter({"sql": 3})
    assert conn.read_stats.summary() == (
        "  API-capable listings: 0 through the API, 3 through SQL"
        " (fallbacks: 1 missed deadline, then SQL for the rest of the run)."
    )


def test_a_listing_that_answers_within_the_deadline_is_used(mocker, monkeypatch):
    account = names_account()
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "30")
    entered, answer = threading.Event(), threading.Event()
    listing = FakeApi.databases

    def slow(api):
        entered.set()
        assert answer.wait(5)
        return listing(api)

    mocker.patch.object(FakeApi, "databases", slow)
    # Answers once the reader is waiting on it
    answerer = threading.Thread(target=lambda: entered.wait(5) and answer.set())
    answerer.start()
    try:
        databases = SnowflakeConnector().show_databases()
    finally:
        answer.set()
        answerer.join(5)

    assert databases == DATABASES
    assert account.api_calls == [("databases",)]
    assert account.queries == []


def test_the_deadline_is_the_setting(mocker, monkeypatch):
    names_account().install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "30")
    watched = mocker.patch(
        "tundra.snowflake_connector.call_with_deadline", wraps=call_with_deadline
    )
    conn = SnowflakeConnector()

    conn.show_databases()
    conn.show_future_grants_to_role("loader")

    assert [call.args[1] for call in watched.call_args_list] == [30.0, 30.0]
    assert conn.api_timeout == 30.0


def test_the_shared_classifier_reads_a_missed_deadline_as_a_stall():
    # So the API writes, which classify their errors with it, fall back on a stall too
    assert _fallback_reason(DeadlineExceeded(1)) == "stall"


@pytest.mark.parametrize("reason,side_effect,sticky", TRIGGERS)
def test_each_fallback_trigger_is_read_on_the_watched_thread(
    mocker, reason, side_effect, sticky
):
    names_account().install(mocker)
    listing, threads = watched(side_effect)
    mocker.patch.object(FakeApi, "databases", side_effect=listing)
    conn = SnowflakeConnector()

    assert conn.show_databases() == DATABASES

    (thread,) = threads
    assert thread.name == WATCHED_THREAD and thread is not threading.current_thread()
    assert conn.read_stats.fallbacks == Counter({("show_databases", reason): 1})
    assert conn._api_failed is sticky


@pytest.mark.parametrize(
    "error,raised",
    [
        (
            api_error(404, "Role 'GONE' does not exist or not authorized."),
            NotFoundError,
        ),
        (TypeError("a tundra bug"), TypeError),
        (OfflineError("a guard"), OfflineError),
    ],
    ids=["missing object", "not a fallback", "BaseException"],
)
def test_an_error_that_isnt_a_fallback_is_raised_from_the_watched_thread(
    mocker, error, raised
):
    names_account().install(mocker)
    listing, threads = watched(error)
    mocker.patch.object(FakeApi, "grants_to_role", side_effect=listing)
    conn = SnowflakeConnector()

    with pytest.raises(raised):
        conn.show_grants_to_role("gone")

    assert [thread.name for thread in threads] == [WATCHED_THREAD]
    assert not conn.read_stats.fallbacks
    assert not conn._api_failed


def test_strict_mode_raises_on_a_stall(mocker, monkeypatch, stall, short_deadline):
    account = names_account()
    account.install(mocker)
    mocker.patch.object(FakeApi, "databases", side_effect=stall())
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    with pytest.raises(ApiFallbackError) as error:
        SnowflakeConnector().show_databases()

    assert (error.value.reader, error.value.reason) == ("show_databases", "stall")
    assert str(error.value) == (
        "show_databases would read through SQL instead of the API (missed deadline of"
        " 0.01 s, which PERMISSION_BOT_API_TIMEOUT sets), and PERMISSION_BOT_API_STRICT"
        " is set"
    )
    assert isinstance(error.value.__cause__, DeadlineExceeded)
    assert account.queries == []


@pytest.mark.parametrize(
    "failure,message",
    [
        (
            "stall",
            "missed deadline of 0.01 s, which PERMISSION_BOT_API_TIMEOUT sets",
        ),
        (ConnectionResetError("reset"), "transport error"),
    ],
    ids=["stall", "transport error"],
)
def test_strict_mode_raises_at_once_for_every_read_after_a_sticky_failure(
    mocker, monkeypatch, stall, short_deadline, failure, message
):
    account = names_account()
    account.install(mocker)
    held = stall() if failure == "stall" else failure
    mocker.patch.object(FakeApi, "databases", side_effect=held)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    conn = SnowflakeConnector()
    with pytest.raises(ApiFallbackError) as first:
        conn.show_databases()
    started = mocker.spy(threading.Thread, "start")

    with pytest.raises(ApiFallbackError) as error:
        conn.show_future_grants_to_role("loader")

    assert (error.value.reader, error.value.reason) == (
        "show_future_grants_to_role",
        first.value.reason,
    )
    assert str(error.value) == (
        "show_future_grants_to_role would read through SQL instead of the API"
        f" ({message}), and PERMISSION_BOT_API_STRICT is set"
    )
    assert error.value.__cause__ is first.value.__cause__
    assert started.call_count == 0
    assert account.api_calls == [] and account.queries == []
    assert not conn.read_stats.fallbacks


def test_strict_mode_looks_an_object_up_through_sql_after_a_stall(
    mocker, monkeypatch, stall, short_deadline
):
    account = names_account()
    account.install(mocker)
    mocker.patch.object(FakeApi, "databases", side_effect=stall())
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    conn = SnowflakeConnector()
    with pytest.raises(ApiFallbackError):
        conn.show_databases()
    started = mocker.spy(threading.Thread, "start")

    # Without a second deadline, and a gone database is still found gone
    assert conn.object_exists("database", "raw")
    assert not conn.object_exists("database", "gone")

    assert started.call_count == 0
    assert account.queries == ["SHOW DATABASES"] * 2


@pytest.mark.parametrize(
    "failure",
    ["stall", ConnectionResetError("reset")],
    ids=["stall", "transport error"],
)
def test_a_strict_apply_still_finds_a_vanished_database_after_a_sticky_failure(
    mocker, monkeypatch, stall, short_deadline, failure
):
    account = names_account()
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    listing = FakeApi.databases
    calls = []

    def fails_once(api):
        calls.append(api)
        if len(calls) > 1:
            return listing(api)
        if failure == "stall":
            return held(api)
        raise failure

    held = stall()
    mocker.patch.object(FakeApi, "databases", fails_once)
    conn = SnowflakeConnector()
    run_query = conn.run_query

    def grant_on_gone(query):
        if query.startswith("GRANT"):
            raise Exception(
                "002003 (02000): SQL compilation error:\n"
                "Database 'GONE' does not exist or not authorized."
            )
        return run_query(query)

    monkeypatch.setattr(conn, "run_query", grant_on_gone)
    # An earlier lookup in the same apply fails on the API, and reads through SQL
    assert conn.object_exists("database", "raw")
    query = {"sql": "GRANT USAGE ON DATABASE gone TO ROLE loader"}

    apply_query(conn, query)

    assert query["vanished"] and not query["run_status"]
    assert len(calls) == 1
    assert account.queries == ["SHOW DATABASES"] * 2


def test_the_summary_names_a_stall_apart_from_a_retry_timeout():
    stats = ReadStats()
    stats.listings["show_grants_to_role", "api"] += 4
    stats.listings["show_grants_to_role", "sql"] += 6
    stats.fallbacks["show_grants_to_role", "timeout"] += 1
    stats.fallbacks["show_grants_to_role", "stall"] += 2

    assert stats.summary() == (
        "  API-capable listings: 4 through the API, 6 through SQL (fallbacks: 1 retry"
        " timeout, 2 missed deadline, then SQL for the rest of the run)."
    )


@pytest.mark.parametrize(
    "outcome",
    [
        [],
        ConnectionResetError("reset"),
        api_error(404, "Database 'GONE' does not exist or not authorized."),
    ],
    ids=["result", "transport error", "missing object"],
)
def test_a_late_answer_changes_nothing(
    mocker, monkeypatch, capfd, caplog, stall, short_deadline, outcome
):
    names_account().install(mocker)
    held = stall(outcome)
    mocker.patch.object(FakeApi, "databases", side_effect=held)
    conn = SnowflakeConnector()
    conn.show_databases()
    listings = Counter(conn.read_stats.listings)
    fallbacks = Counter(conn.read_stats.fallbacks)
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
    assert conn.read_stats.listings == listings
    assert conn.read_stats.fallbacks == fallbacks
    assert conn._api_failed
    assert hooked == []
    assert caplog.records == []
    assert capfd.readouterr() == ("", "")


ROLES = [f"role_{i}" for i in range(8)]

ROLE = """
  - {}:
      warehouses:
        - reporting_wh"""

ROLES_SPEC = """
version: "1.0"
warehouses:
  - reporting_wh:
      size: x-small
roles:{}
"""


def roles_account(tmp_path, roles):
    """A spec for `roles`, each using one warehouse, and an account holding them."""
    spec = tmp_path / "roles.yml"
    spec.write_text(ROLES_SPEC.format("".join(map(ROLE.format, roles))))
    account = FakeAccount(
        warehouses=["REPORTING_WH"],
        roles={role.upper(): "SECURITYADMIN" for role in roles},
        grants=[
            Grant(role.upper(), "USAGE", "WAREHOUSE", "REPORTING_WH")
            for role in roles[::2]
        ],
    )
    return spec, account


def test_readers_stalling_together_each_fall_back_and_the_load_finishes(
    mocker, caplog, tmp_path, stall, short_deadline
):
    spec, account = roles_account(tmp_path, ROLES)
    account.install(mocker)
    held = stall()
    mocker.patch.object(FakeApi, "future_grants_to_role", side_effect=held)
    # No stalled reader goes on to SQL until all eight have stalled, so none of them
    # sees the API already failed and skips its listing
    together = threading.Barrier(len(ROLES), timeout=5)
    fall_back = SnowflakeConnector._fall_back

    def fall_back_together(self, reader, reason, sql, error=None):
        if reason == "stall":
            together.wait()
        return fall_back(self, reader, reason, sql, error)

    mocker.patch.object(SnowflakeConnector, "_fall_back", fall_back_together)
    options = dict(skip_validation=True, run_list=["roles"], max_workers=len(ROLES))

    loader = SnowflakeSpecLoader(str(spec), **options)

    assert held.wait_for(len(ROLES))
    assert len(set(held.threads)) == len(ROLES)
    assert all(thread.daemon for thread in held.threads)
    assert loader.conn.read_stats.fallbacks == Counter(
        {("show_future_grants_to_role", "stall"): len(ROLES)}
    )
    assert loader.conn.read_stats.by_transport() == Counter({"sql": 2 * len(ROLES)})
    # One warning for the switch to SQL, however many readers stalled at once
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [
        logging.WARNING
    ]
    # The role grants that followed went straight to SQL
    assert account.api_calls == []
    sql = SnowflakeSpecLoader(str(spec), sql_only=True, **options)
    assert loader.grants_to_role == sql.grants_to_role
    assert loader.grants_to_role


def test_a_strict_load_starts_no_listing_once_one_has_stalled(
    mocker, monkeypatch, tmp_path, stall, short_deadline
):
    workers = 4
    spec, account = roles_account(tmp_path, [f"role_{i}" for i in range(4 * workers)])
    account.install(mocker)
    held = stall()
    mocker.patch.object(FakeApi, "future_grants_to_role", side_effect=held)
    monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")

    with pytest.raises(ApiFallbackError) as error:
        SnowflakeSpecLoader(
            str(spec), skip_validation=True, run_list=["roles"], max_workers=workers
        )

    assert error.value.reason == "stall"
    # Each worker waits out one deadline at most: the roles it takes up after that
    # raise at once, so the load fails after one deadline, not one per worker's role
    assert held.wait_for(1)
    assert len(held.threads) <= workers


# Through snowflake.core


@pytest.fixture
def http(monkeypatch):
    capture = OfflineHttp()
    capture.install(monkeypatch)
    return capture


def test_building_root_sends_no_rest_request(http):
    # So it's left outside the deadline: its one query goes through the connector,
    # which times its own requests out
    assert http.requests == []
    http.root.connection.cursor.return_value.execute.assert_called_once_with(
        "SELECT SYSTEM$CLIENT_VERSION_INFO()"
    )


def offline_conn(http, monkeypatch):
    """A connector whose API is the real Root over `http`, and whose SQL finds RAW."""
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
    return conn


def test_a_stall_inside_snowflake_core_reads_through_sql(
    http, monkeypatch, stall, short_deadline
):
    held = stall(urllib3.exceptions.ProtocolError("Connection aborted"))
    http.answer("/api/v2/databases", held)
    conn = offline_conn(http, monkeypatch)

    assert SnowflakeConnector.show_databases(conn) == ["raw"]

    assert held.wait_for(1)
    assert [thread.is_alive() for thread in held.threads] == [True]
    assert [(r.method, r.path, r.query["showLimit"]) for r in http.requests] == [
        ("GET", "/api/v2/databases", ["10000"])
    ]
    assert conn.read_stats.fallbacks == Counter({("show_databases", "stall"): 1})
    assert conn._api_failed


def test_a_late_error_inside_snowflake_core_prints_nothing(
    http, monkeypatch, capfd, caplog, stall, short_deadline
):
    # As in the CLI, where nothing handles the root logger, so a record no handler
    # takes goes to logging.lastResort, which prints it to stderr
    printed = []
    last_resort = logging.Handler(logging.WARNING)
    monkeypatch.setattr(last_resort, "emit", printed.append)
    monkeypatch.setattr(logging, "lastResort", last_resort)
    monkeypatch.setattr(logging.getLogger("snowflake.core"), "propagate", False)
    # A gateway's error page, which snowflake.core logs an ERROR for as it raises
    held = stall(b"<html>502 Bad Gateway</html>")
    http.answer("/api/v2/databases", held, status=502)
    conn = offline_conn(http, monkeypatch)
    assert SnowflakeConnector.show_databases(conn) == ["raw"]
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="tundra.logger")
    caplog.clear()
    capfd.readouterr()

    assert held.wait_for(1)
    (thread,) = held.threads
    assert thread.is_alive()
    held.released.set()
    thread.join(5)

    assert not thread.is_alive()
    assert printed == []
    assert caplog.records == []
    assert capfd.readouterr() == ("", "")


# The refusals the conftest guards make, and no socket, put in place before tundra is
# imported
EXITS_WITH_A_STALL = """
import socket
import threading

from snowflake.connector import SnowflakeConnection
from snowflake.core import Root
from snowflake.core._http_requests import SFPoolManager


def refuse(*args, **kwargs):
    raise SystemExit("reached for the network")


for owner, name in [
    (SnowflakeConnection, "connect"),
    (Root, "__init__"),
    (SFPoolManager, "_send"),
    (socket.socket, "connect"),
]:
    setattr(owner, name, refuse)

from tundra.concurrency import DeadlineExceeded, call_with_deadline

try:
    call_with_deadline(threading.Event().wait, 0.05)
except DeadlineExceeded:
    held = [t for t in threading.enumerate() if t.name == "call_with_deadline"]
    print("stalled", [t.is_alive() for t in held])
"""

DUMMY_SETTINGS = ("USER", "PASSWORD", "ACCOUNT", "DATABASE", "ROLE", "WAREHOUSE")


def test_a_process_with_a_stalled_call_still_exits():
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PERMISSION_BOT_")
    }
    env.update({f"PERMISSION_BOT_{setting}": "dummy" for setting in DUMMY_SETTINGS})
    env["PYTHONPATH"] = str(Path(tundra.__file__).parents[1])

    try:
        child = subprocess.run(
            [sys.executable, "-c", EXITS_WITH_A_STALL],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("The process didn't exit with a stalled call outstanding")

    assert (child.returncode, child.stdout, child.stderr) == (0, "stalled [True]\n", "")


# No thread for SQL


@pytest.mark.parametrize("switch", ["argument", "setting", "attribute"])
def test_no_thread_starts_for_a_sql_only_connector(mocker, monkeypatch, switch):
    account = names_account()
    account.install(mocker)
    if switch == "setting":
        monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", "true")
    started = mocker.spy(threading.Thread, "start")
    conn = SnowflakeConnector(sql_only=switch == "argument")
    # As --sql-only sets it, through the spec loader
    if switch == "attribute":
        conn.sql_only = True

    conn.show_databases()
    conn.show_grants_to_role("loader")

    assert started.call_count == 0
    assert account.api_calls == []


@pytest.mark.parametrize(
    "failure",
    ["stall", ConnectionResetError("reset")],
    ids=["after a stall", "after a transport error"],
)
def test_no_thread_starts_after_a_sticky_failure(
    mocker, stall, short_deadline, failure
):
    names_account().install(mocker)
    mocker.patch.object(
        FakeApi, "databases", side_effect=stall() if failure == "stall" else failure
    )
    started = mocker.spy(threading.Thread, "start")
    conn = SnowflakeConnector()

    conn.show_databases()
    assert started.call_count == 1
    started.reset_mock()
    conn.show_warehouses()
    conn.show_future_grants_to_role("loader")

    assert started.call_count == 0


def test_no_thread_starts_when_the_api_cant_be_built(mocker):
    # Without a fake, building the API is refused before a connection is checked out
    mocker.patch("sqlalchemy.create_engine")
    conn = SnowflakeConnector()
    mocker.patch.object(conn, "run_query")
    started = mocker.spy(threading.Thread, "start")

    conn.show_databases()
    conn.show_schemas("raw")

    assert started.call_count == 0
    assert conn.read_stats.fallbacks == Counter({("show_databases", "transport"): 1})


# PERMISSION_BOT_API_TIMEOUT


@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "empty", "blank"])
def test_the_timeout_defaults_to_two_minutes(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", value)

    assert api_timeout() == DEFAULT_API_TIMEOUT == 120


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("30", 30),
        ("0.5", 0.5),
        (" 45 ", 45),
        ("1e3", 1000),
        ("600", 600),
        # Past threading.TIMEOUT_MAX, which call_with_deadline caps its wait at
        ("1e10", 1e10),
    ],
)
def test_the_timeout_is_read_in_seconds(monkeypatch, value, seconds):
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", value)

    assert api_timeout() == seconds


@pytest.mark.parametrize(
    "value", ["0", "-5", "soon", "30s", "nan", "inf", "-inf", "1e999"]
)
def test_a_timeout_that_isnt_a_positive_number_is_refused(monkeypatch, value):
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", value)

    with pytest.raises(InvalidSettingError) as error:
        api_timeout()

    assert str(error.value) == (
        f"PERMISSION_BOT_API_TIMEOUT must be a positive number of seconds, not {value!r}"
    )
    assert isinstance(error.value, ValueError)


def test_a_connector_refuses_a_bad_timeout_as_its_built(mocker, monkeypatch):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "soon")

    with pytest.raises(InvalidSettingError):
        SnowflakeConnector(sql_only=True)

    create_engine.assert_not_called()


def test_a_load_refuses_a_bad_timeout_before_it_reads(mocker, monkeypatch):
    account = names_account()
    account.install(mocker)
    monkeypatch.setenv("PERMISSION_BOT_API_TIMEOUT", "0")
    spec = Path(__file__).parent / "golden" / "users.yml"

    with pytest.raises(InvalidSettingError):
        SnowflakeSpecLoader(str(spec))

    assert account.queries == [] and account.api_calls == []


# The listings a plan reads through the API
LISTINGS = (
    "databases",
    "warehouses",
    "users",
    "schemas",
    "database_roles",
    "grants_to_role",
    "future_grants_to_role",
    "grants_to_user",
)


@pytest.mark.parametrize(
    "name,note",
    [
        ("roles", " (SQL for the rest of the run after a listing's missed deadline)"),
        # The users spec plans no statement on an API row
        ("users", ""),
    ],
    ids=["roles", "users"],
)
def test_a_planning_stall_is_named_after_apply_when_it_sent_statements_to_sql(
    mocker, monkeypatch, stall, short_deadline, name, note
):
    monkeypatch.setenv("PERMISSION_BOT_MAX_WORKERS", "1")
    fake = getattr(golden, f"{name}_account")()
    fake.install(mocker)
    held = stall()
    for listing in LISTINGS:
        mocker.patch.object(FakeApi, listing, side_effect=held)
    secho = mocker.spy(click, "secho")

    result = CliRunner().invoke(cli, ["run", str(golden.GOLDEN / f"{name}.yml")])

    assert result.exit_code == 0, result.output
    assert held.wait_for(1)
    assert [c for c in fake.api_calls if c[0] in WRITE_CALLS] == []
    (summary,) = [
        call
        for call in secho.call_args_list
        if call.args and "API-capable statements" in str(call.args[0])
    ]
    assert summary.args[0].endswith(f" through SQL{note}.")
    assert summary.kwargs["fg"] == ("yellow" if note else "green")
