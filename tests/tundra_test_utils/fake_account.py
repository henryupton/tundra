"""A fake Snowflake account that answers tundra's queries the way Snowflake does.

State is declared with the names Snowflake stores (canonical names), written as
identifier spellings for readability: `raw.public` and `RAW.PUBLIC` declare the same
schema, `raw."Mixed-Case"` declares a case-sensitive one. The fake then answers in the
three forms Snowflake uses:

- SHOW <objects> (databases, schemas, tables, roles, users...) report canonical names,
  unquoted and case-sensitive: `Mixed-Case`, `AGGREGATE`.
- SHOW GRANTS and SHOW FUTURE GRANTS report identifier spellings, quoting any part that
  isn't a plain uppercase identifier and any keyword Snowflake quotes:
  `RAW."Mixed-Case"`, `LAB_RESULTS."AGGREGATE".<TABLE>`.
- The REST API, through FakeApi, answers with snowflake.core's own models, each part
  bare when it's a plain uppercase identifier, keywords included, and quoted otherwise:
  `AGGREGATE`, `"Mixed-Case"`, `"<TABLE>"`. That's the rule all of the dev probe's
  REST rows fit. The models' validators would also take a bare `lower_case`, and the
  probe met no name stored in lowercase, so `rest_bare_mixed_case` sends every
  identifier-shaped part bare instead.

Identifiers tundra sends are resolved with Snowflake's rules, so a query naming
`"group"` finds nothing when the stored object is `GROUP`, and an unquoted reserved word
fails the way it would in Snowflake. A query the fake can't answer fails the test, so
every query a scenario makes is accounted for.

Writes change the account's state, whether they arrive as SQL statements or as
FakeApi's write calls, and both go through the same mutators, so a statement applied
either way leaves the same state. A write naming something the account doesn't have
fails the way Snowflake reports it: SQL names it in its message, and the API answers
with a 404 whose message lowercases the name, as the dev probe recorded.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import urllib3
from snowflake.core.database import Database
from snowflake.core.database_role import DatabaseRole
from snowflake.core.exceptions import (
    APIError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServerError,
    UnauthorizedError,
)
from snowflake.core.rest import RESTResponse
from snowflake.core.role import Grant as RestGrant
from snowflake.core.schema import Schema
from snowflake.core.user import Grant as RestUserGrant
from snowflake.core.user import User
from snowflake.core.warehouse import Warehouse

from tundra.snowflake_api import LIST_LIMIT
from tundra.snowflake_connector import SnowflakeConnector, _BufferedResult
from tundra.table_object_types import ALL_OBJECT_TYPES

Name = Tuple[str, ...]

_PART = re.compile(r'"(?:[^"]|"")*"|[^."]+')
_UNQUOTED = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_PLAIN_UPPER = re.compile(r"[A-Z_][A-Z0-9_$]*")
_PLACEHOLDER = re.compile(r"<[A-Za-z_ ]+>")

# Reserved words can't be used unquoted. Snowflake also quotes some keywords that
# aren't reserved, such as AGGREGATE, when it prints a name in SHOW GRANTS output.
RESERVED = frozenset(word.upper() for word in SnowflakeConnector.reserved_keywords())
QUOTED_IN_GRANTS = RESERVED | {"AGGREGATE", "WINDOW"}


class SnowflakeError(Exception):
    pass


class Missing(Exception):
    """Something a write names that the account doesn't have: its kind and canonical parts."""

    def __init__(self, kind: str, name: "Name") -> None:
        super().__init__(f"{kind} {'.'.join(name)}")
        self.kind = kind
        self.name = name


class Refused(Exception):
    """A write the current role isn't allowed to make."""

    def __init__(self, role: str) -> None:
        super().__init__(role)
        self.role = role


# Registry kinds as statements name them (iceberg table), to the SHOW keyword that
# lists them in `objects` (ICEBERG TABLES)
_SHOW_KEYWORD = {t.name: t.show_command.replace("TERSE ", "") for t in ALL_OBJECT_TYPES}

# The keywords a privilege statement can name its securable with, longest first
_SECURABLE_KEYWORDS = sorted(
    ["database", "schema", "warehouse", "integration", "external volume"]
    + list(_SHOW_KEYWORD),
    key=len,
    reverse=True,
)

# The adapter's write methods, as api_calls records them
WRITE_CALLS = frozenset(
    {
        "grant_privileges",
        "revoke_privileges",
        "grant_privileges_on_all",
        "grant_future_privileges",
        "revoke_future_privileges",
        "grant_role_to_role",
        "revoke_role_from_role",
        "grant_role_to_user",
        "revoke_role_from_user",
        "set_user_tag",
        "unset_user_tag",
    }
)


def granted_on(securable_type: str) -> str:
    """A securable type or SQL keyword as SHOW GRANTS reports it: ICEBERG_TABLE."""
    return securable_type.strip().upper().replace(" ", "_")


