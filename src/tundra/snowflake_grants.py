from typing import Any, Dict, List, Optional, Set, Tuple

from tundra.logger import GLOBAL_LOGGER as logger
from tundra.snowflake_connector import SnowflakeConnector
from tundra.table_object_types import TABLE_OBJECT_TYPES

GRANT_ROLE_TEMPLATE = "GRANT ROLE {role_name} TO {type} {entity_name}"

GRANT_DATABASE_ROLE_TEMPLATE = "GRANT DATABASE ROLE {role_name} TO ROLE {entity_name}"

REVOKE_ROLE_TEMPLATE = "REVOKE ROLE {role_name} FROM {type} {entity_name}"

GRANT_PRIVILEGES_TEMPLATE = (
    "GRANT {privileges} ON {resource_type} {resource_name} TO ROLE {role}"
)

REVOKE_PRIVILEGES_TEMPLATE = (
    "REVOKE {privileges} ON {resource_type} {resource_name} FROM ROLE {role}"
)

GRANT_ALL_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON ALL {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

GRANT_FUTURE_PRIVILEGES_TEMPLATE = "GRANT {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} TO ROLE {role}"

REVOKE_FUTURE_PRIVILEGES_TEMPLATE = "REVOKE {privileges} ON FUTURE {resource_type}s IN {grouping_type} {grouping_name} FROM ROLE {role}"

ALTER_USER_TEMPLATE = "ALTER USER {user_name} SET {privileges}"

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
    ]
)


