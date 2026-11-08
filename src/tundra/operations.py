"""
What each planned statement does, as data.

The generator plans every statement as one of these operations. The SQL tundra prints,
dedupes and runs is rendered from it, and apply sends it through snowflake.core instead
when ROUTES says the API has been shown to take that kind of statement. Both come from
one frozen object, so the printed line and the request agree. A command whose `sql` a
caller has changed runs that SQL instead (see apply_query).

Statements only SQL can express (ownership, ALTER USER properties and application
roles) are SqlStatements, which have no route.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterator, List, Literal, Optional, Tuple, Union

from snowflake.core.user import Securable as UserSecurable
from yaml.representer import Representer, SafeRepresenter

from tundra.identifiers import ObjectName
from tundra.table_object_types import ALL_OBJECT_TYPES

if TYPE_CHECKING:
    from tundra.snowflake_api import SnowflakeApi

GRANT_ROLE_TEMPLATE = "GRANT ROLE {role_name} TO {type} {entity_name}"

GRANT_DATABASE_ROLE_TEMPLATE = "GRANT DATABASE ROLE {role_name} TO ROLE {entity_name}"

REVOKE_ROLE_TEMPLATE = "REVOKE ROLE {role_name} FROM {type} {entity_name}"

REVOKE_DATABASE_ROLE_TEMPLATE = (
    "REVOKE DATABASE ROLE {role_name} FROM ROLE {entity_name}"
)

GRANT_PRIVILEGES_TEMPLATE = (
    "GRANT {privileges} ON {resource_type} {resource_name} TO ROLE {role}"
)

REVOKE_PRIVILEGES_TEMPLATE = (
    "REVOKE {privileges} ON {resource_type} {resource_name} FROM ROLE {role}"
)

GRANT_ACCOUNT_PRIVILEGE_TEMPLATE = "GRANT {privilege} ON ACCOUNT TO ROLE {role}"

REVOKE_ACCOUNT_PRIVILEGE_TEMPLATE = "REVOKE {privilege} ON ACCOUNT FROM ROLE {role}"

GRANT_ALL_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON ALL {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

GRANT_FUTURE_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

REVOKE_FUTURE_PRIVILEGES_TEMPLATE = "REVOKE {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} FROM ROLE {role}"

SET_USER_TAG_TEMPLATE = "ALTER USER {user_name} SET TAG {tag} = {value}"

UNSET_USER_TAG_TEMPLATE = "ALTER USER {user_name} UNSET TAG {tag}"

GRANT: Literal["grant"] = "grant"
REVOKE: Literal["revoke"] = "revoke"
Action = Literal["grant", "revoke"]

BulkScope = Literal["all", "future"]
ALL: Literal["all"] = "all"
FUTURE: Literal["future"] = "future"

# A row of ROUTES: the kind of statement, then what it's on
RouteKey = Tuple[str, ...]

# The names a request carries, as sent: those in its path (a role or user), then those
# in its body (the securable, scope, member or tag's parts)
Spelt = Tuple[Tuple[str, ...], Tuple[str, ...]]

# The securable types whose "does not exist" errors can mean another process dropped
# the object mid-run: databases, schemas and the registry kinds. Warehouses,
# integrations, external volumes, roles and users are spec-managed, so their absence
# is a real failure.
_IN_DATABASES = frozenset({"database", "schema"} | {t.name for t in ALL_OBJECT_TYPES})


def sql_literal(value: str) -> str:
    """A string as a SQL literal, quotes doubled."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _privilege_list(privileges: Tuple[str, ...]) -> List[str]:
    """
    Privileges as the API is sent them. tundra plans them lowercase, and the dev probe
    has only sent them uppercase, the way REST and SHOW GRANTS report them.
    """
    return [privilege.upper() for privilege in privileges]


def _missing_part(target: Optional[ObjectName], kind: str) -> Optional[ObjectName]:
    """
    The part of `target` that a "<kind> '...' does not exist" error names: its
    database, its schema, or the target itself when it's a registry object.
    """
    if target is None:
        return None
    if kind == "database":
        return target.database
    if kind == "schema":
        return target if len(target.parts) == 2 else target.parent
    return target if len(target.parts) == 3 else None


