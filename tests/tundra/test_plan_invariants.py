# A REST write abandoned at its deadline can land after later statements have run as
# SQL, and even after tundra exits: snowflake.core sends the first request with no socket
# timeout, resends one answered 429, 503 or 504 until 600 s after the first send, and a
# 202 is a job Snowflake finishes whatever the client does. GRANT and REVOKE are
# idempotent, so a late request only repeats a statement, and within a run that's safe
# as long as no plan also holds a statement the late one would undo: the opposite
# statement on the same privilege, thing and grantee.
#
# Each plan is checked for such a pair, on the grants Snowflake records. A statement on
# all objects of a type in a database or schema is one on each such object inside it,
# so it counts against a statement on one of those objects, and against another on all
# of them in the same container or one nested in it. A future grant is recorded on its
# container and touches no existing object, so it counts only against another future
# statement on the same type in the same container: a database's future grants are
# recorded apart from a schema's.

from dataclasses import replace
from typing import Iterator, List, NamedTuple, Optional, Tuple

import pytest

import test_apply_transports as transports
import test_golden_scenarios as golden
from tundra.identifiers import ObjectName
from tundra.operations import (
    ALL,
    FUTURE,
    GRANT,
    REVOKE,
    BulkPrivileges,
    Operation,
    Privileges,
    RoleMembership,
    SqlStatement,
    UserTag,
)
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra_test_utils.fake_account import FakeAccount, FutureGrant, Grant

LOADER = ObjectName.single("loader")
ANALYST = ObjectName.single("analyst")


class Write(NamedTuple):
    """One privilege, role or tag a statement grants or revokes, and what it's on."""

    action: str
    grantee: Tuple[str, Tuple[str, ...]]
    privilege: str
    securable_type: str
    # The object's parts, or the container's for a bulk statement, () for the account
    parts: Tuple[str, ...]
    # "all" or "future" for a bulk statement
    bulk: Optional[str] = None
    # A tag's value, which two sets can disagree on
    value: Optional[str] = None


def writes(operation: Operation) -> Iterator[Write]:
    """
    What a statement changes. A SqlStatement never goes through the API, so it's never
    a late request. The ones tundra plans (ownership, application roles, ALTER USER)
    change nothing an API-capable statement does, so they're left out.
    """
    if isinstance(operation, Privileges):
        parts = () if operation.target is None else operation.target.parts
        for privilege in operation.privileges:
            yield Write(
                operation.action,
                ("role", operation.role.parts),
                privilege.lower(),
                operation.securable_type,
                parts,
            )
    elif isinstance(operation, BulkPrivileges):
        for privilege in operation.privileges:
            yield Write(
                operation.action,
                ("role", operation.role.parts),
                privilege.lower(),
                operation.securable_type,
                operation.container.parts,
                operation.on,
            )
    elif isinstance(operation, RoleMembership):
        yield Write(
            operation.action,
            (operation.grantee_type, operation.grantee.parts),
            "usage",
            operation.member_type,
            operation.member.parts,
        )
    elif isinstance(operation, UserTag):
        yield Write(
            GRANT if operation.action == "set" else REVOKE,
            ("user", operation.user.parts),
            "tag",
            "tag",
            operation.tag.parts,
            value=operation.value,
        )
    else:
        assert isinstance(operation, SqlStatement), operation


def _inside(inner: Tuple[str, ...], outer: Tuple[str, ...]) -> bool:
    return len(inner) > len(outer) and inner[: len(outer)] == outer


def overlap(a: Write, b: Write) -> bool:
    """Whether two writes reach a grant Snowflake records for one grantee."""
    if (a.grantee, a.privilege, a.securable_type) != (
        b.grantee,
        b.privilege,
        b.securable_type,
    ):
        return False
    if FUTURE in (a.bulk, b.bulk):
        return a.bulk == b.bulk and a.parts == b.parts
    if a.bulk and b.bulk:
        return (
            a.parts == b.parts or _inside(a.parts, b.parts) or _inside(b.parts, a.parts)
        )
    if a.bulk:
        return _inside(b.parts, a.parts)
    if b.bulk:
        return _inside(a.parts, b.parts)
    return a.parts == b.parts


