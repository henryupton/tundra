from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

from tundra.account_privileges import (
    ACCOUNTADMIN_ONLY_PRIVILEGES,
    normalise_account_privilege,
)
from tundra.grant_coverage import GrantCoverage
from tundra.identifiers import (
    InvalidNameError,
    NameLike,
    NamePattern,
    ObjectName,
    granted_name,
    member_or_pattern,
    name_or_pattern,
)
from tundra.logger import GLOBAL_LOGGER as logger
from tundra.snowflake_connector import (
    APPLICATION_ROLE_GRANT_TYPE,
    DATABASE_ROLE_GRANT_TYPE,
    EXTERNAL_VOLUME_GRANT_TYPE,
    SnowflakeConnector,
)
from tundra.table_object_types import (
    PRIVILEGE_FAMILIES,
    TABLE_OBJECT_TYPES,
    TableObjectType,
)

GRANT_ROLE_TEMPLATE = "GRANT ROLE {role_name} TO {type} {entity_name}"

GRANT_DATABASE_ROLE_TEMPLATE = "GRANT DATABASE ROLE {role_name} TO ROLE {entity_name}"

REVOKE_ROLE_TEMPLATE = "REVOKE ROLE {role_name} FROM {type} {entity_name}"

REVOKE_DATABASE_ROLE_TEMPLATE = (
    "REVOKE DATABASE ROLE {role_name} FROM ROLE {entity_name}"
)

GRANT_APPLICATION_ROLE_TEMPLATE = (
    "GRANT APPLICATION ROLE {role_name} TO ROLE {entity_name}"
)

REVOKE_APPLICATION_ROLE_TEMPLATE = (
    "REVOKE APPLICATION ROLE {role_name} FROM ROLE {entity_name}"
)

GRANT_PRIVILEGES_TEMPLATE = (
    "GRANT {privileges} ON {resource_type} {resource_name} TO ROLE {role}"
)

REVOKE_PRIVILEGES_TEMPLATE = (
    "REVOKE {privileges} ON {resource_type} {resource_name} FROM ROLE {role}"
)

GRANT_ACCOUNT_PRIVILEGE_TEMPLATE = "GRANT {privilege} ON ACCOUNT TO ROLE {role}"

REVOKE_ACCOUNT_PRIVILEGE_TEMPLATE = "REVOKE {privilege} ON ACCOUNT FROM ROLE {role}"

