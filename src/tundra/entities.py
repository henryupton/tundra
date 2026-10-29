from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    TypedDict,
    Union,
)

from tundra.account_privileges import (
    ACCOUNTADMIN_ONLY_PRIVILEGES,
    normalise_account_privilege,
)
from tundra.error import SpecLoadingError
from tundra.identifiers import (
    InvalidNameError,
    NamePattern,
    ObjectName,
    member_or_pattern,
    name_or_pattern,
)
from tundra.logger import GLOBAL_LOGGER as logger
from tundra.table_object_types import PRIVILEGE_FAMILIES
from tundra.types import TundraSpecSchema

SpecEntry = Union[ObjectName, NamePattern]


class EntitySchema(TypedDict):
    databases: Set[ObjectName]
    database_refs: Set[ObjectName]
    shared_databases: Set[ObjectName]
    schema_refs: Set[SpecEntry]
    table_refs: Set[SpecEntry]
    tables_by_database: Dict[ObjectName, Set[SpecEntry]]
    task_refs: Set[SpecEntry]
    tasks_by_database: Dict[ObjectName, Set[SpecEntry]]
    account_privilege_roles: Set[ObjectName]
    roles: Set[SpecEntry]
    role_refs: Set[SpecEntry]
    database_role_refs: Set[ObjectName]
    application_role_refs: Set[ObjectName]
    users: Set[ObjectName]
    warehouses: Set[ObjectName]
    warehouse_refs: Set[ObjectName]
    integrations: Set[ObjectName]
    integration_refs: Set[ObjectName]
    external_volumes: Set[ObjectName]
    external_volume_refs: Set[ObjectName]
    require_owner: bool


def _database_names(spellings: Iterable[str]) -> Set[ObjectName]:
    names = set()
    for spelling in spellings:
        try:
            names.add(ObjectName.single(spelling))
        except InvalidNameError:
            continue
    return names


def _has_no_database(entry: SpecEntry) -> bool:
    return isinstance(entry, NamePattern) and entry.prefix is None


def _namespace(spelling: str) -> ObjectName:
    """A DEFAULT_NAMESPACE, which names a database or schema, never future grants."""
    name = ObjectName(spelling)
    if name.is_future:
        raise InvalidNameError(f"{spelling!r} is a future-grant scope, not a namespace")
    return name


def _tag(spelling: str) -> ObjectName:
    return ObjectName(spelling, arity=3)


