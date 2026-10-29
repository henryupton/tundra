# Unit tests for tundra.identifiers. Rows marked CHANGED render or resolve differently
# from the snowflaky helpers on purpose: they're the 2.0 breaking changes.

import copy
import pickle
import time

import pytest
from snowflake.core import CanonicalName, Identifier, QualifiedIdentifier
from snowflake.core.database_role import ContainingScope as DatabaseRoleScope
from snowflake.core.database_role import Securable as DatabaseRoleSecurable
from snowflake.core.grant import Grantees
from snowflake.core.role import ContainingScope, Securable
from snowflake.core.user import Securable as UserSecurable

import test_golden_scenarios as golden
import test_identifier_rendering as rendering
from tundra.identifiers import (
    INFORMATION_SCHEMA,
    MAX_PART_LENGTH,
    RESERVED_KEYWORDS,
    InvalidNameError,
    NamePattern,
    ObjectName,
    granted_name,
    member_or_pattern,
    name_or_pattern,
)
from tundra.table_object_types import ALL_OBJECT_TYPES
from tundra_test_utils.fake_account import canonical, spell


def rows(table):
    """The values of a parametrize table, with any xfail marks dropped: the xfail rows
    hold the correct behaviour, which this module has to produce."""
    return [getattr(row, "values", row) for row in table]


def listed(*spellings):
    """Names as SHOW <objects> reports them, declared the way FakeAccount takes them."""
    return [ObjectName.from_canonical(*canonical(s)) for s in spellings]


_STRING_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "0": "\0"}


def snowflake_string_value(literal):
    """The text Snowflake reads from a single-quoted string constant ('' and \\x)."""
    assert literal[0] == literal[-1] == "'"
    body, text, index = literal[1:-1], [], 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            escaped = body[index + 1]
            text.append(_STRING_ESCAPES.get(escaped, escaped))
            index += 2
        elif char == "'":
            assert body[index + 1 : index + 2] == "'", f"{literal} ends early"
            text.append("'")
            index += 2
        else:
            text.append(char)
            index += 1
    return "".join(text)


class TestParsing:
    @pytest.mark.parametrize(
        "spelling,parts",
        [
            ("raw", ("RAW",)),
            ("raw.public", ("RAW", "PUBLIC")),
            ("raw.public.events", ("RAW", "PUBLIC", "EVENTS")),
            ("RAW.PUBLIC", ("RAW", "PUBLIC")),
            ("Raw.MixedCase", ("RAW", "MIXEDCASE")),
            ("raw.my$schema", ("RAW", "MY$SCHEMA")),
            ("_raw.s_1", ("_RAW", "S_1")),
            ('raw."PUBLIC"', ("RAW", "PUBLIC")),
            ('raw."Mixed-Case"', ("RAW", "Mixed-Case")),
            ('raw."lower_case"', ("RAW", "lower_case")),
            ('raw."has ""quotes"""', ("RAW", 'has "quotes"')),
            ('raw.""""', ("RAW", '"')),
            ('raw."dot.inside"', ("RAW", "dot.inside")),
            ('raw."a.b.c".t', ("RAW", "a.b.c", "T")),
            ('"my.db"."my.schema"."my.table"', ("my.db", "my.schema", "my.table")),
            ('raw."<TABLE>"', ("RAW", "<TABLE>")),
            ('raw."*"', ("RAW", "*")),
            ('raw."TABLE*WITH*STARS"', ("RAW", "TABLE*WITH*STARS")),
        ],
    )
    def test_spellings_resolve_to_canonical_parts(self, spelling, parts):
        name = ObjectName(spelling)

        assert name.parts == parts
        assert name.placeholder is None

    @pytest.mark.parametrize(
        "spelling,parts",
        [
            ("gitlab-ci", ("gitlab-ci",)),
            ("1234raw.schema", ("1234raw", "SCHEMA")),
            ("my-db.public", ("my-db", "PUBLIC")),
            ("raw.1_leading", ("RAW", "1_leading")),
            ("DEPARTMENT - DATA", ("DEPARTMENT - DATA",)),
            ("db.TABLE%WITH^CHARS", ("DB", "TABLE%WITH^CHARS")),
            ("1_db-9-RANDOM.s", ("1_db-9-RANDOM", "S")),
            ("raw.<a-b>", ("RAW", "<a-b>")),
        ],
    )
    def test_invalid_identifiers_are_exact_canonical_names(self, spelling, parts):
        assert ObjectName(spelling).parts == parts

    @pytest.mark.parametrize(
        "spelling,parts,placeholder",
        [
            ("db.<TABLE>", ("DB",), "<table>"),
            ("db.<table>", ("DB",), "<table>"),
            ("db.<Table>", ("DB",), "<table>"),
            ("db.<SCHEMA>", ("DB",), "<schema>"),
            ("db.schema.<VIEW>", ("DB", "SCHEMA"), "<view>"),
            ("db.schema.<ICEBERG_TABLE>", ("DB", "SCHEMA"), "<iceberg_table>"),
            ("db.schema.<iceberg_table>", ("DB", "SCHEMA"), "<iceberg_table>"),
            (
                'LAB_RESULTS."AGGREGATE".<TABLE>',
                ("LAB_RESULTS", "AGGREGATE"),
                "<table>",
            ),
            ('db."Mixed".<VIEW>', ("DB", "Mixed"), "<view>"),
        ],
    )
    def test_placeholders_are_kept_outside_the_parts(
        self, spelling, parts, placeholder
    ):
        name = ObjectName(spelling)

        assert (name.parts, name.placeholder) == (parts, placeholder)
        assert name.is_future

    @pytest.mark.parametrize(
        "spelling,rendered",
        [
            # Only the kinds FUTURE_PLACEHOLDER_PATTERN has are placeholders. Anything
            # else in angle brackets is a name, as snowflaky quoted it
            ("raw.public.<tables>", 'raw.public."<tables>"'),
            ("DB.<PROCEDURE>", 'db."<PROCEDURE>"'),
            ("DB.SCHEMA.<FUNCTION>", 'db."SCHEMA"."<FUNCTION>"'),
            # SHOW FUTURE GRANTS spells multi-word kinds with an underscore
            ("DB.SCHEMA.<ICEBERG TABLE>", 'db."SCHEMA"."<ICEBERG TABLE>"'),
            ("raw.<admin>", 'raw."<admin>"'),
            ("db.<_x>", 'db."<_x>"'),
        ],
    )
    def test_other_angle_bracket_parts_are_names(self, spelling, rendered):
        name = ObjectName(spelling)

        assert not name.is_future
        assert name.future_kind is None
        assert str(name) == rendered

    @pytest.mark.parametrize("object_type", ALL_OBJECT_TYPES, ids=lambda t: t.name)
    def test_show_future_grants_placeholders_match_the_registry(self, object_type):
        reported = f"DB_1.SCHEMA_1.<{object_type.grant_key.upper()}>"
        name = ObjectName(reported)

        assert name == f"db_1.schema_1.{object_type.future_placeholder}"
        assert name.future_kind == object_type.grant_key

    @pytest.mark.parametrize(
        "spelling",
        [
            "",
            "raw.",
            ".raw",
            "a..b",
            '"unterminated',
            'raw."a"b',
            'raw.ab"c',
            '""',
            'raw.""',
            "raw.*",
            "raw.loading_*",
            "*",
            "<TABLE>",
            "db.<schema>.x",
            "db.<table>.<table>",
            "db.schema.<schema>",
            "a.b.c.d",
            "DATABASE_1.SCHEMA_1.TABLE_1.AMBIGUOUS_IDENTIFIER",
            "a.b.c.<table>",
            "x" * (MAX_PART_LENGTH + 1),
            '"' + "x" * (MAX_PART_LENGTH + 1) + '"',
        ],
    )
    def test_malformed_spellings_raise(self, spelling):
        with pytest.raises(InvalidNameError):
            ObjectName(spelling)

    def test_invalid_name_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            ObjectName("a..b")

    def test_unquoted_wildcard_says_it_is_a_pattern(self):
        with pytest.raises(InvalidNameError, match="wildcard pattern, not a name"):
            ObjectName("raw.loading_*")

    def test_stray_quote_says_to_quote_the_whole_part(self):
        with pytest.raises(InvalidNameError, match="quote the whole part"):
            ObjectName('raw."loading"_x')

    @pytest.mark.parametrize("spelling", ['""', 'raw.""', '"".public'])
    def test_an_empty_quoted_part_says_it_is_empty(self, spelling):
        with pytest.raises(InvalidNameError, match="empty part"):
            ObjectName(spelling)

    def test_parts_up_to_the_length_limit_are_accepted(self):
        longest = "X" * MAX_PART_LENGTH

        assert ObjectName(f"raw.{longest}").parts == ("RAW", longest)
        assert ObjectName(f'raw."{longest.lower()}"').parts[1] == longest.lower()

    @pytest.mark.parametrize("value", [None, 3, b"raw", ["raw"], ("RAW",)])
    def test_non_strings_raise_type_error(self, value):
        with pytest.raises(TypeError):
            ObjectName(value)

    def test_parsing_is_cached_and_interned(self):
        first = ObjectName("RAW.PUBLIC")

        assert ObjectName("RAW.PUBLIC") is first
        assert ObjectName('raw."PUBLIC"') is first
        assert ObjectName.from_canonical("RAW", "PUBLIC") is first

    def test_an_object_name_passes_through_every_constructor(self):
        name = ObjectName("raw.public")
        user = ObjectName.single("heidi.bond@example.com")

        assert ObjectName(name) is name
        assert ObjectName.role_reference(name) is name
        assert name_or_pattern(name) is name
        assert member_or_pattern(name) is name
        assert ObjectName.single(user) is user
        assert ObjectName.from_canonical(name) is name
        assert ObjectName.from_canonical(user) is user

    def test_from_canonical_takes_a_name_only_on_its_own(self):
        # Use child() to add a part to a name
        with pytest.raises(TypeError):
            ObjectName.from_canonical(ObjectName("raw"), "PUBLIC")

    @pytest.mark.parametrize(
        "value,rendered",
        [
            (Identifier('"AGGREGATE"'), "aggregate"),
            (Identifier("MyTable"), "mytable"),
            (Identifier('"a.b"'), '"a.b"'),
            (CanonicalName("Mixed-Case"), '"Mixed-Case"'),
            (CanonicalName("a.b"), '"a.b"'),
            (QualifiedIdentifier.from_string('db."Sch"'), 'db."Sch"'),
            (QualifiedIdentifier.from_string("a.b.c"), "a.b.c"),
        ],
    )
    def test_snowflake_core_values_convert(self, value, rendered):
        assert ObjectName(value) == rendered

    def test_two_part_qualified_identifier_is_database_and_schema(self):
        # QualifiedIdentifier labels db.schema as schema=db, name=schema
        name = ObjectName(QualifiedIdentifier.from_string("raw.public"))

        assert name.parts == ("RAW", "PUBLIC")
        assert name.database == "raw"

    @pytest.mark.parametrize(
        "value",
        [
            QualifiedIdentifier.from_string("raw..events"),
            QualifiedIdentifier("events", database="raw"),
        ],
        ids=str,
    )
    def test_a_qualified_identifier_without_its_schema_raises(self, value):
        # database..name is an object in PUBLIC, never the schema database.name
        assert str(value) == "raw..events"
        with pytest.raises(InvalidNameError, match="no schema"):
            ObjectName(value)
        with pytest.raises(InvalidNameError):
            ObjectName(value, arity=2)
        with pytest.raises(InvalidNameError):
            ObjectName(str(value))


