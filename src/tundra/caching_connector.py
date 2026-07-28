import json
from typing import Any

CACHED_METHODS = [
    "show_databases",
    "show_warehouses",
    "show_integrations",
    "show_external_volumes",
    "show_schemas",
    "show_views",
    "show_tables",
    "show_roles",
    "show_users",
    "show_future_grants",
    "show_grants_to_role",
    "show_roles_granted_to_user",
]

WRITE_PREFIXES = ("GRANT", "REVOKE", "ALTER", "CREATE", "DROP")


class CachingSnowflakeConnector:
    """Transparent read-through cache over a SnowflakeConnector. Read methods
    listed in CACHED_METHODS are served from the StateCache when fresh; a
    mutation via run_query invalidates the account's cached state."""

    def __init__(self, inner, cache, refresh: bool = False) -> None:
        self._inner = inner
        self._cache = cache
        self._refresh = refresh
        for name in CACHED_METHODS:
            setattr(self, name, self._make_cached(name))

    def __getattr__(self, name):
        # Only reached for attributes not set on the instance / class, so this
        # delegates everything not explicitly wrapped to the inner connector.
        return getattr(self._inner, name)

    def _make_cached(self, method_name: str):
        inner_method = getattr(self._inner, method_name)

        def cached(*args, **kwargs) -> Any:
            key = self._key(method_name, args, kwargs)
            if not self._refresh:
                hit = self._cache.get(key)
                if hit is not None:
                    return hit
            value = inner_method(*args, **kwargs)
            self._cache.set(key, value)
            return value

        return cached

    @staticmethod
    def _key(method_name: str, args, kwargs) -> str:
        return json.dumps(
            [method_name, list(args), sorted(kwargs.items())], sort_keys=True
        )

    def run_query(self, query: str):
        result = self._inner.run_query(query)
        if query.lstrip().upper().startswith(WRITE_PREFIXES):
            self._cache.invalidate_all()
        return result