def canonical(spelling: str, *, strict: bool = False) -> Name:
    """Resolve an identifier spelling to the canonical parts Snowflake stores."""
    parts: List[str] = []
    for match in _PART.finditer(spelling.strip()):
        part = match.group(0)
        if part.startswith('"'):
            parts.append(part[1:-1].replace('""', '"'))
        elif _PLACEHOLDER.fullmatch(part):
            parts.append(part.upper().replace(" ", "_"))
        else:
            if strict and (not _UNQUOTED.fullmatch(part) or part.upper() in RESERVED):
                raise SnowflakeError(
                    f"SQL compilation error: syntax error unexpected '{part}'."
                )
            parts.append(part.upper())
    return tuple(parts)


def spell(name: Iterable[str]) -> str:
    """Render canonical parts the way SHOW GRANTS prints them."""
    rendered = []
    for part in name:
        if _PLACEHOLDER.fullmatch(part) or (
            _PLAIN_UPPER.fullmatch(part) and part not in QUOTED_IN_GRANTS
        ):
            rendered.append(part)
        else:
            rendered.append('"' + part.replace('"', '""') + '"')
    return ".".join(rendered)


_REST_FIELDS = {
    1: ("name",),
    2: ("database", "name"),
    3: ("database", "schema", "name"),
}


def rest_part(canonical_part: str, *, mixed_case: bool = False) -> str:
    """Spell one canonical part the way REST models carry it."""
    bare = _UNQUOTED if mixed_case else _PLAIN_UPPER
    if bare.fullmatch(canonical_part):
        return canonical_part
    return '"' + canonical_part.replace('"', '""') + '"'


def api_error(
    status: int, message: str = "", error_code: str = "002003", body: Any = None
) -> APIError:
    """
    The error snowflake.core raises for an HTTP response, built offline. A `body` its
    APIError can't read (not JSON, not UTF-8, or JSON that isn't an object of strings)
    raises here, as it does inside snowflake.core.
    """
    if body is None:
        body = json.dumps(
            {"message": message, "error_code": error_code, "request_id": "fake"}
        ).encode()
    response = RESTResponse(
        urllib3.HTTPResponse(
            body=body, status=status, headers={"Content-Type": "application/json"}
        )
    )
    error_class = {
        401: UnauthorizedError,
        403: ForbiddenError,
        404: NotFoundError,
        409: ConflictError,
    }.get(status, ServerError if status >= 500 else APIError)
    return error_class(root=None, http_resp=response)


@dataclass(frozen=True)
class Grant:
    """A privilege held by a role, as SHOW GRANTS TO ROLE lists it.

    `on` is the granted_on column: DATABASE, SCHEMA, TABLE, VIEW, WAREHOUSE,
    INTEGRATION, EXTERNAL_VOLUME, ACCOUNT, ROLE, DATABASE_ROLE, APPLICATION_ROLE.
    `name` is a spelling, resolved to canonical parts.
    """

    to: str
    privilege: str
    on: str
    name: str
    granted_by: str = "SECURITYADMIN"
    grant_option: bool = False


@dataclass(frozen=True)
class FutureGrant:
    """A future grant in a database or schema. `on` is the grant_on column: TABLE,
    VIEW, SCHEMA, ICEBERG_TABLE, DYNAMIC_TABLE, STREAMLIT, SEMANTIC_VIEW, TASK."""

    to: str
    privilege: str
    on: str
    container: str