class TestSingle:
    @pytest.mark.parametrize(
        "spelling,rendered",
        [
            ("loader", "loader"),
            # CHANGED: snowflaky_user_role kept LOADER and MyRole as written
            ("LOADER", "loader"),
            ("MyRole", "myrole"),
            ("public", "public"),
            ("gitlab-ci", '"gitlab-ci"'),
            ("blake.enyart@gmail.com", '"blake.enyart@gmail.com"'),
            ("heidi.bond@example.com", '"heidi.bond@example.com"'),
            ('"blake.enyart@gmail.com"', '"blake.enyart@gmail.com"'),
            ('"ROLE WITH SPACES"', '"ROLE WITH SPACES"'),
            ("DEPARTMENT - DATA", '"DEPARTMENT - DATA"'),
            ("o'neill", '"o\'neill"'),
            # CHANGED: was a bare 1role, which doesn't parse
            ("1role", '"1role"'),
            # CHANGED: was a bare group, which doesn't parse
            ("group", '"GROUP"'),
            # CHANGED: was a bare all, which doesn't parse. This is a role named ALL:
            # DEFAULT_SECONDARY_ROLES = ('all') takes the keyword ALL as text, so it
            # mustn't go through ObjectName
            ("all", '"ALL"'),
            ("snowflake", '"SNOWFLAKE"'),
            ("etl-wh", '"etl-wh"'),
            # CHANGED: snowflaky split warehouse names, so this was etl.wh
            ("etl.wh", '"etl.wh"'),
            # CHANGED: snowflaky_user_role quoted a $ name as written, so these were the
            # case-sensitive roles "etl$loader" and "My$Role", not ETL$LOADER and MY$ROLE
            ("etl$loader", "etl$loader"),
            ("My$Role", "my$role"),
            # Never a pattern, so a * is part of the name, as snowflaky_user_role had it
            ("svc*bot", '"svc*bot"'),
            ("loader_*", '"loader_*"'),
            ("*", '"*"'),
            ("ops*team@example.com", '"ops*team@example.com"'),
        ],
    )
    def test_account_object_names_never_split(self, spelling, rendered):
        name = ObjectName.single(spelling)

        assert len(name.parts) == 1
        assert str(name) == rendered

    @pytest.mark.parametrize("spelling", ["etl$loader", "My$Role"])
    def test_a_dollar_name_resolves_like_any_unquoted_identifier(self, spelling):
        name = ObjectName.single(spelling)

        assert name.parts == (spelling.upper(),)
        assert name != ObjectName.single(f'"{spelling}"')
        assert name.quoted == f'"{spelling.upper()}"'

    def test_a_star_in_a_single_name_is_the_exact_canonical_name(self):
        assert ObjectName.single("svc*bot") == ObjectName.from_canonical("svc*bot")
        assert ObjectName.single("svc*bot").api_name == '"svc*bot"'
        assert granted_name("ROLE", "svc*bot").parts == ("svc*bot",)
        assert granted_name("USER", "ops*team@example.com").parts == (
            "ops*team@example.com",
        )
        # The dotted and member_of parsers still read an unquoted * as a wildcard
        assert isinstance(member_or_pattern("svc*bot"), NamePattern)
        with pytest.raises(InvalidNameError, match="wildcard pattern"):
            ObjectName("svc*bot")

    def test_a_user_with_an_email_name_is_one_part(self):
        user = ObjectName.single("heidi.bond@example.com")

        assert user.parts == ("heidi.bond@example.com",)
        assert user == ObjectName.from_canonical("heidi.bond@example.com")
        assert ObjectName("heidi.bond@example.com").parts == (
            "HEIDI",
            "bond@example",
            "COM",
        )

    @pytest.mark.parametrize("spelling", ['"a"."b"', 'ab"c', '"abc', "", '""'])
    def test_malformed_single_names_raise(self, spelling):
        with pytest.raises(InvalidNameError):
            ObjectName.single(spelling)

    @pytest.mark.parametrize("spelling", ["", '""'])
    def test_an_empty_name_says_so(self, spelling):
        with pytest.raises(InvalidNameError, match="empty part"):
            ObjectName.single(spelling)

    @pytest.mark.parametrize(
        "spelling", ["<admin>", "<ADMIN>", "<ops team>", "<TABLE>"]
    )
    def test_angle_brackets_are_part_of_the_name(self, spelling):
        # A role, user or warehouse is never a future-grant scope, so this is the
        # role SHOW ROLES reports, as snowflaky_user_role had it
        name = ObjectName.single(spelling)

        assert name == ObjectName.from_canonical(spelling)
        assert str(name) == f'"{spelling}"'
        assert ObjectName.role_reference(spelling) is name
        assert granted_name("ROLE", spelling) is name

    @pytest.mark.parametrize("dotted", ["raw.public", "raw.<table>"])
    def test_a_multi_part_name_is_rejected(self, dotted):
        with pytest.raises(InvalidNameError, match="single-part"):
            ObjectName.single(ObjectName(dotted))

    def test_none_raises_type_error(self):
        with pytest.raises(TypeError):
            ObjectName.single(None)


class TestRoleReference:
    @pytest.mark.parametrize(
        "spelling,parts,rendered",
        [
            ("monitor", ("MONITOR",), "monitor"),
            ("analytics.reader", ("ANALYTICS", "READER"), "analytics.reader"),
            ("mydb.db_role_1", ("MYDB", "DB_ROLE_1"), "mydb.db_role_1"),
            ("MYDB.DB_ROLE_1", ("MYDB", "DB_ROLE_1"), "mydb.db_role_1"),
            (
                "snowflake.organization_billing_viewer",
                ("SNOWFLAKE", "ORGANIZATION_BILLING_VIEWER"),
                '"SNOWFLAKE".organization_billing_viewer',
            ),
            (
                "snowflake.cortex_analyst_requests_viewer",
                ("SNOWFLAKE", "CORTEX_ANALYST_REQUESTS_VIEWER"),
                '"SNOWFLAKE".cortex_analyst_requests_viewer',
            ),
            (
                "SNOWFLAKE.CORTEX_ANALYST_REQUESTS_VIEWER",
                ("SNOWFLAKE", "CORTEX_ANALYST_REQUESTS_VIEWER"),
                '"SNOWFLAKE".cortex_analyst_requests_viewer',
            ),
            (
                'SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
                ("SNOWFLAKE", "CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"),
                '"SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
            ),
            # Only the first dot separates application from role
            (
                "SNOWFLAKE.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH",
                ("SNOWFLAKE", "CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"),
                '"SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
            ),
            # CHANGED: a part that isn't an identifier keeps its case, where
            # snowflaky_application_role uppercased it
            (
                "snowflake.cortex-model-role-gemini-2.5-flash",
                ("SNOWFLAKE", "cortex-model-role-gemini-2.5-flash"),
                '"SNOWFLAKE"."cortex-model-role-gemini-2.5-flash"',
            ),
            ("myapp.my_role", ("MYAPP", "MY_ROLE"), "myapp.my_role"),
            ("snowflake", ("SNOWFLAKE",), '"SNOWFLAKE"'),
            # CHANGED: snowflaky_database_role gave "MY-DB"."DB-ROLE-1", which no
            # longer matches a held "MY-DB"."DB-ROLE-1"
            ("my-db.db-role-1", ("my-db", "db-role-1"), '"my-db"."db-role-1"'),
            # CHANGED: a quoted dot is part of the name, so this is an account role
            ('"my.role"', ("my.role",), '"my.role"'),
            # CHANGED: a role is never a future-grant scope, so <x> is its exact name,
            # which snowflaky_database_role uppercased to db."<X>"
            ("db.<x>", ("DB", "<x>"), 'db."<x>"'),
            ("db.<table>", ("DB", "<table>"), 'db."<table>"'),
        ],
    )
    def test_role_references(self, spelling, parts, rendered):
        name = ObjectName.role_reference(spelling)

        assert name.parts == parts
        assert str(name) == rendered

    @pytest.mark.parametrize(
        "spelling", ['snowflake."a".b', 'a.b."c"', "db.a.", "db..a", "db.*"]
    )
    def test_malformed_role_references_raise(self, spelling):
        with pytest.raises(InvalidNameError):
            ObjectName.role_reference(spelling)

    def test_a_three_part_name_is_rejected(self):
        with pytest.raises(InvalidNameError):
            ObjectName.role_reference(ObjectName("a.b.c"))


