from pathlib import Path
from types import SimpleNamespace

import pytest

from tundra.docs.catalogue import build_catalogue
from tundra.docs.live import ReadOnlyConnector, ReadOnlyError, load_live
from tundra.entities import EntityGenerator
from tundra.identifiers import ObjectName
from tundra.operations import GRANT, Privileges, SqlStatement
from tundra.snowflake_connector import SnowflakeConnector
from tundra.spec_file_loader import load_spec

SPEC = Path(__file__).parents[1] / "specs" / "docs_site"

# What SHOW GRANTS found, filed the way SnowflakeSpecLoader files it
GRANTS_TO_ROLE = {
    "loader": {
        "usage": {
            "role": ["reader", "stale_role"],
            "warehouse": ["loading"],
            "database": ["raw"],
        },
        "operate": {"warehouse": ["loading"]},
        "monitor": {"database": ["raw"]},
        "create schema": {"database": ["raw"]},
    },
    "reader": {
        "usage": {
            "warehouse": ["reporting", "admin_wh"],
            "database": ["raw", "partner_feed"],
        }
    },
    "accountadmin": {"usage": {"role": ["loader", "reader"]}},
}
ROLES_GRANTED_TO_USER = {
    "etl_bot": ["loader"],
    "report_bot": ["reader"],
    "user_one": ["analyst"],
    "user_two": ["reader", "analyst"],
    "user_three": ["reader", "loader"],
}
# analyst hasn't been created yet
ROLES_ON_SNOWFLAKE = [
    "accountadmin",
    "securityadmin",
    "sysadmin",
    "useradmin",
    "loader",
    "reader",
    "stale_role",
]


class StubConnector:
    """Answers SHOW ROLES and refuses anything that could change the account."""

    def show_roles(self):
        return {ObjectName.single(role): "securityadmin" for role in ROLES_ON_SNOWFLAKE}

    def execute(self, *args, **kwargs):
        raise AssertionError("the docs never apply a statement")

    def run_query(self, *args, **kwargs):
        raise AssertionError("the docs only read what the loader already read")


def stub_loader():
    """What SnowflakeSpecLoader holds once it has read the account."""
    spec = load_spec(str(SPEC))
    return SimpleNamespace(
        spec=spec,
        entities=EntityGenerator(spec=spec).inspect_entities(),
        conn=StubConnector(),
        grants_to_role={
            ObjectName.single(role): grants for role, grants in GRANTS_TO_ROLE.items()
        },
        roles_granted_to_user={
            ObjectName.single(user): set(map(ObjectName.single, roles))
            for user, roles in ROLES_GRANTED_TO_USER.items()
        },
        missing_entities={"roles": {ObjectName.single("analyst")}},
    )


@pytest.fixture(scope="module")
def catalogue():
    return build_catalogue(stub_loader(), name="docs_site", live=True)


def status(catalogue, source, target):
    (found,) = [
        e
        for e in catalogue["edges"]
        if e["source"] == source and e["target"] == target and e["type"] != "owner"
    ]
    return found["status"], found.get("missing", []), found.get("extra", [])


@pytest.mark.parametrize(
    "source, target, expected",
    [
        ("role:loader", "role:reader", "both"),
        ("role:loader", "role:raw.ingest_admin", "spec_only"),
        ("role:loader", "role:stale_role", "live_only"),
        ("role:loader", "database:raw", "both"),
        ("role:loader", "integration:s3_landing", "spec_only"),
        ("role:reader", "database:analytics", "spec_only"),
        ("role:reader", "database:partner_feed", "both"),
        ("role:reader", "warehouse:admin_wh", "live_only"),
        ("role:securityadmin", "warehouse:admin_wh", "spec_only"),
        ("user:etl_bot", "role:loader", "both"),
        ("user:user_one", "role:analyst", "both"),
        ("user:user_one", "role:reader", "spec_only"),
        ("user:user_three", "role:loader", "live_only"),
    ],
)
def test_each_edge_is_in_both_spec_only_or_live_only(
    catalogue, source, target, expected
):
    assert status(catalogue, source, target)[0] == expected