@dataclass(frozen=True)
class SqlStatement:
    """A statement that only runs as SQL."""

    sql: str

    route_key = None


@dataclass(frozen=True)
class Privileges:
    """
    Privileges on one securable, granted to or revoked from a role. The securable is a
    database, schema, warehouse, integration, external volume or registry object, or
    the account when `target` is None.
    """

    action: Action
    privileges: Tuple[str, ...]
    securable_type: str
    target: Optional[ObjectName]
    role: ObjectName

    @property
    def sql(self) -> str:
        privileges = ", ".join(self.privileges)
        grant = self.action == GRANT
        if self.target is None:
            account = (
                GRANT_ACCOUNT_PRIVILEGE_TEMPLATE
                if grant
                else REVOKE_ACCOUNT_PRIVILEGE_TEMPLATE
            )
            return account.format(privilege=privileges, role=self.role)
        template = GRANT_PRIVILEGES_TEMPLATE if grant else REVOKE_PRIVILEGES_TEMPLATE
        return template.format(
            privileges=privileges,
            resource_type=self.securable_type,
            resource_name=self.target,
            role=self.role,
        )

    @property
    def route_key(self) -> RouteKey:
        if self.securable_type == "database" and _privilege_list(self.privileges) == [
            "IMPORTED PRIVILEGES"
        ]:
            return ("privileges", "database", "imported privileges")
        return ("privileges", self.securable_type)

    @property
    def spelt(self) -> Spelt:
        body = () if self.target is None else self.target.api_parts
        return (self.role.api_name,), body

    def missing(self, kind: str) -> Optional[ObjectName]:
        if self.securable_type not in _IN_DATABASES:
            return None
        return _missing_part(self.target, kind)

    def send(self, api: "SnowflakeApi") -> None:
        securable = None if self.target is None else self.target.to_securable()
        write = api.grant_privileges if self.action == GRANT else api.revoke_privileges
        write(
            self.role.api_name,
            _privilege_list(self.privileges),
            self.securable_type,
            securable,
        )


@dataclass(frozen=True)
class BulkPrivileges:
    """
    Privileges on all or future objects of one type in a database or schema, granted to
    a role, or future privileges revoked from it. `securable_type` is singular, as the
    API takes it: table, iceberg table, schema. tundra never revokes ON ALL.
    """

    action: Action
    on: BulkScope
    privileges: Tuple[str, ...]
    securable_type: str
    container: ObjectName
    role: ObjectName

    def __post_init__(self) -> None:
        if (self.action, self.on) == (REVOKE, ALL):
            raise ValueError("tundra revokes privileges object by object, not ON ALL")

    @property
    def grouping_type(self) -> str:
        return "database" if len(self.container.parts) == 1 else "schema"

    @property
    def sql(self) -> str:
        template = {
            (GRANT, ALL): GRANT_ALL_PRIVILEGES_TEMPLATE,
            (GRANT, FUTURE): GRANT_FUTURE_PRIVILEGES_TEMPLATE,
            (REVOKE, FUTURE): REVOKE_FUTURE_PRIVILEGES_TEMPLATE,
        }[self.action, self.on]
        return template.format(
            privileges=", ".join(self.privileges),
            resource_type=self.securable_type,
            grouping_type=self.grouping_type,
            grouping_name=self.container,
            role=self.role,
        )

    @property
    def route_key(self) -> RouteKey:
        return (self.on, self.securable_type, f"in {self.grouping_type}")

    @property
    def spelt(self) -> Spelt:
        return (self.role.api_name,), self.container.api_parts

    def missing(self, kind: str) -> Optional[ObjectName]:
        if kind not in ("database", "schema"):
            return None
        return _missing_part(self.container, kind)

    def send(self, api: "SnowflakeApi") -> None:
        write = {
            (GRANT, ALL): api.grant_privileges_on_all,
            (GRANT, FUTURE): api.grant_future_privileges,
            (REVOKE, FUTURE): api.revoke_future_privileges,
        }[self.action, self.on]
        write(
            self.role.api_name,
            _privilege_list(self.privileges),
            self.securable_type,
            self.container.to_containing_scope(),
        )