class TestCanonical:
    @pytest.mark.parametrize(
        "parts,rendered",
        [
            (("PUBLIC",), "public"),
            (("TEST_ROLE",), "test_role"),
            (("SECURITYADMIN",), "securityadmin"),
            (("gitlab-ci",), '"gitlab-ci"'),
            (("lowercase role with spaces",), '"lowercase role with spaces"'),
            # CHANGED: SHOW <objects> readers lowercased alphanumeric names
            (("MixedCase",), '"MixedCase"'),
            (("1ABC",), '"1ABC"'),
            (("AGGREGATE",), "aggregate"),
            (("GROUP",), '"GROUP"'),
            (("WINDOW",), '"WINDOW"'),
            (("SNOWFLAKE",), '"SNOWFLAKE"'),
            (("RAW", "dot.inside"), 'raw."dot.inside"'),
            (("RAW", 'has"quote'), 'raw."has""quote"'),
            (("MYDB", "DB_ROLE_1"), "mydb.db_role_1"),
            (("COST_MANAGEMENT", "TAGS", "TEAM"), "cost_management.tags.team"),
            (("SNOWFLAKE", "CORE", "Mixed-Case"), '"SNOWFLAKE".core."Mixed-Case"'),
        ],
    )
    def test_canonical_names_render(self, parts, rendered):
        assert str(ObjectName.from_canonical(*parts)) == rendered

    def test_canonical_name_values_are_accepted(self):
        name = ObjectName.from_canonical(CanonicalName("RAW"), CanonicalName("Mixed"))

        assert name == 'raw."Mixed"'

    def test_a_name_that_starts_and_ends_with_a_quote_is_escaped(self):
        # CanonicalName.to_identifier_string() would give "x", which resolves to x
        name = ObjectName.from_canonical('"x"')

        assert str(name) == '"""x"""'
        assert ObjectName(str(name)) == name
        assert CanonicalName('"x"').to_identifier_string() == '"x"'

    @pytest.mark.parametrize(
        "parts", [(), ("A", "B", "C", "D"), ("",), ("RAW", ""), ("X" * 256,)]
    )
    def test_bad_canonical_parts_raise(self, parts):
        with pytest.raises(InvalidNameError):
            ObjectName.from_canonical(*parts)

    @pytest.mark.parametrize("part", [None, Identifier("x"), 3])
    def test_non_canonical_values_raise_type_error(self, part):
        with pytest.raises(TypeError):
            ObjectName.from_canonical(part)

    def test_an_empty_grantor_has_to_be_guarded(self):
        # Snowflake's own application role grants report granted_by as ''
        with pytest.raises(InvalidNameError):
            ObjectName.from_canonical("")

    @pytest.mark.parametrize(
        "stored",
        ["PUBLIC", "AGGREGATE", "GROUP", "Mixed-Case", "lower_case", 'has"quote'],
    )
    def test_rendering_round_trips(self, stored):
        name = ObjectName.from_canonical("RAW", stored)

        assert ObjectName(str(name)) is name


class TestIdentity:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("lab_results.aggregate", 'LAB_RESULTS."AGGREGATE"'),
            ("MyTable", "MYTABLE"),
            ("raw.public", '"RAW"."PUBLIC"'),
            ("raw.group", 'RAW."GROUP"'),
            ("db.schema.<table>", "DB.SCHEMA.<TABLE>"),
            ("db.<iceberg_table>", "db.<ICEBERG_TABLE>"),
        ],
    )
    def test_spellings_of_one_object_are_equal(self, left, right):
        assert ObjectName(left) == ObjectName(right)
        assert hash(ObjectName(left)) == hash(ObjectName(right))
        assert len({ObjectName(left), ObjectName(right)}) == 1

    @pytest.mark.parametrize(
        "left,right",
        [
            ('"aggregate"', "aggregate"),
            ('raw."Mixed"', "raw.mixed"),
            ("raw.public", "raw.public.<table>"),
            ("raw.<table>", "raw.<view>"),
            ("raw.public", "raw.public.x"),
            ('raw."a.b"', "raw.a.b"),
        ],
    )
    def test_different_objects_are_not_equal(self, left, right):
        assert ObjectName(left) != ObjectName(right)

    @pytest.mark.parametrize(
        "spellings",
        [
            ["aggregate", '"AGGREGATE"', "Aggregate"],
            ["MyTable", "MYTABLE", '"MYTABLE"'],
            ['"aggregate"'],
            ["a$b", '"A$B"'],
            ['"Mixed"'],
        ],
    )
    def test_identity_agrees_with_snowflake_core(self, spellings):
        for right in spellings:
            assert ObjectName.single(right).parts[0] == str(
                CanonicalName.resolve_from_identifier(right)
            )
            for left in spellings + ['"aggregate"', "aggregate", '"Mixed"', "mixed"]:
                assert (ObjectName.single(left) == ObjectName.single(right)) is (
                    Identifier(left) == Identifier(right)
                )

    def test_names_equal_and_hash_like_their_rendering(self):
        name = ObjectName('LAB_RESULTS."AGGREGATE"')

        assert name == "lab_results.aggregate"
        assert hash(name) == hash("lab_results.aggregate")
        assert "lab_results.aggregate" in {name}
        assert {name: 1}["lab_results.aggregate"] == 1
        assert [ObjectName("DATABASE_1.SCHEMA_1.TABLE_1")] == [
            "database_1.schema_1.table_1"
        ]

    def test_other_spellings_have_to_be_parsed(self):
        name = ObjectName("lab_results.aggregate")

        assert name != 'LAB_RESULTS."AGGREGATE"'
        assert name != "LAB_RESULTS.AGGREGATE"

    def test_names_never_equal_other_types(self):
        assert ObjectName("raw") != Identifier("raw")
        assert ObjectName("raw") != CanonicalName("RAW")
        assert ObjectName('"*"') != NamePattern("*")
        assert ObjectName("raw") != 1

    def test_canonical_rows_match_spec_spellings(self):
        # A role like gitlab-ci keys the same from SHOW ROLES and from the spec
        assert ObjectName.from_canonical("gitlab-ci") == ObjectName.single("gitlab-ci")
        assert ObjectName.from_canonical("SECURITYADMIN") == "securityadmin"
        assert ObjectName.from_canonical("TUNDRA") == ObjectName.single("tundra")


class TestRenderingTables:
    """The tables in test_identifier_rendering.py, through this module alone."""

    @pytest.mark.parametrize("spelling,rendered", rows(rendering.SPEC_SCHEMAS))
    def test_spec_schema(self, spelling, rendered):
        assert str(name_or_pattern(spelling, arity=2)) == rendered

    @pytest.mark.parametrize("stored,rendered", rows(rendering.GRANTED_SCHEMAS))
    def test_granted_schema(self, stored, rendered):
        quoted = '"' + stored.replace('"', '""') + '"'

        assert str(ObjectName(f"RAW.{quoted}")) == rendered
        # SHOW GRANTS quotes keywords such as AGGREGATE, but not plain names
        assert str(granted_name("SCHEMA", spell(("RAW", stored)))) == rendered

    @pytest.mark.parametrize("stored,rendered", rows(rendering.LISTED_SCHEMAS))
    def test_listed_schema(self, stored, rendered):
        name = ObjectName.from_canonical("RAW", stored)

        assert str(name) == rendered
        assert name_or_pattern("raw.*").matches(name)

    @pytest.mark.parametrize("schema,granted,covered", rows(rendering.COVERAGE))
    def test_schema_covers_granted_object(self, schema, granted, covered):
        assert (ObjectName(granted).parent == ObjectName(schema)) is covered

    @pytest.mark.parametrize("held,asked,expected", rows(rendering.CHECKER))
    def test_held_grant_matches_asked_name(self, held, asked, expected):
        reported = granted_name("SCHEMA", spell(canonical(held)))

        assert (reported == ObjectName(asked)) is expected

    @pytest.mark.parametrize("keyword", sorted(RESERVED_KEYWORDS))
    def test_every_reserved_keyword_renders_quoted_uppercase(self, keyword):
        assert str(ObjectName(f"raw.{keyword}")) == f'raw."{keyword.upper()}"'
        assert str(ObjectName(f'raw."{keyword.upper()}"')) == f'raw."{keyword.upper()}"'

    def test_window_is_reserved(self):
        assert "window" in RESERVED_KEYWORDS
        assert str(ObjectName("raw.window")) == 'raw."WINDOW"'


