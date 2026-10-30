# Planned operations: the SQL each renders, the PlannedCommand that carries it, and the
# routing table that decides which go through snowflake.core.

import copy
import json
import pickle

import pytest
import yaml

from tundra.cli.permissions import print_command
from tundra.identifiers import ObjectName
from tundra.operations import (
    ALL,
    FUTURE,
    GATES,
    GRANT,
    QUOTED_BODY,
    QUOTED_PATH,
    REVOKE,
    ROUTES,
    SEVERAL_BULK_PRIVILEGES,
    SEVERAL_PRIVILEGES,
    BulkPrivileges,
    PlannedCommand,
    Privileges,
    RoleMembership,
    Route,
    SqlStatement,
    UserTag,
    api_capable,
    gates,
    routes_to_api,
)
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra.table_object_types import ALL_OBJECT_TYPES, TABLE_OBJECT_TYPES

LOADER = ObjectName.single("loader")


def name(spelling: str) -> ObjectName:
    return ObjectName(spelling)


# Each operation and the SQL 2.0.0's generator printed for the same statement, written
# out rather than rendered from the templates, so a template change shows up here
RENDERINGS = [
    (
        Privileges(GRANT, ("usage",), "warehouse", ObjectName.single("etl-wh"), LOADER),
        'GRANT usage ON warehouse "etl-wh" TO ROLE loader',
    ),
    (
        Privileges(
            REVOKE, ("operate",), "warehouse", ObjectName.single("reporting"), LOADER
        ),
        "REVOKE operate ON warehouse reporting FROM ROLE loader",
    ),
    (
        Privileges(
            GRANT, ("usage",), "integration", ObjectName.single("s3_int"), LOADER
        ),
        "GRANT usage ON integration s3_int TO ROLE loader",
    ),
    (
        Privileges(
            REVOKE,
            ("usage",),
            "external volume",
            ObjectName.single("old_volume"),
            LOADER,
        ),
        "REVOKE usage ON external volume old_volume FROM ROLE loader",
    ),
    (
        Privileges(GRANT, ("apply tag",), "account", None, LOADER),
        "GRANT apply tag ON ACCOUNT TO ROLE loader",
    ),
    (
        Privileges(REVOKE, ("create database",), "account", None, LOADER),
        "REVOKE create database ON ACCOUNT FROM ROLE loader",
    ),
    (
        Privileges(
            GRANT,
            ("usage", "monitor", "create schema"),
            "database",
            ObjectName.single("raw"),
            LOADER,
        ),
        "GRANT usage, monitor, create schema ON database raw TO ROLE loader",
    ),
    (
        Privileges(
            REVOKE,
            ("imported privileges",),
            "database",
            ObjectName.single("snowflake"),
            LOADER,
        ),
        'REVOKE imported privileges ON database "SNOWFLAKE" FROM ROLE loader',
    ),
    (
        Privileges(GRANT, ("usage",), "schema", name('raw."Mixed-Case"'), LOADER),
        'GRANT usage ON schema raw."Mixed-Case" TO ROLE loader',
    ),
    (
        Privileges(
            REVOKE,
            ("select",),
            "iceberg table",
            name('raw.public."Events.2024"'),
            LOADER,
        ),
        'REVOKE select ON iceberg table raw.public."Events.2024" FROM ROLE loader',
    ),
    (
        BulkPrivileges(
            GRANT, ALL, ("select",), "table", name('raw."AGGREGATE"'), LOADER
        ),
        "GRANT select ON ALL tables IN schema raw.aggregate TO ROLE loader",
    ),
    (
        BulkPrivileges(
            GRANT,
            FUTURE,
            ("select", "monitor", "operate"),
            "dynamic table",
            ObjectName.single("raw"),
            LOADER,
        ),
        "GRANT select, monitor, operate ON FUTURE dynamic tables IN database raw"
        " TO ROLE loader",
    ),
    (
        BulkPrivileges(
            GRANT, FUTURE, ("usage",), "schema", ObjectName.single("raw"), LOADER
        ),
        "GRANT usage ON FUTURE schemas IN database raw TO ROLE loader",
    ),
    (
        BulkPrivileges(
            REVOKE, FUTURE, ("select",), "semantic view", name("raw.public"), LOADER
        ),
        "REVOKE select ON FUTURE semantic views IN schema raw.public FROM ROLE loader",
    ),
    (
        RoleMembership(GRANT, ObjectName.single("analyst"), "role", LOADER, "role"),
        "GRANT ROLE analyst TO role loader",
    ),
    (
        RoleMembership(
            REVOKE,
            ObjectName.single("analyst"),
            "role",
            ObjectName.single("jane.doe@example.com"),
            "user",
        ),
        'REVOKE ROLE analyst FROM user "jane.doe@example.com"',
    ),
    (
        RoleMembership(
            GRANT,
            ObjectName.role_reference("snowflake.usage_viewer"),
            "database role",
            LOADER,
            "role",
        ),
        'GRANT DATABASE ROLE "SNOWFLAKE".usage_viewer TO ROLE loader',
    ),
    (
        RoleMembership(
            REVOKE,
            ObjectName.role_reference("my-db.db-role-1"),
            "database role",
            LOADER,
            "role",
        ),
        'REVOKE DATABASE ROLE "my-db"."db-role-1" FROM ROLE loader',
    ),
    (
        UserTag(
            "set",
            ObjectName.single("jane"),
            name("governance.tags.team"),
            "O'BRIEN",
        ),
        "ALTER USER jane SET TAG governance.tags.team = 'O''BRIEN'",
    ),
    (
        UserTag("unset", ObjectName.single("gitlab-ci"), name("governance.tags.team")),
        'ALTER USER "gitlab-ci" UNSET TAG governance.tags.team',
    ),
    (
        SqlStatement("GRANT OWNERSHIP ON schema raw.public TO ROLE loader"),
        "GRANT OWNERSHIP ON schema raw.public TO ROLE loader",
    ),
]


