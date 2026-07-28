from tundra.caching_connector import CachingSnowflakeConnector
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector


class FakeCache:
    def __init__(self):
        self.store = {}
        self.invalidated = 0

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def invalidate_all(self):
        self.invalidated += 1


def test_miss_calls_inner_and_stores(mocker):
    inner = MockSnowflakeConnector()
    spy = mocker.patch.object(inner, "show_databases", return_value=["db1"])
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    assert conn.show_databases() == ["db1"]
    spy.assert_called_once()
    assert list(cache.store.values()) == [["db1"]]


def test_hit_returns_cached_without_calling_inner(mocker):
    inner = MockSnowflakeConnector()
    spy = mocker.patch.object(inner, "show_databases", return_value=["fresh"])
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    conn.show_databases()  # populate
    spy.reset_mock()
    assert conn.show_databases() == ["fresh"]
    spy.assert_not_called()


def test_kwargs_are_keyed_distinctly(mocker):
    inner = MockSnowflakeConnector()
    mocker.patch.object(
        inner,
        "show_future_grants",
        side_effect=lambda database=None, schema=None: {"scope": schema or database},
    )
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    assert conn.show_future_grants(schema="db.s1") == {"scope": "db.s1"}
    assert conn.show_future_grants(schema="db.s2") == {"scope": "db.s2"}


def test_refresh_bypasses_read_but_writes(mocker):
    inner = MockSnowflakeConnector()
    spy = mocker.patch.object(inner, "show_databases", return_value=["v2"])
    cache = FakeCache()
    cache.store["ignored"] = ["v1"]
    conn = CachingSnowflakeConnector(inner, cache, refresh=True)

    assert conn.show_databases() == ["v2"]
    spy.assert_called_once()


def test_run_query_mutation_invalidates(mocker):
    inner = MockSnowflakeConnector()
    mocker.patch.object(inner, "run_query", return_value=None)
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    conn.run_query("GRANT usage ON database db1 TO ROLE r")
    assert cache.invalidated == 1


def test_run_query_read_does_not_invalidate(mocker):
    inner = MockSnowflakeConnector()
    mocker.patch.object(inner, "run_query", return_value=None)
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    conn.run_query("SHOW DATABASES")
    assert cache.invalidated == 0


def test_delegates_unwrapped_attributes(mocker):
    inner = MockSnowflakeConnector()
    mocker.patch.object(inner, "get_current_role", return_value="securityadmin")
    conn = CachingSnowflakeConnector(inner, FakeCache())

    assert conn.get_current_role() == "securityadmin"


def test_positional_and_keyword_calls_share_cache_entry(mocker):
    inner = MockSnowflakeConnector()
    spy = mocker.patch.object(inner, "show_schemas", return_value=["db.s"])
    cache = FakeCache()
    conn = CachingSnowflakeConnector(inner, cache)

    conn.show_schemas("db")  # positional, as the grant generator calls it
    conn.show_schemas(database="db")  # keyword, as the parallel fetch calls it

    spy.assert_called_once()  # second call served from cache, no re-fetch
    assert conn.show_schemas(database="db") == ["db.s"]