class TestGoldenScenarios:
    """The names behind the plans in test_golden_scenarios.py."""

    def test_keyword_schema_held_grants_match_the_spec(self):
        spec = ObjectName("lab_results.aggregate")

        assert spec == granted_name("SCHEMA", spell(("LAB_RESULTS", "AGGREGATE")))
        assert spec == ObjectName('LAB_RESULTS."AGGREGATE"')
        assert spec.future("table") == ObjectName('LAB_RESULTS."AGGREGATE".<TABLE>')
        assert name_or_pattern("lab_results.aggregate.*").parent == spec
        assert str(spec) == "lab_results.aggregate"

    def test_reserved_schema_resolves_to_the_stored_object(self):
        spec = name_or_pattern("raw.group", arity=2)

        assert spec == granted_name("SCHEMA", 'RAW."GROUP"')
        assert spec == ObjectName.from_canonical("RAW", "GROUP")
        assert str(spec) == 'raw."GROUP"'

    def test_unquoted_mixed_case_schema_resolves_to_uppercase(self):
        spec = name_or_pattern("raw.MixedCase", arity=2)

        assert spec == ObjectName.from_canonical("RAW", "MIXEDCASE")
        assert str(spec) == "raw.mixedcase"

    @pytest.mark.parametrize(
        "entry,expected",
        [
            ("raw.loading_*", ["raw.loading_orders", "raw.loading_contact"]),
            ("analytics.*", ["analytics.marts", "analytics.staging"]),
            (
                "raw.*",
                [
                    "raw.public",
                    "raw.loading_orders",
                    "raw.loading_contact",
                    'raw."Mixed-Case"',
                    "raw.archive",
                ],
            ),
        ],
    )
    def test_roles_schema_wildcards(self, entry, expected):
        schemas = listed(*golden.roles_account().schemas)
        pattern = name_or_pattern(entry, arity=2)

        assert [s for s in schemas if pattern.matches(s)] == expected

    def test_wildcards_owned_schemas_skip_information_schema(self):
        schemas = listed(*golden.wildcards_account().schemas)
        pattern = name_or_pattern("raw.*")

        assert [s for s in schemas if pattern.matches(s)] == [
            "raw.public",
            "raw.staging",
        ]

    def test_roles_account_objects_render(self):
        account = golden.roles_account()

        assert [str(ObjectName.from_canonical(w)) for w in account.warehouses] == [
            "reporting_wh",
            '"etl-wh"',
        ]
        assert ObjectName.single("etl-wh") == ObjectName.from_canonical("etl-wh")
        assert ObjectName('analytics.marts."Customer View"') == (
            ObjectName.from_canonical("ANALYTICS", "MARTS", "Customer View")
        )

    def test_roles_memberships_render(self):
        assert str(member_or_pattern("snowflake.cortex_user")) == (
            '"SNOWFLAKE".cortex_user'
        )
        assert member_or_pattern("analytics.reader") == granted_name(
            "DATABASE_ROLE", "ANALYTICS.READER"
        )
        assert member_or_pattern("monitor") == ObjectName.from_canonical("MONITOR")

    def test_users_render(self):
        assert str(ObjectName.single("gitlab-ci")) == '"gitlab-ci"'
        assert ObjectName.single("heidi") == ObjectName.from_canonical("HEIDI")
        assert ObjectName("governance.tags.team") == ObjectName.from_canonical(
            "GOVERNANCE", "TAGS", "TEAM"
        )
        assert str(ObjectName("governance.tags.cost_centre")) == (
            "governance.tags.cost_centre"
        )


class TestSnowflakyCases:
    """The cases snowflaky's own tests in test_snowflake_connector.py pin."""

    @pytest.mark.parametrize(
        "spelling,rendered",
        [
            # CHANGED: reserved words are quoted uppercase, were "schema"."table"
            ("analytics.schema.table", 'analytics."SCHEMA"."TABLE"'),
            ("1234raw.schema.table", '"1234raw"."SCHEMA"."TABLE"'),
            ('"123-with-quotes".schema.table', '"123-with-quotes"."SCHEMA"."TABLE"'),
            ("1_db-9-RANDOM.schema.table", '"1_db-9-RANDOM"."SCHEMA"."TABLE"'),
            ("DATABASE_1.SCHEMA_1.TABLE_1", "database_1.schema_1.table_1"),
            ("DATABASE_1.SCHEMA_1.TABLE$A", "database_1.schema_1.table$a"),
            ('DATABASE_1.SCHEMA_1."GROUP"', 'database_1.schema_1."GROUP"'),
            (
                "DATABASE_1.SCHEMA_1.1_LEADING_DIGIT",
                'database_1.schema_1."1_LEADING_DIGIT"',
            ),
            (
                'DATABASE_1.SCHEMA_1."1_LEADING_DIGIT_IN_QUOTES"',
                'database_1.schema_1."1_LEADING_DIGIT_IN_QUOTES"',
            ),
            (
                'DATABASE_1.SCHEMA_1."QUOTED!TABLE%WITH^SPECIAL*CHARACTERS"',
                'database_1.schema_1."QUOTED!TABLE%WITH^SPECIAL*CHARACTERS"',
            ),
            # CHANGED: unquoted mixed case resolves to uppercase, was quoted as written
            (
                "DATABASE_1.SCHEMA_1.Case_Sensitive_Table_Name",
                "database_1.schema_1.case_sensitive_table_name",
            ),
            ("DATABASE_1.SCHEMA_1.<TABLE>", "database_1.schema_1.<table>"),
            (
                "DATABASE_1.1_LEADING_DIGIT.<TABLE>",
                'database_1."1_LEADING_DIGIT".<table>',
            ),
            (
                "DATABASE_1.1_LEADING_DIGIT.<table>",
                'database_1."1_LEADING_DIGIT".<table>',
            ),
            # CHANGED: a quoted dot is part of the name, where snowflaky warned
            (
                'DATABASE_1.SCHEMA_1."TABLE_1.AMBIGUOUS_IDENTIFIER"',
                'database_1.schema_1."TABLE_1.AMBIGUOUS_IDENTIFIER"',
            ),
            ("DATABASE_1.SCHEMA_1.GROUP", 'database_1.schema_1."GROUP"'),
            (
                'DATABASE_1."CaseSensitiveSchema".<VIEW>',
                'database_1."CaseSensitiveSchema".<view>',
            ),
        ],
    )
    def test_snowflaky_cases(self, spelling, rendered):
        assert str(ObjectName(spelling)) == rendered

    def test_an_unquoted_star_is_a_pattern(self):
        # CHANGED: snowflaky quoted this as a table name
        spelling = "DATABASE_1.SCHEMA_1.TABLE%WITH^SPECIAL*CHARACTERS"

        with pytest.raises(InvalidNameError):
            ObjectName(spelling)
        assert isinstance(name_or_pattern(spelling), NamePattern)
        assert ObjectName(
            'DATABASE_1.SCHEMA_1."TABLE%WITH^SPECIAL*CHARACTERS"'
        ).parts == ("DATABASE_1", "SCHEMA_1", "TABLE%WITH^SPECIAL*CHARACTERS")

    def test_four_parts_raise(self):
        # CHANGED: snowflaky warned with a SyntaxWarning and carried on
        with pytest.raises(InvalidNameError):
            ObjectName("DATABASE_1.SCHEMA_1.TABLE_1.AMBIGUOUS_IDENTIFIER")

    def test_none_raises(self):
        # CHANGED: snowflaky returned "" with a SyntaxWarning
        with pytest.raises(TypeError):
            ObjectName(None)

    @pytest.mark.parametrize(
        "reported,rendered",
        [
            ("db_1.<ICEBERG_TABLE>", "db_1.<iceberg_table>"),
            ("db_1.schema_1.<DYNAMIC_TABLE>", "db_1.schema_1.<dynamic_table>"),
            ("db_1.schema_1.<STREAMLIT>", "db_1.schema_1.<streamlit>"),
            ("db_1.schema_1.<SEMANTIC_VIEW>", "db_1.schema_1.<semantic_view>"),
            ("db_1.<TABLE>", "db_1.<table>"),
            ("db_1.<VIEW>", "db_1.<view>"),
            ("db_1.<SCHEMA>", "db_1.<schema>"),
        ],
    )
    def test_future_placeholders(self, reported, rendered):
        assert str(ObjectName(reported)) == rendered

    @pytest.mark.parametrize(
        "spelling,rendered",
        [
            ("cost_management.tags.team", "cost_management.tags.team"),
            ("COST_MANAGEMENT.TAGS.TEAM", "cost_management.tags.team"),
            ("snowflake.core.privacy_category", '"SNOWFLAKE".core.privacy_category'),
            ("governance.tags.COST-CENTRE", 'governance.tags."COST-CENTRE"'),
            ('governance.tags."cost-centre"', 'governance.tags."cost-centre"'),
            # CHANGED: a part that isn't an identifier keeps its case, where
            # snowflaky_tag uppercased it to governance.tags."COST-CENTRE" and
            # "MY-DB".tags.team. A spec has to spell such a tag the way it's stored,
            # or the plan sets a tag that doesn't exist and unsets the one held
            ("governance.tags.cost-centre", 'governance.tags."cost-centre"'),
            ("governance.tags.Cost-Centre", 'governance.tags."Cost-Centre"'),
            ("my-db.tags.team", '"my-db".tags.team'),
        ],
    )
    def test_tags(self, spelling, rendered):
        assert str(ObjectName(spelling, arity=3)) == rendered

    def test_a_tag_that_isnt_an_identifier_keys_by_its_stored_case(self):
        # CHANGED: snowflaky_tag matched a held "COST-CENTRE" from cost-centre
        held = ObjectName.from_canonical("GOVERNANCE", "TAGS", "COST-CENTRE")

        assert ObjectName("governance.tags.COST-CENTRE") == held
        assert ObjectName("governance.tags.cost-centre") != held

    def test_tags_on_a_user_key_by_tag_name(self):
        live = {
            ObjectName.from_canonical("COST_MANAGEMENT", "TAGS", "TEAM"): "marketing",
            ObjectName.from_canonical("SNOWFLAKE", "CORE", "Mixed-Case"): "x",
        }

        assert live == {
            "cost_management.tags.team": "marketing",
            '"SNOWFLAKE".core."Mixed-Case"': "x",
        }
        assert ObjectName("COST_MANAGEMENT.TAGS.TEAM") in live

    def test_tag_references_takes_the_user_as_a_string_literal(self):
        assert ObjectName.single("blake.enyart@gmail.com").string_literal == (
            "'\"blake.enyart@gmail.com\"'"
        )
        assert ObjectName.single("o'neill").string_literal == "'\"o''neill\"'"
        assert ObjectName.single("TAGGED").string_literal == "'tagged'"

    @pytest.mark.parametrize(
        "stored,literal",
        [
            ("CORP\\heidi", "'\"CORP\\\\heidi\"'"),
            ("tab\\there", "'\"tab\\\\there\"'"),
            ("a\\'b", "'\"a\\\\''b\"'"),
            ("DOMAIN\\", "'\"DOMAIN\\\\\"'"),
        ],
    )
    def test_string_literals_double_backslashes(self, stored, literal):
        # CHANGED: show_tags_on_user sent '"CORP\heidi"', which Snowflake reads as
        # "CORPheidi". It reads backslash escapes in string constants, so \t would be
        # a TAB and \' would end the literal early
        name = ObjectName.from_canonical(stored)

        assert name.string_literal == literal
        assert snowflake_string_value(name.string_literal) == str(name)

    @pytest.mark.parametrize(
        "stored", ["heidi.bond@example.com", "o'neill", "''", "\\'", "\\\\", 'a"\\b']
    )
    def test_string_literals_round_trip(self, stored):
        # CHANGED for the stored names with a backslash, as above
        name = ObjectName.from_canonical(stored)

        assert snowflake_string_value(name.string_literal) == str(name)

    @pytest.mark.parametrize("spelling", ["raw.<schema>", "raw.public.<table>"])
    def test_a_future_scope_isnt_a_string_literal(self, spelling):
        # It isn't SQL, so it can't end up in TAG_REFERENCES('...') either
        with pytest.raises(InvalidNameError, match="future-grant scope"):
            ObjectName(spelling).string_literal

    def test_database_roles_listed_in_a_database(self):
        mydb = ObjectName.single("mydb")
        system = ObjectName.single("snowflake")

        assert f"SHOW DATABASE ROLES IN DATABASE {mydb}" == (
            "SHOW DATABASE ROLES IN DATABASE mydb"
        )
        assert f"SHOW DATABASE ROLES IN DATABASE {system}" == (
            'SHOW DATABASE ROLES IN DATABASE "SNOWFLAKE"'
        )
        assert [mydb.child("DB_ROLE_1"), mydb.child("DB_ROLE_2")] == [
            "mydb.db_role_1",
            "mydb.db_role_2",
        ]
        assert system.child("ORGANIZATION_BILLING_VIEWER") == (
            '"SNOWFLAKE".organization_billing_viewer'
        )

    def test_application_roles_listed_in_an_application(self):
        application = ObjectName.single("snowflake")

        assert f"SHOW APPLICATION ROLES IN APPLICATION {application}" == (
            'SHOW APPLICATION ROLES IN APPLICATION "SNOWFLAKE"'
        )
        assert [
            application.child("CORTEX_ANALYST_REQUESTS_VIEWER"),
            application.child("CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"),
        ] == [
            '"SNOWFLAKE".cortex_analyst_requests_viewer',
            '"SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
        ]

    def test_granted_roles_key_like_the_spec(self):
        assert granted_name("DATABASE_ROLE", "MYDB.DB_ROLE_1") == "mydb.db_role_1"
        assert granted_name(
            "DATABASE ROLE", "SNOWFLAKE.ORGANIZATION_BILLING_VIEWER"
        ) == ('"SNOWFLAKE".organization_billing_viewer')
        assert granted_name(
            "APPLICATION_ROLE", 'SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"'
        ) == member_or_pattern('SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"')

    def test_granted_quoted_table_names(self):
        reported = [
            'DATABASE_1.SCHEMA_1."GROUP"',
            'DATABASE_1.SCHEMA_1."INTERSECT"',
            'DATABASE_1.SCHEMA_1."Capitalized_Name"',
            'DATABASE_1.SCHEMA_1."123_TABLE"',
        ]

        assert [granted_name("TABLE", name) for name in reported] == [
            'database_1.schema_1."GROUP"',
            'database_1.schema_1."INTERSECT"',
            'database_1.schema_1."Capitalized_Name"',
            'database_1.schema_1."123_TABLE"',
        ]