@dataclass
class FakeAccount:
    locator: str = "AB12345"
    current_user: str = "TUNDRA"
    current_role: str = "SECURITYADMIN"
    session_roles: Set[str] = field(default_factory=lambda: {"SECURITYADMIN"})

    warehouses: List[str] = field(default_factory=list)
    integrations: List[str] = field(default_factory=list)
    external_volumes: List[str] = field(default_factory=list)
    databases: List[str] = field(default_factory=list)
    schemas: List[str] = field(default_factory=list)
    # SHOW keyword without TERSE (TABLES, VIEWS, ICEBERG TABLES...) to DB.SCHEMA.NAME
    objects: Dict[str, List[str]] = field(default_factory=dict)
    roles: Dict[str, str] = field(default_factory=dict)  # role to owner
    users: List[str] = field(default_factory=list)
    database_roles: Dict[str, List[str]] = field(default_factory=dict)  # db to roles
    applications: Dict[str, List[str]] = field(default_factory=dict)  # app to roles
    grants: List[Grant] = field(default_factory=list)
    future_grants: List[FutureGrant] = field(default_factory=list)
    user_roles: Dict[str, List[str]] = field(default_factory=dict)
    user_tags: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Each ALTER USER ... SET <properties> the account was sent, by user
    user_properties: Dict[str, List[str]] = field(default_factory=dict)

    # The most rows a REST listing returns, and whether a mixed-case part goes bare
    rest_list_cap: int = LIST_LIMIT
    rest_bare_mixed_case: bool = False

    # Roles the current role can't grant to or revoke from, on either transport
    unmanageable_roles: Set[str] = field(default_factory=set)
    # Errors FakeApi raises instead of answering, by write method, one per call
    api_write_errors: Dict[str, List[BaseException]] = field(default_factory=dict)

    queries: List[str] = field(default_factory=list)
    api_calls: List[Tuple[Any, ...]] = field(default_factory=list)

    def install(self, mocker) -> None:
        """
        Route every SnowflakeConnector, including ones tundra creates itself, here: its
        SQL to run_query and its REST calls to FakeApi.
        """
        account = self

        def init(self, config=None, sql_only=False):
            self._sql_only = sql_only

        mocker.patch.object(SnowflakeConnector, "__init__", init)
        mocker.patch.object(
            SnowflakeConnector,
            "run_query",
            lambda self, query: account.run_query(query),
        )
        mocker.patch.object(
            SnowflakeConnector, "api", property(lambda self: account.api)
        )

    @property
    def api(self) -> "FakeApi":
        return FakeApi(self)

    # Lookups on canonical names

    def _account_objects(self, kind: str) -> List[str]:
        return {
            "WAREHOUSES": [self._key(n) for n in self.warehouses],
            "INTEGRATIONS": [self._key(n) for n in self.integrations],
            "EXTERNAL VOLUMES": [self._key(n) for n in self.external_volumes],
            "DATABASES": [self._key(n) for n in self.databases],
            "USERS": [self._key(n) for n in self.users],
        }[kind]

    def _schemas(self) -> List[Name]:
        return [canonical(s) for s in self.schemas]

    def _objects(self, kind: str) -> List[Name]:
        return [canonical(o) for o in self.objects.get(kind, [])]

    def _role(self, spelling: str) -> str:
        name = canonical(spelling, strict=True)
        known = {self._key(r) for r in self.roles}
        if len(name) != 1 or name[0] not in known:
            raise SnowflakeError(
                f"SQL compilation error: Role '{spelling}' does not exist or not authorized."
            )
        return name[0]

    @staticmethod
    def _key(name: str) -> str:
        """A single-part name: a spelling, or a canonical name that isn't a valid one."""
        return name if _is_canonical(name) else canonical(name)[0]

    # Query dispatch

    def run_query(self, query: str) -> _BufferedResult:
        self.queries.append(query)
        text = " ".join(query.split())
        for pattern, handler in self._handlers():
            match = re.fullmatch(pattern, text, re.IGNORECASE)
            if match:
                return _BufferedResult(handler(*match.groups()))
        raise AssertionError(f"The fake account has no answer for: {query}")

    def _handlers(self):
        return [
            (r"SELECT CURRENT_USER\(\) AS USER", lambda: [{"user": self.current_user}]),
            (r"SELECT CURRENT_ROLE\(\) AS ROLE", lambda: [{"role": self.current_role}]),
            (
                r"SELECT IS_ROLE_IN_SESSION\('(.+)'\) AS IN_SESSION",
                lambda role: [{"in_session": role.upper() in self.session_roles}],
            ),
            (
                r"SHOW (WAREHOUSES|INTEGRATIONS|EXTERNAL VOLUMES|DATABASES|USERS)",
                lambda kind: [{"name": n} for n in self._account_objects(kind.upper())],
            ),
            (r"SHOW TERSE SCHEMAS IN ACCOUNT", lambda: self._show_schemas(None)),
            (r"SHOW TERSE SCHEMAS IN DATABASE (.+)", self._show_schemas),
            (
                r"SHOW (?:TERSE )?(TABLES|VIEWS|ICEBERG TABLES|DYNAMIC TABLES|STREAMLITS"
                r"|SEMANTIC VIEWS|TASKS) IN (ACCOUNT|DATABASE (.+)|SCHEMA (.+))",
                self._show_objects,
            ),
            (r"SHOW FUTURE GRANTS IN (DATABASE|SCHEMA) (.+)", self._show_future_grants),
            (r"SHOW FUTURE GRANTS TO ROLE (.+)", self._show_future_grants_to_role),
            (r"SHOW GRANTS TO ROLE (.+)", self._show_grants_to_role),
            (r"SHOW GRANTS TO USER (.+)", self._show_grants_to_user),
            (r"SHOW ROLES", self._show_roles),
            (r"SHOW DATABASE ROLES IN DATABASE (.+)", self._show_database_roles),
            (
                r"SHOW APPLICATION ROLES IN APPLICATION (.+)",
                self._show_application_roles,
            ),
            (
                r"SELECT tag_database, tag_schema, tag_name, tag_value FROM TABLE\(\s*"
                r"SNOWFLAKE\.INFORMATION_SCHEMA\.TAG_REFERENCES\('(.+)', 'USER'\)\s*\) "
                r"WHERE level = domain",
                self._tag_references,
            ),
            # Writes
            (
                r"GRANT OWNERSHIP ON (\w+) (.+) TO ROLE (.+) COPY CURRENT GRANTS",
                self._sql(self._sql_ownership),
            ),
            (
                r"(GRANT|REVOKE) (DATABASE ROLE|APPLICATION ROLE|ROLE) (.+) "
                r"(?:TO|FROM) (ROLE|USER) (.+)",
                self._sql(self._sql_membership),
            ),
            (
                r"(GRANT|REVOKE) (.+?) ON ACCOUNT (?:TO|FROM) ROLE (.+)",
                self._sql(self._sql_account),
            ),
            (
                r"(GRANT|REVOKE) (.+?) ON (ALL|FUTURE) (.+?)S IN (DATABASE|SCHEMA) (.+) "
                r"(?:TO|FROM) ROLE (.+)",
                self._sql(self._sql_bulk),
            ),
            (
                r"(GRANT|REVOKE) (.+?) ON ({}) (.+) (?:TO|FROM) ROLE (.+)".format(
                    "|".join(_SECURABLE_KEYWORDS)
                ),
                self._sql(self._sql_privileges),
            ),
            (
                r"ALTER USER (.+?) SET TAG (.+) = '((?:[^']|'')*)'",
                self._sql(self._sql_set_tag),
            ),
            (r"ALTER USER (.+?) UNSET TAG (.+)", self._sql(self._sql_unset_tag)),
            (r"ALTER USER (.+?) SET (.+)", self._sql(self._sql_alter_user)),
        ]

    def _show_schemas(self, database: Optional[str]):
        db = canonical(database, strict=True) if database else None
        return [
            {"database_name": s[0], "name": s[1]}
            for s in self._schemas()
            if db is None or s[:1] == db
        ]

    def _show_objects(self, kind, scope, database, schema):
        kind = kind.upper()
        if database:
            container = canonical(database, strict=True)
        elif schema:
            container = canonical(schema, strict=True)
        else:
            container = ()
        return [
            {"database_name": o[0], "schema_name": o[1], "name": o[2]}
            for o in self._objects(kind)
            if o[: len(container)] == container
        ]

    def _show_future_grants(self, scope: str, spelling: str):
        container = canonical(spelling, strict=True)
        return [
            {
                "grant_to": "ROLE",
                "grantee_name": self._key(g.to),
                "privilege": g.privilege,
                "grant_on": g.on,
                "name": spell(canonical(g.container) + (f"<{g.on}>",)),
            }
            for g in self.future_grants
            if canonical(g.container) == container
        ]

    def _show_future_grants_to_role(self, spelling: str):
        role = self._role(spelling)
        return [
            {
                "privilege": g.privilege,
                "grant_on": g.on,
                "name": spell(canonical(g.container) + (f"<{g.on}>",)),
                "grant_to": "ROLE",
                "grantee_name": role,
                "grant_option": "false",
            }
            for g in self.future_grants
            if self._key(g.to) == role
        ]

    def _grant_rows(self, grantee: str, grants: Iterable[Grant]):
        return [
            {
                "privilege": g.privilege,
                "granted_on": g.on,
                "name": self.locator if g.on == "ACCOUNT" else spell(canonical(g.name)),
                "granted_to": "ROLE",
                "grantee_name": grantee,
                "grant_option": "true" if g.grant_option else "false",
                "granted_by": g.granted_by,
            }
            for g in grants
        ]

    def _show_grants_to_role(self, spelling: str):
        role = self._role(spelling)
        return self._grant_rows(
            role, (g for g in self.grants if self._key(g.to) == role)
        )

    def _show_grants_to_user(self, spelling: str):
        user = canonical(spelling, strict=True)[0]
        roles = {self._key(k): v for k, v in self.user_roles.items()}
        return [
            {"role": self._key(role), "granted_to": "USER", "grantee_name": user}
            for role in roles.get(user, [])
        ]

    def _show_roles(self):
        return [
            {"name": self._key(role), "owner": self._key(owner)}
            for role, owner in self.roles.items()
        ]

    def _show_database_roles(self, spelling: str):
        db = canonical(spelling, strict=True)[0]
        roles = {canonical(k)[0]: v for k, v in self.database_roles.items()}
        return [{"name": canonical(r)[0]} for r in roles.get(db, [])]

    def _show_application_roles(self, spelling: str):
        app = canonical(spelling, strict=True)[0]
        apps = {canonical(k)[0]: v for k, v in self.applications.items()}
        if app not in apps:
            raise SnowflakeError(
                f"Application '{app}' does not exist or not authorized."
            )
        return [{"name": canonical(r)[0]} for r in apps[app]]

    # SQL writes: each handler resolves the statement's identifiers and calls a mutator

    def _sql(self, handler):
        """A write handler that answers with no rows and fails the way SQL does."""

        def run(*groups):
            try:
                handler(*groups)
            except Missing as missing:
                kind = missing.kind[0].upper() + missing.kind[1:]
                raise SnowflakeError(
                    f"SQL compilation error:\n{kind} '{spell(missing.name)}' does not"
                    " exist or not authorized."
                )
            except Refused as refused:
                raise SnowflakeError(
                    "SQL access control error:\nInsufficient privileges to operate on"
                    f" role '{refused.role}'"
                )
            return []

        return run

    @staticmethod
    def _privilege_list(privileges: str) -> List[str]:
        return [privilege.strip().upper() for privilege in privileges.split(",")]

    def _sql_ownership(self, kind: str, name: str, role: str) -> None:
        self.grant_ownership(
            self._sql_role(role), kind.lower(), canonical(name, strict=True)
        )

    def _sql_membership(self, action, member_kind, member, grantee_kind, grantee):
        member_kind = member_kind.upper()
        name = canonical(member, strict=True)
        if grantee_kind.upper() == "USER":
            (user,) = canonical(grantee, strict=True)
            (role,) = name
            if action.upper() == "GRANT":
                self.grant_user_role(user, role)
            else:
                self.revoke_user_role(user, role)
            return
        on = member_kind.replace(" ", "_")
        if action.upper() == "GRANT":
            self.grant_role(self._sql_role(grantee), on, name)
        else:
            self.revoke_role(self._sql_role(grantee), on, name)

    def _sql_account(self, action: str, privileges: str, role: str) -> None:
        write = (
            self.grant_privileges
            if action.upper() == "GRANT"
            else self.revoke_privileges
        )
        write(self._sql_role(role), self._privilege_list(privileges), "account", ())

    def _sql_bulk(self, action, privileges, on, kind, scope, container, role):
        role = self._sql_role(role)
        privilege_list = self._privilege_list(privileges)
        name = canonical(container, strict=True)
        if on.upper() == "ALL":
            assert action.upper() == "GRANT", "tundra never revokes ON ALL"
            self.grant_on_all(role, privilege_list, kind.lower(), name)
        elif action.upper() == "GRANT":
            self.grant_future(role, privilege_list, granted_on(kind), name)
        else:
            self.revoke_future(role, privilege_list, granted_on(kind), name)

    def _sql_privileges(self, action, privileges, kind, name, role) -> None:
        write = (
            self.grant_privileges
            if action.upper() == "GRANT"
            else self.revoke_privileges
        )
        write(
            self._sql_role(role),
            self._privilege_list(privileges),
            kind.lower(),
            canonical(name, strict=True),
        )

    def _sql_set_tag(self, user: str, tag: str, value: str) -> None:
        (name,) = canonical(user, strict=True)
        self.set_user_tag(name, canonical(tag, strict=True), value.replace("''", "'"))

    def _sql_unset_tag(self, user: str, tag: str) -> None:
        (name,) = canonical(user, strict=True)
        self.unset_user_tag(name, canonical(tag, strict=True))

    def _sql_alter_user(self, user: str, properties: str) -> None:
        (name,) = canonical(user, strict=True)
        self.require("user", (name,))
        self.user_properties.setdefault(spell((name,)), []).append(properties)

    def _sql_role(self, spelling: str) -> str:
        (role,) = canonical(spelling, strict=True)
        return role

    # Mutators, shared by the SQL statements and FakeApi's write calls. Names are
    # canonical parts and roles and users canonical names.

    def require(self, kind: str, name: Name) -> None:
        """
        Raise Missing for the first part of `name` the account doesn't have, the way
        Snowflake reports a missing parent rather than the object in it.
        """
        known: Dict[str, Iterable[Name]] = {
            "warehouse": [(n,) for n in self._account_objects("WAREHOUSES")],
            "integration": [(n,) for n in self._account_objects("INTEGRATIONS")],
            "external volume": [
                (n,) for n in self._account_objects("EXTERNAL VOLUMES")
            ],
            "user": [(n,) for n in self._account_objects("USERS")],
            "role": [(self._key(r),) for r in self.roles],
        }
        if kind == "account":
            return
        if kind in known:
            if name not in set(known[kind]):
                raise Missing(kind, name)
            return
        if name[:1] not in {(n,) for n in self._account_objects("DATABASES")}:
            if kind not in ("database role", "application role"):
                raise Missing("database", name[:1])
        if kind == "database":
            return
        if kind == "database role":
            roles = {canonical(k)[0]: v for k, v in self.database_roles.items()}
            if name[1] not in {canonical(r)[0] for r in roles.get(name[0], [])}:
                raise Missing(kind, name)
            return
        if kind == "application role":
            apps = {canonical(k)[0]: v for k, v in self.applications.items()}
            if name[1] not in {canonical(r)[0] for r in apps.get(name[0], [])}:
                raise Missing(kind, name)
            return
        if name[:2] not in set(self._schemas()):
            raise Missing("schema", name[:2])
        if kind != "schema" and name not in set(self._objects(_SHOW_KEYWORD[kind])):
            raise Missing(kind, name)

    def _require_role(self, role: str) -> None:
        self.require("role", (role,))
        if role in {self._key(r) for r in self.unmanageable_roles}:
            raise Refused(role)

    def _held(self, grant: Grant, role: str, privilege: str, on: str, name: Name):
        return (
            self._key(grant.to) == role
            and grant.privilege.upper() == privilege
            and granted_on(grant.on) == on
            and (on == "ACCOUNT" or canonical(grant.name) == name)
        )

    def grant_privileges(
        self, role: str, privileges: Iterable[str], kind: str, name: Name
    ) -> None:
        """Privileges on a securable of `kind` (database, iceberg table, account...)."""
        self.require(kind, name)
        self.grant(role, privileges, granted_on(kind), name)

    def revoke_privileges(
        self, role: str, privileges: Iterable[str], kind: str, name: Name
    ) -> None:
        self.require(kind, name)
        self.revoke(role, privileges, granted_on(kind), name)

    def grant(self, role: str, privileges: Iterable[str], on: str, name: Name):
        """GRANT <privileges> ON <on> <name> TO ROLE <role>. `on` is SHOW GRANTS' granted_on."""
        self._require_role(role)
        for privilege in privileges:
            # SHOW GRANTS reports IMPORTED PRIVILEGES on a database as USAGE on it
            if privilege == "IMPORTED PRIVILEGES":
                privilege = "USAGE"
            if any(self._held(g, role, privilege, on, name) for g in self.grants):
                continue
            self.grants.append(
                Grant(
                    spell((role,)),
                    privilege,
                    on,
                    "-" if on == "ACCOUNT" else spell(name),
                    granted_by=self.current_role,
                )
            )

    def revoke(self, role: str, privileges: Iterable[str], on: str, name: Name):
        self._require_role(role)
        for privilege in privileges:
            held = (
                ("USAGE", "IMPORTED PRIVILEGES")
                if privilege == "IMPORTED PRIVILEGES"
                else (privilege,)
            )
            self.grants = [
                g
                for g in self.grants
                if not any(self._held(g, role, p, on, name) for p in held)
            ]

    def grant_on_all(
        self, role: str, privileges: List[str], kind: str, container: Name
    ):
        self._require_role(role)
        self.require("database" if len(container) == 1 else "schema", container)
        for name in self._objects(_SHOW_KEYWORD[kind]):
            if name[: len(container)] == container:
                self.grant(role, privileges, granted_on(kind), name)

    def grant_future(self, role: str, privileges: List[str], on: str, container: Name):
        self._require_role(role)
        self.require("database" if len(container) == 1 else "schema", container)
        for privilege in privileges:
            if not self._future_held(role, privilege, on, container):
                self.future_grants.append(
                    FutureGrant(spell((role,)), privilege, on, spell(container))
                )

    def revoke_future(self, role: str, privileges: List[str], on: str, container: Name):
        self._require_role(role)
        self.require("database" if len(container) == 1 else "schema", container)
        self.future_grants = [
            g
            for g in self.future_grants
            if not any(
                self._future_held(role, p, on, container, [g]) for p in privileges
            )
        ]

    def _future_held(self, role, privilege, on, container, grants=None) -> bool:
        return any(
            self._key(g.to) == role
            and g.privilege.upper() == privilege
            and granted_on(g.on) == on
            and canonical(g.container) == container
            for g in (self.future_grants if grants is None else grants)
        )

    def grant_role(self, grantee: str, on: str, member: Name) -> None:
        """A role, database role or application role (on) granted to a role."""
        self.require(on.replace("_", " ").lower(), member)
        self.grant(grantee, ["USAGE"], on, member)

    def revoke_role(self, grantee: str, on: str, member: Name) -> None:
        self.require(on.replace("_", " ").lower(), member)
        self.revoke(grantee, ["USAGE"], on, member)

    def grant_user_role(self, user: str, role: str) -> None:
        self.require("user", (user,))
        self.require("role", (role,))
        roles = self._user_role_list(user)
        if role not in {self._key(r) for r in roles}:
            roles.append(spell((role,)))

    def revoke_user_role(self, user: str, role: str) -> None:
        self.require("user", (user,))
        self.require("role", (role,))
        roles = self._user_role_list(user)
        roles[:] = [r for r in roles if self._key(r) != role]

    def _user_role_list(self, user: str) -> List[str]:
        for key, roles in self.user_roles.items():
            if self._key(key) == user:
                return roles
        return self.user_roles.setdefault(spell((user,)), [])

    def set_user_tag(self, user: str, tag: Name, value: str) -> None:
        self.unset_user_tag(user, tag)
        self._user_tag_map(user)[spell(tag)] = value

    def unset_user_tag(self, user: str, tag: Name) -> None:
        self.require("user", (user,))
        tags = self._user_tag_map(user)
        for key in [k for k in tags if canonical(k) == tag]:
            del tags[key]

    def _user_tag_map(self, user: str) -> Dict[str, str]:
        for key, tags in self.user_tags.items():
            if self._key(key) == user:
                return tags
        return self.user_tags.setdefault(spell((user,)), {})

    def grant_ownership(self, role: str, kind: str, name: Name) -> None:
        self._require_role(role)
        self.require(kind, name)
        on = granted_on(kind)
        self.grants = [
            g
            for g in self.grants
            if not (
                g.privilege.upper() == "OWNERSHIP"
                and granted_on(g.on) == on
                and canonical(g.name) == name
            )
        ]
        self.grant(role, ["OWNERSHIP"], on, name)

    def state(self) -> Dict[str, Any]:
        """
        What writes change, in canonical names and sorted, so two accounts that took
        the same statements compare equal however each spelt or ordered them.
        """
        return {
            "grants": sorted(
                {
                    (
                        self._key(g.to),
                        g.privilege.upper(),
                        granted_on(g.on),
                        () if granted_on(g.on) == "ACCOUNT" else canonical(g.name),
                    )
                    for g in self.grants
                }
            ),
            "future_grants": sorted(
                {
                    (
                        self._key(g.to),
                        g.privilege.upper(),
                        granted_on(g.on),
                        canonical(g.container),
                    )
                    for g in self.future_grants
                }
            ),
            "user_roles": sorted(
                (self._key(user), tuple(sorted(self._key(r) for r in roles)))
                for user, roles in self.user_roles.items()
                if roles
            ),
            "user_tags": sorted(
                (
                    self._key(user),
                    tuple(sorted((canonical(t), v) for t, v in tags.items())),
                )
                for user, tags in self.user_tags.items()
                if tags
            ),
            "user_properties": sorted(
                (self._key(user), tuple(properties))
                for user, properties in self.user_properties.items()
            ),
        }

    def _tag_references(self, identifier: str):
        user = canonical(identifier.replace("''", "'"), strict=True)[0]
        rows = []
        tags = {self._key(k): v for k, v in self.user_tags.items()}
        for tag, value in tags.get(user, {}).items():
            database, schema, name = canonical(tag)
            rows.append(
                {
                    "tag_database": database,
                    "tag_schema": schema,
                    "tag_name": name,
                    "tag_value": value,
                }
            )
        return rows