@dataclass(frozen=True)
class RoleMembership:
    """
    An account role or database role (db.role) granted to or revoked from a role, or
    an account role granted to or revoked from a user. Application roles are
    SqlStatements: snowflake.core's grant_role can't grant them.
    """

    action: Action
    member: ObjectName
    member_type: Literal["role", "database role"]
    grantee: ObjectName
    grantee_type: Literal["role", "user"]

    @property
    def sql(self) -> str:
        grant = self.action == GRANT
        if self.member_type == "database role":
            template = (
                GRANT_DATABASE_ROLE_TEMPLATE if grant else REVOKE_DATABASE_ROLE_TEMPLATE
            )
            return template.format(role_name=self.member, entity_name=self.grantee)
        template = GRANT_ROLE_TEMPLATE if grant else REVOKE_ROLE_TEMPLATE
        return template.format(
            role_name=self.member, type=self.grantee_type, entity_name=self.grantee
        )

    @property
    def route_key(self) -> RouteKey:
        return ("membership", self.member_type, f"to {self.grantee_type}")

    @property
    def spelt(self) -> Spelt:
        return (self.grantee.api_name,), self.member.api_parts

    def missing(self, kind: str) -> Optional[ObjectName]:
        # A missing role or user is a spec failure, never something that vanished
        return None

    def send(self, api: "SnowflakeApi") -> None:
        grantee = self.grantee.api_name
        if self.grantee_type == "user":
            securable = self.member.to_securable(UserSecurable)
            if self.action == GRANT:
                api.grant_role_to_user(grantee, securable)
            else:
                api.revoke_role_from_user(grantee, securable)
            return
        write = (
            api.grant_role_to_role
            if self.action == GRANT
            else api.revoke_role_from_role
        )
        write(grantee, self.member_type, self.member.to_securable())


@dataclass(frozen=True)
class UserTag:
    """A tag set on a user to a value, or unset from it."""

    action: Literal["set", "unset"]
    user: ObjectName
    tag: ObjectName
    value: Optional[str] = None

    @property
    def sql(self) -> str:
        if self.action == "unset":
            return UNSET_USER_TAG_TEMPLATE.format(user_name=self.user, tag=self.tag)
        assert self.value is not None, f"Setting {self.tag!s} needs a value"
        return SET_USER_TAG_TEMPLATE.format(
            user_name=self.user, tag=self.tag, value=sql_literal(self.value)
        )

    @property
    def route_key(self) -> RouteKey:
        return ("tag", "user")

    @property
    def spelt(self) -> Spelt:
        return (self.user.api_name,), self.tag.api_parts

    def missing(self, kind: str) -> Optional[ObjectName]:
        return None

    def send(self, api: "SnowflakeApi") -> None:
        if self.action == "unset":
            api.unset_user_tag(self.user.api_name, self.tag.api_parts)
            return
        assert self.value is not None, f"Setting {self.tag!s} needs a value"
        api.set_user_tag(self.user.api_name, self.tag.api_parts, self.value)


ApiOperation = Union[Privileges, BulkPrivileges, RoleMembership, UserTag]
Operation = Union[ApiOperation, SqlStatement]


class PlannedCommand(dict):
    """
    A planned statement, as the dict tundra has always planned: `already_granted` and
    `sql`, with apply's `run_status`, `error` and `vanished` added later. The operation
    behind it rides along as an attribute, so dict equality, JSON and YAML dumps, pickles
    and callers that index the dict see what they always did.
    """

    def __init__(self, already_granted: bool, operation: Operation) -> None:
        super().__init__(already_granted=already_granted, sql=operation.sql)
        self.operation = operation