class TestUserProperties:
    @pytest.mark.parametrize(
        "value,rendered",
        [
            ("monitor", '"MONITOR"'),
            ("MONITOR", '"MONITOR"'),
            ("metrics_wh", '"METRICS_WH"'),
            ("Ftl", '"FTL"'),
            ("role1", '"ROLE1"'),
            ("reporting_wh", '"REPORTING_WH"'),
            ("group", '"GROUP"'),
            ('"mixedCase"', '"mixedCase"'),
            # CHANGED: a value that isn't an identifier keeps its case, was "MY-WH"
            ("my-wh", '"my-wh"'),
            # CHANGED: DEFAULT_ROLE and DEFAULT_WAREHOUSE don't split, was "ETL"."WH"
            ("etl.wh", '"etl.wh"'),
        ],
    )
    def test_default_role_and_warehouse(self, value, rendered):
        assert ObjectName.single(value).quoted == rendered

    @pytest.mark.parametrize(
        "value,rendered",
        [
            ("public", '"PUBLIC"'),
            ("raw.public", '"RAW"."PUBLIC"'),
            ("analytics.marts", '"ANALYTICS"."MARTS"'),
            ('raw."Mixed-Case"', '"RAW"."Mixed-Case"'),
            ('"mixedCase"', '"mixedCase"'),
            # CHANGED: was "MY-DB"."PUBLIC"
            ("my-db.public", '"my-db"."PUBLIC"'),
        ],
    )
    def test_default_namespace(self, value, rendered):
        assert ObjectName(value).quoted == rendered

    def test_sql_and_property_renderings_of_one_name(self):
        name = ObjectName.single("monitor")

        assert str(name) == "monitor"
        assert name.quoted == '"MONITOR"'
        assert name.api_name == "MONITOR"

    def test_a_future_scope_isnt_a_property(self):
        with pytest.raises(InvalidNameError):
            ObjectName("raw.<table>").quoted


class TestStructure:
    @pytest.mark.parametrize(
        "spelling,database,parent",
        [
            ("raw", "raw", None),
            ("raw.public", "raw", "raw"),
            ("raw.public.events", "raw", "raw.public"),
            ("raw.<schema>", "raw", "raw"),
            ("raw.<table>", "raw", "raw"),
            ("raw.public.<table>", "raw", "raw.public"),
            ("snowflake.cortex_user", '"SNOWFLAKE"', '"SNOWFLAKE"'),
        ],
    )
    def test_database_and_parent(self, spelling, database, parent):
        name = ObjectName(spelling)

        assert name.database == database
        assert name.parent == parent

    def test_database_and_parent_are_cached(self):
        name = ObjectName("raw.public.events")
        database = ObjectName("raw")

        assert name.parent is name.parent
        assert name.database is name.database
        assert database.database is database

    @pytest.mark.parametrize(
        "container,kind,expected",
        [
            ("raw", "schema", "raw.<schema>"),
            ("raw", "table", "raw.<table>"),
            ("raw.public", "table", "raw.public.<table>"),
            ("raw.public", "iceberg table", "raw.public.<iceberg_table>"),
            ("raw.public", "iceberg_table", "raw.public.<iceberg_table>"),
            ("raw.public", "ICEBERG_TABLE", "raw.public.<iceberg_table>"),
            ("raw.public", "<ICEBERG_TABLE>", "raw.public.<iceberg_table>"),
            ('raw."Mixed-Case"', "view", 'raw."Mixed-Case".<view>'),
        ],
    )
    def test_future(self, container, kind, expected):
        assert ObjectName(container).future(kind) == ObjectName(expected)

    @pytest.mark.parametrize("object_type", ALL_OBJECT_TYPES, ids=lambda t: t.name)
    def test_future_takes_keywords_and_grant_keys(self, object_type):
        schema = ObjectName("db_1.schema_1")
        expected = f"db_1.schema_1.{object_type.future_placeholder}"

        assert schema.future(object_type.name) == expected
        assert schema.future(object_type.grant_key) == expected
        assert schema.future(object_type.name).future_kind == object_type.grant_key

    @pytest.mark.parametrize(
        "container,kind",
        [
            # Snowflake has no future schemas inside a schema
            ("raw.public", "schema"),
            ("raw.public.events", "table"),
            ("raw.<table>", "view"),
            ("raw", "not a kind!"),
            ("raw", "<_x>"),
            ("raw", ""),
            # Only the kinds tundra grants future grants on
            ("raw.public", "function"),
            ("raw.public", "<PROCEDURE>"),
            ("raw.public", "tables"),
        ],
    )
    def test_future_rejects_what_snowflake_has_no_scope_for(self, container, kind):
        with pytest.raises(InvalidNameError):
            ObjectName(container).future(kind)

    def test_child(self):
        assert ObjectName("raw").child("Mixed-Case") == 'raw."Mixed-Case"'
        assert ObjectName("raw.public").child(CanonicalName("T")) == "raw.public.t"
        with pytest.raises(InvalidNameError):
            ObjectName("a.b.c").child("D")
        with pytest.raises(InvalidNameError):
            ObjectName("raw.<table>").child("D")

    def test_properties(self):
        assert ObjectName("raw.public.events").name == "EVENTS"
        assert ObjectName.single("gitlab-ci").name == "gitlab-ci"
        assert ObjectName("raw.public").future_kind is None
        assert not ObjectName("raw.public").is_future
        assert ObjectName("RAW.INFORMATION_SCHEMA").is_information_schema
        assert not ObjectName('raw."information_schema"').is_information_schema
        assert not ObjectName("raw.information_schema.<table>").is_information_schema
        with pytest.raises(InvalidNameError):
            ObjectName("raw.<table>").name

    def test_names_format_into_sql(self):
        schema = ObjectName("RAW.PUBLIC")

        assert f"SHOW TERSE TABLES IN SCHEMA {schema}" == (
            "SHOW TERSE TABLES IN SCHEMA raw.public"
        )
        assert "GRANT usage ON schema {} TO ROLE x".format(schema) == (
            "GRANT usage ON schema raw.public TO ROLE x"
        )
        assert f"{schema:>12}" == "  raw.public"

    @pytest.mark.parametrize("spelling", ["raw.<schema>", "RAW.PUBLIC.<TABLE>"])
    def test_a_future_scope_cant_be_formatted_into_sql(self, spelling):
        scope = ObjectName(spelling)

        with pytest.raises(TypeError, match="future-grant scope"):
            f"GRANT select ON table {scope} TO ROLE r"
        with pytest.raises(TypeError):
            "{}".format(scope)
        assert f"IN SCHEMA {scope.parent}" == f"IN SCHEMA {str(scope.parent)}"
        assert f"{scope!s}" == str(scope) == "%s" % scope
        assert scope != scope.parent and hash(scope) != hash(scope.parent)

    def test_errors_about_a_future_scope_still_name_it(self):
        with pytest.raises(InvalidNameError, match=r"raw\.public\.<table> is a future"):
            ObjectName("raw.public.<table>").name
        with pytest.raises(InvalidNameError, match=r"raw\.<table> isn't a single"):
            ObjectName.single(ObjectName("raw.<table>"))


