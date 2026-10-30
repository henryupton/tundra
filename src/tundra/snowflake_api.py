"""
The snowflake.core calls tundra makes, one method each.

Every reader returns snowflake.core's own models as REST spells them, so turning names
into ObjectNames stays in the connector's readers, and the test fake stands in for this
class with models built from its own state. Names are passed the way REST paths take
them, as ObjectName.api_name spells them. Writers take the request body's models as the
operation built them, from ObjectName.api_parts, and privileges as a list.

Nothing here imports snowflake.core as the module loads: the connector builds this over
a Root the first time a reader or writer needs it.
"""

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from snowflake.core import Root
    from snowflake.core.database import Database
    from snowflake.core.database_role import DatabaseRole
    from snowflake.core.role import ContainingScope, Grant, Securable
    from snowflake.core.schema import Schema
    from snowflake.core.tag import TagResource
    from snowflake.core.user import Grant as UserGrant
    from snowflake.core.user import Securable as UserSecurable
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

    # Writes. Grants leave grant_option unset, so nothing is granted WITH GRANT OPTION,
    # and revokes send no mode, so Snowflake's default RESTRICT applies, as it does to
    # tundra's REVOKE statements.

    def grant_privileges(
        self,
        role: str,
        privileges: List[str],
        securable_type: str,
        securable: Optional["Securable"],
    ) -> None:
        self.root.roles[role].grant_privileges(privileges, securable_type, securable)

    def revoke_privileges(
        self,
        role: str,
        privileges: List[str],
        securable_type: str,
        securable: Optional["Securable"],
    ) -> None:
        self.root.roles[role].revoke_privileges(
            privileges, securable_type, securable, mode=None
        )

    def grant_privileges_on_all(
        self,
        role: str,
        privileges: List[str],
        securable_type: str,
        containing_scope: "ContainingScope",
    ) -> None:
        self.root.roles[role].grant_privileges_on_all(
            privileges, securable_type, containing_scope
        )

    def grant_future_privileges(
        self,
        role: str,
        privileges: List[str],
        securable_type: str,
        containing_scope: "ContainingScope",
    ) -> None:
        self.root.roles[role].grant_future_privileges(
            privileges, securable_type, containing_scope
        )

    def revoke_future_privileges(
        self,
        role: str,
        privileges: List[str],
        securable_type: str,
        containing_scope: "ContainingScope",
    ) -> None:
        self.root.roles[role].revoke_future_privileges(
            privileges, securable_type, containing_scope, mode=None
        )

    def grant_role_to_role(
        self, grantee: str, role_type: str, role: "Securable"
    ) -> None:
        self.root.roles[grantee].grant_role(role_type, role)

    def revoke_role_from_role(
        self, grantee: str, role_type: str, role: "Securable"
    ) -> None:
        self.root.roles[grantee].revoke_role(role_type, role)

    def grant_role_to_user(self, user: str, role: "UserSecurable") -> None:
        self.root.users[user].grant_role("role", role)

    def revoke_role_from_user(self, user: str, role: "UserSecurable") -> None:
        self.root.users[user].revoke_role("role", role)

    def set_user_tag(self, user: str, tag: Tuple[str, ...], value: str) -> None:
        from snowflake.core.tag import TagValue

        self.root.users[user].set_tags({self._tag(tag): TagValue(value)})

    def unset_user_tag(self, user: str, tag: Tuple[str, ...]) -> None:
        self.root.users[user].unset_tags({self._tag(tag)})

    def _tag(self, tag: Tuple[str, ...]) -> "TagResource":
        database, schema, name = tag
        return self.root.databases[database].schemas[schema].tags[name]
