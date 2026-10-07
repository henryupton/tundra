import os
from functools import lru_cache
from typing import Any, Dict, List, Set

import cerberus
import yaml

from tundra.error import SpecLoadingError
from tundra.spec_schemas.snowflake import (
    SNOWFLAKE_SPEC_DATABASE_SCHEMA,
    SNOWFLAKE_SPEC_INTEGRATION_SCHEMA,
    SNOWFLAKE_SPEC_ROLE_SCHEMA,
    SNOWFLAKE_SPEC_SCHEMA,
    SNOWFLAKE_SPEC_USER_SCHEMA,
    SNOWFLAKE_SPEC_WAREHOUSE_SCHEMA,
    SNOWFLAKE_SPEC_EXTERNAL_VOLUME_SCHEMA,
)
from tundra.types import TundraSpecSchema

VALIDATION_ERR_MSG = 'Spec error: {} "{}", field "{}": {}'


@lru_cache(maxsize=None)
def iana_timezones() -> Set[str]:
    """IANA zone names known to this interpreter; empty when zoneinfo or tzdata is unavailable."""
    try:
        from zoneinfo import available_timezones
    except ImportError:  # Python 3.8
        return set()
    return available_timezones()


class SpecValidator(cerberus.Validator):
    """cerberus Validator plus the custom `check_with` rules the spec schemas name."""

    def _check_with_iana_timezone(self, field: str, value: str) -> None:
        # Snowflake only accepts IANA zone names, so fail the spec load rather than the ALTER USER.
        zones = iana_timezones()
        if not zones:
            self._error(
                field,
                f"cannot validate '{value}': no IANA time zone database is available "
                "(install the tzdata package)",
            )
        elif value not in zones:
            self._error(field, f"'{value}' is not an IANA time zone name")


def construct_include(loader, node) -> Any:
    """Include file referenced at node."""
    filename = os.path.abspath(
        os.path.join(
            os.path.split(loader.stream.name)[0], loader.construct_scalar(node)
        )
    )
    extension = os.path.splitext(filename)[1].lstrip(".")
    with open(filename, "r") as f:
        if extension in ("yaml", "yml"):
            return yaml.safe_load(f)


yaml.SafeLoader.add_constructor("!include", construct_include)


def ensure_valid_schema(spec: Dict) -> List[str]:
    """
    Ensure that the provided spec has no schema errors.

    Returns a list with all the errors found.
    """
    error_messages = []

    validator = cerberus.Validator(yaml.safe_load(SNOWFLAKE_SPEC_SCHEMA))
    validator.validate(spec)
    for entity_type, err_msg in validator.errors.items():
        if isinstance(err_msg[0], str):
            error_messages.append(f"Spec error: {entity_type}: {err_msg[0]}")
            continue

        for error in err_msg[0].values():
            error_messages.append(f"Spec error: {entity_type}: {error[0]}")
    if error_messages:
        return error_messages

    schema = {
        "databases": yaml.safe_load(SNOWFLAKE_SPEC_DATABASE_SCHEMA),
        "roles": yaml.safe_load(SNOWFLAKE_SPEC_ROLE_SCHEMA),
        "users": yaml.safe_load(SNOWFLAKE_SPEC_USER_SCHEMA),
        "warehouses": yaml.safe_load(SNOWFLAKE_SPEC_WAREHOUSE_SCHEMA),
        "integrations": yaml.safe_load(SNOWFLAKE_SPEC_INTEGRATION_SCHEMA),
        "external_volumes": yaml.safe_load(SNOWFLAKE_SPEC_EXTERNAL_VOLUME_SCHEMA),
    }

    validators = {
        "databases": SpecValidator(schema["databases"]),
        "roles": SpecValidator(schema["roles"]),
        "users": SpecValidator(schema["users"]),
        "warehouses": SpecValidator(schema["warehouses"]),
        "integrations": SpecValidator(schema["integrations"]),
        "external_volumes": SpecValidator(schema["external_volumes"]),
    }

    entities_by_type = []
    for entity_type, entities in spec.items():
        if entities and entity_type in [
            "databases",
            "roles",
            "users",
            "warehouses",
            "integrations",
            "external_volumes",
        ]:
            entities_by_type.append((entity_type, entities))

    for entity_type, entities in entities_by_type:
        for entity_dict in entities:
            for entity_name, config in entity_dict.items():
                validators[entity_type].validate(config)
                for field, err_msg in validators[entity_type].errors.items():
                    error_messages.append(
                        VALIDATION_ERR_MSG.format(
                            entity_type, entity_name, field, err_msg[0]
                        )
                    )

    return error_messages