class TestPatterns:
    SCHEMAS = [
        "LOADING_ORDERS",
        "LOADING_CONTACT",
        "loading_lower",
        "Loading_Mixed",
        "PUBLIC",
        "INFORMATION_SCHEMA",
        "Mixed-Case",
        "Mixed-Other",
        "PRE_X_SUF",
        "HAS_MID_IN",
    ]

    def matched(self, entry):
        pattern = name_or_pattern(entry)
        schemas = [ObjectName.from_canonical("RAW", s) for s in self.SCHEMAS]
        return [s.parts[1] for s in schemas if pattern.matches(s)]

    @pytest.mark.parametrize(
        "entry,expected",
        [
            ("raw.*", [s for s in SCHEMAS if s != INFORMATION_SCHEMA]),
            # CHANGED: the exclusion compared strings built from the spec spelling
            ("RAW.*", [s for s in SCHEMAS if s != INFORMATION_SCHEMA]),
            ('"RAW".*', [s for s in SCHEMAS if s != INFORMATION_SCHEMA]),
            ("raw.loading_*", ["LOADING_ORDERS", "LOADING_CONTACT"]),
            # CHANGED: only a lowercase pattern matched LOADING_ORDERS before
            ("RAW.LOADING_*", ["LOADING_ORDERS", "LOADING_CONTACT"]),
            ("raw.Loading_*", ["LOADING_ORDERS", "LOADING_CONTACT"]),
            ('raw."loading_"*', ["loading_lower"]),
            ('raw."Loading_"*', ["Loading_Mixed"]),
            ("raw.*_orders", ["LOADING_ORDERS"]),
            ("raw.*_ORDERS", ["LOADING_ORDERS"]),
            # CHANGED: a glob that isn't an identifier keeps its case
            ("raw.Mixed-*", ["Mixed-Case", "Mixed-Other"]),
            ("raw.mixed-*", []),
            # CHANGED: * matches anywhere, so these never matched or matched everything
            ("raw.pre*suf", ["PRE_X_SUF"]),
            ("raw.*mid*", ["HAS_MID_IN"]),
            ("raw.**", [s for s in SCHEMAS if s != INFORMATION_SCHEMA]),
            # No wildcard reaches it, however close the glob
            ("raw.information_*", []),
        ],
    )
    def test_schema_wildcards_match_canonical_names(self, entry, expected):
        assert self.matched(entry) == expected

    def test_a_wildcard_only_matches_its_own_database(self):
        pattern = name_or_pattern("raw.*")

        assert not pattern.matches(ObjectName.from_canonical("OTHER", "PUBLIC"))
        assert not pattern.matches(ObjectName.from_canonical("RAW"))
        assert not pattern.matches(ObjectName("raw.public.events"))
        assert not pattern.matches(ObjectName("raw.<table>"))

    def test_information_schema_is_only_skipped_as_a_schema(self):
        tables = name_or_pattern("raw.public.*")
        everything = name_or_pattern("raw.*.*")

        assert tables.matches(ObjectName("RAW.PUBLIC.INFORMATION_SCHEMA"))
        assert everything.matches(ObjectName("RAW.PUBLIC.EVENTS"))
        assert not everything.matches(ObjectName("RAW.INFORMATION_SCHEMA.TABLES"))
        assert name_or_pattern("raw.information_schema.*").matches(
            ObjectName("RAW.INFORMATION_SCHEMA.TABLES")
        )

    def test_table_wildcards(self):
        tables = name_or_pattern("raw.public.*", arity=3)

        assert tables.matches(ObjectName("RAW.PUBLIC.EVENTS"))
        assert tables.matches(ObjectName('RAW.PUBLIC."Mixed Table"'))
        assert not tables.matches(ObjectName("RAW.OTHER.EVENTS"))
        assert not tables.matches(ObjectName("RAW.PUBLIC"))
        assert not tables.matches(ObjectName("RAW.PUBLIC.<TABLE>"))
        assert name_or_pattern("raw.loading_*.*").matches(
            ObjectName("RAW.LOADING_ORDERS.CHARGES")
        )

    def test_structure_for_the_generator(self):
        schemas = name_or_pattern("raw.*")
        everything = name_or_pattern("raw.*.*")
        tables = name_or_pattern("raw.public.*")
        prefixed = name_or_pattern("raw.loading_*.*")

        assert (schemas.prefix, schemas.globs) == (ObjectName("raw"), ("*",))
        assert (everything.prefix, everything.globs) == (ObjectName("raw"), ("*", "*"))
        assert (tables.prefix, tables.globs) == (ObjectName("raw.public"), ("*",))
        assert prefixed.globs == ("loading_*", "*")
        assert schemas.parent == "raw"
        assert everything.parent == name_or_pattern("raw.*")
        assert tables.parent == ObjectName("raw.public")
        assert prefixed.parent == name_or_pattern("RAW.LOADING_*")
        assert {p.database for p in (schemas, everything, tables, prefixed)} == {
            ObjectName("raw")
        }

    def test_every_role(self):
        star = member_or_pattern("*")

        assert isinstance(star, NamePattern)
        assert star.prefix is None
        assert star.parent is None
        assert star.matches(ObjectName.from_canonical("LOADER"))
        assert star.matches(ObjectName.single("heidi.bond@example.com"))
        assert not star.matches(ObjectName("analytics.reader"))
        with pytest.raises(InvalidNameError):
            star.database

    def test_matches_takes_only_names(self):
        # A str could be a role with dots in it, so only the caller can parse it
        with pytest.raises(TypeError, match="ObjectName.single"):
            name_or_pattern("*").matches("heidi.bond@example.com")

    @pytest.mark.parametrize(
        "spelling,expected_type",
        [
            ("raw.public", ObjectName),
            ('raw."a*b"', ObjectName),
            ('"*"', ObjectName),
            ("raw.*", NamePattern),
            ("*", NamePattern),
            ('raw."loading_"*', NamePattern),
            ('raw.*."Mixed"', NamePattern),
        ],
    )
    def test_name_or_pattern_routes_on_unquoted_stars(self, spelling, expected_type):
        assert type(name_or_pattern(spelling)) is expected_type

    @pytest.mark.parametrize(
        "spelling,text",
        [
            ("raw.*", "raw.*"),
            ("RAW.*", "raw.*"),
            ("raw.*.*", "raw.*.*"),
            ("RAW.PUBLIC.*", "raw.public.*"),
            ("raw.Loading_*", "raw.loading_*"),
            ('RAW."Mixed".Loading_*', 'raw."Mixed".loading_*'),
            ("raw.my-*", 'raw."my-"*'),
            ('raw."my-"*', 'raw."my-"*'),
            ('raw."loading_"*', 'raw."loading_"*'),
            ('raw."Loading"_*', 'raw."Loading_"*'),
            ("raw.1a*", 'raw."1a"*'),
            # Read both ways, so the unquoted text keeps the case it was written in
            ("raw.*1234raw", "raw.*1234raw"),
            ("RAW.*1234Raw", "raw.*1234Raw"),
            ("raw.*2024_backup", "raw.*2024_backup"),
            ("raw.*$tmp", "raw.*$tmp"),
            ("raw.*1234raw*", "raw.*1234raw*"),
            ('raw.*1234raw"x-y"', 'raw.*1234raw"x-y"'),
            # These read the same both ways
            ('raw.*1234"raw"', 'raw.*"1234raw"'),
            ("raw.*1234RAW", 'raw.*"1234RAW"'),
            ('raw.*"1234RAW"', 'raw.*"1234RAW"'),
            ("raw.**", "raw.*"),
            ("raw.*mid*", "raw.*mid*"),
            ('raw.*."Mixed"', 'raw.*."Mixed"'),
            ("raw.*.group", 'raw.*."GROUP"'),
            ('raw."a*b"*', 'raw."a*b"*'),
            ("*", "*"),
        ],
    )
    def test_patterns_have_one_canonical_text(self, spelling, text):
        pattern = name_or_pattern(spelling)

        assert str(pattern) == text
        assert pattern == text
        assert hash(pattern) == hash(text)
        assert NamePattern(text) == pattern

    def test_patterns_compare_by_canonical_form(self):
        assert NamePattern("raw.*") == NamePattern("RAW.*")
        assert NamePattern("raw.*") != "RAW.*"
        assert NamePattern("raw.loading_*") != NamePattern('raw."loading_"*')
        assert len({NamePattern("raw.*"), NamePattern('"RAW".*')}) == 1

    def test_a_pattern_cant_be_formatted_into_sql(self):
        pattern = name_or_pattern("raw.*")

        with pytest.raises(TypeError, match="can't be formatted into SQL"):
            f"GRANT usage ON schema {pattern} TO ROLE x"
        with pytest.raises(TypeError):
            "{}".format(pattern)
        assert f"{pattern!s}" == "raw.*"
        assert "%s" % pattern == "raw.*"
        assert repr(pattern) == "NamePattern('raw.*')"

    @pytest.mark.parametrize(
        "spelling",
        [
            "raw.*.<table>",
            "raw.<table>.*",
            "a.*.c.d",
            'raw."loading_*',
            'raw.*."a"b',
            "raw.*.",
            "raw..*",
        ],
    )
    def test_malformed_patterns_raise(self, spelling):
        with pytest.raises(InvalidNameError):
            name_or_pattern(spelling)

    @pytest.mark.parametrize(
        "spelling", ['raw.""*', 'raw.*""', 'raw.*""*', 'raw.a""*', 'raw.*"x"*""']
    )
    def test_an_empty_quoted_fragment_raises(self, spelling):
        # It was dropped, so raw.""* read as raw.*, every schema in the database
        with pytest.raises(InvalidNameError, match="empty quoted part"):
            name_or_pattern(spelling)
        with pytest.raises(InvalidNameError, match="empty quoted part"):
            member_or_pattern(spelling)

    def test_an_escaped_quote_isnt_an_empty_fragment(self):
        assert name_or_pattern('raw.""""*').matches(
            ObjectName.from_canonical("RAW", '"x')
        )

    def test_a_pattern_needs_a_wildcard(self):
        with pytest.raises(InvalidNameError):
            NamePattern("raw.public")
        with pytest.raises(TypeError):
            NamePattern(None)
        with pytest.raises(TypeError):
            name_or_pattern(None)

    @pytest.mark.parametrize(
        "spelling,arity",
        [
            ("raw.*", 2),
            ("raw.public", 2),
            ("raw.*.*", 3),
            ("raw.public.*", 3),
            ("raw.public.events", 3),
        ],
    )
    def test_arity_counts_every_part(self, spelling, arity):
        assert name_or_pattern(spelling, arity=arity) == name_or_pattern(spelling)

    @pytest.mark.parametrize(
        "spelling,arity",
        [
            ("raw.*", 3),
            ("raw.public", 3),
            ("raw.public.events", 2),
            ("raw", 2),
            ("raw.<table>", 1),
            ("raw.public.<table>", 2),
            ("*.*", 3),
        ],
    )
    def test_arity_rejects_other_shapes(self, spelling, arity):
        with pytest.raises(InvalidNameError):
            name_or_pattern(spelling, arity=arity)

    def test_arity_on_names(self):
        assert ObjectName("mydb.db_role_1", arity=2) == "mydb.db_role_1"
        with pytest.raises(InvalidNameError):
            ObjectName("mydb", arity=2)
        with pytest.raises(InvalidNameError):
            ObjectName("db.schema.<table>", arity=2)

    def test_member_of_patterns_have_at_most_two_parts(self):
        assert member_or_pattern("snowflake.*") == '"SNOWFLAKE".*'
        with pytest.raises(InvalidNameError):
            member_or_pattern('a."b".*')
        with pytest.raises(InvalidNameError):
            member_or_pattern(NamePattern("a.*.c"))

    def test_member_of_patterns_split_at_the_first_dot(self):
        # As role_reference(): application role names have dots in them
        pattern = member_or_pattern("snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-*")
        role = "snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"

        assert pattern == '"SNOWFLAKE"."CORTEX-MODEL-ROLE-GEMINI-2.5-"*'
        assert pattern.prefix == ObjectName.single("snowflake")
        assert pattern.matches(ObjectName.role_reference(role))
        assert member_or_pattern(str(pattern)) == pattern
        assert member_or_pattern("a.*.c") == 'a.*".c"'
        assert member_or_pattern("a.*.c").matches(ObjectName.role_reference("a.b.c"))

    @pytest.mark.parametrize(
        "glob",
        [
            "raw.1234raw*",
            "raw.*1234raw",
            "raw.*234raw",
            "raw.12*raw",
            "raw.*2024q1",
            "raw.2024*q1",
            "raw.*$x",
            "raw.loading_*",
            "raw.*_orders",
            "raw.pre*suf",
            "raw.*mid*",
            "raw.Mixed-*",
            'raw."loading_"*',
            'raw.*"Mixed"',
        ],
    )
    def test_a_glob_matches_the_name_it_spells_without_its_stars(self, glob):
        assert name_or_pattern(glob).matches(ObjectName(glob.replace("*", "")))

    @pytest.mark.parametrize(
        "glob,schema",
        [
            # As today's full_schema_list matches the lowercase rendering of these
            ("raw.*2024_backup", "SALES_2024_BACKUP"),
            ("raw.*2024q1", "SALES2024Q1"),
            ("raw.*2024q1", "SALES_2024Q1"),
            ("raw.*1234raw", "X1234RAW"),
            ("raw.*1234raw", "LOADING_1234RAW"),
            ("raw.*$tmp", "STAGE$TMP"),
            ("raw.*1234raw*", "X1234RAW_OLD"),
            # CHANGED: only a lowercase pattern matched these before
            ("raw.*2024_BACKUP", "SALES_2024_BACKUP"),
            ("raw.*2024Q1", "SALES2024Q1"),
            ("raw.*1234Raw", "X1234RAW"),
        ],
    )
    def test_a_leading_star_fills_in_an_identifier(self, glob, schema):
        # With its * filled in, *2024_backup is an identifier like sales_2024_backup,
        # so its text resolves to uppercase like any unquoted identifier's
        assert name_or_pattern(glob).matches(ObjectName.from_canonical("RAW", schema))

    @pytest.mark.parametrize(
        "glob,schema,matched",
        [
            # CHANGED: globs match names that need quoting, which never matched before.
            # With its * taken out, *1234raw spells the name 1234raw, which isn't an
            # identifier and so keeps its case
            ("raw.*1234raw", "1234raw", True),
            ("raw.*1234raw", "x-1234raw", True),
            ("raw.*1234raw", "1234Raw", False),
            # Read as an identifier, the * is at least its first letter
            ("raw.*1234raw", "1234RAW", False),
            ("raw.*1234raw*", "1234RAW_OLD", False),
            ("raw.*$tmp", "$TMP", False),
            ("raw.*$tmp", "$tmp", True),
            ("raw.*1234RAW", "1234RAW", True),
            ("raw.*1234RAW", "1234raw", False),
            ("raw.*1234Raw", "1234Raw", True),
            ("raw.*1234Raw", "1234raw", False),
            ('raw.*1234"raw"', "1234raw", True),
            ('raw.*1234"raw"', "X1234RAW", False),
            ('raw.*1234"raw"', "X1234raw", True),
            # With its * taken out, *_1234raw spells the identifier _1234raw
            ("raw.*_1234raw", "L_1234RAW", True),
            ("raw.*_1234raw", "_1234raw", False),
            # raw spells the identifier RAW, the way _orders spells _ORDERS
            ("raw.*raw", "1234raw", False),
            ("raw.*raw", "X_RAW", True),
        ],
    )
    def test_a_leading_star_also_keeps_the_case_of_a_name(self, glob, schema, matched):
        name = ObjectName.from_canonical("RAW", schema)

        assert name_or_pattern(glob).matches(name) is matched

    def test_a_leading_star_reads_both_ways_in_member_of(self):
        pattern = member_or_pattern("*2fa")

        assert pattern.matches(ObjectName.single("svc2fa"))
        assert pattern.matches(ObjectName.single('"2fa"'))
        assert not pattern.matches(ObjectName.single('"svc2Fa"'))
        assert member_or_pattern("db.*2fa").matches(ObjectName("db.svc_2fa"))

    def test_information_schema_is_a_role_name_in_member_of(self):
        # In DB.ROLE the second part is a role, so the schema rule doesn't apply
        roles = member_or_pattern("db.*")
        schemas = name_or_pattern("db.*")

        assert roles.matches(ObjectName.role_reference("db.information_schema"))
        assert roles.matches(ObjectName.role_reference("db.analyst"))
        assert not schemas.matches(ObjectName("db.information_schema"))
        assert member_or_pattern("*.*").matches(ObjectName("db.information_schema"))
        assert roles != schemas
        assert roles == "db.*" == schemas
        assert hash(roles) == hash(schemas)

    def test_a_pattern_keeps_its_kind_through_every_constructor(self):
        roles = member_or_pattern("db.*")
        schemas = name_or_pattern("db.*")
        information_schema = ObjectName("db.information_schema")

        assert member_or_pattern(roles) is roles
        assert name_or_pattern(schemas) is schemas
        assert member_or_pattern(schemas) == roles
        assert name_or_pattern(roles) == schemas
        assert pickle.loads(pickle.dumps(roles)) == roles
        assert pickle.loads(pickle.dumps(schemas)) == schemas
        assert pickle.loads(pickle.dumps(roles)).matches(information_schema)
        assert not pickle.loads(pickle.dumps(schemas)).matches(information_schema)
        assert member_or_pattern("*.*").parent == member_or_pattern("*")
        assert member_or_pattern("*.*").parent != NamePattern("*")

    @pytest.mark.parametrize(
        "glob,schema,matched",
        [
            ("raw.*ab*b", "ABB", True),
            ("raw.*ab*b", "AB", False),
            ("raw.*aa*a", "AAA", True),
            ("raw.*aa*a", "AA", False),
            ("raw.x*ab*ab", "XABAB", True),
            ("raw.x*ab*ab", "XAB", False),
            ("raw.*a*b*c", "CBACBA", False),
            ("raw.*a*b*c", "AXBXC", True),
        ],
    )
    def test_inner_segments_dont_overlap(self, glob, schema, matched):
        name = ObjectName.from_canonical("RAW", schema)

        assert name_or_pattern(glob).matches(name) is matched

    def test_many_stars_dont_backtrack(self):
        # A greedy .* per star took seconds on a 60-character name
        pattern = NamePattern("raw." + "*a" * 7 + "*z")
        start = time.perf_counter()

        assert not pattern.matches(ObjectName.from_canonical("RAW", "A" * 60))
        assert pattern.matches(ObjectName.from_canonical("RAW", "A" * 7 + "Z"))
        assert time.perf_counter() - start < 0.5


