"""A fake Snowflake account that answers tundra's queries the way Snowflake does.

State is declared with the names Snowflake stores (canonical names), written as
identifier spellings for readability: `raw.public` and `RAW.PUBLIC` declare the same
schema, `raw."Mixed-Case"` declares a case-sensitive one. The fake then answers in the
two forms Snowflake uses:

- SHOW <objects> (databases, schemas, tables, roles, users...) report canonical names,
  unquoted and case-sensitive: `Mixed-Case`, `AGGREGATE`.
- SHOW GRANTS and SHOW FUTURE GRANTS report identifier spellings, quoting any part that
  isn't a plain uppercase identifier and any keyword Snowflake quotes:
  `RAW."Mixed-Case"`, `LAB_RESULTS."AGGREGATE".<TABLE>`.

Identifiers tundra sends are resolved with Snowflake's rules, so a query naming
`"group"` finds nothing when the stored object is `GROUP`, and an unquoted reserved word
fails the way it would in Snowflake. A query the fake can't answer fails the test, so
every query a scenario makes is accounted for.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from tundra.snowflake_connector import SnowflakeConnector, _BufferedResult

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


@dataclass(frozen=True)
class Grant:
    """A privilege held by a role (or database role), as SHOW GRANTS TO ROLE lists it.

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

    queries: List[str] = field(default_factory=list)

    def install(self, mocker) -> None:
        """Route every SnowflakeConnector, including ones tundra creates itself, here."""
        account = self
        mocker.patch.object(
            SnowflakeConnector, "__init__", lambda self, config=None: None
        )
        mocker.patch.object(
            SnowflakeConnector,
            "run_query",
            lambda self, query: account.run_query(query),
        )

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
            (r"SHOW GRANTS TO ROLE (.+)", self._show_grants_to_role),
            (r"SHOW GRANTS TO DATABASE ROLE (.+)", self._show_grants_to_database_role),
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

    def _show_grants_to_database_role(self, spelling: str):
        name = canonical(spelling, strict=True)
        return self._grant_rows(
            ".".join(name), (g for g in self.grants if canonical(g.to) == name)
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


def _is_canonical(name: str) -> bool:
    """Single-part names that aren't valid spellings are taken as canonical as-is."""
    return not (_UNQUOTED.fullmatch(name) or name.startswith('"'))