def opposed(a: Write, b: Write) -> bool:
    """Whether one of two overlapping writes, landing late, would undo the other."""
    return a.action != b.action or (a.value is not None and a.value != b.value)


def conflicts(operations: List[Operation]) -> List[Tuple[str, str]]:
    """Each pair of statements in a plan where a late one could undo the other."""
    found = []
    for i, first in enumerate(operations):
        for second in operations[i + 1 :]:
            if any(
                overlap(a, b) and opposed(a, b)
                for a in writes(first)
                for b in writes(second)
            ):
                found.append((first.sql, second.sql))
    return found


def revoked(operation):
    return replace(operation, action=REVOKE)


SCHEMA = ObjectName("raw.public")
TABLE = ObjectName("raw.public.events")

GRANT_TABLE = Privileges(GRANT, ("select",), "table", TABLE, LOADER)
GRANT_ALL_IN_SCHEMA = BulkPrivileges(GRANT, ALL, ("select",), "table", SCHEMA, LOADER)
GRANT_FUTURE_IN_SCHEMA = BulkPrivileges(
    GRANT, FUTURE, ("select",), "table", SCHEMA, LOADER
)
GRANT_ALL_IN_DATABASE = BulkPrivileges(
    GRANT, ALL, ("select",), "table", ObjectName.single("raw"), LOADER
)


@pytest.mark.parametrize(
    "first,second",
    [
        (GRANT_TABLE, revoked(GRANT_TABLE)),
        (
            Privileges(
                GRANT, ("usage", "monitor"), "database", SCHEMA.database, LOADER
            ),
            Privileges(REVOKE, ("monitor",), "database", SCHEMA.database, LOADER),
        ),
        (
            Privileges(GRANT, ("create database",), "account", None, LOADER),
            Privileges(REVOKE, ("create database",), "account", None, LOADER),
        ),
        (GRANT_ALL_IN_SCHEMA, revoked(GRANT_TABLE)),
        (GRANT_ALL_IN_DATABASE, revoked(GRANT_TABLE)),
        (GRANT_FUTURE_IN_SCHEMA, revoked(GRANT_FUTURE_IN_SCHEMA)),
        (
            RoleMembership(GRANT, ANALYST, "role", LOADER, "role"),
            RoleMembership(REVOKE, ANALYST, "role", LOADER, "role"),
        ),
        (
            UserTag("set", ObjectName.single("jane"), ObjectName("gov.tags.team"), "a"),
            UserTag("unset", ObjectName.single("jane"), ObjectName("gov.tags.team")),
        ),
        (
            UserTag("set", ObjectName.single("jane"), ObjectName("gov.tags.team"), "a"),
            UserTag("set", ObjectName.single("jane"), ObjectName("gov.tags.team"), "b"),
        ),
    ],
    ids=[
        "one table",
        "one privilege of a list",
        "account",
        "all in its schema",
        "all in its database",
        "future against future",
        "role membership",
        "tag unset",
        "tag set to another value",
    ],
)
def test_the_check_finds_a_late_write_that_would_undo_another(first, second):
    for pair in ((first, second), (second, first)):
        assert conflicts(list(pair)) == [(pair[0].sql, pair[1].sql)]


