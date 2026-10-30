import pytest
import os
import logging
from colorama import init, Fore, Style

from tundra.snowflake_connector import SnowflakeConnector
from tundra_test_utils.offline import BUILD_API, GUARDS

init()

logging.basicConfig(level=logging.INFO)

pytest_plugins = ["fixtures.fs", "fixtures.cli"]

# These run once on each transport against the same expected output, so REST and SQL
# reads are pinned to the same plans, and API and SQL writes to the same outcomes
BOTH_TRANSPORTS = (
    "test_golden_scenarios",
    "test_identifier_rendering",
    "test_apply_transports",
)

# The connection settings every test starts from, whatever the shell running the tests
# has: dummy credentials, and no other way to log in
DUMMY_SETTINGS = ("USER", "PASSWORD", "ACCOUNT", "DATABASE", "ROLE", "WAREHOUSE")
UNSET_SETTINGS = (
    "AUTHENTICATOR",
    "OAUTH_TOKEN",
    "KEY_PATH",
    "KEY_PASSPHRASE",
    "TOKEN_COMMAND",
    "WORKLOAD_IDENTITY_PROVIDER",
    "SQL_ONLY",
    "API_STRICT",
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """
    No test reaches Snowflake. Logging in, building a snowflake.core Root and sending a
    REST request raise OfflineError, which fails the test. A connector's API can't be
    built either, unless FakeAccount.install has replaced SnowflakeConnector.api, so
    without one the connector reads through SQL and never checks out a connection.
    """
    for owner, name, guard in GUARDS:
        monkeypatch.setattr(owner, name, guard)
    for setting in DUMMY_SETTINGS:
        monkeypatch.setenv(f"PERMISSION_BOT_{setting}", "dummy")
    for setting in UNSET_SETTINGS:
        monkeypatch.delenv(f"PERMISSION_BOT_{setting}", raising=False)


@pytest.fixture
def real_build_api(offline, monkeypatch):
    """
    SnowflakeConnector.api built through the real _build_api, for tests that mock the
    engine and Root themselves. A real engine still can't log in.
    """
    monkeypatch.setattr(SnowflakeConnector, "_build_api", BUILD_API)


def pytest_generate_tests(metafunc):
    if metafunc.module.__name__.rsplit(".", 1)[-1] in BOTH_TRANSPORTS:
        metafunc.parametrize("read_transport", ["rest", "sql"], indirect=True)


@pytest.fixture(autouse=True)
def read_transport(request, offline, monkeypatch):
    """
    The transport a BOTH_TRANSPORTS test reads through. On rest, a listing that would
    fall back to SQL fails the test, so SQL can't answer for the API and pass.
    """
    transport = getattr(request, "param", None)
    if transport == "sql":
        monkeypatch.setenv("PERMISSION_BOT_SQL_ONLY", "true")
    elif transport == "rest":
        monkeypatch.setenv("PERMISSION_BOT_API_STRICT", "true")
    return transport


@pytest.fixture(scope="session")
def concurrency():
    return {
        "threads": int(os.getenv("PYTEST_CONCURRENCY_THREADS", 8)),
        "processes": int(os.getenv("PYTEST_CONCURRENCY_PROCESSES", 8)),
        "cases": int(os.getenv("PYTEST_CONCURRENCY_CASES", 64)),
    }


def pytest_itemcollected(item):
    """
    Brought in for docstring output:
    https://stackoverflow.com/a/39035226
    """
    par = item.parent.obj
    node = item.obj
    pref = " ".join(par.__doc__.split()) + " " if par.__doc__ else ""
    try:
        # Enables docstrings for class, methods, functions passed in mark.parametrize
        # to be concatenated and output to the CLI when pytest is run
        suf = " ".join(node.__doc__.split()) + " " if node.__doc__ else ""
        if "[" in item.nodeid:
            mark_list = item.nodeid.split("[")[-1][:-1].split("-")
        else:
            mark_list = []
        for pytest_mark in node.pytestmark:
            if pytest_mark.name == "parametrize":
                for param in pytest_mark.args[1]:
                    # Works for functions passed into mark.parametrize
                    if param.__name__ in mark_list:
                        clean_doc = " ".join(param.__doc__.split())
                        suf += clean_doc + " "
    except AttributeError:
        suf = suf = " ".join(node.__doc__.split()) + " " if node.__doc__ else ""
    except Exception:
        suf = suf = " ".join(node.__doc__.split()) + " " if node.__doc__ else ""
    # pytest 9 keeps node ids elsewhere, and there the docstrings just aren't shown
    if (pref or suf) and hasattr(item, "_nodeid"):
        item._nodeid = (
            Fore.YELLOW + "".join((pref, suf)) + "\n" + Style.RESET_ALL + item._nodeid
        )
