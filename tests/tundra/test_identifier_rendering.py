# How names come out in generated SQL, whether they arrive from the spec, from SHOW
# GRANTS (identifier spellings) or from SHOW SCHEMAS (canonical names). Every case goes
# through the generator, the connector or the grant checker rather than the name
# parsing itself, so the tables pin what reaches Snowflake.

import re

import pytest

from tundra.grant_coverage import GrantCoverage
from tundra.snowflake_grants import SnowflakeGrantsGenerator
from tundra.snowflake_permission import SnowflakePermission
from tundra.snowflake_role_grant_checker import SnowflakeRoleGrantChecker
from tundra.snowflake_connector import SnowflakeConnector
from tundra_test_utils.fake_account import FakeAccount, Grant


def schema_in(sql: str) -> str:
    match = re.fullmatch(
        r"(?:GRANT|REVOKE) usage ON schema (.+) (?:TO|FROM) ROLE loader", sql
    )
    assert match, sql
    return match.group(1)


SPEC_SCHEMAS = [
    ("raw.public", "raw.public"),
    ("RAW.PUBLIC", "raw.public"),
    ('raw."Mixed-Case"', 'raw."Mixed-Case"'),
    ('raw."CaseSensitive"', 'raw."CaseSensitive"'),
    ('raw."lower_case"', 'raw."lower_case"'),
    ('raw."GROUP"', 'raw."GROUP"'),
    ("raw.my$schema", "raw.my$schema"),
    ("raw.my-schema", 'raw."my-schema"'),
    ("raw.1_leading", 'raw."1_leading"'),
    ('raw."has ""quotes"""', 'raw."has ""quotes"""'),
    ("my-db.public", '"my-db".public'),
    ('raw."PUBLIC"', "raw.public"),
    ("raw.group", 'raw."GROUP"'),
    ("raw.window", 'raw."WINDOW"'),
    ("raw.MixedCase", "raw.mixedcase"),
    ('raw."dot.inside"', 'raw."dot.inside"'),
]


@pytest.mark.parametrize("spelling,rendered", SPEC_SCHEMAS)
def test_spec_schema_renders(spelling, rendered, mocker):
    FakeAccount().install(mocker)
    commands = SnowflakeGrantsGenerator({}, {}).generate_schema_grants(
        "loader", {"read": [spelling], "write": []}, set(), set()
    )

    assert [schema_in(c["sql"]) for c in commands] == [rendered]


# Canonical schema names as Snowflake stores them, reported by SHOW GRANTS as spellings
GRANTED_SCHEMAS = [
    ("PUBLIC", "raw.public"),
    ("Mixed-Case", 'raw."Mixed-Case"'),
    ("CamelCase", 'raw."CamelCase"'),
    ("lower_case", 'raw."lower_case"'),
    ("GROUP", 'raw."GROUP"'),
    ('has"quote', 'raw."has""quote"'),
    ("AGGREGATE", "raw.aggregate"),
    ("dot.inside", 'raw."dot.inside"'),
]


@pytest.mark.parametrize("stored,rendered", GRANTED_SCHEMAS)
def test_granted_schema_renders_in_revoke(stored, rendered, mocker):
    quoted = '"' + stored.replace('"', '""') + '"'
    account = FakeAccount(
        roles={"LOADER": "SECURITYADMIN"},
        grants=[Grant("LOADER", "USAGE", "SCHEMA", f"RAW.{quoted}")],
    )
    account.install(mocker)
    grants = SnowflakeConnector().show_grants_to_role("loader")
    generator = SnowflakeGrantsGenerator({"loader": grants}, {})

    commands = generator.generate_schema_grants(
        "loader", {"read": [], "write": []}, set(), {"raw"}
    )

    assert [schema_in(c["sql"]) for c in commands] == [rendered]


# Canonical schema names as SHOW SCHEMAS reports them, expanded from a `raw.*` entry
LISTED_SCHEMAS = [
    ("PUBLIC", "raw.public"),
    ("AGGREGATE", "raw.aggregate"),
    ("Mixed-Case", 'raw."Mixed-Case"'),
    ("CamelCase", 'raw."CamelCase"'),
    ("GROUP", 'raw."GROUP"'),
    ("lower_case", 'raw."lower_case"'),
    ('has"quote', 'raw."has""quote"'),
    ("dot.inside", 'raw."dot.inside"'),
]


@pytest.mark.parametrize("stored,rendered", LISTED_SCHEMAS)
def test_listed_schema_renders(stored, rendered, mocker):
    quoted = '"' + stored.replace('"', '""') + '"'
    FakeAccount(schemas=["RAW.INFORMATION_SCHEMA", f"RAW.{quoted}"]).install(mocker)

    commands = SnowflakeGrantsGenerator({}, {}).generate_schema_grants(
        "loader", {"read": ["raw.*"], "write": []}, set(), set()
    )
    schemas = [schema_in(c["sql"]) for c in commands if " FUTURE " not in c["sql"]]

    assert schemas == [rendered]


COVERAGE = [
    ("database_1.schema_1", "DATABASE_1.SCHEMA_1.TABLE_1", True),
    ('database_1."Mixed-Case"', 'DATABASE_1."Mixed-Case".TABLE_1', True),
    ('database_1."aggregate"', 'DATABASE_1."AGGREGATE".TABLE_1', False),
    ('database_1."aggregate"', "database_1.aggregate.table_1", False),
    ("lab_results.aggregate", 'LAB_RESULTS."AGGREGATE".<TABLE>', True),
    ('LAB_RESULTS."AGGREGATE"', "lab_results.aggregate.table_1", True),
    ("database_1.group", 'DATABASE_1."GROUP".TABLE_1', True),
]


@pytest.mark.parametrize("schema,granted,covered", COVERAGE)
def test_grant_coverage_matches_however_names_are_quoted(schema, granted, covered):
    coverage = GrantCoverage()
    coverage.add_schema(schema)

    assert coverage.covers(granted) is covered


CHECKER = [
    ('RAW."Mixed-Case"', 'raw."Mixed-Case"', True),
    ("RAW.PUBLIC", "raw.public", True),
    ("RAW.PUBLIC", "RAW.PUBLIC", True),
    ('RAW."aggregate"', "raw.aggregate", False),
    ("RAW.AGGREGATE", "raw.aggregate", True),
    ("RAW.GROUP", "raw.group", True),
]


@pytest.mark.parametrize("held,asked,expected", CHECKER)
def test_role_grant_checker_matches_however_names_are_quoted(
    held, asked, expected, mocker
):
    FakeAccount(
        roles={"LOADER": "SECURITYADMIN"},
        grants=[Grant("LOADER", "USAGE", "SCHEMA", held, grant_option=True)],
    ).install(mocker)
    checker = SnowflakeRoleGrantChecker(SnowflakeConnector())

    assert (
        checker.has_permission(
            "loader", SnowflakePermission(asked, "schema", ["usage"], False)
        )
        is expected
    )
