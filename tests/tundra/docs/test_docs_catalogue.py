import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

import tundra
from tundra.docs.catalogue import FamilyRule, SpecSource, build_catalogue
from tundra.error import SpecLoadingError

SPEC = Path(__file__).parents[1] / "specs" / "docs_site"


@pytest.fixture(scope="module")
def catalogue():
    return build_catalogue(SpecSource(str(SPEC)), name="docs_site")


def edges_from(catalogue, source, kind="member", origin="spec"):
    return sorted(
        edge["target"]
        for edge in catalogue["edges"]
        if edge["source"] == source
        and edge["type"] == kind
        and edge["origin"] == origin
    )


def edges_to(catalogue, target, kind="member"):
    return sorted(
        edge["source"]
        for edge in catalogue["edges"]
        if edge["target"] == target and edge["type"] == kind
    )


def edge(catalogue, source, target):
    (found,) = [
        e
        for e in catalogue["edges"]
        if e["source"] == source and e["target"] == target and e["type"] != "owner"
    ]
    return found


class TestInheritance:
    def test_member_of_entries_are_member_edges(self, catalogue):
        assert edges_from(catalogue, "role:loader") == [
            "role:raw.ingest_admin",
            "role:reader",
        ]

    def test_what_inherits_a_role_is_every_member_edge_into_it(self, catalogue):
        assert edges_to(catalogue, "role:reader") == [
            "role:accountadmin",
            "role:analyst",
            "role:loader",
            "role:sysadmin",
            "user:report_bot",
            "user:user_one",
            "user:user_three",
            "user:user_two",
        ]

    def test_a_dotted_member_of_is_a_database_role(self, catalogue):
        node = catalogue["nodes"]["role:raw.ingest_admin"]
        assert node["type"] == "database_role"
        assert node["name"] == "raw.ingest_admin"
        assert node["defined"] is False
        assert node["family"] == "managed"

    def test_a_role_only_referenced_is_a_node_the_spec_does_not_define(self, catalogue):
        assert edges_from(catalogue, "role:useradmin") == ["role:bridge_provisioner"]
        assert catalogue["nodes"]["role:bridge_provisioner"]["defined"] is False
        assert catalogue["nodes"]["role:useradmin"]["defined"] is True

    def test_snowflake_wires_the_system_roles_together(self, catalogue):
        builtin = sorted(
            (e["source"], e["target"])
            for e in catalogue["edges"]
            if e["origin"] == "builtin"
        )
        assert builtin == [
            ("role:accountadmin", "role:securityadmin"),
            ("role:accountadmin", "role:sysadmin"),
            ("role:securityadmin", "role:useradmin"),
        ]


class TestStarExpansion:
    def test_star_is_every_other_spec_role_bar_snowflakes_default_roles(
        self, catalogue
    ):
        assert edges_from(catalogue, "role:accountadmin") == [
            "role:analyst",
            "role:loader",
            "role:reader",
        ]

    def test_star_leaves_out_what_exclude_lists(self, catalogue):
        assert edges_from(catalogue, "role:sysadmin") == [
            "role:analyst",
            "role:reader",
        ]

    def test_membership_between_default_roles_is_not_planned(self, catalogue):
        assert edges_from(catalogue, "role:securityadmin") == []


