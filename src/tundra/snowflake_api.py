"""
The snowflake.core calls tundra makes, one method each.

Every method returns snowflake.core's own models as REST spells them, so turning names
into ObjectNames stays in the connector's readers, and the test fake stands in for this
class with models built from its own state. Names are passed the way REST paths take
them, as ObjectName.api_name spells them.

Nothing here imports snowflake.core at runtime: the connector builds this over a Root
the first time a reader needs it.
"""

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from snowflake.core import Root
    from snowflake.core.database import Database
    from snowflake.core.database_role import DatabaseRole
    from snowflake.core.role import Grant
    from snowflake.core.schema import Schema
    from snowflake.core.user import Grant as UserGrant
    from snowflake.core.user import User
    from snowflake.core.warehouse import Warehouse

# The most rows the SHOW behind a REST listing returns (showLimit can't go higher).
# Nothing in a response says rows were left out, so the connector reads a listing this
# long again through SQL, which has no limit.
LIST_LIMIT = 10_000


class SnowflakeApi:
    list_limit = LIST_LIMIT

    def __init__(self, root: "Root") -> None:
        self.root = root

    def grants_to_role(self, role: str) -> List["Grant"]:
        return list(self.root.roles[role].iter_grants_to(show_limit=self.list_limit))

    def future_grants_to_role(self, role: str) -> List["Grant"]:
        return list(
            self.root.roles[role].iter_future_grants_to(show_limit=self.list_limit)
        )

    def grants_to_user(self, user: str) -> List["UserGrant"]:
        # UserResource.iter_grants_to takes no limit, so the server's default applies
        return list(self.root.users[user].iter_grants_to())

    def databases(self) -> List["Database"]:
        return list(self.root.databases.iter(limit=self.list_limit))

    def warehouses(self) -> List["Warehouse"]:
        # WarehouseCollection.iter takes no limit either
        return list(self.root.warehouses.iter())

    def users(self) -> List["User"]:
        return list(self.root.users.iter(limit=self.list_limit))

    def schemas(self, database: str) -> List["Schema"]:
        return list(self.root.databases[database].schemas.iter(limit=self.list_limit))

    def database_roles(self, database: str) -> List["DatabaseRole"]:
        return list(
            self.root.databases[database].database_roles.iter(limit=self.list_limit)
        )
