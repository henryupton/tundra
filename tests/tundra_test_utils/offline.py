"""What tests/conftest.py refuses, so that no test reaches Snowflake."""

from snowflake.connector import SnowflakeConnection
from snowflake.core import Root
from snowflake.core._http_requests import SFPoolManager

from tundra.snowflake_connector import SnowflakeConnector

# Taken before the guards replace them, for the tests that put them back
ROOT_INIT = Root.__init__
BUILD_API = SnowflakeConnector._build_api


class OfflineError(BaseException):
    """
    A test reached for Snowflake: a login, a Root or a REST request. Not an Exception,
    so no `except Exception` in tundra can turn it into a SQL fallback and pass.
    """


class ApiNotInstalled(Exception):
    """
    A connector built its API with neither FakeAccount nor OfflineHttp installed. It's
    raised before a connection is checked out of the pool, and the connector reads
    through SQL, as it does when snowflake.core can't be reached.
    """


def refuse_connect(self, *args, **kwargs):
    raise OfflineError("A test logged in to Snowflake through snowflake.connector")


def refuse_root(self, *args, **kwargs):
    raise OfflineError(
        "A test built a snowflake.core Root: install a FakeAccount, whose api stands"
        " in for it, or tundra_test_utils.offline_http.OfflineHttp"
    )


def refuse_send(self, *args, **kwargs):
    raise OfflineError("A test sent an HTTP request through snowflake.core")


def refuse_api(self):
    raise ApiNotInstalled(
        "No FakeAccount or OfflineHttp stands in for snowflake.core, so this connector"
        " reads through SQL"
    )


GUARDS = [
    (SnowflakeConnection, "connect", refuse_connect),
    (Root, "__init__", refuse_root),
    (SFPoolManager, "_send", refuse_send),
    (SnowflakeConnector, "_build_api", refuse_api),
]