class TestGrantedName:
    @pytest.mark.parametrize(
        "granted_on,name,parts",
        [
            ("DATABASE", "RAW", ("RAW",)),
            ("WAREHOUSE", '"etl.wh"', ("etl.wh",)),
            ("WAREHOUSE", "REPORTING_WH", ("REPORTING_WH",)),
            ("INTEGRATION", "S3_INTEGRATION", ("S3_INTEGRATION",)),
            ("EXTERNAL_VOLUME", "ICEBERG_VOLUME", ("ICEBERG_VOLUME",)),
            ("EXTERNAL VOLUME", "ICEBERG_VOLUME", ("ICEBERG_VOLUME",)),
            ("ROLE", "LOADER", ("LOADER",)),
            ("ROLE", "gitlab-ci", ("gitlab-ci",)),
            ("USER", "heidi.bond@example.com", ("heidi.bond@example.com",)),
            ("USER", '"heidi.bond@example.com"', ("heidi.bond@example.com",)),
            ("SCHEMA", 'RAW."Mixed-Case"', ("RAW", "Mixed-Case")),
            ("SCHEMA", 'RAW."dot.inside"', ("RAW", "dot.inside")),
            ("TABLE", "RAW.PUBLIC.EVENTS", ("RAW", "PUBLIC", "EVENTS")),
            ("DATABASE_ROLE", "MYDB.DB_ROLE_1", ("MYDB", "DB_ROLE_1")),
            ("DATABASE ROLE", "MYDB.DB_ROLE_1", ("MYDB", "DB_ROLE_1")),
            (
                "APPLICATION_ROLE",
                'SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
                ("SNOWFLAKE", "CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"),
            ),
        ],
    )
    def test_each_kind_is_parsed_the_way_snowflake_spells_it(
        self, granted_on, name, parts
    ):
        assert granted_name(granted_on, name).parts == parts

    @pytest.mark.parametrize("object_type", ALL_OBJECT_TYPES, ids=lambda t: t.name)
    def test_table_like_kinds_are_dotted(self, object_type):
        granted_on = object_type.grant_key.upper()

        assert granted_name(granted_on, 'RAW.PUBLIC."Mixed"').parts == (
            "RAW",
            "PUBLIC",
            "Mixed",
        )

    @pytest.mark.parametrize(
        "granted_on,name",
        [("ACCOUNT", "AB12345"), ("FUNCTION", "RAW.PUBLIC.F"), ("STAGE", "A.B.C")],
    )
    def test_unmanaged_kinds_raise(self, granted_on, name):
        with pytest.raises(InvalidNameError):
            granted_name(granted_on, name)

    @pytest.mark.parametrize(
        "granted_on,name", [(None, "RAW"), (3, "RAW"), ("ROLE", None), ("SCHEMA", None)]
    )
    def test_non_strings_raise_type_error(self, granted_on, name):
        with pytest.raises(TypeError):
            granted_name(granted_on, name)