@pytest.mark.parametrize(
    "first,second",
    [
        (GRANT_TABLE, GRANT_ALL_IN_SCHEMA),
        (GRANT_TABLE, Privileges(REVOKE, ("select",), "table", TABLE, ANALYST)),
        (GRANT_TABLE, Privileges(REVOKE, ("insert",), "table", TABLE, LOADER)),
        (
            GRANT_TABLE,
            Privileges(
                REVOKE, ("select",), "table", ObjectName("raw.public.orders"), LOADER
            ),
        ),
        (GRANT_TABLE, Privileges(REVOKE, ("select",), "view", TABLE, LOADER)),
        (
            GRANT_ALL_IN_SCHEMA,
            Privileges(
                REVOKE, ("select",), "table", ObjectName("raw.other.events"), LOADER
            ),
        ),
        (
            BulkPrivileges(
                GRANT, FUTURE, ("usage",), "schema", SCHEMA.database, LOADER
            ),
            Privileges(REVOKE, ("usage",), "database", SCHEMA.database, LOADER),
        ),
        (GRANT_ALL_IN_DATABASE, revoked(GRANT_FUTURE_IN_SCHEMA)),
        (
            GRANT_FUTURE_IN_SCHEMA,
            revoked(replace(GRANT_FUTURE_IN_SCHEMA, container=SCHEMA.database)),
        ),
        # A future grant touches no existing object, so narrowing a role from a schema
        # to one of its tables, or widening it the other way, commutes
        (GRANT_TABLE, revoked(GRANT_FUTURE_IN_SCHEMA)),
        (GRANT_FUTURE_IN_SCHEMA, revoked(GRANT_TABLE)),
        (GRANT_ALL_IN_SCHEMA, revoked(GRANT_FUTURE_IN_SCHEMA)),
        (
            BulkPrivileges(
                GRANT, FUTURE, ("usage",), "schema", SCHEMA.database, LOADER
            ),
            Privileges(REVOKE, ("usage",), "schema", SCHEMA, LOADER),
        ),
        (
            RoleMembership(GRANT, ANALYST, "role", LOADER, "role"),
            RoleMembership(REVOKE, ANALYST, "role", LOADER, "user"),
        ),
        (
            SqlStatement(
                "GRANT OWNERSHIP ON table raw.public.events TO ROLE loader"
                " COPY CURRENT GRANTS"
            ),
            revoked(GRANT_TABLE),
        ),
    ],
    ids=[
        "both grants",
        "another grantee",
        "another privilege",
        "another table",
        "another type",
        "a table in another schema",
        "future schemas against their database",
        "all in a database against future in one of its schemas",
        "future in a schema against future in its database",
        "a table against future in its schema",
        "future in a schema against a table in it",
        "all against future in one schema",
        "future schemas against one schema",
        "a role and a user of one name",
        "a statement only SQL runs",
    ],
)
def test_the_check_passes_writes_that_cant_undo_each_other(first, second):
    assert conflicts([first, second]) == []
    assert conflicts([second, first]) == []


def bulk(action: str, on: str, *parts: str) -> Write:
    return Write(action, ("role", LOADER.parts), "select", "table", parts, on)


# tundra never plans a REVOKE ON ALL, so these are Writes rather than statements
@pytest.mark.parametrize(
    "first,second,overlaps",
    [
        (bulk(GRANT, ALL, "RAW"), bulk(REVOKE, ALL, "RAW"), True),
        (bulk(GRANT, ALL, "RAW"), bulk(REVOKE, ALL, "RAW", "PUBLIC"), True),
        (bulk(GRANT, ALL, "RAW", "PUBLIC"), bulk(REVOKE, ALL, "RAW", "OTHER"), False),
        (bulk(GRANT, FUTURE, "RAW"), bulk(REVOKE, ALL, "RAW", "PUBLIC"), False),
    ],
    ids=[
        "all against all",
        "all in a database against all in one of its schemas",
        "all in sibling schemas",
        "future in a database against all in one of its schemas",
    ],
)
def test_statements_on_all_objects_overlap_where_their_objects_do(
    first, second, overlaps
):
    assert overlap(first, second) is overlaps
    assert overlap(second, first) is overlaps


NARROWED_SPEC = """
version: "1.0"
databases:
  - raw:
      shared: no
roles:
  - loader:
      privileges:
        databases:
          read:
            - raw
        schemas:
          read:
            - raw.public
        tables:
          read:
            - raw.public.events
"""