class TestPrivileges:
    def test_privileges_are_grouped_by_object_type(self, catalogue):
        loader = catalogue["nodes"]["role:loader"]
        assert loader["privileges"] == {
            "databases": {
                "write": [{"name": "raw", "database": "database:raw", "pattern": False}]
            },
            "schemas": {
                "write": [
                    {"name": "raw.*", "database": "database:raw", "pattern": True}
                ]
            },
            "tables": {
                "write": [
                    {"name": "raw.*.*", "database": "database:raw", "pattern": True}
                ]
            },
            "tasks": {
                "write": [
                    {"name": "raw.ops.*", "database": "database:raw", "pattern": True}
                ]
            },
        }
        assert loader["warehouses"] == ["warehouse:loading"]
        assert loader["integrations"] == ["integration:s3_landing"]
        assert loader["external_volumes"] == []
        assert loader["account_privileges"] == ["execute task"]

    def test_wildcard_entries_keep_their_spelling_and_database(self, catalogue):
        reader = catalogue["nodes"]["role:reader"]["privileges"]
        assert reader["schemas"]["read"] == [
            {
                "name": "analytics.mart_*",
                "database": "database:analytics",
                "pattern": True,
            },
            {"name": "raw.public", "database": "database:raw", "pattern": False},
        ]
        assert reader["tables"]["read"] == [
            {
                "name": "analytics.mart_*.*",
                "database": "database:analytics",
                "pattern": True,
            },
            {"name": "raw.public.events", "database": "database:raw", "pattern": False},
        ]

    def test_owns_entries_are_listed(self, catalogue):
        assert catalogue["nodes"]["role:analyst"]["owns"] == {
            "schemas": [
                {
                    "name": "analytics.scratch",
                    "database": "database:analytics",
                    "pattern": False,
                }
            ]
        }

    def test_usage_edges_carry_the_privileges_run_grants(self, catalogue):
        assert edge(catalogue, "role:loader", "warehouse:loading")["privileges"] == [
            "usage",
            "operate",
            "monitor",
        ]
        assert edge(catalogue, "role:loader", "database:raw")["privileges"] == [
            "usage",
            "monitor",
            "create schema",
        ]
        assert edge(catalogue, "role:reader", "database:partner_feed")[
            "privileges"
        ] == ["imported privileges"]
        assert edge(catalogue, "role:loader", "integration:s3_landing")[
            "privileges"
        ] == ["usage"]

    def test_owners_are_owner_edges(self, catalogue):
        owners = sorted(
            (e["source"], e["target"])
            for e in catalogue["edges"]
            if e["type"] == "owner"
        )
        assert owners == [
            ("role:securityadmin", "role:loader"),
            ("role:sysadmin", "database:raw"),
        ]


class TestUsers:
    def test_service_user(self, catalogue):
        bot = catalogue["nodes"]["user:etl_bot"]
        assert bot["family"] == "service"
        assert bot["comment"] == "Runs the nightly load."
        assert bot["default_role"] == "role:loader"
        assert bot["default_warehouse"] == "warehouse:loading"
        assert bot["can_login"] is True

    def test_person(self, catalogue):
        person = catalogue["nodes"]["user:user_three"]
        assert person["family"] == "people"
        assert person["can_login"] is False
        assert person["default_role"] is None

    def test_personal_details_stay_out(self, catalogue):
        dumped = json.dumps(catalogue)
        assert "user.one@example.com" not in dumped
        assert '"first_name"' not in dumped

    def test_people_are_grouped_by_the_exact_roles_they_hold(self, catalogue):
        groups = [(g["roles"], g["users"]) for g in catalogue["user_groups"]]
        assert groups == [
            (["role:analyst", "role:reader"], ["user:user_one", "user:user_two"]),
            (["role:reader"], ["user:user_three"]),
        ]
        assert [g["id"] for g in catalogue["user_groups"]] == ["group:1", "group:2"]


class TestFamilies:
    def test_families_by_kind(self, catalogue):
        nodes = catalogue["nodes"]
        assert nodes["role:accountadmin"]["family"] == "system"
        assert nodes["role:loader"]["family"] == "functional"
        assert nodes["warehouse:loading"]["family"] == "warehouse"
        assert nodes["database:raw"]["family"] == "database"
        assert nodes["integration:s3_landing"]["family"] == "integration"

    def test_present_families_are_listed_in_key_order(self, catalogue):
        assert [f["id"] for f in catalogue["families"]] == [
            "people",
            "service",
            "system",
            "functional",
            "managed",
            "warehouse",
            "database",
            "integration",
        ]

    def test_custom_families_take_the_first_matching_rule(self):
        catalogue = build_catalogue(
            SpecSource(str(SPEC)),
            name="docs_site",
            families=[
                FamilyRule.parse("Loading roles=load*"),
                FamilyRule.parse("Other roles=*"),
            ],
        )
        nodes = catalogue["nodes"]
        assert nodes["role:loader"]["family"] == "loading-roles"
        assert nodes["role:reader"]["family"] == "other-roles"
        assert nodes["role:accountadmin"]["family"] == "system"
        assert nodes["role:raw.ingest_admin"]["family"] == "managed"
        labels = {f["id"]: f["label"] for f in catalogue["families"]}
        assert labels["loading-roles"] == "Loading roles"
        assert "functional" not in labels

    @pytest.mark.parametrize("rule", ["no equals sign", "=load*", "Loaders="])
    def test_a_family_rule_needs_a_label_and_a_pattern(self, rule):
        with pytest.raises(ValueError):
            FamilyRule.parse(rule)