class EntityGenerator:
    def __init__(self, spec: TundraSpecSchema):
        self.spec = spec
        self.entities: EntitySchema = {
            "databases": set(),
            "database_refs": set(),
            "shared_databases": set(),
            "schema_refs": set(),
            "table_refs": set(),
            "tables_by_database": dict(),
            "task_refs": set(),
            "tasks_by_database": dict(),
            "account_privilege_roles": set(),
            "roles": set(),
            "role_refs": set(),
            "database_role_refs": set(),
            "application_role_refs": set(),
            "users": set(),
            "warehouses": set(),
            "warehouse_refs": set(),
            "integrations": set(),
            "integration_refs": set(),
            "external_volumes": set(),
            "external_volume_refs": set(),
            "require_owner": False,
        }
        self.error_messages: List[str] = []
        self._name_errors: List[str] = []
        self._definitions: Dict[Tuple[str, ObjectName], str] = {}

    def _name(
        self, construct: Callable[[str], Any], spelling: str, message: str
    ) -> Optional[Any]:
        """<spelling> read with <construct>, or None once <message> is recorded."""
        try:
            return construct(spelling)
        except InvalidNameError:
            self._name_errors.append(message)
            return None

    def _single(self, spelling: str, noun: str) -> Optional[ObjectName]:
        return self._name(
            ObjectName.single,
            spelling,
            f"Name error: Not a valid {noun} name: {spelling}",
        )

    def _define(self, spelling: str, noun: str) -> Optional[ObjectName]:
        """
        The <noun> a spec section defines as <spelling>. Every spelling of one name is
        the same object, so a later entry for it is an error: planned on its own, it
        would revoke what the earlier entry grants.
        """
        name = self._single(spelling, noun)
        if name is None:
            return None
        first = self._definitions.get((noun, name))
        if first is None:
            self._definitions[(noun, name)] = spelling
            return name
        message = f"Spec error: {noun} {spelling} is defined more than once"
        if first != spelling:
            message += f" (first as {first})"
        if message not in self.error_messages:
            self.error_messages.append(message)
        return name

    def _schema_entry(self, spelling: str) -> Optional[SpecEntry]:
        return self._name(
            lambda s: name_or_pattern(s, arity=2),
            spelling,
            f"Name error: Not a valid schema name: {spelling}"
            " (Proper definition: DB.[SCHEMA | *])",
        )

    def _object_entry(self, spelling: str, noun: str) -> Optional[SpecEntry]:
        return self._name(
            lambda s: name_or_pattern(s, arity=3),
            spelling,
            f"Name error: Not a valid {noun} name: {spelling}"
            f" (Proper definition: DB.[SCHEMA | *].[{noun.upper()} | *])",
        )

    def inspect_entities(self) -> EntitySchema:
        """
        Inspect a valid spec and make sure that no logic errors exist.

        e.g. a role granted to a user not defined in roles
             or a user given access to a database not defined in databases
        """
        self.generate()
        self.error_messages.extend(self.ensure_valid_entity_names(self.entities))

        self.error_messages.extend(
            self.ensure_valid_spec_for_conditional_settings(self.entities)
        )

        self.error_messages.extend(self.ensure_valid_references(self.entities))

        if self.error_messages:
            raise SpecLoadingError("\n".join(self.error_messages))

        return self.entities

    @staticmethod
    def filter_grouped_entities_by_type(grouped_entities: List, type: str) -> List:
        """
        Takes a list of grouped entities and filters them for a particular entity_type

        For example:
            filter_grouped_entities_by_type(grouped_entities, 'role') ->
                {'accountadmin', 'demo', 'securityadmin', 'sysadmin', 'useradmin'}

        """
        filtered_entities = [
            entries for entity_type, entries in grouped_entities if entity_type == type
        ]

        # Avoid returning a nested list if there are entities
        if not filtered_entities:
            return filtered_entities
        else:
            return filtered_entities[0]

    def group_table_by_database(self):
        self.entities["tables_by_database"] = self._group_refs_by_database(
            self.entities["table_refs"]
        )
        self.entities["tasks_by_database"] = self._group_refs_by_database(
            self.entities["task_refs"]
        )

    @staticmethod
    def _group_refs_by_database(
        refs: Iterable[SpecEntry],
    ) -> Dict[ObjectName, Set[SpecEntry]]:
        by_database: Dict[ObjectName, Set[SpecEntry]] = {}
        for ref in refs:
            if not _has_no_database(ref):
                by_database.setdefault(ref.database, set()).add(ref)
        return by_database

    @staticmethod
    def group_spec_by_type(spec: TundraSpecSchema) -> List[Tuple[str, Any]]:
        """
        Takes as input the Tundra Spec and converts it to a grouped list
        For example:
            [('databases',
                [{'demo': {'shared': False}}]),
            ('roles',
                [
                  {'accountadmin':
                    {'warehouses': ['loading'],
                     'member_of': ['sysadmin', 'securityadmin']}},
                  {'securityadmin':
                    {'warehouses': ['loading'],
                    'member_of': ['useradmin']}},
                ]
            ...
            ]

        """
        entities_by_type = [
            (entity_type, entry)
            for entity_type, entry in spec.items()
            if entry and entity_type != "version"
        ]
        return entities_by_type

    def generate(self) -> EntitySchema:
        """
        Generate and return a dictionary with all the entities defined or
        referenced in the permissions specification file.

        The xxx_refs entities are referenced by various permissions.
        For example:
        'roles' --> All the roles defined in the spec
        'role_refs' --> All the roles referenced in a member_of permission
        'table_refs' --> All the tables referenced in read/write privileges
                         or in owns entries

        Returns a tuple (entities, error_messages) with all the entities defined
        in the spec and any errors found (e.g. a user not assigned their user role)
        """
        entities_by_type = self.group_spec_by_type(self.spec)
        self.generate_roles(
            self.filter_grouped_entities_by_type(entities_by_type, "roles")
        )
        self.generate_databases(
            self.filter_grouped_entities_by_type(entities_by_type, "databases")
        )
        self.generate_warehouses(
            self.filter_grouped_entities_by_type(entities_by_type, "warehouses")
        )
        self.generate_integrations(
            self.filter_grouped_entities_by_type(entities_by_type, "integrations")
        )
        self.generate_external_volumes(
            self.filter_grouped_entities_by_type(entities_by_type, "external_volumes")
        )
        self.generate_users(
            self.filter_grouped_entities_by_type(entities_by_type, "users")
        )

        # Filter the owner requirement and set it to True or False
        require_owner = [
            entry
            for entity_type, entry in entities_by_type
            if entity_type == "require-owner"
        ]
        self.entities["require_owner"] = require_owner == [True]

        self.generate_implicit_refs_from_schemas()
        self.generate_implicit_refs_from_tables()
        self.group_table_by_database()
        # Add implicit references to DBs and Schemas.
        #  e.g. RAW.MYSCHEMA.TABLE references also DB RAW and Schema MYSCHEMA

        return self.entities

    def generate_implicit_refs_from_schemas(self):
        """Adds implicit database refs from schemas"""
        for schema in self.entities["schema_refs"]:
            if not _has_no_database(schema):
                self.entities["database_refs"].add(schema.database)

    def generate_implicit_refs_from_tables(self):
        """Adds implicit db/schema refs from tables and tasks"""
        for table in self.entities["table_refs"] | self.entities["task_refs"]:
            if _has_no_database(table):
                continue
            self.entities["database_refs"].add(table.database)

            schema = table.parent
            if not (isinstance(schema, NamePattern) and schema.globs == ("*",)):
                self.entities["schema_refs"].add(schema)

    def ensure_valid_entity_names(self, entities: EntitySchema) -> List[str]:
        """
        Check that all entity names are valid.

        Returns a list with all the errors found.
        """
        error_messages = list(dict.fromkeys(self._name_errors))

        # A period in a role name is what marks it as a database role reference,
        # so roles defined in the spec must not contain one.
        for role_entry in self.spec.get("roles") or []:
            for role in role_entry:
                if "." in role:
                    error_messages.append(
                        f"Name error: Not a valid role name: {role}"
                        " (Proper definition: ROLE. Periods are reserved for"
                        " referencing database roles as DB.ROLE in member_of)"
                    )

        for schema in sorted(entities["schema_refs"]):
            if _has_no_database(schema):
                error_messages.append(
                    f"Name error: Not a valid schema name: {schema!s}"
                    " (Proper definition: DB.[SCHEMA | *])"
                )

        for noun, refs_key in (("table", "table_refs"), ("task", "task_refs")):
            for table in sorted(entities[refs_key]):  # type: ignore
                if _has_no_database(table):
                    error_messages.append(
                        f"Name error: Not a valid {noun} name: {table!s}"
                        f" (Proper definition: DB.[SCHEMA | *].[{noun.upper()} | *])"
                    )
                elif (
                    isinstance(table, NamePattern)
                    and len(table.globs) == 2
                    and table.globs[0] == "*"
                    and table.globs[1] != "*"
                ):
                    error_messages.append(
                        f"Name error: Not a valid {noun} name: {table!s}"
                        f" (Can't have a {noun.capitalize()} name after selecting all"
                        f" schemas with *: DB.SCHEMA.[{noun.upper()} | *])"
                    )

        return error_messages

    @staticmethod
    def ensure_valid_references(entities: EntitySchema) -> List[str]:
        """
        Make sure that all references are well defined.

        Returns a list with all the errors found.
        """
        error_messages = []

        # Check that all the referenced entities are also defined
        for database in entities["database_refs"]:
            if database not in entities["databases"]:
                error_messages.append(
                    f"Reference error: Database {database!s} is referenced "
                    "in the spec but not defined"
                )

        for role in entities["role_refs"]:
            if role not in entities["roles"] and role != "*":
                error_messages.append(
                    f"Reference error: Role {role!s} is referenced in the "
                    "spec but not defined"
                )

        for warehouse in entities["warehouse_refs"]:
            if warehouse not in entities["warehouses"]:
                error_messages.append(
                    f"Reference error: Warehouse {warehouse!s} is referenced "
                    "in the spec but not defined"
                )

        for integration in entities["integration_refs"]:
            if integration not in entities["integrations"]:
                error_messages.append(
                    f"Reference error: Integration {integration!s} is referenced "
                    "in the spec but not defined"
                )

        for external_volume in entities["external_volume_refs"]:
            if external_volume not in entities["external_volumes"]:
                error_messages.append(
                    f"Reference error: External Volume {external_volume!s} is referenced "
                    "in the spec but not defined"
                )

        return error_messages

    def ensure_valid_spec_for_conditional_settings(
        self, entities: EntitySchema
    ) -> List[str]:
        """
        Make sure that the spec is valid based on conditional settings such as require-owner
        """
        error_messages = []

        if entities["require_owner"]:
            error_messages.extend(self.check_entities_define_owner())

        return error_messages

    def check_entities_define_owner(self) -> List[str]:
        error_messages = []

        entities_by_type = [
            (entity_type, entry)
            for entity_type, entry in self.spec.items()
            if entry
            and entity_type
            in [
                "databases",
                "roles",
                "users",
                "warehouses",
                "integrations",
                "external_volumes",
            ]
        ]

        for entity_type, entry in entities_by_type:
            for entity_dict in entry:  # type: ignore
                for entity_name, config in entity_dict.items():
                    if "owner" not in config.keys():
                        error_messages.append(
                            f"Spec Error: Owner not defined for {entity_type} {entity_name} and require-owner is set!"
                        )

        return error_messages

    def generate_warehouses(self, warehouse_list: List[Dict[str, Dict]]) -> None:
        for warehouse_entry in warehouse_list:
            for warehouse_name, _ in warehouse_entry.items():
                warehouse = self._define(warehouse_name, "warehouse")
                if warehouse is not None:
                    self.entities["warehouses"].add(warehouse)

    def generate_integrations(self, integration_list: List[Dict[str, Dict]]) -> None:
        for integration_entry in integration_list:
            for integration_name, _ in integration_entry.items():
                integration = self._define(integration_name, "integration")
                if integration is not None:
                    self.entities["integrations"].add(integration)

    def generate_external_volumes(
        self, external_volume_list: List[Dict[str, Dict]]
    ) -> None:
        for external_volume_entry in external_volume_list:
            for external_volume_name, _ in external_volume_entry.items():
                external_volume = self._define(external_volume_name, "external volume")
                if external_volume is not None:
                    self.entities["external_volumes"].add(external_volume)

    def generate_databases(self, db_list: List[Dict[str, Dict]]) -> None:
        for db_entry in db_list:
            for db_name, config in db_entry.items():
                database = self._define(db_name, "database")
                if database is None:
                    continue
                self.entities["databases"].add(database)
                if "shared" in config:
                    if isinstance(config["shared"], bool):
                        if config["shared"]:
                            self.entities["shared_databases"].add(database)
                    else:
                        logger.debug(
                            "`shared` for database {} must be boolean, skipping Role Reference generation.".format(
                                db_name
                            )
                        )

    def _member_entry(self, member_role: str, alone: bool) -> Optional[SpecEntry]:
        """
        A member_of entry, or None once its error is recorded. The only pattern
        member_of takes is a * on its own, standing for every role in the spec.
        """
        member = self._name(
            member_or_pattern,
            member_role,
            f"Name error: Not a valid role name: {member_role}",
        )
        if isinstance(member, NamePattern) and not (alone and member == "*"):
            self._name_errors.append(
                f"Name error: Not a valid role name: {member_role}"
                " (A * in member_of has to be the only role included)"
            )
            return None
        return member

    def add_member_of_ref(self, member_role, alone: bool = True):
        """
        Route a member_of entry to the right reference set. A DB.ROLE name is a
        database role reference, everything else is an account role. The spec loader
        later moves the DB.ROLE refs that Snowflake reports as application roles into
        application_role_refs; the spec spells both the same.
        """
        member = self._member_entry(member_role, alone)
        if member is None:
            return
        if isinstance(member, ObjectName) and len(member.parts) == 2:
            self.entities["database_role_refs"].add(member)
        else:
            self.entities["roles"].add(member)

    def generate_member_of_roles(self, config, role_name):
        try:
            if isinstance(config["member_of"], dict):
                include = config["member_of"].get("include", [])
                for member_role in include:
                    self.add_member_of_ref(member_role, alone=len(include) == 1)
                for member_role in config["member_of"].get("exclude", []):
                    self.add_member_of_ref(member_role, alone=False)

            if isinstance(config["member_of"], list):
                for member_role in config["member_of"]:
                    self.add_member_of_ref(
                        member_role, alone=len(config["member_of"]) == 1
                    )
        except KeyError:
            logger.debug(
                "`member_of` not found for role {}, skipping Role Reference generation.".format(
                    role_name
                )
            )

    def generate_warehouse_roles(self, config, role_name):
        try:
            for warehouse_name in config["warehouses"]:
                warehouse = self._single(warehouse_name, "warehouse")
                if warehouse is not None:
                    self.entities["warehouse_refs"].add(warehouse)
        except KeyError:
            logger.debug(
                "`warehouses` not found for role {}, skipping Warehouse Reference generation.".format(
                    role_name
                )
            )

    def generate_integration_roles(self, config, role_name):
        try:
            for integration_name in config["integrations"]:
                integration = self._single(integration_name, "integration")
                if integration is not None:
                    self.entities["integration_refs"].add(integration)
        except KeyError:
            logger.debug(
                "`Integration` not found for role {}, skipping Integration Reference generation.".format(
                    role_name
                )
            )

    def generate_external_volume_roles(self, config, role_name):
        try:
            for external_volume_name in config["external_volumes"]:
                external_volume = self._single(external_volume_name, "external volume")
                if external_volume is not None:
                    self.entities["external_volume_refs"].add(external_volume)
        except KeyError:
            logger.debug(
                "`external_volumes` not found for role {}, skipping External Volume Reference generation.".format(
                    role_name
                )
            )

    def _add_database_refs(self, spellings: Iterable[str]) -> None:
        for spelling in spellings:
            database = self._single(spelling, "database")
            if database is not None:
                self.entities["database_refs"].add(database)

    def generate_database_roles(self, config, role_name):
        try:
            self._add_database_refs(config["privileges"]["databases"]["read"])
        except KeyError:
            logger.debug(
                "`privileges.databases.read` not found for role {}, skipping Database Reference generation.".format(
                    role_name
                )
            )
        try:
            self._add_database_refs(config["privileges"]["databases"]["write"])
        except KeyError:
            logger.debug(
                "`privileges.databases.write` not found for role {}, skipping Database Reference generation.".format(
                    role_name
                )
            )

    @staticmethod
    def generate_read_write_database_names(
        config,
    ) -> Tuple[Set[ObjectName], Set[ObjectName]]:
        databases = config.get("privileges", {}).get("databases", {})
        return (
            _database_names(databases.get("read", [])),
            _database_names(databases.get("write", [])),
        )

    def generate_schema_roles(self, config, role_name):
        read_databases, write_databases = self.generate_read_write_database_names(
            config
        )

        try:
            for spelling in config["privileges"]["schemas"]["read"]:
                schema = self._schema_entry(spelling)
                if schema is None:
                    continue
                self.entities["schema_refs"].add(schema)
                if not _has_no_database(schema) and (
                    schema.database not in read_databases
                ):
                    self.error_messages.append(
                        f"Privilege Error: Database {schema.database} referenced in "
                        "schema read privileges but not in database privileges "
                        f"for role {role_name}"
                    )
        except KeyError:
            logger.debug(
                "`privileges.schemas.read` not found for role {}, skipping Schema Reference generation.".format(
                    role_name
                )
            )

        try:
            for spelling in config["privileges"]["schemas"]["write"]:
                schema = self._schema_entry(spelling)
                if schema is None:
                    continue
                self.entities["schema_refs"].add(schema)
                if not _has_no_database(schema) and (
                    schema.database not in write_databases | read_databases
                ):
                    self.error_messages.append(
                        f"Privilege Error: Database {schema.database} referenced in "
                        "schema write privileges but not in database privileges "
                        f"for role {role_name}"
                    )
        except KeyError:
            logger.debug(
                "`privileges.schemas.write` not found for role {}, skipping Schema Reference generation.".format(
                    role_name
                )
            )

    def generate_table_roles(self, config, role_name):
        self.generate_object_family_roles(config, role_name, family="tables")

    def generate_object_family_roles(self, config, role_name, family: str):
        """
        Collect the `db.schema.object` references of one registry-backed
        `privileges` family (`tables`, `tasks`) and check each names a database
        the role also holds database privileges on.
        """
        noun = family[:-1]
        refs_key = f"{noun}_refs"
        read_databases, write_databases = self.generate_read_write_database_names(
            config
        )

        for access, allowed_databases in (
            ("read", read_databases),
            ("write", write_databases | read_databases),
        ):
            try:
                for spelling in config["privileges"][family][access]:
                    ref = self._object_entry(spelling, noun)
                    if ref is None:
                        continue
                    self.entities[refs_key].add(ref)  # type: ignore
                    if not _has_no_database(ref) and (
                        ref.database not in allowed_databases
                    ):
                        self.error_messages.append(
                            f"Privilege Error: Database {ref.database} referenced in "
                            f"{noun} {access} privileges but not in database privileges "
                            f"for role {role_name}"
                        )
            except KeyError:
                logger.debug(
                    "`privileges.{}.{}` not found for role {}, skipping {} Reference generation.".format(
                        family, access, role_name, noun.capitalize()
                    )
                )

    def generate_account_privilege_roles(self, config, role_name):
        """
        Validate a role's `account_privileges`. Names are normalised the way SHOW
        GRANTS keys them; the ACCOUNTADMIN-only privileges are rejected here, at
        load time, rather than as a partially executed GRANT at apply time.
        """
        try:
            account_privileges = config["account_privileges"]
        except KeyError:
            logger.debug(
                "`account_privileges` not found for role {}, skipping account privilege validation.".format(
                    role_name
                )
            )
            return

        # Declaring the key, even as an empty list, opts the role into reconciliation.
        role = self._single(role_name, "role")
        if role is not None:
            self.entities["account_privilege_roles"].add(role)

        for privilege in account_privileges:
            normalised = normalise_account_privilege(privilege)
            if not normalised:
                self.error_messages.append(
                    f"Spec Error: Empty account privilege for role {role_name}"
                )
            elif normalised in ACCOUNTADMIN_ONLY_PRIVILEGES:
                self.error_messages.append(
                    f"Spec Error: Account privilege {normalised} for role {role_name} "
                    "can only be granted by ACCOUNTADMIN, and tundra runs as "
                    "SECURITYADMIN. Grant it outside the spec."
                )

    def generate_ownership_roles(self, config, role_name):
        try:
            self._add_database_refs(config["owns"]["databases"])
        except KeyError:
            logger.debug(
                "`owns.databases` not found for role {}, skipping Database Reference generation.".format(
                    role_name
                )
            )

        try:
            for spelling in config["owns"]["schemas"]:
                schema = self._schema_entry(spelling)
                if schema is not None:
                    self.entities["schema_refs"].add(schema)
        except KeyError:
            logger.debug(
                "`owns.schemas` not found for role {}, skipping Schema Reference generation.".format(
                    role_name
                )
            )

        try:
            for spelling in config["owns"]["tables"]:
                table = self._object_entry(spelling, "table")
                if table is not None:
                    self.entities["table_refs"].add(table)
        except KeyError:
            logger.debug(
                "`owns.tables` not found for role {}, skipping Table Reference generation.".format(
                    role_name
                )
            )

    def generate_roles(self, role_list):  # noqa
        """
        Generate all the role entities.
        Also, can populate the role_refs, database_refs,
        schema_refs, table_refs, warehouse_refs & integration_refs
        """

        for role_entry in role_list:
            for role_name, config in role_entry.items():
                role = self._define(role_name, "role")
                if role is not None:
                    self.entities["roles"].add(role)
                self.generate_member_of_roles(config, role_name)
                self.generate_warehouse_roles(config, role_name)
                self.generate_integration_roles(config, role_name)
                self.generate_external_volume_roles(config, role_name)
                self.generate_account_privilege_roles(config, role_name)
                self.generate_database_roles(config, role_name)
                self.generate_schema_roles(config, role_name)
                for family in PRIVILEGE_FAMILIES:
                    self.generate_object_family_roles(config, role_name, family)
                self.generate_ownership_roles(config, role_name)

    def generate_user_fn(self, config, key, ref, user_name):
        if key in config:
            for item in config[key]:
                if ref == "schema_refs":
                    entry = self._schema_entry(item)
                elif ref == "table_refs":
                    entry = self._object_entry(item, "table")
                else:
                    entry = self._single(item, "database")
                if entry is not None:
                    self.entities[ref].add(entry)
        else:
            logger.debug(
                "`{}` not found for user {}, skipping Role Reference generation.".format(
                    key, user_name
                )
            )

    def generate_user_member_of(self, config, user_name):
        if "member_of" not in config:
            logger.debug(
                "`member_of` not found for user {}, skipping Role Reference generation.".format(
                    user_name
                )
            )
            return

        for member_role in config["member_of"]:
            member = self._member_entry(
                member_role, alone=len(config["member_of"]) == 1
            )
            if member is None:
                continue
            # Snowflake only allows database roles to be granted to other roles
            if isinstance(member, ObjectName) and len(member.parts) == 2:
                self.error_messages.append(
                    f"Reference error: Database role {member_role} is granted to user "
                    f"{user_name}, but database roles can only be granted to roles"
                )
            else:
                self.entities["role_refs"].add(member)

    def generate_user_properties(self, config):
        """
        Parse the names in a user's properties and tags. They aren't references, but a
        malformed one is a Name error here rather than a failure while planning.
        """
        for key, noun in (("default_role", "role"), ("default_warehouse", "warehouse")):
            if key in config:
                self._single(config[key], noun)

        if "default_namespace" in config:
            namespace = config["default_namespace"]
            self._name(
                _namespace,
                namespace,
                f"Name error: Not a valid namespace name: {namespace}",
            )

        for tag in config.get("tags", {}):
            self._name(
                _tag,
                tag,
                f"Name error: Not a valid tag name: {tag}"
                " (Proper definition: DB.SCHEMA.TAG)",
            )

    def generate_users(self, user_list):
        """
        Generate all the user entities.
        Also, can populate the role_refs, database_refs, schema_refs & table_refs
        """

        for user_entry in user_list:
            for user_name, config in user_entry.items():
                user = self._define(user_name, "user")
                if user is not None:
                    self.entities["users"].add(user)
                self.generate_user_member_of(config, user_name)
                self.generate_user_properties(config)
                self.generate_user_fn(
                    config.get("owns", {}), "database", "database_refs", user_name
                )
                self.generate_user_fn(
                    config.get("owns", {}), "schemas", "schema_refs", user_name
                )
                self.generate_user_fn(
                    config.get("owns", {}), "tables", "table_refs", user_name
                )