def narrowed_account() -> FakeAccount:
    # LOADER read raw.public.*, and the spec now names one table, so the plan revokes
    # the future grant beside the table's grant
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.PUBLIC"],
        objects={"TABLES": ["RAW.PUBLIC.EVENTS", "RAW.PUBLIC.ORDERS"]},
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", "RAW.PUBLIC"),
            Grant("LOADER", "SELECT", "TABLE", "RAW.PUBLIC.EVENTS"),
            Grant("LOADER", "SELECT", "TABLE", "RAW.PUBLIC.ORDERS"),
        ],
        future_grants=[FutureGrant("LOADER", "SELECT", "TABLE", "RAW.PUBLIC")],
    )


WIDENED_SPEC = """
version: "1.0"
databases:
  - raw:
      shared: no
roles:
  - loader:
      privileges:
        databases:
          read:
            - raw
        schemas:
          read:
            - raw.*
        tables:
          read:
            - raw.*.*
"""


def widened_account() -> FakeAccount:
    # LOADER read one table and a schema's future tables, and the spec now names the
    # whole database
    return FakeAccount(
        databases=["RAW"],
        schemas=["RAW.PUBLIC", "RAW.STAGING"],
        objects={"TABLES": ["RAW.PUBLIC.EVENTS", "RAW.STAGING.LOADS"]},
        roles={"LOADER": "SECURITYADMIN"},
        grants=[
            Grant("LOADER", "USAGE", "DATABASE", "RAW"),
            Grant("LOADER", "USAGE", "SCHEMA", "RAW.PUBLIC"),
            Grant("LOADER", "SELECT", "TABLE", "RAW.PUBLIC.EVENTS"),
        ],
        future_grants=[FutureGrant("LOADER", "SELECT", "TABLE", "RAW.PUBLIC")],
    )


# Every plan the golden and apply tests make, and a role narrowed and widened, those
# written here to a file
SCENARIOS = [
    pytest.param(
        golden.GOLDEN / f"{name}.yml",
        account,
        options,
        id=f"{name}-{account.__name__}",
    )
    for name, account, options in golden.PLANNED
] + [
    pytest.param(
        transports.DROPPED_SPEC, transports.dropped_account, {}, id="dropped schemas"
    ),
    pytest.param(NARROWED_SPEC, narrowed_account, {}, id="narrowed"),
    pytest.param(WIDENED_SPEC, widened_account, {}, id="widened"),
]


def planned(mocker, tmp_path, spec, account, options) -> List[Operation]:
    if isinstance(spec, str):
        path = tmp_path / "roles.yml"
        path.write_text(spec)
        spec = path
    account().install(mocker)
    loader = SnowflakeSpecLoader(
        str(spec), conn=SnowflakeConnector(), max_workers=1, **options
    )
    return [c.operation for c in loader.generate_permission_queries()]


@pytest.mark.parametrize("spec,account,options", SCENARIOS)
def test_no_plan_grants_and_revokes_the_same_privilege(
    mocker, tmp_path, spec, account, options
):
    # What's already granted doesn't run, but it's checked too, so that no plan is
    # left with nothing to check
    operations = planned(mocker, tmp_path, spec, account, options)
    assert operations

    assert conflicts(operations) == []


def test_a_narrowed_role_revokes_a_future_grant_beside_a_table_grant(mocker, tmp_path):
    # The shape where the old, stricter check and Snowflake differ: a late REVOKE of the
    # future grant can't undo the grant on a table that already exists
    plan = planned(mocker, tmp_path, NARROWED_SPEC, narrowed_account, {})
    revoke_future = (
        "REVOKE select ON FUTURE tables IN schema raw.public FROM ROLE loader"
    )
    grant_table = "GRANT select ON table raw.public.events TO ROLE loader"

    assert {revoke_future, grant_table} <= {operation.sql for operation in plan}
    assert conflicts(plan) == []