def _spec_files_in(spec_dir: str) -> List[str]:
    """Every YAML file under spec_dir, recursively, in sorted path order.

    Dotfiles and dot-dirs are skipped.
    """
    found: List[str] = []
    for root, dirs, files in os.walk(spec_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        found.extend(
            os.path.join(root, f)
            for f in files
            if not f.startswith(".") and f.endswith((".yml", ".yaml"))
        )
    return sorted(found)


def load_spec_directory(spec_dir: str) -> Dict[str, Any]:
    """
    Merge every YAML file under spec_dir into one spec.

    Each file is a mapping of top-level spec keys. List sections (roles, users,
    databases, ...) are concatenated across files; an entity defined in more than
    one file is an error. Scalar settings (version, require-owner) must agree
    wherever they are set.
    """
    files = _spec_files_in(spec_dir)
    if not files:
        raise SpecLoadingError(f"Spec directory {spec_dir} contains no YAML files")

    spec: Dict[str, Any] = {}
    scalar_sources: Dict[str, str] = {}
    entity_sources: Dict[tuple, str] = {}
    errors = []

    for path in files:
        rel = os.path.relpath(path, spec_dir)
        with open(path, "r") as stream:
            fragment = yaml.safe_load(stream)
        if fragment is None:
            continue
        if not isinstance(fragment, dict):
            errors.append(
                f"Spec error: {rel}: expected a mapping of spec sections, "
                f"got {type(fragment).__name__}"
            )
            continue

        for key, value in fragment.items():
            if isinstance(value, list):
                for entity in value:
                    if isinstance(entity, dict) and len(entity) == 1:
                        name = next(iter(entity))
                        first = entity_sources.setdefault((key, name), rel)
                        if first != rel:
                            errors.append(
                                f'Spec error: {key} "{name}" is defined in both '
                                f"{first} and {rel}"
                            )
                spec.setdefault(key, []).extend(value)
            elif value is None:
                spec.setdefault(key, None)
            elif key in spec and spec[key] != value:
                errors.append(
                    f'Spec error: "{key}" is {spec[key]!r} in {scalar_sources[key]} '
                    f"but {value!r} in {rel}"
                )
            else:
                spec[key] = value
                scalar_sources.setdefault(key, rel)

    if errors:
        raise SpecLoadingError("\n".join(errors))
    return spec


def load_spec(spec_path: str) -> TundraSpecSchema:
    """
    Load a permissions specification from a file, or from a directory of spec
    fragments (see load_spec_directory).

    If the file is not found or at least an error is found during validation,
    raise a SpecLoadingError with the appropriate error messages.

    Otherwise, return the valid specification as a Dictionary to be used
    in other operations. Remove meta data if any is present.

    Raises a SpecLoadingError with all the errors found in the spec if at
    least one error is found.

    Returns the spec as a dictionary if everything is OK
    """
    spec: Any
    if os.path.isdir(spec_path):
        spec = load_spec_directory(spec_path)
    else:
        try:
            with open(spec_path, "r") as stream:
                spec = yaml.safe_load(stream)
        except FileNotFoundError:
            raise SpecLoadingError(f"Spec File {spec_path} not found")

    error_messages = ensure_valid_schema(spec)
    if error_messages:
        raise SpecLoadingError("\n".join(error_messages))

    for entity_type, entities in spec.items():
        if entities and entity_type in [
            "databases",
            "roles",
            "users",
            "warehouses",
            "integrations",
            "external_volumes",
        ]:
            for entity in entities:
                entity.pop("meta", None)

    return spec