def test_an_edge_held_in_part_is_live_and_names_what_is_missing(catalogue):
    assert status(catalogue, "role:loader", "warehouse:loading") == (
        "both",
        ["monitor"],
        [],
    )


def test_a_live_only_edge_names_the_privileges_held(catalogue):
    assert status(catalogue, "role:reader", "warehouse:admin_wh") == (
        "live_only",
        [],
        ["usage"],
    )


def test_a_role_only_snowflake_has_is_a_node_the_spec_does_not_define(catalogue):
    node = catalogue["nodes"]["role:stale_role"]
    assert node["defined"] is False
    assert node["name"] == "stale_role"


def test_star_expands_against_the_roles_on_snowflake_as_run_does(catalogue):
    expanded = sorted(
        (e["target"], e["status"])
        for e in catalogue["edges"]
        if e["source"] == "role:accountadmin" and e["origin"] != "builtin"
    )
    assert expanded == [("role:loader", "both"), ("role:reader", "both")]


def test_spec_objects_missing_from_snowflake_are_flagged(catalogue):
    assert catalogue["nodes"]["role:analyst"]["missing"] is True
    assert catalogue["nodes"]["role:reader"]["missing"] is False


def test_builtin_and_owner_edges_are_not_compared(catalogue):
    builtin = [e["status"] for e in catalogue["edges"] if e["origin"] == "builtin"]
    owners = [e["status"] for e in catalogue["edges"] if e["type"] == "owner"]
    assert builtin == [None, None, None]
    assert owners == [None, None]


def test_every_other_edge_is_classified(catalogue):
    for e in catalogue["edges"]:
        if e["origin"] != "builtin" and e["type"] != "owner":
            assert e["status"] in {"both", "spec_only", "live_only"}
            assert e["origin"] == ("live" if e["status"] == "live_only" else "spec")


def test_people_are_grouped_by_the_roles_drawn_for_them(catalogue):
    groups = [(g["roles"], g["users"]) for g in catalogue["user_groups"]]
    assert groups == [
        (["role:analyst", "role:reader"], ["user:user_one", "user:user_two"]),
        (["role:loader", "role:reader"], ["user:user_three"]),
    ]


def test_metadata_says_it_is_live(catalogue):
    assert catalogue["metadata"]["live"] is True


class TestReadOnlyConnector:
    def test_refuses_to_apply_a_statement(self):
        conn = ReadOnlyConnector()
        operation = Privileges(
            GRANT,
            ("usage",),
            "warehouse",
            ObjectName.single("loading"),
            ObjectName.single("loader"),
        )
        with pytest.raises(ReadOnlyError):
            conn.execute(operation)
        with pytest.raises(ReadOnlyError):
            conn.execute(SqlStatement("REVOKE ROLE reader FROM ROLE loader"))

    @pytest.mark.parametrize(
        "query",
        [
            "GRANT ROLE reader TO ROLE loader",
            "revoke usage on warehouse loading from role loader",
            "  ALTER USER etl_bot SET DEFAULT_ROLE = loader",
            "CREATE ROLE spare",
        ],
    )
    def test_refuses_sql_that_is_not_a_read(self, mocker, query):
        run_query = mocker.patch.object(SnowflakeConnector, "run_query")
        with pytest.raises(ReadOnlyError):
            ReadOnlyConnector().run_query(query)
        run_query.assert_not_called()

    @pytest.mark.parametrize(
        "query",
        ["SHOW GRANTS TO ROLE loader", "select current_role() as role"],
    )
    def test_passes_reads_through(self, mocker, query):
        run_query = mocker.patch.object(
            SnowflakeConnector, "run_query", return_value="rows"
        )
        assert ReadOnlyConnector().run_query(query) == "rows"
        run_query.assert_called_once_with(query)


def test_load_live_reads_through_a_read_only_connector(mocker):
    loader = mocker.patch("tundra.docs.live.SnowflakeSpecLoader")

    assert load_live("spec.yml", sql_only=True) is loader.return_value

    (spec_path,), kwargs = loader.call_args
    assert spec_path == "spec.yml"
    assert isinstance(kwargs["conn"], ReadOnlyConnector)
    assert kwargs["sql_only"] is True
    assert kwargs["ignore_missing_objects"] is True