class TestShape:
    def test_top_level_keys(self, catalogue):
        assert set(catalogue) == {
            "metadata",
            "families",
            "nodes",
            "edges",
            "user_groups",
        }

    def test_metadata(self, catalogue):
        metadata = catalogue["metadata"]
        assert metadata["catalogue_version"] == 1
        assert metadata["tundra_version"] == tundra.__version__
        assert metadata["spec"] == "docs_site"
        assert metadata["live"] is False
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", metadata["generated_at"]
        )
        datetime.strptime(metadata["generated_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_counts_give_each_kind_one_definition(self, catalogue):
        # Roles are account roles, defined or only referenced. Snowflake's database
        # and application roles are counted on their own.
        assert catalogue["metadata"]["counts"] == {
            "roles": 8,
            "managed_roles": 1,
            "users": 5,
            "people": 3,
            "service_users": 2,
            "warehouses": 3,
            "databases": 3,
            "integrations": 1,
            "external_volumes": 0,
        }

    def test_counts_match_the_nodes_the_site_lists(self, catalogue):
        types = Counter(node["type"] for node in catalogue["nodes"].values())
        counts = catalogue["metadata"]["counts"]
        assert counts["roles"] == types["role"]
        assert (
            counts["managed_roles"]
            == types["database_role"] + types["application_role"]
        )
        assert counts["users"] == types["user"]
        assert counts["people"] + counts["service_users"] == counts["users"]

    def test_every_node_is_keyed_by_its_id(self, catalogue):
        family_ids = {f["id"] for f in catalogue["families"]}
        for key, node in catalogue["nodes"].items():
            assert node["id"] == key
            assert key.split(":", 1)[0] in {
                "role",
                "user",
                "warehouse",
                "database",
                "integration",
                "external_volume",
            }
            assert node["family"] in family_ids
            assert isinstance(node["name"], str)

    def test_every_edge_joins_two_nodes(self, catalogue):
        for e in catalogue["edges"]:
            assert e["source"] in catalogue["nodes"]
            assert e["target"] in catalogue["nodes"]
            assert e["type"] in {"member", "usage", "owner"}
            assert e["origin"] in {"spec", "builtin"}
            assert e["status"] is None

    def test_nodes_come_in_kind_then_name_order(self, catalogue):
        assert list(catalogue["nodes"])[:4] == [
            "role:accountadmin",
            "role:analyst",
            "role:bridge_provisioner",
            "role:loader",
        ]

    def test_names_keep_the_specs_spelling(self, tmp_path):
        spec = tmp_path / "spec.yml"
        spec.write_text(
            "roles:\n"
            "  - '\"Etl-Loader\"':\n"
            "      member_of: [Reporter]\n"
            "  - Reporter:\n"
            "      warehouses: [Loading]\n"
            "warehouses:\n"
            "  - Loading:\n"
            "      size: x-small\n"
        )
        catalogue = build_catalogue(SpecSource(str(spec)), name="spec")
        names = {key: node["name"] for key, node in catalogue["nodes"].items()}
        assert names == {
            'role:"Etl-Loader"': '"Etl-Loader"',
            "role:reporter": "Reporter",
            "warehouse:loading": "Loading",
        }

    def test_round_trips_through_json(self, catalogue):
        assert json.loads(json.dumps(catalogue)) == catalogue

    def test_a_spec_that_fails_tundras_checks_fails_here(self, tmp_path):
        spec = tmp_path / "spec.yml"
        spec.write_text("roles:\n  - reader:\n      warehouses: [missing_wh]\n")
        with pytest.raises(SpecLoadingError):
            SpecSource(str(spec))
