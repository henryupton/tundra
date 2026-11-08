# tests/conftest.py keeps every test away from Snowflake. Each guard is called here with
# arguments that fail before any network call if the guard is missing.

import os
from collections import Counter
from unittest import mock

import pytest
from snowflake.connector import SnowflakeConnection
from snowflake.core import Root
from snowflake.core._http_requests import SFPoolManager

from tundra.snowflake_connector import SnowflakeConnector
from tundra_test_utils.offline import ApiNotInstalled, OfflineError


def test_a_guard_cant_be_caught_as_an_exception():
    assert not issubclass(OfflineError, Exception)


def test_logging_in_is_refused():
    # Both snowflake.connector.connect() and SQLAlchemy's pool log in through this
    with pytest.raises(OfflineError):
        SnowflakeConnection.connect(None)


def test_building_a_root_is_refused():
    with pytest.raises(OfflineError):
        Root(mock.Mock(spec=SnowflakeConnection))


def test_sending_a_rest_request_is_refused():
    with pytest.raises(OfflineError):
        SFPoolManager._send(None, "GET", "https://offline.invalid/", None, {}, {})


def test_settings_are_dummies():
    for setting in ("USER", "PASSWORD", "ACCOUNT", "DATABASE", "ROLE", "WAREHOUSE"):
        assert os.environ[f"PERMISSION_BOT_{setting}"] == "dummy"
    for setting in ("AUTHENTICATOR", "OAUTH_TOKEN", "KEY_PATH", "TOKEN_COMMAND"):
        assert f"PERMISSION_BOT_{setting}" not in os.environ


def test_a_connector_without_fakes_reads_through_sql(mocker):
    create_engine = mocker.patch("sqlalchemy.create_engine")
    conn = SnowflakeConnector()
    run_query = mocker.patch.object(conn, "run_query")

    conn.show_schemas("raw")

    run_query.assert_called_once_with("SHOW TERSE SCHEMAS IN DATABASE raw")
    # Refused before a connection is checked out, so a real engine wouldn't log in
    create_engine.return_value.raw_connection.assert_not_called()
    assert isinstance(conn._api_error, ApiNotInstalled)
    assert conn.read_stats.fallbacks == Counter({("show_schemas", "transport"): 1})