# PyYAML represents only exact dicts as mappings, so a plan would otherwise dump as
# python/object tags or not at all
for _representer in (SafeRepresenter, Representer):
    _representer.add_representer(PlannedCommand, SafeRepresenter.represent_dict)


# One row per kind of statement and securable type, each going through the API or SQL.
# A row on the API cites what the first dev write probe showed for it. A row on SQL
# names the check in the second dev write probe that has to pass before it's flipped,
# or says the probe doesn't try it. A kind with no row, or no route_key at all, goes
# through SQL.


@dataclass(frozen=True)
class Route:
    api: bool
    evidence: str


def _shown(evidence: str) -> Route:
    return Route(api=True, evidence=f"The first dev write probe {evidence}")


def _until(check: str) -> Route:
    return Route(api=False, evidence=f"Until the second dev write probe passes {check}")


def _unprobed(what: str) -> Route:
    return Route(api=False, evidence=f"The second dev write probe doesn't try {what}")


# Gates: a statement on an API row still goes through SQL unless each gate it meets is
# on the API too. A privilege list longer than one on a named securable (the database
# and schema write bundles, table, dynamic table and task write privileges), or on
# future or all objects (the table write bundle in a schema)
SEVERAL_PRIVILEGES: RouteKey = ("privilege list", "several", "on one securable")
SEVERAL_BULK_PRIVILEGES: RouteKey = ("privilege list", "several", "on future or all")
# A name the request quotes, because it's mixed or lower case, a reserved word, or has
# characters an identifier can't: a role or user in the path, or a database, schema,
# object, scope, member or tag part in the body. Every dev write so far sent bare names.
QUOTED_PATH: RouteKey = ("quoted name", "in the path")
QUOTED_BODY: RouteKey = ("quoted name", "in the body")
GATES = (SEVERAL_PRIVILEGES, SEVERAL_BULK_PRIVILEGES, QUOTED_PATH, QUOTED_BODY)

