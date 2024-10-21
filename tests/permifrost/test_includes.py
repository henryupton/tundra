import pytest
import os

from permifrost.entities import EntityGenerator
from permifrost.spec_file_loader import load_spec


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE_DIR = os.path.join(THIS_DIR, "specs")
SCHEMA_FILE_DIR = os.path.join(THIS_DIR, "schemas")


@pytest.fixture
def test_dir(request):
    return request.fspath.dirname


@pytest.fixture
def entities(test_dir):
    spec = load_spec(
        os.path.join(test_dir, "specs", "snowflake_spec_include_parent.yml")
    )
    entities = EntityGenerator(spec).generate()
    yield entities


class TestEntityGenerator:
    def test_entity_databases(self, entities):
        """
        Expect only demo and shared_demo from databases section in
        snowflake_spec_include_parent.yml spec exist, showing
        parent is referenced
        """
        expected = {"demo", "shared_demo"}
        assert entities["databases"] == expected

    def test_entity_roles(self, entities):
        """
        Expect all <roles> from the roles section in
        snowflake_spec_include_child.yml spec exist, showing
        child is referenced
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
