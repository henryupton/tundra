import os

from tundra.state_cache import StateCache


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cache(tmp_path, ttl=3600, clock=None):
    return StateCache(
        path=os.path.join(str(tmp_path), "cache.db"),
        account="acct1",
        ttl_seconds=ttl,
        clock=clock or FakeClock(),
    )


def test_miss_returns_none(tmp_path):
    assert _cache(tmp_path).get("k") is None


def test_set_then_get_roundtrips_structure(tmp_path):
    cache = _cache(tmp_path)
    cache.set("k", {"role": {"select": {"table": ["db.s.t"]}}})
    assert cache.get("k") == {"role": {"select": {"table": ["db.s.t"]}}}


def test_expired_entry_is_a_miss(tmp_path):
    clock = FakeClock(1000.0)
    cache = _cache(tmp_path, ttl=60, clock=clock)
    cache.set("k", ["v"])
    clock.t = 1000.0 + 61  # past TTL
    assert cache.get("k") is None


def test_within_ttl_is_a_hit(tmp_path):
    clock = FakeClock(1000.0)
    cache = _cache(tmp_path, ttl=60, clock=clock)
    cache.set("k", ["v"])
    clock.t = 1000.0 + 59
    assert cache.get("k") == ["v"]


def test_invalidate_all_clears_account(tmp_path):
    cache = _cache(tmp_path)
    cache.set("k", ["v"])
    cache.invalidate_all()
    assert cache.get("k") is None


def test_accounts_are_isolated(tmp_path):
    path = os.path.join(str(tmp_path), "cache.db")
    clock = FakeClock()
    a = StateCache(path=path, account="acctA", ttl_seconds=3600, clock=clock)
    b = StateCache(path=path, account="acctB", ttl_seconds=3600, clock=clock)
    a.set("k", ["only-A"])
    assert b.get("k") is None