@pytest.mark.parametrize(
    "operation,sql", RENDERINGS, ids=[sql for _, sql in RENDERINGS]
)
def test_each_operation_renders_the_sql_tundra_always_planned(operation, sql):
    assert operation.sql == sql
    assert PlannedCommand(False, operation)["sql"] == sql


def test_tundra_never_revokes_on_all():
    with pytest.raises(ValueError):
        BulkPrivileges(REVOKE, ALL, ("select",), "table", name("raw.public"), LOADER)


class TestPlannedCommand:
    operation = Privileges(GRANT, ("usage",), "database", name("raw"), LOADER)

    def test_is_the_dict_tundra_always_planned(self):
        command = PlannedCommand(True, self.operation)

        assert command == {
            "already_granted": True,
            "sql": "GRANT usage ON database raw TO ROLE loader",
        }
        assert [command] == [dict(command)]
        assert json.loads(json.dumps(command)) == dict(command)
        assert command.operation is self.operation

    def test_apply_adds_its_keys(self):
        command = PlannedCommand(False, self.operation)
        command["run_status"] = True

        assert command["run_status"] is True
        assert command.operation is self.operation

    def test_copies_and_pickles_keep_the_operation(self):
        command = PlannedCommand(False, self.operation)
        pickles = [
            pickle.loads(pickle.dumps(command, protocol))
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1)
        ]

        for other in [copy.copy(command), copy.deepcopy(command), *pickles]:
            assert type(other) is PlannedCommand
            assert other == command
            assert other.operation == self.operation

    @pytest.mark.parametrize("dump", [yaml.safe_dump, yaml.dump])
    def test_dumps_to_yaml_as_the_dict_it_is(self, dump):
        commands = [PlannedCommand(False, self.operation)]

        assert dump(commands) == dump([dict(c) for c in commands])
        assert yaml.safe_load(dump(commands)) == commands

    def test_prints_its_sql(self, capsys):
        print_command(PlannedCommand(False, self.operation), diff=True, dry=True)

        assert capsys.readouterr().out == (
            "+ [PENDING] GRANT usage ON database raw TO ROLE loader;\n"
        )

    def test_ownership_dedupes_by_its_sql(self):
        def ownership(role: str) -> PlannedCommand:
            return PlannedCommand(
                False,
                SqlStatement(
                    f"GRANT OWNERSHIP ON schema raw.public TO ROLE {role}"
                    " COPY CURRENT GRANTS"
                ),
            )

        commands = [ownership("loader"), ownership("transformer")]

        assert SnowflakeSpecLoader.remove_duplicate_queries(commands) == [
            ownership("transformer")
        ]


# Through the API now, each on the first dev write probe's evidence
API_ROWS = {
    ("privileges", "database", "imported privileges"),
    ("privileges", "database"),
    ("future", "table", "in schema"),
    ("all", "table", "in schema"),
}