class FakeApi:
    """
    The account's answers to tundra.snowflake_api.SnowflakeApi, as snowflake.core's own
    models, so their validators check every spelling the fake sends. Each call is
    recorded in api_calls, and a listing stops at rest_list_cap rows, in the order SHOW
    lists them. A name in a path resolves like an identifier: quoted exactly, bare in
    uppercase. A role that doesn't exist is a 404, as Snowflake answers it. Write calls
    change the account through the same mutators as the statements' SQL.
    """

    def __init__(self, account: FakeAccount) -> None:
        self.account = account

    @property
    def list_limit(self) -> int:
        return self.account.rest_list_cap

    def grants_to_role(self, role: str) -> List[RestGrant]:
        self._called("grants_to_role", role)
        name = self._role(role)
        return self._capped(
            [
                RestGrant.from_dict(
                    {
                        "securable": (
                            {"name": self._part(self.account.locator)}
                            if g.on == "ACCOUNT"
                            else self._securable(canonical(g.name))
                        ),
                        "securable_type": g.on.replace("_", " "),
                        "grant_option": g.grant_option,
                        "privileges": [g.privilege],
                        "granted_by": self._part(g.granted_by) if g.granted_by else "",
                    }
                )
                for g in self.account.grants
                if self.account._key(g.to) == name
            ]
        )

    def future_grants_to_role(self, role: str) -> List[RestGrant]:
        self._called("future_grants_to_role", role)
        name = self._role(role)
        return self._capped(
            [
                RestGrant.from_dict(
                    {
                        # The placeholder keeps its underscore, the type has a space
                        "securable": self._securable(
                            canonical(g.container) + (f"<{g.on}>",)
                        ),
                        "securable_type": g.on.replace("_", " "),
                        "grant_option": False,
                        "privileges": [g.privilege],
                    }
                )
                for g in self.account.future_grants
                if self.account._key(g.to) == name
            ]
        )

    def grants_to_user(self, user: str) -> List[RestUserGrant]:
        self._called("grants_to_user", user)
        name = self._path(user)
        roles = {self.account._key(k): v for k, v in self.account.user_roles.items()}
        return self._capped(
            [
                RestUserGrant.from_dict(
                    {
                        "securable": {"name": self._part(self.account._key(role))},
                        "securable_type": "ROLE",
                    }
                )
                for role in roles.get(name, [])
            ]
        )

    def databases(self) -> List[Database]:
        self._called("databases")
        return self._named(Database, self.account._account_objects("DATABASES"))

    def warehouses(self) -> List[Warehouse]:
        self._called("warehouses")
        return self._named(Warehouse, self.account._account_objects("WAREHOUSES"))

    def users(self) -> List[User]:
        self._called("users")
        return self._named(User, self.account._account_objects("USERS"))

    def schemas(self, database: str) -> List[Schema]:
        self._called("schemas", database)
        db = self._path(database)
        return self._named(
            Schema, [s[1] for s in self.account._schemas() if s[0] == db]
        )

    def database_roles(self, database: str) -> List[DatabaseRole]:
        self._called("database_roles", database)
        db = self._path(database)
        roles = {canonical(k)[0]: v for k, v in self.account.database_roles.items()}
        return self._named(DatabaseRole, [canonical(r)[0] for r in roles.get(db, [])])

    # Writes, answered by changing the account the way the statement's SQL does

    def grant_privileges(self, role, privileges, securable_type, securable) -> None:
        self._write(
            "grant_privileges",
            (role, privileges, securable_type, securable),
            lambda: self.account.grant_privileges(
                self._role(role),
                self._privileges(privileges),
                securable_type.lower(),
                self._name(securable),
            ),
        )

    def revoke_privileges(self, role, privileges, securable_type, securable) -> None:
        self._write(
            "revoke_privileges",
            (role, privileges, securable_type, securable),
            lambda: self.account.revoke_privileges(
                self._role(role),
                self._privileges(privileges),
                securable_type.lower(),
                self._name(securable),
            ),
        )

    def grant_privileges_on_all(
        self, role, privileges, securable_type, containing_scope
    ) -> None:
        self._write(
            "grant_privileges_on_all",
            (role, privileges, securable_type, containing_scope),
            lambda: self.account.grant_on_all(
                self._role(role),
                self._privileges(privileges),
                securable_type.lower(),
                self._name(containing_scope),
            ),
        )

    def grant_future_privileges(
        self, role, privileges, securable_type, containing_scope
    ) -> None:
        self._write(
            "grant_future_privileges",
            (role, privileges, securable_type, containing_scope),
            lambda: self.account.grant_future(
                self._role(role),
                self._privileges(privileges),
                granted_on(securable_type),
                self._name(containing_scope),
            ),
        )

    def revoke_future_privileges(
        self, role, privileges, securable_type, containing_scope
    ) -> None:
        self._write(
            "revoke_future_privileges",
            (role, privileges, securable_type, containing_scope),
            lambda: self.account.revoke_future(
                self._role(role),
                self._privileges(privileges),
                granted_on(securable_type),
                self._name(containing_scope),
            ),
        )

    def grant_role_to_role(self, grantee, role_type, role) -> None:
        self._write(
            "grant_role_to_role",
            (grantee, role_type, role),
            lambda: self.account.grant_role(
                self._role(grantee), self._role_type(role_type), self._name(role)
            ),
        )

    def revoke_role_from_role(self, grantee, role_type, role) -> None:
        self._write(
            "revoke_role_from_role",
            (grantee, role_type, role),
            lambda: self.account.revoke_role(
                self._role(grantee), self._role_type(role_type), self._name(role)
            ),
        )

    def grant_role_to_user(self, user, role) -> None:
        self._write(
            "grant_role_to_user",
            (user, role),
            lambda: self.account.grant_user_role(self._path(user), *self._name(role)),
        )

    def revoke_role_from_user(self, user, role) -> None:
        self._write(
            "revoke_role_from_user",
            (user, role),
            lambda: self.account.revoke_user_role(self._path(user), *self._name(role)),
        )

    def set_user_tag(self, user, tag, value) -> None:
        self._write(
            "set_user_tag",
            (user, tag, value),
            lambda: self.account.set_user_tag(
                self._path(user), tuple(map(self._canonical, tag)), value
            ),
        )

    def unset_user_tag(self, user, tag) -> None:
        self._write(
            "unset_user_tag",
            (user, tag),
            lambda: self.account.unset_user_tag(
                self._path(user), tuple(map(self._canonical, tag))
            ),
        )

    def _write(self, call: str, args: Tuple[Any, ...], change) -> None:
        """
        Record the call, then answer it as Snowflake would: an error queued in
        api_write_errors, a 404 naming what's missing in lowercase, a 403 for a role
        the current role can't manage, or the change made.
        """
        self._called(call, *map(_jsonable, args))
        errors = self.account.api_write_errors.get(call)
        if errors:
            raise errors.pop(0)
        try:
            change()
        except Missing as missing:
            name = ".".join(missing.name).lower()
            raise api_error(
                404,
                f"{missing.kind} '{name}' does not exist or not authorized. your"
                f" primary role {self.account.current_role.lower()} must have at least"
                f" one privilege granted on {missing.kind} {name}.",
            )
        except Refused as refused:
            raise api_error(
                403,
                f"Insufficient privileges to operate on role '{refused.role}'.",
                "003001",
            )

    @staticmethod
    def _privileges(privileges: List[str]) -> List[str]:
        return [privilege.upper() for privilege in privileges]

    @staticmethod
    def _role_type(role_type: str) -> str:
        if role_type == "application role":
            # What the dev probe got back for an application role through grant_role
            raise api_error(
                400, "syntax error line 1 at position 12 unexpected 'on'.", "001003"
            )
        return granted_on(role_type)

    @staticmethod
    def _canonical(part: str) -> str:
        """One part of a request body, resolved the way the server resolves it."""
        (name,) = canonical(part)
        return name

    def _name(self, model: Any) -> Name:
        """The canonical parts of a Securable or ContainingScope, or () for none."""
        if model is None:
            return ()
        parts = (
            getattr(model, "database", None),
            getattr(model, "var_schema", None),
            getattr(model, "name", None),
        )
        return tuple(self._canonical(part) for part in parts if part is not None)

    def _called(self, call: str, *args: Any) -> None:
        self.account.api_calls.append((call, *args))

    def _capped(self, models: List[Any]) -> List[Any]:
        return models[: self.list_limit]

    def _named(self, model: Any, names: Iterable[str]) -> List[Any]:
        return self._capped([model.from_dict({"name": self._part(n)}) for n in names])

    def _part(self, canonical_part: str) -> str:
        return rest_part(canonical_part, mixed_case=self.account.rest_bare_mixed_case)

    def _securable(self, parts: Name) -> Dict[str, str]:
        return dict(zip(_REST_FIELDS[len(parts)], map(self._part, parts)))

    @staticmethod
    def _path(spelling: str) -> str:
        if spelling.startswith('"'):
            (name,) = canonical(spelling)
            return name
        if not _UNQUOTED.fullmatch(spelling):
            raise api_error(400, f"invalid identifier '{spelling}'", "002025")
        return spelling.upper()

    def _role(self, spelling: str) -> str:
        role = self._path(spelling)
        if role not in {self.account._key(r) for r in self.account.roles}:
            raise api_error(
                404, f"Role '{role.lower()}' does not exist or not authorized."
            )
        return role


def _jsonable(value: Any) -> Any:
    """A write call's argument as api_calls records it: models as their request body."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def _is_canonical(name: str) -> bool:
    """Single-part names that aren't valid spellings are taken as canonical as-is."""
    return not (_UNQUOTED.fullmatch(name) or name.startswith('"'))