class SnowflakeGrantsGenerator:
    def __init__(
        self,
        grants_to_role: Dict,
        roles_granted_to_user: Dict[str, List[str]],
        ignore_memberships: Optional[bool] = False,
        conn: Optional[SnowflakeConnector] = None,
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

        """
        self.grants_to_role = grants_to_role
        self.roles_granted_to_user = roles_granted_to_user
        self.ignore_memberships = ignore_memberships
        self.conn = conn or SnowflakeConnector()

    def is_granted_privilege(
        self, role: str, privilege: str, entity_type: str, entity_name: str
    ) -> bool:
        """
        Check if <role> has been granted the privilege <privilege> on entity type
        <entity_type> with name <entity_name>.

        For example:
        is_granted_privilege('reporter', 'usage', 'database', 'analytics') -> True
        means that role reporter has been granted the privilege to use the
        Database ANALYTICS on the Snowflake server.
        """

        grants = (
            self.grants_to_role.get(role, {}).get(privilege, {}).get(entity_type, [])
        )

        if SnowflakeConnector.snowflaky(entity_name) in grants:
            return True

        return False

    def _all_privileges_granted(
        self, role: str, privileges: List[str], entity_type: str, entity_name: str
    ) -> bool:
        return all(
            self.is_granted_privilege(role, privilege, entity_type, entity_name)
            for privilege in privileges
        )

    def _generate_member_lists(self, config: Dict) -> Tuple[List[str], List[str]]:
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
            member_include_list = config.get("member_of", {}).get("include", [])
            member_include_list = [
                SnowflakeConnector.snowflaky_database_role(role)
                if "." in role
                else SnowflakeConnector.snowflaky_user_role(role)
                for role in member_include_list
            ]
            member_exclude_list = config.get("member_of", {}).get("exclude", [])
            member_exclude_list = [
                SnowflakeConnector.snowflaky_database_role(role)
                if "." in role
                else SnowflakeConnector.snowflaky_user_role(role)
                for role in member_exclude_list
            ]
        elif isinstance(config.get("member_of", []), list):
            member_include_list = config.get("member_of", [])
            member_include_list = [
                SnowflakeConnector.snowflaky_database_role(role)
                if "." in role
                else SnowflakeConnector.snowflaky_user_role(role)
                for role in member_include_list
            ]

        return (member_include_list, member_exclude_list)

    def _generate_member_star_lists(self, all_entities: List, entity: str) -> List[str]:
        """
        Generates the member include list when a * privilege is granted

        all_entities: a List of all entities defined in the spec
        entity: the entity to generate the list for

        Returns: a list of all roles to include for the entity
        """
        conn = SnowflakeConnector()
        show_roles = conn.show_roles()
        member_include_list = [
            role for role in show_roles if role in all_entities and role != entity
        ]
        return member_include_list

    def _generate_sql_commands_for_member_of_list(
        self, member_of_list: List[str], entity: str, entity_type: str
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

        sql_commands = []
        for member_role in member_of_list:
            is_database_role = "." in member_role
            already_granted = False
            if is_database_role:
                if self.is_granted_privilege(
                    entity, "usage", "database role", member_role
                ):
                    already_granted = True
            else:
                granted_role = SnowflakeConnector.snowflaky_user_role(member_role)
                if (
                    entity_type == "users"
                    and granted_role in self.roles_granted_to_user[entity]
                ) or (
                    entity_type == "roles"
                    and self.is_granted_privilege(entity, "usage", "role", member_role)
                ):
                    already_granted = True

            # Don't generate grants for Snowflake default roles as this will raise errors
            # on Snowflake
            snowflake_default_roles = [
                "accountadmin",
                "sysadmin",
                "securityadmin",
                "useradmin",
                "public",
            ]
            if (
                entity in snowflake_default_roles
                and member_role in snowflake_default_roles
            ):
                continue

            if is_database_role:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_DATABASE_ROLE_TEMPLATE.format(
                            role_name=SnowflakeConnector.snowflaky_database_role(member_role),
                            entity_name=SnowflakeConnector.snowflaky_user_role(entity),
                        ),
                    }
                )
            else:
                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_ROLE_TEMPLATE.format(
                            role_name=SnowflakeConnector.snowflaky_user_role(
                                member_role
                            ),
                            type=grant_type,
                            entity_name=SnowflakeConnector.snowflaky_user_role(entity),
                        ),
                    }
                )
        return sql_commands

    def _generate_revoke_sql_commands_for_user(
        self, username: str, member_of_list: List[str]
    ) -> List[Dict]:
        """For a given user, generate the SQL commands to revoke privileges
        to any roles not defined in the member of list
        """
        sql_commands = []
        for granted_role in self.roles_granted_to_user[username]:
            if granted_role not in member_of_list:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_ROLE_TEMPLATE.format(
                            role_name=SnowflakeConnector.snowflaky_user_role(
                                granted_role
                            ),
                            type="user",
                            entity_name=SnowflakeConnector.snowflaky_user_role(
                                username
                            ),
                        ),
                    }
                )

        return sql_commands

    def _generate_revoke_sql_commands_for_role(self, rolename, member_of_list):
        sql_commands = []
        for granted_role in (
            self.grants_to_role.get(rolename, {}).get("usage", {}).get("role", [])
        ):
            if granted_role not in member_of_list:
                snowflake_default_roles = [
                    "accountadmin",
                    "sysadmin",
                    "securityadmin",
                    "useradmin",
                    "public",
                ]
                if (
                    granted_role in snowflake_default_roles
                    and rolename in snowflake_default_roles
                ):
                    continue
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_ROLE_TEMPLATE.format(
                            role_name=SnowflakeConnector.snowflaky_user_role(
                                granted_role
                            ),
                            type="role",
                            entity_name=SnowflakeConnector.snowflaky_user_role(
                                rolename
                            ),
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

        if len(member_include_list) == 1 and member_include_list[0] == '"*"':
            if not all_entities:
                raise ValueError(
                    "Cannot generate grant roles if all_entities not provided"
                )
            member_include_list = self._generate_member_star_lists(all_entities, entity)

        member_of_list = [
            role for role in member_include_list if role not in member_exclude_list
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
        tables = {
            "read": config.get("privileges", {}).get("tables", {}).get("read", []),
            "write": config.get("privileges", {}).get("tables", {}).get("write", []),
        }

        if len(tables.get("read", "")) == 0:
            logger.debug(
                "`privileges.tables.read` not found for role {}, skipping generation of tables read level GRANT statements.".format(
                    role
                )
            )

        if len(tables.get("write", "")) == 0:
            logger.debug(
                "`privileges.tables.write` not found for role {}, skipping generation of tables write level GRANT statements.".format(
                    role
                )
            )

        table_commands = self.generate_table_and_view_grants(
            role=role, tables=tables, shared_dbs=shared_dbs, spec_dbs=spec_dbs
        )
        return table_commands

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

        database_commands = self._generate_database_commands(
            role, config, shared_dbs, spec_dbs
        )
        sql_commands.extend(database_commands)

        schema_commands = self._generate_schema_commands(
            role, config, shared_dbs, spec_dbs
        )
        sql_commands.extend(schema_commands)

        table_commands = self._generate_table_commands(
            role, config, shared_dbs, spec_dbs
        )
        sql_commands.extend(table_commands)

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

        for warehouse in warehouses:
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
                            resource_name=SnowflakeConnector.snowflaky(warehouse),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        for priv in ["usage", "operate", "monitor"]:
            for granted_warehouse in (
                self.grants_to_role.get(role, {}).get(priv, {}).get("warehouse", [])
            ):
                if granted_warehouse not in warehouses:
                    sql_commands.append(
                        {
                            "already_granted": False,
                            "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                                privileges=priv,
                                resource_type="warehouse",
                                resource_name=SnowflakeConnector.snowflaky(
                                    granted_warehouse
                                ),
                                role=SnowflakeConnector.snowflaky_user_role(role),
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

        for integration in integrations:
            already_granted = self.is_granted_privilege(
                role, "usage", "integration", integration
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                        privileges="usage",
                        resource_type="integration",
                        resource_name=SnowflakeConnector.snowflaky(integration),
                        role=SnowflakeConnector.snowflaky_user_role(role),
                    ),
                }
            )
        for granted_integration in (
            self.grants_to_role.get(role, {}).get("usage", {}).get("integration", [])
        ):
            if granted_integration not in integrations:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="usage",
                            resource_type="integration",
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_integration
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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

        for external_volume in external_volumes:
            already_granted = self.is_granted_privilege(
                role, "usage", "external volume", external_volume
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                        privileges="usage",
                        resource_type="external volume",
                        resource_name=SnowflakeConnector.snowflaky(external_volume),
                        role=SnowflakeConnector.snowflaky_user_role(role),
                    ),
                }
            )

        for granted_external_volume in (
            self.grants_to_role.get(role, {})
            .get("usage", {})
            .get("external volume", [])
        ):
            if granted_external_volume not in external_volumes:
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="usage",
                            resource_type="external volume",
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_external_volume
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )

        return sql_commands

    def _generate_database_read_privs(
        self, database: str, role: str, shared_dbs: Set[str], read_privileges: str
    ) -> Dict:
        already_granted = self.is_granted_privilege(role, "usage", "database", database)

        # If this is a shared database, we have to grant the "imported privileges"
        # privilege to the user and skip granting the specific permissions as
        # "Granting individual privileges on imported databases is not allowed."
        if database in shared_dbs:
            return {
                "already_granted": already_granted,
                "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                    privileges="imported privileges",
                    resource_type="database",
                    resource_name=SnowflakeConnector.snowflaky(database),
                    role=SnowflakeConnector.snowflaky_user_role(role),
                ),
            }
        else:
            return {
                "already_granted": already_granted,
                "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                    privileges=read_privileges,
                    resource_type="database",
                    resource_name=SnowflakeConnector.snowflaky(database),
                    role=SnowflakeConnector.snowflaky_user_role(role),
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

        read_privileges = "usage"
        partial_write_privileges = "monitor, create schema"
        write_privileges = f"{read_privileges}, {partial_write_privileges}"

        for database in databases.get("read", []):
            read_grant = self._generate_database_read_privs(
                database=database,
                role=role,
                shared_dbs=shared_dbs,
                read_privileges=read_privileges,
            )
            sql_commands.append(read_grant)
        for database in databases.get("write", []):
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
                            resource_name=SnowflakeConnector.snowflaky(database),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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
                        resource_name=SnowflakeConnector.snowflaky(database),
                        role=SnowflakeConnector.snowflaky_user_role(role),
                    ),
                }
            )

        # REVOKES

        # The "Usage" privilege is consistent across read and write.
        # Compare granted usage to full read/write usage set
        # and revoke missing ones
        usage_privs_on_db = (
            self.grants_to_role.get(role, {}).get("usage", {}).get("database", [])
        )

        for granted_database in usage_privs_on_db:
            # If it's a shared database, only revoke imported
            # We'll only know if it's a shared DB based on the spec
            all_databases = databases.get("read", []) + databases.get("write", [])
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
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_database
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_database
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        # Get all other write privilege dbs in case there are dbs where
        # usage was revoked but other write permissions still exist
        # This also preserves the case where somebody switches write access
        # for read access
        monitor_privs_on_db = (
            self.grants_to_role.get(role, {}).get("monitor", {}).get("database", [])
        )

        create_privs_on_db = (
            self.grants_to_role.get(role, {})
            .get("create schema", {})
            .get("database", [])
        )

        full_write_privs_on_dbs = monitor_privs_on_db + create_privs_on_db

        for granted_database in full_write_privs_on_dbs:
            # If it's a shared database, only revoke imported
            # We'll only know if it's a shared DB based on the spec
            if (
                granted_database not in databases.get("write", [])
                and granted_database in shared_dbs
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges="imported privileges",
                            resource_type="database",
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_database
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
            elif granted_database not in databases.get("write", []):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="database",
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_database
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )

        return sql_commands

    def _generate_schema_read_grants(
        self, schemas, shared_dbs, role
    ) -> Tuple[List[Dict], List]:
        sql_commands = []
        read_grant_schemas = []
        read_privileges = "usage"

        for schema in schemas:
            # Split the schema identifier into parts {DB_NAME}.{SCHEMA_NAME}
            # so that we can check and use each one
            name_parts = schema.split(".")

            # Do nothing if this is a schema inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            database = name_parts[0]
            if database in shared_dbs:
                continue

            conn = SnowflakeConnector()
            fetched_schemas = conn.full_schema_list(schema)
            read_grant_schemas.extend(fetched_schemas)

            if name_parts[1] == "*":
                # If <db_name>.* then you can grant future and add future schema to grant list
                future_schema = f"{database}.<schema>"
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
                            grouping_name=SnowflakeConnector.snowflaky(database),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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
                            resource_name=SnowflakeConnector.snowflaky(db_schema),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        return (sql_commands, read_grant_schemas)

    def _generate_schema_write_grants(
        self, schemas, shared_dbs, role
    ) -> Tuple[List[Dict], List]:
        sql_commands = []
        write_grant_schemas = []

        read_privileges = "usage"
        partial_write_privileges = SCHEMA_PARTIAL_WRITE_PRIVILEGES
        write_privileges = f"{read_privileges}, {partial_write_privileges}"
        write_privileges_array = write_privileges.split(", ")

        for schema in schemas:
            # Split the schema identifier into parts {DB_NAME}.{SCHEMA_NAME}
            # so that we can check and use each one
            name_parts = schema.split(".")

            # Do nothing if this is a schema inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            database = name_parts[0]
            if database in shared_dbs:
                continue

            conn = SnowflakeConnector()
            fetched_schemas = conn.full_schema_list(schema)
            write_grant_schemas.extend(fetched_schemas)

            if name_parts[1] == "*":
                # If <db_name>.* then you can grant future and add future schema to grant list
                future_schema = f"{database}.<schema>"
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
                            grouping_name=SnowflakeConnector.snowflaky(database),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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
                            resource_name=SnowflakeConnector.snowflaky(db_schema),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        return (sql_commands, write_grant_schemas)

    def _generate_schema_revokes(
        self, usage_schemas, all_grant_schemas, shared_dbs, spec_dbs, role
    ):
        sql_commands = []
        read_privileges = "usage"

        for granted_schema in usage_schemas:
            database_name = granted_schema.split(".")[0]
            future_schema_name = f"{database_name}.<schema>"
            if granted_schema not in all_grant_schemas and (
                database_name in shared_dbs or database_name not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif (  # If future privilege is granted on snowflake but not in grant list
                granted_schema == future_schema_name
                and future_schema_name not in all_grant_schemas  #
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=SnowflakeConnector.snowflaky(database_name),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
            elif (
                granted_schema not in all_grant_schemas
                and future_schema_name not in all_grant_schemas
            ):
                # Covers case where schema is granted in Snowflake
                # But it's not in the grant list and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=read_privileges,
                            resource_type="schema",
                            resource_name=SnowflakeConnector.snowflaky(granted_schema),
                            role=SnowflakeConnector.snowflaky_user_role(role),
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
        usage_schemas = set(
            self.grants_to_role.get(role, {}).get("usage", {}).get("schema", [])
        )
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

        other_schema_grants = list()
        for privilege in other_privileges:
            other_schema_grants.extend(
                self.grants_to_role.get(role, {}).get(privilege, {}).get("schema", [])
            )

        for granted_schema in other_schema_grants:
            database_name = granted_schema.split(".")[0]
            future_schema_name = f"{database_name}.<schema>"
            if granted_schema not in write_grant_schemas and (
                database_name in shared_dbs or database_name not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif (  # If future privilege is granted but not in grant list
                granted_schema == future_schema_name
                and future_schema_name not in write_grant_schemas
            ):
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="schema",
                            grouping_type="database",
                            grouping_name=SnowflakeConnector.snowflaky(database_name),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
            elif (
                granted_schema not in write_grant_schemas
                and future_schema_name not in write_grant_schemas
            ):
                # Covers case where schema is granted and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=partial_write_privileges,
                            resource_type="schema",
                            resource_name=SnowflakeConnector.snowflaky(granted_schema),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )

        return sql_commands

    def _generate_table_read_grants(self, conn, tables, shared_dbs, role):
        sql_commands: List[Dict] = []
        grant_lists: Dict[str, List[str]] = {
            t.name: [] for t in TABLE_OBJECT_TYPES
        }

        for table in tables:
            # Split the table identifier into parts {DB_NAME}.{SCHEMA_NAME}.{TABLE_NAME}
            # so that we can check and use each one
            name_parts = table.split(".")
            database_name = name_parts[0] if 0 < len(name_parts) else None
            schema_name = name_parts[1] if 1 < len(name_parts) else None
            table_view_name = name_parts[2] if 2 < len(name_parts) else None

            # Do nothing if this is a table inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            if database_name in shared_dbs:
                continue

            fetched_schemas = conn.full_schema_list(f"{database_name}.{schema_name}")

            for object_type in TABLE_OBJECT_TYPES:
                privileges = object_type.read_privileges

                # For grants at the database level
                future_database_object = (
                    f"{database_name}.{object_type.future_placeholder}"
                )
                already_granted = self.is_granted_privilege(
                    role, privileges, object_type.name, future_database_object
                )
                grant_lists[object_type.name].append(future_database_object)

                if schema_name == "*" and table_view_name == "*":
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
                                    grouping_name=SnowflakeConnector.snowflaky(
                                        database_name
                                    ),
                                    role=SnowflakeConnector.snowflaky_user_role(role),
                                ),
                            }
                        )

                # All objects of this type in the fetched schemas, so we can
                # check that a name given in the config is valid
                object_list = []
                for schema in fetched_schemas:
                    object_list.extend(
                        getattr(conn, object_type.connector_method)(schema=schema)
                    )

                # Objects of this type that privileges will be granted to
                grant_objects = []

                if table_view_name == "*":
                    # If <schema_name>.* then you add all objects to the grant
                    # list and then grant future. If *.* was provided then
                    # full_schema_list already fetched all schemas.
                    grant_lists[object_type.name].extend(object_list)

                    for schema in fetched_schemas:
                        future_object = f"{schema}.{object_type.future_placeholder}"
                        grant_lists[object_type.name].append(future_object)

                        already_granted = self.is_granted_privilege(
                            role, privileges, object_type.name, future_object
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
                                        grouping_name=SnowflakeConnector.snowflaky(
                                            schema
                                        ),
                                        role=SnowflakeConnector.snowflaky_user_role(
                                            role
                                        ),
                                    ),
                                }
                            )

                # TODO Future elif to have partial table name

                else:
                    # The table passed is a single entity:
                    # check that it's valid and add to list
                    if table in object_list:
                        grant_objects = [table]
                        grant_lists[object_type.name].append(table)

                # Grant explicitly to each flagged object instead of
                # granting to all objects in the schema
                for db_object in grant_objects:
                    already_granted = self.is_granted_privilege(
                        role, privileges, object_type.name, db_object
                    )

                    sql_commands.append(
                        {
                            "already_granted": already_granted,
                            "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                                privileges=privileges,
                                resource_type=object_type.name,
                                resource_name=SnowflakeConnector.snowflaky(db_object),
                                role=SnowflakeConnector.snowflaky_user_role(role),
                            ),
                        }
                    )

        return sql_commands, grant_lists

    def _generate_table_write_grants(self, conn, tables, shared_dbs, role):
        sql_commands: List[Dict] = []
        grant_lists: Dict[str, List[str]] = {
            t.name: [] for t in TABLE_OBJECT_TYPES
        }

        for table in tables:
            name_parts = table.split(".")
            database_name = name_parts[0] if 0 < len(name_parts) else None
            schema_name = name_parts[1] if 1 < len(name_parts) else None
            table_view_name = name_parts[2] if 2 < len(name_parts) else None

            # Do nothing if this is a table inside a shared database:
            # "Granting individual privileges on imported databases is not allowed."
            if database_name in shared_dbs:
                continue

            fetched_schemas = conn.full_schema_list(f"{database_name}.{schema_name}")

            for object_type in TABLE_OBJECT_TYPES:
                privileges = object_type.write_privileges
                privileges_array = privileges.split(", ")

                future_database_object = (
                    f"{database_name}.{object_type.future_placeholder}"
                )
                already_granted = self._all_privileges_granted(
                    role, privileges_array, object_type.name, future_database_object
                )
                grant_lists[object_type.name].append(future_database_object)

                if schema_name == "*" and table_view_name == "*":
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
                                    grouping_name=SnowflakeConnector.snowflaky(
                                        database_name
                                    ),
                                    role=SnowflakeConnector.snowflaky_user_role(role),
                                ),
                            }
                        )

                object_list = []
                for schema in fetched_schemas:
                    object_list.extend(
                        getattr(conn, object_type.connector_method)(schema=schema)
                    )

                grant_objects = []

                if table_view_name == "*":
                    grant_lists[object_type.name].extend(object_list)

                    for schema in fetched_schemas:
                        future_object = f"{schema}.{object_type.future_placeholder}"
                        grant_lists[object_type.name].append(future_object)

                        already_granted = self._all_privileges_granted(
                            role, privileges_array, object_type.name, future_object
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
                                        grouping_name=SnowflakeConnector.snowflaky(
                                            schema
                                        ),
                                        role=SnowflakeConnector.snowflaky_user_role(
                                            role
                                        ),
                                    ),
                                }
                            )

                # TODO Future elif to have partial table name

                else:
                    if table in object_list:
                        grant_objects = [table]
                        grant_lists[object_type.name].append(table)

                for db_object in grant_objects:
                    already_granted = self._all_privileges_granted(
                        role, privileges_array, object_type.name, db_object
                    )

                    sql_commands.append(
                        {
                            "already_granted": already_granted,
                            "sql": GRANT_PRIVILEGES_TEMPLATE.format(
                                privileges=privileges,
                                resource_type=object_type.name,
                                resource_name=SnowflakeConnector.snowflaky(db_object),
                                role=SnowflakeConnector.snowflaky_user_role(role),
                            ),
                        }
                    )

        return sql_commands, grant_lists

    def _generate_revoke_select_privs(
        self,
        role: str,
        all_grant_resources: List[str],
        shared_dbs: Set[Any],
        spec_dbs: Set[Any],
        privilege_set: str,
        resource_type: str,
        granted_resources: List[str],
    ) -> List[Dict[str, Any]]:
        """
        Generates REVOKE privileges for tables/views known as resources here

        role: Snowflake role to revoke the resource from
        all_grant_resources: All the GRANTS applied
        shared_dbs: Shared databases to be skipped
        spec_dbs: Databases to apply REVOKE statements on
        privilege_set: Privileges to revoke (i.e. SELECT, INSERT, etc.)
        resource_type: Database object to revoke (i.e. table, view, etc.)
        granted_resources: List of GRANTS to filter through

        Returns a list of REVOKE statements
        """
        sql_commands = []
        for granted_resource in granted_resources:
            resource_split = granted_resource.split(".")
            database_name = resource_split[0]
            schema_name = resource_split[1] if 1 < len(resource_split) else None

            # For future grants at the database level
            if len(resource_split) == 2 or (
                len(resource_split) == 3 and schema_name == "*"
            ):
                future_resource = f"{database_name}.<{resource_type}>"
                grouping_type = "database"
                grouping_name = database_name
            else:
                future_resource = f"{database_name}.{schema_name}.<{resource_type}>"
                grouping_type = "schema"
                grouping_name = f"{database_name}.{schema_name}"

            if granted_resource not in all_grant_resources and (
                database_name in shared_dbs or database_name not in spec_dbs
            ):
                # No privileges to revoke on imported db. Done at database level
                # Don't revoke on privileges on databases not defined in spec.
                continue
            elif (
                granted_resource == future_resource
                and future_resource not in all_grant_resources
            ):
                # If future privilege is granted in Snowflake but not in grant list
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_FUTURE_PRIVILEGES_TEMPLATE.format(
                            privileges=privilege_set,
                            resource_type=resource_type,
                            grouping_type=grouping_type,
                            grouping_name=SnowflakeConnector.snowflaky(grouping_name),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
            elif (
                granted_resource not in all_grant_resources
                and future_resource not in all_grant_resources
            ):
                # Covers case where resource is granted in Snowflake
                # But it's not in the grant list and it's not explicitly granted as a future grant
                sql_commands.append(
                    {
                        "already_granted": False,
                        "sql": REVOKE_PRIVILEGES_TEMPLATE.format(
                            privileges=privilege_set,
                            resource_type=resource_type,
                            resource_name=SnowflakeConnector.snowflaky(
                                granted_resource
                            ),
                            role=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        return sql_commands

    def generate_revoke_privs(
        self,
        role: str,
        shared_dbs: Set[Any],
        spec_dbs: Set[Any],
        all_grants_by_type: Dict[str, List[str]],
        write_grants_by_type: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        sql_commands = []

        for object_type in TABLE_OBJECT_TYPES:
            granted_resources = list(
                set(
                    self.grants_to_role.get(role, {})
                    .get(object_type.read_privileges, {})
                    .get(object_type.name, [])
                )
            )
            sql_commands.extend(
                self._generate_revoke_select_privs(
                    role=role,
                    all_grant_resources=all_grants_by_type[object_type.name],
                    shared_dbs=shared_dbs,
                    spec_dbs=spec_dbs,
                    privilege_set=object_type.read_privileges,
                    resource_type=object_type.name,
                    granted_resources=granted_resources,
                )
            )

        # Write privileges only need revoking for writable types:
        # select-only types (views, dynamic tables) are fully covered above
        for object_type in TABLE_OBJECT_TYPES:
            if not object_type.is_writable:
                continue

            write_partial_privileges = object_type.write_partial_privileges
            granted_resources_set = set()
            for privilege in write_partial_privileges.split(", "):
                granted_resources_set.update(
                    self.grants_to_role.get(role, {})
                    .get(privilege, {})
                    .get(object_type.name, [])
                )

            sql_commands.extend(
                self._generate_revoke_select_privs(
                    role=role,
                    all_grant_resources=write_grants_by_type[object_type.name],
                    shared_dbs=shared_dbs,
                    spec_dbs=spec_dbs,
                    privilege_set=write_partial_privileges,
                    resource_type=object_type.name,
                    granted_resources=list(granted_resources_set),
                )
            )

        return sql_commands

    def generate_table_and_view_grants(
        self, role: str, tables: Dict[str, List], shared_dbs: Set, spec_dbs: Set
    ) -> List[Dict]:
        """
        Generate the GRANT and REVOKE statements for all table-like object
        types in the registry (tables, views, iceberg/dynamic tables),
        including future grants.

        role: the name of the role the privileges are GRANTed to
        tables: read/write lists of object names (e.g. "raw.public.my_table")
        shared_dbs: a set of all the shared databases defined in the spec.
        spec_dbs: a set of all the databases defined in the spec. This is used in revoke
                  commands to validate revocations are only for spec'd databases

        Returns the SQL commands generated as a List
        """
        sql_commands = []

        conn = SnowflakeConnector()

        read_commands, read_grants_by_type = self._generate_table_read_grants(
            conn, tables.get("read", []), shared_dbs, role
        )
        sql_commands.extend(read_commands)

        write_commands, write_grants_by_type = self._generate_table_write_grants(
            conn, tables.get("write", []), shared_dbs, role
        )
        sql_commands.extend(write_commands)

        all_grants_by_type = {
            t.name: read_grants_by_type[t.name] + write_grants_by_type[t.name]
            for t in TABLE_OBJECT_TYPES
        }

        sql_commands.extend(
            self.generate_revoke_privs(
                role,
                shared_dbs,
                spec_dbs,
                all_grants_by_type,
                write_grants_by_type,
            )
        )
        return sql_commands

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
                property_value = config.get(identifier)
                alter_privileges.append(
                    f"{property_name} = {SnowflakeConnector.snowflaky(property_value)}"
                )

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
        quoted_roles = [
            f"'{SnowflakeConnector.snowflaky_user_role(role)}'"
            for role in secondary_roles
        ]
        roles_list = ", ".join(quoted_roles)
        alter_privileges.append(f"DEFAULT_SECONDARY_ROLES = ({roles_list})")

        if alter_privileges:
            sql_commands.append(
                {
                    "already_granted": False,
                    "sql": ALTER_USER_TEMPLATE.format(
                        user_name=SnowflakeConnector.snowflaky_user_role(user),
                        privileges=", ".join(alter_privileges),
                    ),
                }
            )

        return sql_commands

    def _generate_ownership_grant_database(
        self, role: str, database_refs: List[str]
    ) -> List[Dict]:
        sql_commands = []
        for database in database_refs:
            already_granted = self.is_granted_privilege(
                role, "ownership", "database", database
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                        resource_type="database",
                        resource_name=SnowflakeConnector.snowflaky(database),
                        role_name=SnowflakeConnector.snowflaky_user_role(role),
                    ),
                }
            )
        return sql_commands

    def _generate_ownership_grant_schema(self, conn, role, schema_refs) -> List[Dict]:
        sql_commands = []
        for schema in schema_refs:
            name_parts = schema.split(".")
            info_schema = f"{name_parts[0]}.information_schema"

            schemas = []

            if name_parts[1] == "*":
                db_schemas = conn.show_schemas(name_parts[0])

                for db_schema in db_schemas:
                    if db_schema != info_schema:
                        schemas.append(db_schema)
            else:
                schemas = [schema]

            for db_schema in schemas:
                already_granted = self.is_granted_privilege(
                    role, "ownership", "schema", db_schema
                )

                sql_commands.append(
                    {
                        "already_granted": already_granted,
                        "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                            resource_type="schema",
                            resource_name=SnowflakeConnector.snowflaky(db_schema),
                            role_name=SnowflakeConnector.snowflaky_user_role(role),
                        ),
                    }
                )
        return sql_commands

    def _generate_ownership_grant_table(
        self, conn: SnowflakeConnector, role, table_refs
    ) -> List[Dict]:
        sql_commands = []

        tables = []

        for table in table_refs:
            name_parts = table.split(".")
            info_schema = f"{name_parts[0]}.information_schema"

            if name_parts[2] == "*":
                schemas = []

                if name_parts[1] == "*":
                    db_schemas = conn.show_schemas(name_parts[0])

                    for schema in db_schemas:
                        if schema != info_schema:
                            schemas.append(schema)
                else:
                    schemas = [f"{name_parts[0]}.{name_parts[1]}"]

                for schema in schemas:
                    tables.extend(conn.show_tables(schema=schema))
            else:
                schemas = [f"{name_parts[0]}.{name_parts[1]}"]
                tables.append(table)

        existing_views = []
        for schema in schemas:
            existing_views = conn.show_views(schema=schema)

        # And then grant ownership to all tables
        for db_table in tables:
            # In case `db_table` does not exist, call it a table.
            # Regardless, the SQL will be validated later and alert it doesn't exist.
            resource_type = "table"
            if SnowflakeConnector.snowflaky(db_table) in existing_views:
                resource_type = "view"

            already_granted = self.is_granted_privilege(
                role, "ownership", resource_type, db_table
            )

            sql_commands.append(
                {
                    "already_granted": already_granted,
                    "sql": GRANT_OWNERSHIP_TEMPLATE.format(
                        resource_type=resource_type,
                        resource_name=SnowflakeConnector.snowflaky(db_table),
                        role_name=SnowflakeConnector.snowflaky_user_role(role),
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
