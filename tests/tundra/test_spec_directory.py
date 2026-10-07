import os

import pytest

from tundra.entities import EntityGenerator
from tundra.error import SpecLoadingError
from tundra.spec_file_loader import load_spec


SPEC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "specs", "snowflake_spec_directory"
)


def write(base, rel, content):
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def spec():
    return load_spec(SPEC_DIR)


class TestLoadSpecDirectory:
    def test_concatenates_sections_across_nested_files(self, spec):
        assert [next(iter(r)) for r in spec["roles"]] == [
            "securityadmin",
            "useradmin",
            "demo",
        ]
        assert [next(iter(u)) for u in spec["users"]] == ["jane_doe", "airflow_demo"]

    def test_keeps_scalar_settings(self, spec):
        assert spec["version"] == "1.0"
        assert spec["require-owner"] is False

    def test_resolves_includes_inside_fragments(self, spec):
        assert [next(iter(w)) for w in spec["warehouses"]] == ["loading", "demo"]

    def test_generates_entities(self, spec):
        entities = EntityGenerator(spec).generate()
        assert entities["databases"] == {"demo", "shared_demo"}
        assert entities["users"] == {"jane_doe", "airflow_demo"}

    def test_single_file_spec_still_loads(self):
        spec = load_spec(
            os.path.join(os.path.dirname(SPEC_DIR), "snowflake_spec_include_parent.yml")
        )
        assert "roles" in spec

    def test_duplicate_entity_across_files_names_both(self, tmp_path):
        write(tmp_path, "a.yml", "users:\n  - jane:\n      can_login: yes\n")
        write(tmp_path, "b/c.yml", "users:\n  - jane:\n      can_login: no\n")
        with pytest.raises(
            SpecLoadingError, match=r'users "jane" .* a\.yml and b/c\.yml'
        ):
            load_spec(str(tmp_path))

    def test_same_name_in_different_sections_is_fine(self, tmp_path):
        write(tmp_path, "roles.yml", "roles:\n  - demo:\n      warehouses: []\n")
        write(
            tmp_path, "warehouses.yml", "warehouses:\n  - demo:\n      size: x-small\n"
        )
        spec = load_spec(str(tmp_path))
        assert len(spec["roles"]) == len(spec["warehouses"]) == 1

    def test_conflicting_scalar_setting_errors(self, tmp_path):
        write(tmp_path, "a.yml", "require-owner: true\n")
        write(tmp_path, "b.yml", "require-owner: false\n")
        with pytest.raises(SpecLoadingError, match="require-owner"):
            load_spec(str(tmp_path))

    def test_non_mapping_fragment_errors(self, tmp_path):
        write(tmp_path, "roles.yml", "- demo:\n    warehouses: []\n")
        with pytest.raises(SpecLoadingError, match="expected a mapping"):
            load_spec(str(tmp_path))

    def test_empty_fragment_is_skipped(self, tmp_path):
        write(tmp_path, "empty.yml", "")
        write(tmp_path, "w.yml", "warehouses:\n  - demo:\n      size: x-small\n")
        assert len(load_spec(str(tmp_path))["warehouses"]) == 1

    def test_directory_without_yaml_errors(self, tmp_path):
        write(tmp_path, "notes.md", "nothing here")
        with pytest.raises(SpecLoadingError, match="contains no YAML files"):
            load_spec(str(tmp_path))

    def test_merged_spec_is_still_validated(self, tmp_path):
        write(tmp_path, "w.yml", "warehouses:\n  - demo:\n      size: 3\n")
        with pytest.raises(SpecLoadingError, match="size"):
            load_spec(str(tmp_path))
