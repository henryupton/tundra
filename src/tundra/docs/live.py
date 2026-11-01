"""
The account read behind `tundra docs generate --live`: SnowflakeSpecLoader's snapshot,
the one `run` plans from, taken through a connector that can't write.
"""

import re
from typing import Any

from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader

READ_STATEMENT = re.compile(r"\s*(SHOW|SELECT)\b", re.IGNORECASE)


class ReadOnlyError(RuntimeError):
    """A write reached the connector `tundra docs` reads the account through."""


class ReadOnlyConnector(SnowflakeConnector):
    """
    A SnowflakeConnector that only reads. `execute` is the one way tundra applies a
    planned statement, through snowflake.core or as SQL, so it refuses everything, and
    SQL that isn't a SHOW or SELECT never reaches Snowflake.
    """

    def execute(self, operation: Any, through_api: bool = True) -> Any:
        raise ReadOnlyError(f"tundra docs only reads, so it won't run: {operation.sql}")

    def run_query(self, query: str) -> Any:
        if not READ_STATEMENT.match(query):
            raise ReadOnlyError(f"tundra docs only reads, so it won't run: {query}")
        return super().run_query(query)


def load_live(spec_path: str, sql_only: bool = False) -> SnowflakeSpecLoader:
    """
    Load the spec and read the account as `run` does, checks and grant snapshot
    included. Objects missing from Snowflake are noted rather than fatal, so the docs
    can show them.
    """
    return SnowflakeSpecLoader(
        spec_path,
        conn=ReadOnlyConnector(),
        sql_only=sql_only,
        ignore_missing_objects=True,
    )