SECURABLE_TYPES = ["database", "schema", "warehouse", "integration"]
SECURABLE_TYPES += ["external volume", "account"]
SECURABLE_TYPES += [t.name for t in ALL_OBJECT_TYPES]


def expected_rows():
    rows = {("privileges", kind) for kind in SECURABLE_TYPES}
    rows.add(("privileges", "database", "imported privileges"))
    for on in ("future", "all"):
        for kind in (t.name for t in ALL_OBJECT_TYPES):
            rows |= {(on, kind, "in schema"), (on, kind, "in database")}
    rows.add(("future", "schema", "in database"))
    rows |= {
        ("membership", "role", "to role"),
        ("membership", "role", "to user"),
        ("membership", "database role", "to role"),
        ("tag", "user"),
    }
    return rows | set(GATES)


def test_every_kind_tundra_plans_has_one_row():
    assert set(ROUTES) == expected_rows()


def test_only_the_probed_rows_go_through_the_api():
    assert {key for key, route in ROUTES.items() if route.api} == API_ROWS
    for key in API_ROWS:
        assert ROUTES[key].evidence.startswith("The first dev write probe ")
    for key in set(ROUTES) - API_ROWS:
        assert "second dev write probe" in ROUTES[key].evidence


def operation_for(key):
    """An operation planned on this row, with one privilege and names sent bare."""
    raw, schema, role = ObjectName.single("raw"), name("raw.public"), LOADER
    if key[0] == "privileges":
        target = {
            "database": raw,
            "schema": schema,
            "warehouse": ObjectName.single("wh"),
            "integration": ObjectName.single("s3_int"),
            "external volume": ObjectName.single("volume"),
            "account": None,
        }.get(key[1], name("raw.public.thing"))
        privilege = "imported privileges" if len(key) == 3 else "usage"
        return Privileges(GRANT, (privilege,), key[1], target, role)
    if key[0] in ("future", "all"):
        container = schema if key[2] == "in schema" else raw
        return BulkPrivileges(GRANT, key[0], ("select",), key[1], container, role)
    if key[0] == "membership":
        member = (
            ObjectName.role_reference("snowflake.usage_viewer")
            if key[1] == "database role"
            else ObjectName.single("analyst")
        )
        grantee = ObjectName.single("jane") if key[2] == "to user" else role
        return RoleMembership(GRANT, member, key[1], grantee, key[2][3:])
    return UserTag("set", ObjectName.single("jane"), name("gov.tags.team"), "DATA")


ROWS = sorted(expected_rows() - set(GATES))


@pytest.mark.parametrize("key", ROWS, ids=" ".join)
def test_each_row_routes_its_own_kind(key):
    operation = operation_for(key)

    assert operation.route_key == key
    assert list(gates(operation)) == []
    assert api_capable(operation)
    assert routes_to_api(operation) is (key in API_ROWS)


@pytest.mark.parametrize("key", ROWS, ids=" ".join)
def test_each_row_flips_on_its_own(monkeypatch, key):
    flipped = Route(api=key not in API_ROWS, evidence="flipped for the test")
    monkeypatch.setitem(ROUTES, key, flipped)

    assert routes_to_api(operation_for(key)) is flipped.api
    others = [operation_for(other) for other in ROWS if other != key]
    assert {o.route_key for o in others if routes_to_api(o)} == API_ROWS - {key}


def test_imported_privileges_has_its_own_row_however_its_spelt():
    snowflake = ObjectName.single("snowflake")
    for privileges in [("imported privileges",), ("IMPORTED PRIVILEGES",)]:
        operation = Privileges(REVOKE, privileges, "database", snowflake, LOADER)
        assert operation.route_key == ("privileges", "database", "imported privileges")
    # Listed with another privilege, it's the database row's
    both = Privileges(
        GRANT, ("usage", "imported privileges"), "database", snowflake, LOADER
    )
    assert both.route_key == ("privileges", "database")


def flip(monkeypatch, *keys):
    for key in keys:
        monkeypatch.setitem(ROUTES, key, Route(api=True, evidence="for the test"))


TEAM_ROLE = ObjectName.single("org-wide-viewer")
TABLE_WRITE = tuple(TABLE_OBJECT_TYPES[0].write_privileges.split(", "))