ROUTES: Dict[RouteKey, Route] = {
    ("privileges", "database", "imported privileges"): _shown(
        "granted IMPORTED PRIVILEGES on SNOWFLAKE and revoked it, and SHOW GRANTS"
        " reported USAGE on the database in between"
    ),
    ("privileges", "database"): _shown(
        "sent IMPORTED PRIVILEGES on a database in this request shape. USAGE and the"
        " database's other privileges ride on that until the second dev write probe's"
        " 'database USAGE' check runs them"
    ),
    ("future", "table", "in schema"): _shown(
        "granted SELECT on future tables in a schema and revoked it, and did the same"
        " in a keyword-named schema sent lowercase and unquoted"
    ),
    ("all", "table", "in schema"): _shown(
        "granted SELECT on all tables in a schema, and SHOW GRANTS listed each one"
    ),
    SEVERAL_PRIVILEGES: _until("'database write bundle' and 'schema write bundle'"),
    SEVERAL_BULK_PRIVILEGES: _unprobed(
        "a list longer than one on future or all objects: its bulk checks send one"
        " privilege"
    ),
    QUOTED_PATH: _until(
        "'account role to role, quoted path and body' and 'account role to user,"
        " quoted path'"
    ),
    QUOTED_BODY: _unprobed(
        "a quoted database, schema, object, scope or tag part in a request body. It"
        " quotes only a role's name, in 'account role to role, quoted path and body'"
    ),
    ("privileges", "schema"): _until("'schema USAGE'"),
    ("privileges", "warehouse"): _until("'warehouse USAGE'"),
    ("privileges", "integration"): _until("'integration USAGE'"),
    ("privileges", "external volume"): _until("'external volume USAGE'"),
    ("privileges", "account"): _until("'account CREATE DATABASE'"),
    ("privileges", "table"): _until("'table SELECT'"),
    ("privileges", "view"): _until("'view SELECT'"),
    ("privileges", "iceberg table"): _unprobed("a grant on one iceberg table"),
    ("privileges", "dynamic table"): _unprobed("a grant on one dynamic table"),
    ("privileges", "streamlit"): _unprobed("a grant on one streamlit"),
    ("privileges", "semantic view"): _unprobed("a grant on one semantic view"),
    ("privileges", "task"): _unprobed("a grant on one task"),
    ("future", "schema", "in database"): _until("'future schema in DATABASE'"),
    ("future", "table", "in database"): _until("'future table in DATABASE'"),
    ("future", "view", "in schema"): _until("'future view in SCHEMA'"),
    ("future", "view", "in database"): _until("'future view in DATABASE'"),
    ("future", "iceberg table", "in schema"): _until(
        "'future iceberg table in SCHEMA'"
    ),
    ("future", "iceberg table", "in database"): _until(
        "'future iceberg table in DATABASE'"
    ),
    ("future", "dynamic table", "in schema"): _until(
        "'future dynamic table in SCHEMA'"
    ),
    ("future", "dynamic table", "in database"): _until(
        "'future dynamic table in DATABASE'"
    ),
    ("future", "streamlit", "in schema"): _until("'future streamlit in SCHEMA'"),
    ("future", "streamlit", "in database"): _until("'future streamlit in DATABASE'"),
    ("future", "semantic view", "in schema"): _until(
        "'future semantic view in SCHEMA'"
    ),
    ("future", "semantic view", "in database"): _until(
        "'future semantic view in DATABASE'"
    ),
    ("future", "task", "in schema"): _until("'future task in SCHEMA'"),
    ("future", "task", "in database"): _until("'future task in DATABASE'"),
    ("all", "table", "in database"): _until("'on all table in DATABASE'"),
    ("all", "view", "in schema"): _until("'on all view in SCHEMA'"),
    ("all", "view", "in database"): _until("'on all view in DATABASE'"),
    ("all", "iceberg table", "in schema"): _until("'on all iceberg table in SCHEMA'"),
    ("all", "iceberg table", "in database"): _until(
        "'on all iceberg table in DATABASE'"
    ),
    ("all", "dynamic table", "in schema"): _until("'on all dynamic table in SCHEMA'"),
    ("all", "dynamic table", "in database"): _until(
        "'on all dynamic table in DATABASE'"
    ),
    ("all", "streamlit", "in schema"): _until("'on all streamlit in SCHEMA'"),
    ("all", "streamlit", "in database"): _until("'on all streamlit in DATABASE'"),
    ("all", "semantic view", "in schema"): _until("'on all semantic view in SCHEMA'"),
    ("all", "semantic view", "in database"): _until(
        "'on all semantic view in DATABASE'"
    ),
    ("all", "task", "in schema"): _until("'on all task in SCHEMA'"),
    ("all", "task", "in database"): _until("'on all task in DATABASE'"),
    ("membership", "role", "to role"): _until("'account role to role'"),
    ("membership", "role", "to user"): _until(
        "'account role to user' and 'account role to user, quoted path'"
    ),
    ("membership", "database role", "to role"): _until("'database role <db.role>'"),
    ("tag", "user"): _until("'set_tags and unset_tags'"),
}


def api_capable(operation: Operation) -> bool:
    """Whether snowflake.core can run the operation at all, whichever way it's routed."""
    return operation.route_key is not None


def gates(operation: ApiOperation) -> Iterator[RouteKey]:
    """The gate rows an operation meets, beyond its own row."""
    if len(getattr(operation, "privileges", ())) > 1:
        bulk = isinstance(operation, BulkPrivileges)
        yield SEVERAL_BULK_PRIVILEGES if bulk else SEVERAL_PRIVILEGES
    path, body = operation.spelt
    if any(part.startswith('"') for part in path):
        yield QUOTED_PATH
    if any(part.startswith('"') for part in body):
        yield QUOTED_BODY


def routes_to_api(operation: Operation) -> bool:
    """Whether ROUTES sends the operation through snowflake.core."""
    if isinstance(operation, SqlStatement):
        return False
    route = ROUTES.get(operation.route_key)
    if route is None or not route.api:
        return False
    return all(ROUTES[gate].api for gate in gates(operation))
