import pytest
import os

from tundra.entities import EntityGenerator
from tundra.error import SpecLoadingError
from tundra.spec_file_loader import load_spec

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE_DIR = os.path.join(THIS_DIR, "specs")
SCHEMA_FILE_DIR = os.path.join(THIS_DIR, "schemas")


@pytest.fixture
def test_dir(request):
    return request.fspath.dirname


@pytest.fixture
def entities(test_dir):
    spec = load_spec(
        os.path.join(test_dir, "specs", "snowflake_spec_reference_roles.yml")
    )
    entities = EntityGenerator(spec).generate()
    yield entities


class TestEntityGenerator:
    def test_entity_databases(self, entities):
        """
        Expect only demo and shared_demo from databases section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"demo", "shared_demo"}
        assert entities["databases"] == expected

    def test_entity_require_owner(self, entities):
        assert entities["require_owner"] is True

    def test_db_refs(self, entities):
        """
        Expect all actionable <database> references from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"demodb", "demodb2", "demodb3", "demodb4", "demodb5", "demodb6"}
        assert entities["database_refs"] == expected

    def test_schema_refs(self, entities):
        """
        Expect all <database>.<schema> references from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {
            "demodb.*",
            "demodb2.*",
            "demodb3.read_only_schema",
            "demodb4.write_schema",
            "demodb5.demo_schema",
            "demodb6.demo_schema",
        }
        assert entities["schema_refs"] == expected

    def test_table_refs(self, entities):
        """
        Expect all <database>.<schema>.<table> references from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {
            "demodb6.demo_schema.demo_table",
            "demodb.*.*",
            "demodb5.demo_schema.demo_table",
            "demodb5.demo_schema.demo_table_2",
            "demodb2.*.*",
        }
        assert entities["table_refs"] == expected

    def test_tables_by_database(self, entities):
        """
        Expect all <database>.<schema>.<table> references from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {
            "demodb6": {"demodb6.demo_schema.demo_table"},
            "demodb": {"demodb.*.*"},
            "demodb5": {
                "demodb5.demo_schema.demo_table_2",
                "demodb5.demo_schema.demo_table",
            },
            "demodb2": {"demodb2.*.*"},
        }
        assert entities["tables_by_database"] == expected

    def test_entity_roles(self, entities):
        """
        Expect all <roles> from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {
            "*",
            "accountadmin",
            "demo",
            "securityadmin",
            "sysadmin",
            "useradmin",
        }
        assert entities["roles"] == expected

    def test_entity_role_refs(self, entities):
        """
        Expect all actionable <roles> from the roles section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"demo"}
        assert entities["role_refs"] == expected

    def test_entity_users(self, entities):
        """
        Expect all actionable <users> from the users section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"airflow_demo", "dbt_demo"}
        assert entities["users"] == expected

    def test_entity_warehouses(self, entities):
        """
        Expect all actionable <warehouses> from the warehouses section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"demo", "loading", "transforming", "reporting"}
        assert entities["warehouses"] == expected

    def test_entity_integrations(self, entities):
        """
        Expect all actionable <integrations> from the integrations section in
        snowflake_spec_reference_roles.yml spec
        """
        expected = {"demo"}
        assert entities["integrations"] == expected


class TestDatabaseRoleEntities:
    def test_database_role_refs(self):
        """
        A member_of entry containing a period is a database role reference, not an
        account role
        """
        spec = {
            "roles": [
                {"role_1": {"member_of": ["role_2", "mydb.db_role_1"]}},
                {"role_2": {}},
            ]
        }

        entities = EntityGenerator(spec).inspect_entities()

        assert entities["database_role_refs"] == {"mydb.db_role_1"}
        assert entities["roles"] == {"role_1", "role_2"}

    def test_database_role_refs_from_include_exclude(self):
        spec = {
            "roles": [
                {
                    "role_1": {
                        "member_of": {
                            "include": ["mydb.db_role_1"],
                            "exclude": ["mydb.db_role_2"],
                        }
                    }
                },
            ]
        }

        entities = EntityGenerator(spec).inspect_entities()

        assert entities["database_role_refs"] == {"mydb.db_role_1", "mydb.db_role_2"}

    def test_role_name_with_period_is_rejected(self):
        spec = {"roles": [{"mydb.db_role_1": {}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert "Not a valid role name: mydb.db_role_1" in str(context.value)

    def test_dotted_database_role_tail_is_one_role_name(self):
        """
        Only the first unquoted dot splits a member_of entry, as application role
        names can have dots in them, so the rest is one role name. Checking the spec
        against Snowflake reports it missing.
        """
        spec = {"roles": [{"role_1": {"member_of": ["mydb.myschema.db_role_1"]}}]}

        entities = EntityGenerator(spec).inspect_entities()

        assert entities["database_role_refs"] == {'mydb."myschema.db_role_1"'}

    def test_database_role_granted_to_user_is_rejected(self):
        spec = {
            "roles": [{"role_1": {}}],
            "users": [{"user_1": {"member_of": ["mydb.db_role_1"]}}],
        }

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert "Database role mydb.db_role_1 is granted to user user_1" in str(
            context.value
        )


def test_filter_by_type(entities):
    expected = {"demo", "sysadmin", "accountadmin", "useradmin", "securityadmin", "*"}
    grouped_entities = EntityGenerator.group_spec_by_type(entities)
    assert (
        EntityGenerator.filter_grouped_entities_by_type(grouped_entities, "roles")
        == expected
    )


def test_application_role_refs_start_empty():
    """
    The entity generator cannot tell an application role from a database role by
    its spelling; the spec loader moves refs across after asking Snowflake.
    """
    spec = {"roles": [{"role_1": {"member_of": ["mydb.db_role_1"]}}]}

    entities = EntityGenerator(spec).inspect_entities()

    assert entities["database_role_refs"] == {"mydb.db_role_1"}
    assert entities["application_role_refs"] == set()


class TestNameErrors:
    def test_unparsable_names_are_reported_once_in_spec_order(self):
        """
        Every spec name is parsed at load, so a malformed one is a Name error here
        rather than a failure while planning. A wildcard schema or table entry with
        no database reports only its Name error.
        """
        spec = {
            "databases": [{'bad"db': {}}, {"raw": {}}],
            "roles": [
                {
                    "role_1": {
                        "privileges": {
                            "databases": {"read": ['bad"db', "raw"]},
                            "schemas": {"read": ["raw.a.b", "*.public"]},
                            "tables": {
                                "read": ["raw.public", "*.s.t", "raw.public.events"]
                            },
                        }
                    }
                }
            ],
            "users": [
                {
                    "user_1": {
                        "owns": {
                            "database": ['bad"db'],
                            "schemas": ['raw."x', "raw.public"],
                            "tables": ["raw.x", "raw.public.events"],
                        }
                    }
                }
            ],
        }

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value).splitlines() == [
            'Name error: Not a valid database name: bad"db',
            "Name error: Not a valid schema name: raw.a.b"
            " (Proper definition: DB.[SCHEMA | *])",
            "Name error: Not a valid table name: raw.public"
            " (Proper definition: DB.[SCHEMA | *].[TABLE | *])",
            'Name error: Not a valid schema name: raw."x'
            " (Proper definition: DB.[SCHEMA | *])",
            "Name error: Not a valid table name: raw.x"
            " (Proper definition: DB.[SCHEMA | *].[TABLE | *])",
            "Name error: Not a valid schema name: *.public"
            " (Proper definition: DB.[SCHEMA | *])",
            "Name error: Not a valid table name: *.s.t"
            " (Proper definition: DB.[SCHEMA | *].[TABLE | *])",
        ]

    def test_user_owns_entries_are_references(self):
        spec = {
            "databases": [{"raw": {}}],
            "users": [
                {
                    "user_1": {
                        "owns": {
                            "database": ["raw"],
                            "schemas": ['raw."Mixed-Case"'],
                            "tables": ["raw.public.*"],
                        }
                    }
                }
            ],
        }

        entities = EntityGenerator(spec).inspect_entities()

        assert entities["database_refs"] == {"raw"}
        assert entities["schema_refs"] == {'raw."Mixed-Case"', "raw.public"}
        assert entities["table_refs"] == {"raw.public.*"}

    @pytest.mark.parametrize(
        "properties,error",
        [
            ({"default_role": 'my"role'}, 'Not a valid role name: my"role'),
            ({"default_role": ""}, "Not a valid role name: "),
            ({"default_warehouse": '"wh'}, 'Not a valid warehouse name: "wh'),
            ({"default_namespace": "raw."}, "Not a valid namespace name: raw."),
            (
                {"default_namespace": "raw.public.events.extra"},
                "Not a valid namespace name: raw.public.events.extra",
            ),
            (
                {"default_namespace": "raw.<table>"},
                "Not a valid namespace name: raw.<table>",
            ),
            (
                {"tags": {'gov.tags."x': "a"}},
                'Not a valid tag name: gov.tags."x (Proper definition: DB.SCHEMA.TAG)',
            ),
            (
                {"tags": {"gov.tags.te*m": "a"}},
                "Not a valid tag name: gov.tags.te*m"
                " (Proper definition: DB.SCHEMA.TAG)",
            ),
            (
                {"tags": {"gov.tags.<table>": "a"}},
                "Not a valid tag name: gov.tags.<table>"
                " (Proper definition: DB.SCHEMA.TAG)",
            ),
        ],
        ids=[
            "role_stray_quote",
            "role_empty",
            "warehouse_unterminated_quote",
            "namespace_empty_part",
            "namespace_four_parts",
            "namespace_future_scope",
            "tag_unterminated_quote",
            "tag_wildcard",
            "tag_future_scope",
        ],
    )
    def test_malformed_user_property_names_are_name_errors(self, properties, error):
        spec = {"users": [{"user_1": {"can_login": True, **properties}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value) == f"Name error: {error}"

    def test_user_property_names_that_parse_pass(self):
        spec = {
            "users": [
                {
                    "user_1": {
                        "can_login": True,
                        "default_role": "gitlab-ci",
                        "default_warehouse": "etl.wh",
                        "default_namespace": 'raw."Mixed-Case"',
                        "default_secondary_roles": ["all"],
                        "tags": {"gov.tags.team": "data"},
                    }
                }
            ]
        }

        EntityGenerator(spec).inspect_entities()


class TestMemberOfPatterns:
    @pytest.mark.parametrize(
        "member_of",
        [["*"], {"include": ["*"]}, {"include": ["*"], "exclude": ["role_2"]}],
    )
    def test_a_lone_star_stands_for_every_role(self, member_of):
        spec = {"roles": [{"role_1": {"member_of": member_of}}, {"role_2": {}}]}

        entities = EntityGenerator(spec).inspect_entities()

        assert "*" in entities["roles"]

    @pytest.mark.parametrize(
        "member_of,entry",
        [
            (["*", "role_2"], "*"),
            ({"include": ["*", "role_2"]}, "*"),
            ({"include": ["role_2"], "exclude": ["*"]}, "*"),
            ({"include": ["*"], "exclude": ["*"]}, "*"),
            (["role_*"], "role_*"),
            (["mydb.*"], "mydb.*"),
        ],
    )
    def test_any_other_pattern_is_a_name_error(self, member_of, entry):
        spec = {"roles": [{"role_1": {"member_of": member_of}}, {"role_2": {}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value) == (
            f"Name error: Not a valid role name: {entry}"
            " (A * in member_of has to be the only role included)"
        )

    def test_a_user_member_of_pattern_is_a_name_error(self):
        spec = {
            "roles": [{"role_1": {}}],
            "users": [{"user_1": {"member_of": ["*", "role_1"]}}],
        }

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value) == (
            "Name error: Not a valid role name: *"
            " (A * in member_of has to be the only role included)"
        )


class TestDuplicateEntities:
    @pytest.mark.parametrize(
        "section,noun",
        [
            ("roles", "role"),
            ("users", "user"),
            ("databases", "database"),
            ("warehouses", "warehouse"),
            ("integrations", "integration"),
            ("external_volumes", "external volume"),
        ],
    )
    def test_spellings_of_one_name_are_one_entity_defined_twice(self, section, noun):
        """
        loader, LOADER and "LOADER" are one object, so each entry after the first is a
        second definition of it, not a second entity planned against the same grants.
        """
        spec = {section: [{"loader": {}}, {"LOADER": {}}, {'"LOADER"': {}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value).splitlines() == [
            f"Spec error: {noun} LOADER is defined more than once (first as loader)",
            f'Spec error: {noun} "LOADER" is defined more than once (first as loader)',
        ]

    def test_the_same_spelling_twice_is_reported_once(self):
        spec = {"roles": [{"loader": {}}, {"loader": {}}, {"loader": {}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value) == "Spec error: role loader is defined more than once"

    def test_case_sensitive_spellings_are_different_entities(self):
        spec = {"roles": [{"loader": {}}, {'"loader"': {}}, {"Loader": {}}]}

        with pytest.raises(SpecLoadingError) as context:
            EntityGenerator(spec).inspect_entities()

        assert str(context.value) == (
            "Spec error: role Loader is defined more than once (first as loader)"
        )

    def test_one_name_in_different_sections_is_fine(self):
        spec = {
            "warehouses": [{"demo": {}}],
            "databases": [{"DEMO": {}}],
            "roles": [{"Demo": {}}],
            "users": [{'"DEMO"': {}}],
        }

        entities = EntityGenerator(spec).inspect_entities()

        assert entities["roles"] == entities["users"] == {"demo"}