GRANT_ALL_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON ALL {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

GRANT_FUTURE_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

REVOKE_FUTURE_PRIVILEGES_TEMPLATE = "REVOKE {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} FROM ROLE {role}"

ALTER_USER_TEMPLATE = "ALTER USER {user_name} SET {privileges}"

SET_USER_TAG_TEMPLATE = "ALTER USER {user_name} SET TAG {tag} = {value}"

UNSET_USER_TAG_TEMPLATE = "ALTER USER {user_name} UNSET TAG {tag}"

GRANT_OWNERSHIP_TEMPLATE = "GRANT OWNERSHIP ON {resource_type} {resource_name} TO ROLE {role_name} COPY CURRENT GRANTS"

SCHEMA_PARTIAL_WRITE_PRIVILEGES = ", ".join(
    ["monitor"]
    + [
        t.schema_create_privilege
        for t in TABLE_OBJECT_TYPES
        if t.schema_create_privilege
    ]
    + [
        "create stage",
        "create file format",
        "create sequence",
        "create function",
        "create pipe",
        "create cortex search service",
        "create mcp server",
    ]
)

# Grants between these raise errors on Snowflake, so they're never generated or revoked
SNOWFLAKE_DEFAULT_ROLES = frozenset(
    map(
        ObjectName.single,
        ("accountadmin", "sysadmin", "securityadmin", "useradmin", "public"),
    )
)


# A name as the generator takes it: an ObjectName, or a spelling of one
Name = Union[str, ObjectName]


def _single_names(names: Iterable[NameLike]) -> Set[ObjectName]:
    return {ObjectName.single(name) for name in names}


def _is_star(entry: Any) -> bool:
    """Whether a spec entry's last part is a lone *: db.*, db.schema.* or db.*.*."""
    return isinstance(entry, NamePattern) and entry.globs[-1] == "*"


class SnowflakeGrantsGenerator:
    def __init__(
        self,
        grants_to_role: Mapping[Any, Any],
        roles_granted_to_user: Mapping[Any, Iterable[Name]],
        ignore_memberships: Optional[bool] = False,
        conn: Optional[SnowflakeConnector] = None,
        account_grants_to_role: Optional[Mapping[Any, Mapping[str, Any]]] = None,
        application_roles: Optional[Iterable[Name]] = None,
        tags_on_user: Optional[Mapping[Any, Mapping[Any, str]]] = None,
    ) -> None:
        """
        Initializes a grants generator, used to generate SQL for generating grants

        grants_to_role: a dict, mapping role to grants where role is a string
            and grants is a dictionary of privileges to entities.
            e.g. {'functional_role': {'create schema': {'database': ['database_1', 'database_2']}, ...}}

        roles_granted_to_user: a dict, mapping the user to a list of roles.,
            e.g. {'user_name': ['role_1', 'role_2']

        ignore_memberships: bool, whether to skip role grant/revoke of memberships

        conn: optional SnowflakeConnector instance. If not provided, creates a new one.

        account_grants_to_role: a dict, mapping role to its account-level privileges
            and the role that granted each, for roles declaring `account_privileges`.
            e.g. {'loader': {'execute task': 'accountadmin', 'apply tag': 'securityadmin'}}
        application_roles: the member_of references (app.role) the spec loader resolved
            to application roles rather than database roles, e.g.
            {'snowflake.cortex_analyst_requests_viewer'}
        tags_on_user: a dict, mapping each user to the tags set directly on it and
            their values, keyed by tag name.
            e.g. {'user_name': {'cost_management.tags.team': 'marketing'}}

        Names can be ObjectNames or spellings of them, read with the constructor for
        their kind. Roles and users are looked up as ObjectName keys, which a str key
        matches when it's the name as tundra renders it.
        """
        self.grants_to_role = grants_to_role
        self.roles_granted_to_user = roles_granted_to_user
        self.ignore_memberships = ignore_memberships
        self.conn = conn or SnowflakeConnector()
        self.account_grants_to_role = account_grants_to_role or {}
        self.tags_on_user = tags_on_user or {}
        self.application_roles = frozenset(
            map(ObjectName.role_reference, application_roles or ())
        )
        self._held_cache: Dict[Tuple[ObjectName, str, str], FrozenSet[ObjectName]] = {}
        self._user_roles_cache: Dict[ObjectName, FrozenSet[ObjectName]] = {}

    @staticmethod
    def _name(kind: str, name: Name) -> ObjectName:
        """A name held under <kind>, spelt the way SHOW GRANTS spells that kind."""
        return name if isinstance(name, ObjectName) else granted_name(kind, name)

    def _held(self, role: NameLike, privilege: str, kind: str) -> FrozenSet[ObjectName]:
        """The names of <kind> that <role> holds <privilege> on in Snowflake."""
        key = (ObjectName.single(role), privilege, kind)
        held = self._held_cache.get(key)
        if held is None:
            names = []
            for name in (
                self.grants_to_role.get(key[0], {}).get(privilege, {}).get(kind, ())
            ):
                try:
                    names.append(self._name(kind, name))
                except InvalidNameError:
                    continue
            held = self._held_cache[key] = frozenset(names)
        return held

    def _user_roles(self, user: NameLike) -> FrozenSet[ObjectName]:
        """The roles granted to <user> in Snowflake."""
        key = ObjectName.single(user)
        roles = self._user_roles_cache.get(key)
        if roles is None:
            roles = self._user_roles_cache[key] = frozenset(
                map(ObjectName.single, self.roles_granted_to_user[key])
            )
        return roles

    def is_granted_privilege(
        self, role: str, privilege: str, entity_type: str, entity_name: Name
    ) -> bool:
        """
        Check if <role> has been granted the privilege <privilege> on entity type
        <entity_type> with name <entity_name>.

        For example:
        is_granted_privilege('reporter', 'usage', 'database', 'analytics') -> True
        means that role reporter has been granted the privilege to use the
        Database ANALYTICS on the Snowflake server.
        """
        try:
            name = self._name(entity_type, entity_name)
        except InvalidNameError:
            return False

        return name in self._held(role, privilege, entity_type)

    def is_granted_database_role(self, role: str, database_role: NameLike) -> bool:
        """
        Check if <role> has been granted membership of the database role
        <database_role>, given as a db.role FQN.

        For example:
        is_granted_database_role('reporter', 'snowflake.usage_viewer') -> True
        means that role reporter has already been granted the SNOWFLAKE.USAGE_VIEWER
        database role on the Snowflake server.
        """
        return ObjectName.role_reference(database_role) in self._held(
            role, "usage", DATABASE_ROLE_GRANT_TYPE
        )

    def is_granted_application_role(
        self, role: str, application_role: NameLike
    ) -> bool:
        """
        Check if <role> has been granted membership of the application role
        <application_role>, given as an app.role FQN.

        For example:
        is_granted_application_role('auditor', 'snowflake.cortex_analyst_requests_viewer') -> True
        means that role auditor has already been granted the SNOWFLAKE application's
        CORTEX_ANALYST_REQUESTS_VIEWER application role on the Snowflake server.
        """
        return ObjectName.role_reference(application_role) in self._held(
            role, "usage", APPLICATION_ROLE_GRANT_TYPE
        )

    def _all_privileges_granted(
        self, role: str, privileges: List[str], entity_type: str, entity_name: Name
    ) -> bool:
        return all(
            self.is_granted_privilege(role, privilege, entity_type, entity_name)
            for privilege in privileges
        )

    def _generate_member_lists(self, config: Dict) -> Tuple[List[Any], List[Any]]:
        """
        Generate a tuple with the member_include_list (e.g. roles that should be granted)
        and member_exclude_list (e.g. roles that should not be granted)

        config: the subtree for the entity as specified in the spec

        Returns: A tuple of two lists with the roles/users to include and exclude:
            (member_include_list, member_exclude_list)
        """
        member_include_list = []
        member_exclude_list = []

        if isinstance(config.get("member_of", []), dict):
            member_include_list = [
                member_or_pattern(role)
                for role in config.get("member_of", {}).get("include", [])
            ]
            member_exclude_list = [
                member_or_pattern(role)
                for role in config.get("member_of", {}).get("exclude", [])
            ]
        elif isinstance(config.get("member_of", []), list):
            member_include_list = [
                member_or_pattern(role) for role in config.get("member_of", [])
            ]

        return (member_include_list, member_exclude_list)

    def _generate_member_star_lists(
        self, all_entities: List, entity: str
    ) -> List[ObjectName]:
        """
        Generates the member include list when a * privilege is granted

        all_entities: a List of all entities defined in the spec
        entity: the entity to generate the list for

        Returns: a list of all roles to include for the entity
        """
        conn = SnowflakeConnector()
        entity_name = ObjectName.single(entity)
        wanted = _single_names(all_entities)
        return [
            role
            for role in map(ObjectName.single, conn.show_roles())
            if role in wanted and role != entity_name
        ]

    def _generate_sql_commands_for_member_of_list(
        self, member_of_list: List[ObjectName], entity: str, entity_type: str
    ) -> List[Dict]:
        """For a given member_of list and entity, generate the SQL commands
        to grant the entity privileges for every member_role in the member_of list

        member_of_list: List of roles to generate sql commands for
        entity: the user or role to grant permissions for
        entity_type: the type of enttiy, either "users" or "roles"

        returns:  a List of SQL Commands
        """
        if entity_type == "users":
            grant_type = "user"
        elif entity_type == "roles":
            grant_type = "role"
        else:
            raise ValueError("grant_type must be either 'users' or 'roles'")

        entity_name = ObjectName.single(entity)
        sql_commands = []
        for member_role in member_of_list:
            is_application_role = member_role in self.application_roles
            is_database_role = len(member_role.parts) == 2 and not is_application_role
            already_granted = False
            if is_application_role:
                if self.is_granted_application_role(entity, member_role):
                    already_granted = True
            elif is_database_role:
                if self.is_granted_database_role(entity, member_role):
                    already_granted = True
            else:
                if (
                    entity_type == "users" and member_role in self._user_roles(entity)
                ) or (
                    entity_type == "roles"
                    and self.is_granted_privilege(entity, "usage", "role", member_role)
                ):
                    already_granted = True

            # Don't generate grants for Snowflake default roles as this will raise errors
            # on Snowflake
            if (
                entity_name in SNOWFLAKE_DEFAULT_ROLES
                and member_role in SNOWFLAKE_DEFAULT_ROLES
            ):
                continue

            if is_application_role:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_APPLICATION_ROLE_TEMPLATE.format(
                            role_name=member_role,
                            entity_name=entity_name,
                        ),
                    }
                )
            elif is_database_role:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_DATABASE_ROLE_TEMPLATE.format(
                            role_name=member_role,
                            entity_name=entity_name,
                        ),
                    }
                )
            else:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_ROLE_TEMPLATE.format(
                            role_name=member_role,
                            type=grant_type,
                            entity_name=entity_name,
                        ),
                    }
                )
        return sql_commands

    def _generate_revoke_sql_commands_for_user(
        self, username: str, member_of_list: List[ObjectName]
    ) -> List[Dict]:
        """For a given user, generate the SQL commands to revoke privileges
        to any roles not defined in the member of list
        """
        user_name = ObjectName.single(username)
        wanted = set(member_of_list)
        sql_commands = []
        for granted_role in sorted(self._user_roles(username)):
            if granted_role not in wanted:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_ROLE_TEMPLATE.format(
                            role_name=granted_role,
                            type="user",
                            entity_name=user_name,
                        ),
                    }
                )

        return sql_commands

    def _generate_revoke_sql_commands_for_role(self, rolename, member_of_list):
        role_name = ObjectName.single(rolename)
        wanted = set(member_of_list)
        sql_commands = []
        for granted_role in sorted(self._held(rolename, "usage", "role")):
            if granted_role not in wanted:
                if (
                    granted_role in SNOWFLAKE_DEFAULT_ROLES
                    and role_name in SNOWFLAKE_DEFAULT_ROLES
                ):
                    continue
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_ROLE_TEMPLATE.format(
                            role_name=granted_role,
                            type="role",
                            entity_name=role_name,
                        ),
                    }
                )

        for granted_database_role in sorted(
            self._held(rolename, "usage", DATABASE_ROLE_GRANT_TYPE)
        ):
            if granted_database_role not in wanted:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_DATABASE_ROLE_TEMPLATE.format(
                            role_name=granted_database_role,
                            entity_name=role_name,
                        ),
                    }
                )

        for granted_application_role in sorted(
            self._held(rolename, "usage", APPLICATION_ROLE_GRANT_TYPE)
        ):
            if granted_application_role not in wanted:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_APPLICATION_ROLE_TEMPLATE.format(
                            role_name=granted_application_role,
                            entity_name=role_name,
                        ),
                    }
                )

        return sql_commands

    def generate_grant_roles(
        self,
        entity_type: str,
        entity: str,
        config: Dict[str, Any],
        all_entities: Optional[List] = None,
    ) -> List[Dict]:
        """
        Generate the GRANT statements for both roles and users.

        entity_type: "users" or "roles"
        entity: the name of the entity (e.g. "yannis" or "reporter")
        config: the subtree for the entity as specified in the spec
        all_entities: all roles defined in spec

        Returns the SQL commands generated as a list
        """
        sql_commands: List[Dict] = []

        if self.ignore_memberships:
            return sql_commands

        member_include_list, member_exclude_list = self._generate_member_lists(config)

        if (
            len(member_include_list) == 1
            and isinstance(member_include_list[0], NamePattern)
            and member_include_list[0] == "*"
        ):
            if not all_entities:
                raise ValueError(
                    "Cannot generate grant roles if all_entities not provided"
                )
            member_include_list = self._generate_member_star_lists(all_entities, entity)

        excluded = set(member_exclude_list)
        member_of_list = [
            ObjectName.role_reference(role)
            for role in member_include_list
            if role not in excluded
        ]

        sql_commands.extend(
            self._generate_sql_commands_for_member_of_list(
                member_of_list, entity, entity_type
            )
        )
        if entity_type == "users":
            sql_commands.extend(
                self._generate_revoke_sql_commands_for_user(entity, member_of_list)
            )
        if entity_type == "roles":
            sql_commands.extend(
                self._generate_revoke_sql_commands_for_role(entity, member_of_list)
            )

        return sql_commands

    def _generate_database_commands(self, role, config, shared_dbs, spec_dbs):
        databases = {
            "read": config.get("privileges", {}).get("databases", {}).get("read", []),
            "write": config.get("privileges", {}).get("databases", {}).get("write", []),
        }

        if len(databases.get("read", "")) == 0:
            logger.debug(
                "`privileges.databases.read` not found for role {}, skipping generation of database read level GRANT statements.".format(
                    role
                )
            )

        if len(databases.get("write", "")) == 0:
            logger.debug(
                "`privileges.databases.write` not found for role {}, skipping generation of database write level GRANT statements.".format(
                    role
                )
            )

        database_commands = self.generate_database_grants(
            role=role, databases=databases, shared_dbs=shared_dbs, spec_dbs=spec_dbs
        )
        return database_commands

    def _generate_schema_commands(self, role, config, shared_dbs, spec_dbs):
        schemas = {
            "read": config.get("privileges", {}).get("schemas", {}).get("read", []),
            "write": config.get("privileges", {}).get("schemas", {}).get("write", []),
        }

        if len(schemas.get("read", "")) == 0:
            logger.debug(
                "`privileges.schemas.read` not found for role {}, skipping generation of schemas read level GRANT statements.".format(
                    role
                )
            )

        if len(schemas.get("write", "")) == 0:
            logger.debug(
                "`privileges.schemas.write` not found for role {}, skipping generation of schemas write level GRANT statements.".format(
                    role
                )
            )

        schema_commands = self.generate_schema_grants(
            role=role, schemas=schemas, shared_dbs=shared_dbs, spec_dbs=spec_dbs
        )
        return schema_commands

    def _generate_table_commands(self, role, config, shared_dbs, spec_dbs):
        return self._generate_object_family_commands(
            role, config, shared_dbs, spec_dbs, family="tables"
        )

    def _generate_object_family_commands(
        self, role, config, shared_dbs, spec_dbs, family: str
    ):
        """
        Grants and revokes for one registry-backed `privileges` family (`tables`,
        `tasks`). Every family is fully managed within the spec's databases, so
        this runs even when the role declares nothing for it: that is what revokes
        privileges the spec no longer lists.
        """
        family_config = config.get("privileges", {}).get(family, {})
        objects = {
            "read": family_config.get("read", []),
            "write": family_config.get("write", []),
        }

        for access in ("read", "write"):
            if len(objects[access]) == 0:
                logger.debug(
                    "`privileges.{}.{}` not found for role {}, skipping generation of {} {} level GRANT statements.".format(
                        family, access, role, family, access
                    )
                )

        return self.generate_table_and_view_grants(
            role=role,
            tables=objects,
            shared_dbs=shared_dbs,
            spec_dbs=spec_dbs,
            object_types=PRIVILEGE_FAMILIES[family],
        )

    def generate_grant_privileges_to_role(
        self, role: str, config: Dict[str, Any], shared_dbs: Set, spec_dbs: Set
    ) -> List[Dict]:
        """
        Generate all the privilege granting and revocation
        statements for a role so Snowflake matches the spec.

        Most of the SQL command that will be generated are privileges granted to
        roles and this function orchestrates the whole process.

        role: the name of the role (e.g. "loader" or "reporter") the privileges
              are granted to and revoked from
        config: the subtree for the role as specified in the spec
        shared_dbs: a set of all the shared databases defined in the spec.
                    Used down the road by generate_database_grants() to also grant
                    "imported privileges" when access is granted to a shared DB.
        spec_dbs: a set of all the databases defined in the spec. This is used in revoke
                  commands to validate revocations are only for spec'd databases

        Returns the SQL commands generated as a list
        """
        sql_commands: List[Dict] = []

        try:
            warehouses = config["warehouses"]
            new_commands = self.generate_warehouse_grants(
                role=role, warehouses=warehouses
            )
            sql_commands.extend(new_commands)
        except KeyError:
            logger.debug(
                "`warehouses` not found for role {}, skipping generation of Warehouse GRANT statements.".format(
                    role
                )
            )

        try:
            integrations = config["integrations"]
            new_commands = self.generate_integration_grants(
                role=role, integrations=integrations
            )
            sql_commands.extend(new_commands)
        except KeyError:
            logger.debug(
                "`integrations` not found for role {}, skipping generation of Integrations GRANT statements.".format(
                    role
                )
            )

        try:
            external_volumes = config["external_volumes"]
            new_commands = self.generate_external_volume_grants(
                role=role, external_volumes=external_volumes
            )
            sql_commands.extend(new_commands)
        except KeyError:
            logger.debug(
                "`external_volumes` not found for role {}, skipping generation of External Volume GRANT statements.".format(
                    role
                )
            )

        try:
            account_privileges = config["account_privileges"]
            new_commands = self.generate_account_privilege_grants(
                role=role, account_privileges=account_privileges
            )
            sql_commands.extend(new_commands)
        except KeyError:
            logger.debug(
                "`account_privileges` not found for role {}, skipping generation of account-level GRANT statements.".format(
                    role
                )
            )

        database_commands = self._generate_database_commands(
            role, config, shared_dbs, spec_dbs
        )
        sql_commands.extend(database_commands)

        schema_commands = self._generate_schema_commands(
            role, config, shared_dbs, spec_dbs
        )
        sql_commands.extend(schema_commands)

        for family in PRIVILEGE_FAMILIES:
            sql_commands.extend(
                self._generate_object_family_commands(
                    role, config, shared_dbs, spec_dbs, family=family
                )
            )

        return sql_commands

    def generate_warehouse_grants(
        self, role: str, warehouses: list
    ) -> List[Dict[str, Any]]:
        """
        Generate the GRANT statements for Warehouse usage and operation.

        role: the name of the role the privileges are GRANTed to
        warehouses: list of warehouses for the specified role

        Returns the SQL command generated
        """
        sql_commands: List[Dict] = []
        role_name = ObjectName.single(role)
        wanted = [ObjectName.single(warehouse) for warehouse in warehouses]

        for warehouse in wanted:
            for priv in ["usage", "operate", "monitor"]:
                already_granted = self.is_granted_privilege(
                    role, priv, "warehouse", warehouse
                )

                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                            privileges=priv,
                            resource_type="warehouse",
                            resource_name=warehouse,
                            role=role_name,
                        ),
                    }
                )
        for priv in ["usage", "operate", "monitor"]:
            for granted_warehouse in sorted(self._held(role, priv, "warehouse")):
                if granted_warehouse not in wanted:
                    sql_commands.append(
                        {
                            "already_granted": False,
                            "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                                privileges=priv,
                                resource_type="warehouse",
                                resource_name=granted_warehouse,
                                role=role_name,
                            ),
                        }
                    )

        return sql_commands

    def generate_integration_grants(
        self, role: str, integrations: list
    ) -> List[Dict[str, Any]]:
        """
        Generate the GRANT statements for Integrations usage.

        role: the name of the role the privileges are GRANTed to
        integrations: list of integrations for the specified role

        Returns the SQL command generated
        """
        sql_commands: List[Dict] = []
        role_name = ObjectName.single(role)
        wanted = [ObjectName.single(integration) for integration in integrations]

        for integration in wanted:
            already_granted = self.is_granted_privilege(
                role, "usage", "integration", integration
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                        privileges="usage",
                        resource_type="integration",
                        resource_name=integration,
                        role=role_name,
                    ),
                }
            )
        for granted_integration in sorted(self._held(role, "usage", "integration")):
            if granted_integration not in wanted:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="usage",
                            resource_type="integration",
                            resource_name=granted_integration,
                            role=role_name,
                        ),
                    }
                )

        return sql_commands

    def generate_external_volume_grants(
        self, role: str, external_volumes: list
    ) -> List[Dict[str, Any]]:
        """
        Generate the GRANT statements for External Volume usage.

        role: the name of the role the privileges are GRANTed to
        external_volumes: list of external volumes for the specified role

        Returns the SQL command generated
        """
        sql_commands: List[Dict] = []
        role_name = ObjectName.single(role)
        wanted = [ObjectName.single(volume) for volume in external_volumes]

        for external_volume in wanted:
            already_granted = self.is_granted_privilege(
                role, "usage", EXTERNAL_VOLUME_GRANT_TYPE, external_volume
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                        privileges="usage",
                        resource_type="external volume",
                        resource_name=external_volume,
                        role=role_name,
                    ),
                }
            )

        for granted_external_volume in sorted(
            self._held(role, "usage", EXTERNAL_VOLUME_GRANT_TYPE)
        ):
            if granted_external_volume not in wanted:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="usage",
                            resource_type="external volume",
                            resource_name=granted_external_volume,
                            role=role_name,
                        ),
                    }
                )

        return sql_commands

    def generate_account_privilege_grants(
        self, role: str, account_privileges: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Generate the GRANT and REVOKE statements for a role's account-level
        (global) privileges: EXECUTE TASK, APPLY TAG, MANAGE GRANTS and so on.

        Declaring the key makes the list authoritative: anything else the role
        holds on the account is revoked, with two exceptions. Privileges Snowflake
        itself granted to a system-defined role report no grantor and cannot be
        revoked, and the ACCOUNTADMIN-only privileges are outside what SECURITYADMIN
        can touch in either direction, so both are left alone.

        role: the name of the role the privileges are GRANTed to
        account_privileges: the privileges the spec wants the role to hold

        Returns the SQL commands generated
        """
        sql_commands: List[Dict] = []
        role_name = ObjectName.single(role)
        granted = self.account_grants_to_role.get(role_name, {})
        wanted = [normalise_account_privilege(p) for p in account_privileges]

        for privilege in wanted:
            sql_commands.append(
                {
                    "already_granted": privilege in granted,
                    "sql": GRANT_ACCOUNT_PRIVILEGE_TEMPLATE.format(
                        privilege=privilege,
                        role=role_name,
                    ),
                }
            )

        for privilege, granted_by in granted.items():
            if privilege in wanted:
                continue
            if not granted_by:
                logger.debug(
                    "{} on account is a system-defined privilege of {}, not revoking.".format(
                        privilege, role
                    )
                )
                continue
            if privilege in ACCOUNTADMIN_ONLY_PRIVILEGES:
                logger.debug(
                    "{} on account can only be revoked from {} by ACCOUNTADMIN, not revoking.".format(
                        privilege, role
                    )
                )
                continue

            sql_commands.append(
                {
                    "already_granted": False,
                    "sql": REVOKE_ACCOUNT_PRIVILEGE_TEMPLATE.format(
                        privilege=privilege,
                        role=role_name,
                    ),
                }
            )

        return sql_commands

    def _generate_database_read_privs(
        self,
        database: NameLike,
        role: str,
        shared_dbs: Set[ObjectName],
        read_privileges: str,
    ) -> Dict:
        database = ObjectName.single(database)
        already_granted = self.is_granted_privilege(role, "usage", "database", database)

        # If this is a shared database, we have to grant the "imported privileges"
        # privilege to the user and skip granting the specific permissions as
        # "Granting individual privileges on imported databases is not allowed."
        if database in _single_names(shared_dbs):
            return {
                "already_granted": already_granted,
                "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                    privileges="imported privileges",
                    resource_type="database",
                    resource_name=database,
                    role=ObjectName.single(role),
                ),
            }
        else:
            return {
                "already_granted": already_granted,
                "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                    privileges=read_privileges,
                    resource_type="database",
                    resource_name=database,
                    role=ObjectName.single(role),
                ),
            }

    def generate_database_grants(
        self, role: str, databases: Dict[str, List], shared_dbs: Set, spec_dbs: Set
    ) -> List[Dict[str, Any]]:
        """
        Generate the GRANT and REVOKE statements for Databases
        to align Snowflake with the spec.

        role: the name of the role the privileges are GRANTed to
        databases: list of databases (e.g. "raw")
        shared_dbs: a set of all the shared databases defined in the spec.
        spec_dbs: a set of all the databases defined in the spec. This is used in revoke
                  commands to validate revocations are only for spec'd databases

        Returns the SQL commands generated as a list
        """
        sql_commands = []
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)
        spec_dbs = _single_names(spec_dbs)
        read_dbs = [ObjectName.single(db) for db in databases.get("read", [])]
        write_dbs = [ObjectName.single(db) for db in databases.get("write", [])]

        read_privileges = "usage"
        partial_write_privileges = "monitor, create schema"
        write_privileges = f"{read_privileges}, {partial_write_privileges}"

        for database in read_dbs:
            read_grant = self._generate_database_read_privs(
                database=database,
                role=role,
                shared_dbs=shared_dbs,
                read_privileges=read_privileges,
            )
            sql_commands.append(read_grant)
        for database in write_dbs:
            already_granted = (
                self.is_granted_privilege(role, "usage", "database", database)
                and self.is_granted_privilege(role, "monitor", "database", database)
                and self.is_granted_privilege(
                    role, "create schema", "database", database
                )
            )

            # If this is a shared database, we have to grant the "imported privileges"
            # privilege to the user and skip granting the specific permissions as
            # "Granting individual privileges on imported databases is not allowed."
            if database in shared_dbs:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                            privileges="imported privileges",
                            resource_type="database",
                            resource_name=database,
                            role=role_name,
                        ),
                    }
                )
                continue

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                        privileges=write_privileges,
                        resource_type="database",
                        resource_name=database,
                        role=role_name,
                    ),
                }
            )

        # REVOKES

        # The "Usage" privilege is consistent across read and write.
        # Compare granted usage to full read/write usage set
        # and revoke missing ones
        all_databases = set(read_dbs) | set(write_dbs)
        for granted_database in sorted(self._held(role, "usage", "database")):
            # If it's a shared database, only revoke imported
            # We'll only know if it's a shared DB based on the spec
            if granted_database not in spec_dbs:
                # Skip revocation on database that are not defined in spec
                continue
            # Revoke read/write permissions on shared databases
            elif (
                granted_database not in all_databases and granted_database in shared_dbs
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="imported privileges",
                            resource_type="database",
                            resource_name=granted_database,
                            role=role_name,
                        ),
                    }
                )
            # Revoke read permissions on created databases in Snowflake
            elif granted_database not in all_databases:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="database",
                            resource_name=granted_database,
                            role=role_name,
                        ),
                    }
                )
        # Get all other write privilege dbs in case there are dbs where
        # usage was revoked but other write permissions still exist
        # This also preserves the case where somebody switches write access
        # for read access
        full_write_privs_on_dbs = sorted(
            self._held(role, "monitor", "database")
        ) + sorted(self._held(role, "create schema", "database"))

        write_databases = set(write_dbs)
        for granted_database in full_write_privs_on_dbs:
            # If it's a shared database, only revoke imported
            # We'll only know if it's a shared DB based on the spec
            if (
                granted_database not in write_databases
                and granted_database in shared_dbs
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="imported privileges",
                            resource_type="database",
                            resource_name=granted_database,
                            role=role_name,
                        ),
                    }
                )
            elif granted_database not in write_databases:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="database",
                            resource_name=granted_database,
                            role=role_name,
                        ),
                    }
                )

        return sql_commands

    def _generate_schema_read_grants(
        self, schemas, shared_dbs, role
    ) -> Tuple[List[Dict], List[ObjectName]]:
        sql_commands = []
        read_grant_schemas = []
        read_privileges = "usage"
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)

        for schema in schemas:
            entry = name_or_pattern(schema, arity=2)

            # Do nothing if this is a schema inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            database = entry.database
            if database in shared_dbs:
                continue

            conn = SnowflakeConnector()
            fetched_schemas = [ObjectName(s) for s in conn.full_schema_list(entry)]
            read_grant_schemas.extend(fetched_schemas)

            if isinstance(entry, NamePattern) and entry.globs == ("*",):
                # If <db_name>.* then you can grant future and add future schema to grant list
                future_schema = database.future("schema")
                read_grant_schemas.append(future_schema)

                schema_already_granted = self.is_granted_privilege(
                    role, read_privileges, "schema", future_schema
                )

                # Grant on FUTURE schemas
                sql_commands.append(
                    {
                        "already_granted": schema_already_granted,
                        "sql": GRANT_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=database,
                            role=role_name,
                        ),
                    }
                )

            for db_schema in fetched_schemas:
                already_granted = False

                if self.is_granted_privilege(
                    role, read_privileges, "schema", db_schema
                ):
                    already_granted = True

                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            resource_name=db_schema,
                            role=role_name,
                        ),
                    }
                )
        return (sql_commands, read_grant_schemas)

    def _generate_schema_write_grants(
        self, schemas, shared_dbs, role
    ) -> Tuple[List[Dict], List[ObjectName]]:
        sql_commands = []
        write_grant_schemas = []
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)

        read_privileges = "usage"
        partial_write_privileges = SCHEMA_PARTIAL_WRITE_PRIVILEGES
        write_privileges = f"{read_privileges}, {partial_write_privileges}"
        write_privileges_array = write_privileges.split(", ")

        for schema in schemas:
            entry = name_or_pattern(schema, arity=2)

            # Do nothing if this is a schema inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            database = entry.database
            if database in shared_dbs:
                continue

            conn = SnowflakeConnector()
            fetched_schemas = [ObjectName(s) for s in conn.full_schema_list(entry)]
            write_grant_schemas.extend(fetched_schemas)

            if isinstance(entry, NamePattern) and entry.globs == ("*",):
                # If <db_name>.* then you can grant future and add future schema to grant list
                future_schema = database.future("schema")
                write_grant_schemas.append(future_schema)

                already_granted = True

                for privilege in write_privileges_array:
                    # If any of the privileges are not granted, set already_granted to False
                    if not self.is_granted_privilege(
                        role, privilege, "schema", future_schema
                    ):
                        already_granted = False

                # Grant on FUTURE schemas
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=write_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=database,
                            role=role_name,
                        ),
                    }
                )

            for db_schema in fetched_schemas:
                already_granted = True

                for privilege in write_privileges_array:
                    # If any of the privileges are not granted, set already_granted to False
                    if not self.is_granted_privilege(
                        role, privilege, "schema", db_schema
                    ):
                        already_granted = False

                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                            privileges=write_privileges,
                            resource_type="schema",
                            resource_name=db_schema,
                            role=role_name,
                        ),
                    }
                )
        return (sql_commands, write_grant_schemas)

    def _generate_schema_revokes(
        self, usage_schemas, all_grant_schemas, shared_dbs, spec_dbs, role
    ):
        sql_commands = []
        read_privileges = "usage"
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)
        spec_dbs = _single_names(spec_dbs)
        all_grant_schemas = set(map(ObjectName, all_grant_schemas))

        for granted_schema in map(ObjectName, usage_schemas):
            database = granted_schema.database
            future_schema = database.future("schema")
            if granted_schema not in all_grant_schemas and (
                database in shared_dbs or database not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif (  # If future privilege is granted on snowflake but not in grant list
                granted_schema == future_schema
                and future_schema not in all_grant_schemas  #
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=database,
                            role=role_name,
                        ),
                    }
                )
            elif granted_schema.is_future:
                continue
            elif (
                granted_schema not in all_grant_schemas
                and future_schema not in all_grant_schemas
            ):
                # Covers case where schema is granted in Snowflake
                # But it's not in the grant list and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            resource_name=granted_schema,
                            role=role_name,
                        ),
                    }
                )

        return sql_commands

    # TODO: This method is too complex, consider refactoring
    def generate_schema_grants(
        self, role: str, schemas: Dict[str, List], shared_dbs: Set, spec_dbs: Set
    ) -> List[Dict]:
        """
        Generate the GRANT and REVOKE statements for schemas
        including future grants.

        role: the name of the role the privileges are GRANTed to
        schemas: the name of the Schema (e.g. "raw.public", "raw.*")
        shared_dbs: a set of all the shared databases defined in the spec.
        spec_dbs: a set of all the databases defined in the spec. This is used in revoke
                  commands to validate revocations are only for spec'd databases

        Returns the SQL commands generated as a List
        """
        sql_commands = []
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)
        spec_dbs = _single_names(spec_dbs)

        # Schema lists to hold read/write grants. This is necessary
        # as the provided schemas are not the full list - we determine
        # the full list via full_schema_list and store in these variables
        read_grant_schemas = []
        write_grant_schemas = []

        partial_write_privileges = SCHEMA_PARTIAL_WRITE_PRIVILEGES

        # Get Schema Read Commands
        read_schemas = schemas.get("read", [])
        read_commands, read_grants = self._generate_schema_read_grants(
            read_schemas, shared_dbs, role
        )
        sql_commands.extend(read_commands)
        read_grant_schemas.extend(read_grants)

        # Get Schema Write Commands
        write_schemas = schemas.get("write", [])
        write_commands, write_grants = self._generate_schema_write_grants(
            write_schemas, shared_dbs, role
        )
        sql_commands.extend(write_commands)
        write_grant_schemas.extend(write_grants)

        # REVOKES

        # The "usage" privilege is consistent across read and write.
        # Compare granted usage to full read/write set and revoke missing ones
        usage_schemas = sorted(self._held(role, "usage", "schema"))
        all_grant_schemas = read_grant_schemas + write_grant_schemas
        sql_commands.extend(
            self._generate_schema_revokes(
                usage_schemas, all_grant_schemas, shared_dbs, spec_dbs, role
            )
        )

        # Get all other write privilege schemas in case there are schemas where
        # usage was revoked but other write permissions still exist
        # This also preserves the case where somebody switches write access
        # for read access
        other_privileges = SCHEMA_PARTIAL_WRITE_PRIVILEGES.split(", ")

        other_schema_grants: List[ObjectName] = list()
        for privilege in other_privileges:
            other_schema_grants.extend(sorted(self._held(role, privilege, "schema")))

        write_grant_schemas_set = set(write_grant_schemas)
        for granted_schema in other_schema_grants:
            database = granted_schema.database
            future_schema = database.future("schema")
            if granted_schema not in write_grant_schemas_set and (
                database in shared_dbs or database not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif (  # If future privilege is granted but not in grant list
                granted_schema == future_schema
                and future_schema not in write_grant_schemas_set
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=database,
                            role=role_name,
                        ),
                    }
                )
            elif granted_schema.is_future:
                continue
            elif (
                granted_schema not in write_grant_schemas_set
                and future_schema not in write_grant_schemas_set
            ):
                # Covers case where schema is granted and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="schema",
                            resource_name=granted_schema,
                            role=role_name,
                        ),
                    }
                )

        return sql_commands

    @staticmethod
    def _list_objects(conn, object_type: TableObjectType, schemas) -> List[ObjectName]:
        """Every object of this type across `schemas`, one SHOW per schema."""
        object_list: List[ObjectName] = []
        for schema in schemas:
            object_list.extend(
                map(
                    ObjectName,
                    getattr(conn, object_type.connector_method)(schema=schema),
                )
            )
        return object_list

    def _record_wildcard_coverage(
        self,
        conn,
        object_type: TableObjectType,
        fetched_schemas,
        coverage: GrantCoverage,
    ) -> None:
        """Mark a wildcard entry's schemas as covered for the revoke pass.

        For a type that accepts ON ALL and ON FUTURE, the schema scope says everything the
        enumerated object list used to, so nothing needs listing. Types without bulk-grant
        support have no such guarantee and still have to be enumerated.
        """
        if object_type.supports_bulk_grants:
            coverage.add_schemas(fetched_schemas)
        else:
            coverage.add_objects(self._list_objects(conn, object_type, fetched_schemas))

    def _generate_table_grants(
        self,
        conn,
        tables,
        shared_dbs,
        role,
        object_types: List[TableObjectType],
        access: str,
    ):
        """
        The GRANTs for one access level (`read` or `write`) of a family's spec entries,
        and what they cover for the revoke pass, by grant key.
        """
        sql_commands: List[Dict] = []
        grant_lists: Dict[str, GrantCoverage] = {
            t.grant_key: GrantCoverage() for t in object_types
        }
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)

        for table in tables:
            entry = name_or_pattern(table, arity=3)

            # Do nothing if this is a table inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            database = entry.database
            if database in shared_dbs:
                continue

            fetched_schemas = [
                ObjectName(s) for s in conn.full_schema_list(entry.parent)
            ]

            for object_type in object_types:
                privileges = (
                    object_type.read_privileges
                    if access == "read"
                    else object_type.write_privileges
                )
                privileges_array = privileges.split(", ")
                coverage = grant_lists[object_type.grant_key]

                # For grants at the database level
                future_database_object = database.future(object_type.grant_key)
                already_granted = self._all_privileges_granted(
                    role,
                    privileges_array,
                    object_type.grant_key,
                    future_database_object,
                )
                coverage.add_object(future_database_object)

                if isinstance(entry, NamePattern) and entry.globs == ("*", "*"):
                    for template in (
                        GRANT_FUTURE_PRIVILEGES_TEMPLATE,
                        GRANT_ALL_PRIVILEGES_TEMPLATE,
                    ):
                        sql_commands.append(
                            {
                                "already_granted": already_granted,
                                "sql": template.format(
                                    privileges=privileges,
                                    resource_type=object_type.name,
                                    grouping_type="database",
                                    grouping_name=database,
                                    role=role_name,
                                ),
                            }
                        )

                # Objects of this type that privileges will be granted to
                grant_objects = []

                if _is_star(entry):
                    # If <schema_name>.* then the schema is covered wholesale by the
                    # ON ALL plus ON FUTURE pair below, so record the scope rather than
                    # listing its objects. If *.* was provided then full_schema_list
                    # already fetched all schemas.
                    self._record_wildcard_coverage(
                        conn, object_type, fetched_schemas, coverage
                    )

                    for schema in fetched_schemas:
                        future_object = schema.future(object_type.grant_key)
                        coverage.add_object(future_object)

                        already_granted = self._all_privileges_granted(
                            role, privileges_array, object_type.grant_key, future_object
                        )

                        for template in (
                            GRANT_FUTURE_PRIVILEGES_TEMPLATE,
                            GRANT_ALL_PRIVILEGES_TEMPLATE,
                        ):
                            sql_commands.append(
                                {
                                    "already_granted": already_granted,
                                    "sql": template.format(
                                        privileges=privileges,
                                        resource_type=object_type.name,
                                        grouping_type="schema",
                                        grouping_name=schema,
                                        role=role_name,
                                    ),
                                }
                            )

                # TODO Future elif to have partial table name

                elif isinstance(entry, ObjectName):
                    # The table passed is a single entity:
                    # check that it's valid and add to list. This is the only branch
                    # that needs the schema's objects listed, and it is bounded by the
                    # handful of objects a spec names explicitly.
                    if entry in self._list_objects(conn, object_type, fetched_schemas):
                        grant_objects = [entry]
                        coverage.add_object(entry)

                # Grant explicitly to each flagged object instead of
                # granting to all objects in the schema
                for db_object in grant_objects:
                    already_granted = self._all_privileges_granted(
                        role, privileges_array, object_type.grant_key, db_object
                    )

                    sql_commands.append(
                        {
                            "already_granted": already_granted,
                            "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                                privileges=privileges,
                                resource_type=object_type.name,
                                resource_name=db_object,
                                role=role_name,
                            ),
                        }
                    )

        return sql_commands, grant_lists

    def _generate_table_read_grants(
        self, conn, tables, shared_dbs, role, object_types: List[TableObjectType]
    ):
        return self._generate_table_grants(
            conn, tables, shared_dbs, role, object_types, access="read"
        )

    def _generate_table_write_grants(
        self, conn, tables, shared_dbs, role, object_types: List[TableObjectType]
    ):
        return self._generate_table_grants(
            conn, tables, shared_dbs, role, object_types, access="write"
        )

    def _generate_revoke_select_privs(
        self,
        role: str,
        coverage: GrantCoverage,
        shared_dbs: Set[Any],
        spec_dbs: Set[Any],
        privilege_set: str,
        object_type: TableObjectType,
        granted_resources: Iterable[Name],
    ) -> List[Dict[str, Any]]:
        """
        Generates REVOKE privileges for tables/views known as resources here

        role: Snowflake role to revoke the resource from
        coverage: What the spec's grants legitimately cover, as explicit object names
                  plus wholesale schema scopes from wildcard entries
        shared_dbs: Shared databases to be skipped
        spec_dbs: Databases to apply REVOKE statements on
        privilege_set: Privileges to revoke (i.e. SELECT, INSERT, etc.)
        object_type: Registry entry for the object being revoked. Supplies the SQL
                     keyword (`name`) and the grant key its future-grant scopes are
                     made with
        granted_resources: List of GRANTS to filter through

        Returns a list of REVOKE statements
        """
        sql_commands = []
        role_name = ObjectName.single(role)
        shared_dbs = _single_names(shared_dbs)
        spec_dbs = _single_names(spec_dbs)

        for granted_resource in map(ObjectName, granted_resources):
            database = granted_resource.database
            # The database for db.<table>, the schema for db.schema.<table> and
            # db.schema.table
            scope = granted_resource.parent
            assert scope is not None, f"{granted_resource!s} isn't in a database"
            future_resource = scope.future(object_type.grant_key)
            grouping_type = "database" if len(scope.parts) == 1 else "schema"

            resource_covered = coverage.covers(granted_resource)
            future_covered = coverage.covers(future_resource)

            if not resource_covered and (
                database in shared_dbs or database not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif granted_resource == future_resource and not future_covered:
                # If future privilege is granted in Snowflake but not in grant list
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=privilege_set,
                            resource_type=object_type.name,
                            grouping_type=grouping_type,
                            grouping_name=scope,
                            role=role_name,
                        ),
                    }
                )
            elif granted_resource.is_future:
                continue
            elif not resource_covered and not future_covered:
                # Covers case where resource is granted in Snowflake
                # But it's not in the grant list and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=privilege_set,
                            resource_type=object_type.name,
                            resource_name=granted_resource,
                            role=role_name,
                        ),
                    }
                )
        return sql_commands

    def generate_revoke_privs(
        self,
        role: str,
        shared_dbs: Set[Any],
        spec_dbs: Set[Any],
        all_grants_by_type: Dict[str, GrantCoverage],
        write_grants_by_type: Dict[str, GrantCoverage],
        object_types: Optional[List[TableObjectType]] = None,
    ) -> List[Dict[str, Any]]:
        sql_commands = []

        # Resolved at call time rather than in the signature so the module-level
        # registry stays the single source of truth for the table-like default.
        if object_types is None:
            object_types = TABLE_OBJECT_TYPES

        for object_type in object_types:
            granted_resources = sorted(
                self._held(role, object_type.read_privileges, object_type.grant_key)
            )
            sql_commands.extend(
                self._generate_revoke_select_privs(
                    role=role,
                    coverage=all_grants_by_type[object_type.grant_key],
                    shared_dbs=shared_dbs,
                    spec_dbs=spec_dbs,
                    privilege_set=object_type.read_privileges,
                    object_type=object_type,
                    granted_resources=granted_resources,
                )
            )

        # Write privileges only need revoking for writable types:
        # select-only types (views, dynamic tables) are fully covered above
        for object_type in object_types:
            if not object_type.is_writable:
                continue

            write_partial_privileges = object_type.write_partial_privileges
            granted_resources_set: Set[ObjectName] = set()
            for privilege in write_partial_privileges.split(", "):
                granted_resources_set.update(
                    self._held(role, privilege, object_type.grant_key)
                )

            sql_commands.extend(
                self._generate_revoke_select_privs(
                    role=role,
                    coverage=write_grants_by_type[object_type.grant_key],
                    shared_dbs=shared_dbs,
                    spec_dbs=spec_dbs,
                    privilege_set=write_partial_privileges,
                    object_type=object_type,
                    granted_resources=sorted(granted_resources_set),
                )
            )

        return sql_commands

    def generate_table_and_view_grants(
        self,
        role: str,
        tables: Dict[str, List],
        shared_dbs: Set,
        spec_dbs: Set,
        object_types: Optional[List[TableObjectType]] = None,
    ) -> List[Dict]:
        """
        Generate the GRANT and REVOKE statements for the registry object types in
        `object_types` (by default every table-like type: tables, views,
        iceberg/dynamic tables, streamlits), including future grants. The `tasks`
        family reuses this with its own registry slice.

        role: the name of the role the privileges are GRANTed to
        tables: read/write lists of object names (e.g. "raw.public.my_table")
        shared_dbs: a set of all the shared databases defined in the spec.
        spec_dbs: a set of all the databases defined in the spec. This is used in revoke
                  commands to validate revocations are only for spec'd databases
        object_types: the registry slice to grant and revoke for; None means the
                      table-like types

        Returns the SQL commands generated as a List
        """
        sql_commands = []
        shared_dbs = _single_names(shared_dbs)
        spec_dbs = _single_names(spec_dbs)

        if object_types is None:
            object_types = TABLE_OBJECT_TYPES

        conn = SnowflakeConnector()

        read_commands, read_grants_by_type = self._generate_table_read_grants(
            conn, tables.get("read", []), shared_dbs, role, object_types
        )
        sql_commands.extend(read_commands)

        write_commands, write_grants_by_type = self._generate_table_write_grants(
            conn, tables.get("write", []), shared_dbs, role, object_types
        )
        sql_commands.extend(write_commands)

        all_grants_by_type = {
            t.grant_key: read_grants_by_type[t.grant_key].merge(
                write_grants_by_type[t.grant_key]
            )
            for t in object_types
        }

        sql_commands.extend(
            self.generate_revoke_privs(
                role,
                shared_dbs,
                spec_dbs,
                all_grants_by_type,
                write_grants_by_type,
                object_types,
            )
        )
        return sql_commands

    @staticmethod
    def _sql_parameter_literal(value: Any) -> str:
        """Render a session parameter value as a SQL literal: booleans and numbers bare, strings quoted."""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    def generate_alter_user(self, user: str, config: Dict[str, Any]) -> List[Dict]:
        """
        Generate the ALTER statements for USERs.

        user: the name of the USER
        config: the subtree for the user as specified in the spec

        Returns the SQL commands generated as a List
        """
        sql_commands: List[Any] = []
        alter_privileges: List[Any] = []
        if self.ignore_memberships:
            return sql_commands

        if "can_login" in config:
            if config.get("can_login"):
                alter_privileges.append("DISABLED = FALSE")
            else:
                alter_privileges.append("DISABLED = TRUE")

        if "has_password" in config:
            if not config.get("has_password"):
                alter_privileges.append("PASSWORD = NULL")

        identifiers = [
            "default_warehouse",
            "default_namespace",
            "default_role",
        ]

        others = [
            "display_name",
            "first_name",
            "middle_name",
            "last_name",
            "email",
            "comment",
        ]

        for other in others:
            if other in config:
                property_name = str.upper(other)
                property_value = config.get(other)
                alter_privileges.append(f"{property_name} = '{property_value}'")

        for identifier in identifiers:
            if identifier in config:
                property_name = str.upper(identifier)
                property_value = config[identifier]
                # Stored as text, so every part is sent quoted with its case kept
                name = (
                    ObjectName(property_value)
                    if identifier == "default_namespace"
                    else ObjectName.single(property_value)
                )
                alter_privileges.append(f"{property_name} = {name.quoted}")

        # Handle user type with validation (case-insensitive)
        user_type = config.get("type", "PERSON").upper()

        # SERVICE type cannot have password, convert to LEGACY_SERVICE if needed
        if config.get("has_password") and user_type == "SERVICE":
            user_type = "LEGACY_SERVICE"

        allowed_types = ["PERSON", "SERVICE", "LEGACY_SERVICE"]
        if user_type not in allowed_types:
            raise ValueError(
                f"Invalid user type '{config.get('type')}'. "
                f"Must be one of: {', '.join(allowed_types)} (case-insensitive)"
            )
        alter_privileges.append(f"TYPE = '{user_type}'")

        secondary_roles = config.get("default_secondary_roles", [])
        roles_list = ", ".join(map(self._sql_parameter_literal, secondary_roles))
        alter_privileges.append(f"DEFAULT_SECONDARY_ROLES = ({roles_list})")

        # Session parameters (TIMEZONE, WEEK_START, ...). The spec schema allow-lists the
        # keys and types, so formatting only has to follow the Python type.
        for parameter, value in config.get("parameters", {}).items():
            alter_privileges.append(
                f"{parameter.upper()} = {self._sql_parameter_literal(value)}"
            )
        if alter_privileges:
            sql_commands.append(
                {
                    "already_granted": False,
                    "sql": ALTER_USER_TEMPLATE.format(
                        user_name=ObjectName.single(user),
                        privileges=", ".join(alter_privileges),
                    ),
                }
            )

        return sql_commands

    def generate_user_tags(self, user: str, config: Dict[str, Any]) -> List[Dict]:
        """
        Generate the SET / UNSET TAG statements for a USER.

        The spec owns every tag set directly on a user it lists: each tag under `tags`
        is set to its value, and any other tag on the user is unset, so a tag set by
        hand or removed from the spec does not linger. Values are case-insensitive in
        the spec and set upper case, like the user's `type`.

        user: the name of the USER
        config: the subtree for the user as specified in the spec

        Returns the SQL commands generated as a List
        """
        sql_commands: List[Dict] = []
        if self.ignore_memberships:
            return sql_commands

        user_name = ObjectName.single(user)
        live_tags = {
            ObjectName(tag): value
            for tag, value in self.tags_on_user.get(user_name, {}).items()
        }
        spec_tags = {
            ObjectName(tag): value.upper()
            for tag, value in config.get("tags", {}).items()
        }

        for tag, value in sorted(spec_tags.items()):
            sql_commands.append(
                {
                    "already_granted": live_tags.get(tag) == value,
                    "sql": SET_USER_TAG_TEMPLATE.format(
                        user_name=user_name,
                        tag=tag,
                        value=self._sql_parameter_literal(value),
                    ),
                }
            )

        for tag in sorted(set(live_tags) - set(spec_tags)):
            sql_commands.append(
                {
                    "already_granted": False,
                    "sql": UNSET_USER_TAG_TEMPLATE.format(user_name=user_name, tag=tag),
                }
            )

        return sql_commands

    def _generate_ownership_grant_database(
        self, role: str, database_refs: List[str]
    ) -> List[Dict]:
        sql_commands = []
        for database in map(ObjectName.single, database_refs):
            already_granted = self.is_granted_privilege(
                role, "ownership", "database", database
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                        resource_type="database",
                        resource_name=database,
                        role_name=ObjectName.single(role),
                    ),
                }
            )
        return sql_commands

    @staticmethod
    def _matching_schemas(
        conn, entry: Union[ObjectName, NamePattern]
    ) -> List[ObjectName]:
        """The schemas a schema entry names: itself, or those its pattern matches."""
        if isinstance(entry, ObjectName):
            return [entry]
        return [
            schema
            for schema in map(ObjectName, conn.show_schemas(entry.database))
            if entry.matches(schema)
        ]

    def _generate_ownership_grant_schema(self, conn, role, schema_refs) -> List[Dict]:
        sql_commands = []
        for schema in schema_refs:
            entry = name_or_pattern(schema, arity=2)

            for db_schema in self._matching_schemas(conn, entry):
                already_granted = self.is_granted_privilege(
                    role, "ownership", "schema", db_schema
                )

                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                            resource_type="schema",
                            resource_name=db_schema,
                            role_name=ObjectName.single(role),
                        ),
                    }
                )
        return sql_commands

    def _generate_ownership_grant_table(
        self, conn: SnowflakeConnector, role, table_refs
    ) -> List[Dict]:
        sql_commands = []

        tables: List[ObjectName] = []

        for table in table_refs:
            entry = name_or_pattern(table, arity=3)
            schema_entry = entry.parent
            assert schema_entry is not None, f"{entry!s} isn't in a schema"
            schemas = self._matching_schemas(conn, schema_entry)

            if isinstance(entry, NamePattern):
                for schema in schemas:
                    tables.extend(
                        db_table
                        for db_table in map(ObjectName, conn.show_tables(schema=schema))
                        if entry.matches(db_table)
                    )
            else:
                tables.append(entry)

        existing_views: Set[ObjectName] = set()
        for schema in schemas:
            existing_views = set(map(ObjectName, conn.show_views(schema=schema)))

        # And then grant ownership to all tables
        for db_table in tables:
            # In case `db_table` does not exist, call it a table.
            # Regardless, the SQL will be validated later and alert it doesn't exist.
            resource_type = "table"
            if db_table in existing_views:
                resource_type = "view"

            already_granted = self.is_granted_privilege(
                role, "ownership", resource_type, db_table
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                        resource_type=resource_type,
                        resource_name=db_table,
                        role_name=ObjectName.single(role),
                    ),
                }
            )
        return sql_commands

    def generate_grant_ownership(  # noqa
        self, role: str, config: Dict[str, Any]
    ) -> List[Dict]:
        """
        Generate the GRANT ownership statements for databases, schemas and tables.

        role: the name of the role (e.g. "loader") ownership will be GRANTed to
        config: the subtree for the role as specified in the spec

        Returns the SQL commands generated as a List
        """
        sql_commands = []

        db_refs = config.get("owns", {}).get("databases")
        if db_refs:
            db_ownership_grants = self._generate_ownership_grant_database(role, db_refs)
            sql_commands.extend(db_ownership_grants)

        schema_refs = config.get("owns", {}).get("schemas")
        if schema_refs:
            schema_ownership_grants = self._generate_ownership_grant_schema(
                self.conn, role, schema_refs
            )
            sql_commands.extend(schema_ownership_grants)

        table_refs = config.get("owns", {}).get("tables")
        if table_refs:
            table_ownership_grants = self._generate_ownership_grant_table(
                self.conn, role, table_refs
            )
            sql_commands.extend(table_ownership_grants)
        return sql_commands