GATED = [
    pytest.param(
        SEVERAL_PRIVILEGES,
        Privileges(
            GRANT,
            ("usage", "monitor", "create schema"),
            "database",
            name("raw"),
            LOADER,
        ),
        id="database write",
    ),
    pytest.param(
        SEVERAL_PRIVILEGES,
        Privileges(
            REVOKE, ("monitor", "create schema"), "database", name("raw"), LOADER
        ),
        id="database partial write",
    ),
    pytest.param(
        SEVERAL_BULK_PRIVILEGES,
        BulkPrivileges(GRANT, FUTURE, TABLE_WRITE, "table", name("raw.public"), LOADER),
        id="future table write",
    ),
    pytest.param(
        SEVERAL_BULK_PRIVILEGES,
        BulkPrivileges(GRANT, ALL, TABLE_WRITE, "table", name("raw.public"), LOADER),
        id="all table write",
    ),
    pytest.param(
        SEVERAL_BULK_PRIVILEGES,
        BulkPrivileges(
            REVOKE, FUTURE, TABLE_WRITE[1:], "table", name("raw.public"), LOADER
        ),
        id="future table partial write revoked",
    ),
    pytest.param(
        QUOTED_PATH,
        Privileges(GRANT, ("usage",), "database", name("raw"), TEAM_ROLE),
        id="quoted role on a database",
    ),
    pytest.param(
        QUOTED_PATH,
        BulkPrivileges(
            GRANT, FUTURE, ("select",), "table", name("raw.public"), TEAM_ROLE
        ),
        id="quoted role on future tables",
    ),
    pytest.param(
        QUOTED_BODY,
        Privileges(GRANT, ("usage",), "database", name('"MyDb"'), LOADER),
        id="mixed-case database",
    ),
    pytest.param(
        QUOTED_BODY,
        BulkPrivileges(GRANT, FUTURE, ("select",), "table", name("raw.group"), LOADER),
        id="reserved-word schema",
    ),
    pytest.param(
        QUOTED_BODY,
        BulkPrivileges(
            GRANT, ALL, ("select",), "table", name('raw."my-schema"'), LOADER
        ),
        id="dashed schema",
    ),
]


@pytest.mark.parametrize("gate,operation", GATED)
def test_a_gated_statement_goes_through_sql_until_its_gate_flips(
    monkeypatch, gate, operation
):
    assert ROUTES[operation.route_key].api
    assert list(gates(operation)) == [gate]
    assert not routes_to_api(operation)

    flip(monkeypatch, gate)

    assert routes_to_api(operation)


THRICE_GATED = BulkPrivileges(
    GRANT, FUTURE, TABLE_WRITE, "table", name("raw.group"), TEAM_ROLE
)


@pytest.mark.parametrize(
    "left_out",
    [SEVERAL_BULK_PRIVILEGES, QUOTED_PATH, QUOTED_BODY, None],
    ids=["several", "path", "body", "none"],
)
def test_each_gate_a_statement_meets_has_to_flip(monkeypatch, left_out):
    met = list(gates(THRICE_GATED))
    assert met == [SEVERAL_BULK_PRIVILEGES, QUOTED_PATH, QUOTED_BODY]

    flip(monkeypatch, *(gate for gate in met if gate != left_out))

    assert routes_to_api(THRICE_GATED) is (left_out is None)


def test_a_flipped_gate_doesnt_send_a_row_still_on_sql(monkeypatch):
    flip(monkeypatch, *GATES)

    operation = Privileges(
        GRANT, ("usage", "monitor"), "schema", name("raw.group"), TEAM_ROLE
    )

    assert not ROUTES[operation.route_key].api
    assert not routes_to_api(operation)


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT OWNERSHIP ON schema raw.public TO ROLE loader COPY CURRENT GRANTS",
        "ALTER USER jane SET DISABLED = FALSE, TYPE = 'PERSON'",
        'GRANT APPLICATION ROLE "SNOWFLAKE".cortex_user TO ROLE loader',
        'REVOKE APPLICATION ROLE "SNOWFLAKE".cortex_user FROM ROLE loader',
    ],
    ids=["ownership", "ALTER USER", "grant application role", "revoke"],
)
def test_statements_only_sql_can_express_have_no_row_to_flip(monkeypatch, sql):
    operation = SqlStatement(sql)
    for key in ROUTES:
        monkeypatch.setitem(ROUTES, key, Route(api=True, evidence="everything"))

    assert operation.route_key is None
    assert not api_capable(operation)
    assert not routes_to_api(operation)
