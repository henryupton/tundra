import pytest

from tundra.error import SpecLoadingError
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra.spec_file_loader import load_spec
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector
from tundra_test_utils.snowflake_schema_builder import SnowflakeSchemaBuilder

TEAM = "cost_management.tags.team"


@pytest.fixture
def spec_file():
    yield (
        SnowflakeSchemaBuilder()
        .add_user(name="tagged", tags={TEAM: "marketing"})
        .add_user(name="untagged")
        .add_user(name="multi", tags={TEAM: "finance", "governance.tags.tier": "gold"})
        .build()
    )


def connector(mocker, live_tags):
    mock_connector = MockSnowflakeConnector()
    mocker.patch.object(
        mock_connector, "show_users", return_value=["tagged", "untagged", "multi"]
    )
    mocker.patch.object(
        mock_connector,
        "show_tags_on_user",
        side_effect=lambda user: live_tags.get(user, {}),
    )
    return mock_connector


def tag_queries(mocker, spec_file, user, live_tags):
    mocker.patch("builtins.open", mocker.mock_open(read_data=spec_file))
    spec_loader = SnowflakeSpecLoader(spec_path="", conn=connector(mocker, live_tags))
    queries = spec_loader.generate_permission_queries(users=[user], run_list=["users"])
    return [q for q in queries if " TAG " in q["sql"]]


class TestUserTags:
    def test_new_tag_is_set(self, mocker, spec_file):
        assert tag_queries(mocker, spec_file, "tagged", {}) == [
            {
                "already_granted": False,
                "sql": f"ALTER USER tagged SET TAG {TEAM} = 'marketing'",
            }
        ]

    def test_matching_tag_is_already_granted(self, mocker, spec_file):
        live = {"tagged": {TEAM: "marketing"}}
        assert tag_queries(mocker, spec_file, "tagged", live) == [
            {
                "already_granted": True,
                "sql": f"ALTER USER tagged SET TAG {TEAM} = 'marketing'",
            }
        ]

    def test_changed_value_is_set(self, mocker, spec_file):
        live = {"tagged": {TEAM: "finance"}}
        assert tag_queries(mocker, spec_file, "tagged", live) == [
            {
                "already_granted": False,
                "sql": f"ALTER USER tagged SET TAG {TEAM} = 'marketing'",
            }
        ]

    def test_tag_not_in_spec_is_unset(self, mocker, spec_file):
        live = {"tagged": {TEAM: "marketing", "governance.tags.tier": "gold"}}
        assert tag_queries(mocker, spec_file, "tagged", live) == [
            {
                "already_granted": True,
                "sql": f"ALTER USER tagged SET TAG {TEAM} = 'marketing'",
            },
            {
                "already_granted": False,
                "sql": "ALTER USER tagged UNSET TAG governance.tags.tier",
            },
        ]

    def test_user_without_tags_key_has_live_tags_unset(self, mocker, spec_file):
        live = {"untagged": {TEAM: "marketing"}}
        assert tag_queries(mocker, spec_file, "untagged", live) == [
            {"already_granted": False, "sql": f"ALTER USER untagged UNSET TAG {TEAM}"}
        ]

    def test_user_without_tags_emits_nothing(self, mocker, spec_file):
        assert tag_queries(mocker, spec_file, "untagged", {}) == []

    def test_tags_are_emitted_in_name_order(self, mocker, spec_file):
        assert [q["sql"] for q in tag_queries(mocker, spec_file, "multi", {})] == [
            f"ALTER USER multi SET TAG {TEAM} = 'finance'",
            "ALTER USER multi SET TAG governance.tags.tier = 'gold'",
        ]

    def test_tag_names_compare_case_insensitively(self, mocker):
        spec = SnowflakeSchemaBuilder().add_user(
            name="tagged", tags={"COST_MANAGEMENT.TAGS.TEAM": "marketing"}
        )
        live = {"tagged": {TEAM: "marketing"}}
        assert tag_queries(mocker, spec.build(), "tagged", live) == [
            {
                "already_granted": True,
                "sql": f"ALTER USER tagged SET TAG {TEAM} = 'marketing'",
            }
        ]

    def test_value_quotes_are_escaped(self, mocker):
        spec = SnowflakeSchemaBuilder().add_user(
            name="tagged", tags={TEAM: '"o\'neill"'}
        )
        assert [q["sql"] for q in tag_queries(mocker, spec.build(), "tagged", {})] == [
            f"ALTER USER tagged SET TAG {TEAM} = 'o''neill'"
        ]

    def test_ignore_memberships_skips_tags(self, mocker, spec_file):
        mocker.patch("builtins.open", mocker.mock_open(read_data=spec_file))
        spec_loader = SnowflakeSpecLoader(
            spec_path="",
            conn=connector(mocker, {"tagged": {"governance.tags.tier": "gold"}}),
            ignore_memberships=True,
        )
        queries = spec_loader.generate_permission_queries(
            users=["tagged"], run_list=["users"], ignore_memberships=True
        )
        assert [q for q in queries if " TAG " in q["sql"]] == []


class TestUserTagsSchema:
    def test_tag_must_be_fully_qualified(self, tmp_path):
        spec = tmp_path / "spec.yml"
        spec.write_text(
            "users:\n  - u:\n      can_login: yes\n      tags:\n        team: x\n"
        )
        with pytest.raises(SpecLoadingError, match="tags"):
            load_spec(str(spec))

    def test_tag_value_must_be_a_string(self, tmp_path):
        spec = tmp_path / "spec.yml"
        spec.write_text(
            "users:\n  - u:\n      can_login: yes\n      tags:\n        a.b.c: 3\n"
        )
        with pytest.raises(SpecLoadingError, match="tags"):
            load_spec(str(spec))


class TestShowTagsOnUser:
    def test_reads_direct_tags_from_tag_references(self, mocker):
        mocker.patch("sqlalchemy.create_engine")
        conn = SnowflakeConnector()
        conn.run_query = mocker.MagicMock()
        mocker.patch.object(
            conn.run_query(),
            "fetchall",
            return_value=[
                {
                    "tag_database": "COST_MANAGEMENT",
                    "tag_schema": "TAGS",
                    "tag_name": "TEAM",
                    "tag_value": "marketing",
                },
                {
                    "tag_database": "SNOWFLAKE",
                    "tag_schema": "CORE",
                    "tag_name": "Mixed-Case",
                    "tag_value": "x",
                },
            ],
        )

        tags = conn.show_tags_on_user("blake.enyart@gmail.com")

        conn.run_query.assert_has_calls(
            [
                mocker.call(
                    "SELECT tag_database, tag_schema, tag_name, tag_value FROM TABLE("
                    "SNOWFLAKE.INFORMATION_SCHEMA.TAG_REFERENCES("
                    "'\"blake.enyart@gmail.com\"', 'USER')) WHERE level = domain"
                )
            ]
        )
        assert tags == {
            TEAM: "marketing",
            '"SNOWFLAKE".core."Mixed-Case"': "x",
        }

    def test_snowflaky_tag_matches_stored_form(self):
        assert SnowflakeConnector.snowflaky_tag(TEAM) == TEAM
        assert (
            SnowflakeConnector.snowflaky_tag("snowflake.core.privacy_category")
            == '"SNOWFLAKE".core.privacy_category'
        )