class TestOrdering:
    def test_sort_key_is_the_rendered_text(self):
        name = ObjectName("snowflake.core.x")

        assert name.sort_key == '"SNOWFLAKE".core.x'
        assert NamePattern("RAW.*").sort_key == "raw.*"

    def test_sorted_keeps_the_order_of_the_sql_text(self):
        # Today's sorted(spec_tags) order: a quoted part sorts before letters
        names = {
            ObjectName("governance.tags.tier"),
            ObjectName("cost_management.tags.team"),
            ObjectName("snowflake.core.privacy_category"),
            ObjectName('"Abc".s.t'),
        }

        assert [str(n) for n in sorted(names)] == [
            '"Abc".s.t',
            '"SNOWFLAKE".core.privacy_category',
            "cost_management.tags.team",
            "governance.tags.tier",
        ]
        assert sorted(names) == sorted(reversed(sorted(names)))

    def test_names_order_against_strings_and_patterns_as_text(self):
        mixed = [ObjectName("raw.public"), "analytics.x", NamePattern("raw.*")]

        assert [str(x) for x in sorted(mixed)] == ["analytics.x", "raw.*", "raw.public"]

    def test_every_comparison(self):
        a, b = ObjectName("raw.a"), ObjectName("raw.b")

        assert a < b and a <= b and b > a and b >= a
        assert a <= ObjectName("RAW.A") and a >= "raw.a"
        assert not a < "raw.a"

    def test_other_types_dont_order(self):
        with pytest.raises(TypeError):
            ObjectName("raw") < 1
        with pytest.raises(TypeError):
            NamePattern("*") > None


class TestValues:
    def test_names_are_immutable(self):
        name = ObjectName("raw.public")

        with pytest.raises(AttributeError):
            name.parts = ("X",)
        with pytest.raises(AttributeError):
            del name.placeholder
        with pytest.raises(AttributeError):
            name.extra = 1

    def test_patterns_are_immutable(self):
        pattern = NamePattern("raw.*")

        with pytest.raises(AttributeError):
            pattern.globs = ()

    @pytest.mark.parametrize(
        "value",
        [
            ObjectName("raw.public"),
            ObjectName('raw."Mixed-Case".<iceberg_table>'),
            ObjectName.single("heidi.bond@example.com"),
            NamePattern("raw.loading_*"),
            NamePattern('raw."my-"*.*'),
            NamePattern("raw.*1234raw"),
            member_or_pattern("snowflake.CORTEX-MODEL-ROLE-*"),
        ],
        ids=str,
    )
    def test_pickle_and_copy(self, value):
        restored = pickle.loads(pickle.dumps(value))

        assert restored == value
        assert type(restored) is type(value)
        assert copy.copy(value) is value
        assert copy.deepcopy({value: [value]}) == {value: [value]}

    def test_unpickled_names_are_interned(self):
        name = ObjectName("raw.public")

        assert pickle.loads(pickle.dumps(name)) is name

    @pytest.mark.parametrize("spelling", ['RAW."Mixed"', 'RAW."Mixed".<TABLE>'])
    def test_repr_shows_the_rendering(self, spelling):
        name = ObjectName(spelling)

        assert repr(name) == f"ObjectName({str(name)!r})"
        assert ObjectName(str(name)) is name


API_NAMES = [
    ("raw", ("RAW",)),
    ("raw.public", ("RAW", "PUBLIC")),
    ("raw.public.events", ("RAW", "PUBLIC", "EVENTS")),
    ("snowflake", ("SNOWFLAKE",)),
    ("raw.group", ("RAW", '"GROUP"')),
    ('raw."Mixed-Case"', ("RAW", '"Mixed-Case"')),
    ('raw."lower_case"', ("RAW", '"lower_case"')),
    ('raw."has ""quote"""', ("RAW", '"has ""quote"""')),
    ('raw."dot.inside".t', ("RAW", '"dot.inside"', "T")),
    ("my-db.public", ('"my-db"', "PUBLIC")),
    (
        'SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
        ("SNOWFLAKE", '"CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"'),
    ),
]


class TestSnowflakeCore:
    @pytest.mark.parametrize("spelling,api_parts", API_NAMES)
    def test_api_parts_and_identifiers(self, spelling, api_parts):
        name = ObjectName(spelling)

        assert name.api_parts == api_parts
        assert all(isinstance(i, Identifier) for i in name.identifiers)
        assert tuple(map(str, name.identifiers)) == api_parts
        assert name.identifiers == tuple(
            Identifier.from_canonical_name(CanonicalName(part)) for part in name.parts
        )

    @pytest.mark.parametrize("spelling,api_parts", API_NAMES)
    def test_api_spellings_resolve_to_the_same_object(self, spelling, api_parts):
        name = ObjectName(spelling)
        resolved = [CanonicalName.resolve_from_identifier(p) for p in name.api_parts]

        assert ObjectName.from_canonical(*resolved) == name

    def test_sql_and_api_spell_one_name_differently(self):
        system = ObjectName.single("snowflake")
        schema = ObjectName("raw.public")

        assert (str(system), system.api_name) == ('"SNOWFLAKE"', "SNOWFLAKE")
        assert (str(schema), schema.api_parts) == ("raw.public", ("RAW", "PUBLIC"))

    @pytest.mark.parametrize(
        "model", [Securable, DatabaseRoleSecurable, UserSecurable], ids=str
    )
    @pytest.mark.parametrize("spelling,api_parts", API_NAMES)
    def test_securables_validate(self, model, spelling, api_parts):
        securable = ObjectName(spelling).to_securable(model)

        assert isinstance(securable, model)
        assert securable.name == api_parts[-1]

    @pytest.mark.parametrize(
        "spelling,expected",
        [
            ("raw", Securable(name="RAW")),
            ("raw.public", Securable(database="RAW", name="PUBLIC")),
            (
                "raw.public.events",
                Securable(database="RAW", schema="PUBLIC", name="EVENTS"),
            ),
            (
                "mydb.db_role_1",
                Securable(database="MYDB", name="DB_ROLE_1"),
            ),
            (
                'SNOWFLAKE."CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"',
                Securable(
                    database="SNOWFLAKE", name='"CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"'
                ),
            ),
            (
                "raw.group.t",
                Securable(database="RAW", schema='"GROUP"', name="T"),
            ),
        ],
    )
    def test_to_securable(self, spelling, expected):
        assert ObjectName(spelling).to_securable() == expected

    def test_an_application_role_reference_becomes_a_securable(self):
        reference = member_or_pattern("snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH")

        assert reference.to_securable() == Securable(
            database="SNOWFLAKE", name='"CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH"'
        )

    @pytest.mark.parametrize(
        "spelling,expected",
        [
            ("raw", ContainingScope(database="RAW")),
            ("raw.public", ContainingScope(database="RAW", schema="PUBLIC")),
            ("raw.<schema>", ContainingScope(database="RAW")),
            ("raw.<table>", ContainingScope(database="RAW")),
            ("raw.public.<table>", ContainingScope(database="RAW", schema="PUBLIC")),
            (
                'raw."Mixed-Case".<iceberg_table>',
                ContainingScope(database="RAW", schema='"Mixed-Case"'),
            ),
            (
                'LAB_RESULTS."AGGREGATE".<TABLE>',
                ContainingScope(database="LAB_RESULTS", schema="AGGREGATE"),
            ),
        ],
    )
    def test_to_containing_scope(self, spelling, expected):
        assert ObjectName(spelling).to_containing_scope() == expected

    def test_containing_scope_model_can_be_chosen(self):
        scope = ObjectName("raw.public").to_containing_scope(DatabaseRoleScope)

        assert isinstance(scope, DatabaseRoleScope)

    def test_shapes_the_models_dont_take_raise(self):
        with pytest.raises(InvalidNameError):
            ObjectName("raw.public.<table>").to_securable()
        with pytest.raises(InvalidNameError):
            ObjectName("raw.public.events").to_containing_scope()

    def test_api_name(self):
        assert ObjectName.single("loader").api_name == "LOADER"
        assert ObjectName.single("gitlab-ci").api_name == '"gitlab-ci"'
        assert Grantees.role(ObjectName.single("loader").api_name).name == "LOADER"
        with pytest.raises(InvalidNameError):
            ObjectName("raw.public").api_name
