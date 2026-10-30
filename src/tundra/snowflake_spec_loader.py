import os
from typing import Any, Dict, Iterable, List, Optional, Set, cast

import click

from tundra.concurrency import parallel_map
from tundra.entities import EntityGenerator
from tundra.error import SpecLoadingError
from tundra.identifiers import (
    InvalidNameError,
    NamePattern,
    ObjectName,
    granted_name,
    name_or_pattern,
)
from tundra.logger import GLOBAL_LOGGER as logger
from tundra.snowflake_connector import (
    APPLICATION_ROLE_GRANT_TYPE,
    DATABASE_ROLE_GRANT_TYPE,
    ReadStats,
    SnowflakeConnector,
    api_timeout,
)
from tundra.snowflake_grants import SnowflakeGrantsGenerator
from tundra.spec_file_loader import load_spec
from tundra.table_object_types import PRIVILEGE_FAMILIES

VALIDATION_ERR_MSG = 'Spec error: {} "{}", field "{}": {}'


def _filter_error(name: Any, noun: str) -> Optional[str]:
    try:
        ObjectName.single(name)
    except InvalidNameError as error:
        return f"Name error: Not a valid {noun} name for --{noun}: {error}"
    return None


def _check_filters(
    roles: Optional[Iterable[Any]] = None, users: Optional[Iterable[Any]] = None
) -> None:
    """Raise a SpecLoadingError for every --role and --user value that isn't a name."""
    errors = [
        error
        for noun, names in (("role", roles), ("user", users))
        for name in dict.fromkeys(names or ())
        if (error := _filter_error(name, noun))
    ]
    if errors:
        raise SpecLoadingError("\n".join(errors))


def _single_names(names, noun: str) -> Optional[Set[ObjectName]]:
    """A --role or --user filter as names, or None when it isn't given."""
    if not names:
        return None
    names = list(names)
    _check_filters(**{f"{noun}s": names})
    return set(map(ObjectName.single, names))


def _same_role(owner_on_snowflake: Any, owner_in_spec: Any) -> bool:
    """
    Whether the spec names the role's owner on Snowflake. A spelling that isn't a role
    name names no role.
    """
    if not owner_on_snowflake:
        return False
    try:
        return ObjectName.single(owner_on_snowflake) == ObjectName.single(owner_in_spec)
    except InvalidNameError:
        return False


class SnowflakeSpecLoader:
    def __init__(
        self,
        spec_path: str,
        conn: Optional[SnowflakeConnector] = None,
        roles: Optional[List[str]] = None,
        users: Optional[List[str]] = None,
        run_list: Optional[List[str]] = None,
        ignore_memberships: Optional[bool] = False,
        spec_test: Optional[bool] = False,
        skip_validation: Optional[bool] = False,
        ignore_missing_objects: Optional[bool] = False,
        max_workers: Optional[int] = None,
        sql_only: Optional[bool] = False,
    ) -> None:
        run_list = run_list or ["users", "roles"]
        # The filters are read again when grants are fetched, so an iterator is
        # copied before the check uses it up.
        roles = None if roles is None else list(roles)
        users = None if users is None else list(users)
        _check_filters(roles, users)
        # A bad PERMISSION_BOT_API_TIMEOUT fails here, before anything is read, whatever
        # connector was passed in
        api_timeout()
        self.conn = conn or SnowflakeConnector()
        if sql_only:
            self.conn.sql_only = True
        self.max_workers = (
            max_workers
            if max_workers is not None
            else int(os.getenv("PERMISSION_BOT_MAX_WORKERS", "8"))
        )
        # Load the specification file and check for (syntactical) errors
        click.secho("Loading spec file", fg="green")
        self.spec = load_spec(spec_path)

        # Generate the entities (e.g databases, schemas, users, etc) referenced
        #  by the spec file and make sure that no syntactical or reference errors
        #  exist (all referenced entities are also defined by the spec)
        click.secho("Checking spec file for errors", fg="green")
        entity_generator = EntityGenerator(spec=self.spec)
        self.entities = entity_generator.inspect_entities()

        # Connect to Snowflake to make sure that the current user has correct
        # permissions
        click.secho("Checking permissions on current snowflake connection", fg="green")
        self.check_permissions_on_snowflake_server(self.conn)

        # Connect to Snowflake to make sure that all entities defined in the
        # spec file are also defined in Snowflake (no missing databases, etc)
        # Store missing entities for filtering if ignore_missing_objects is set
        self.missing_entities: Dict[str, Any] = {
            "warehouses": set(),
            "integrations": set(),
            "databases": set(),
            "schemas": set(),
            "tables": set(),
            "tasks": set(),
            "roles": set(),
            "database_roles": set(),
            "users": set(),
        }

        if not skip_validation:
            click.secho(
                "Checking that all entities in the spec file are defined in Snowflake",
                fg="green",
            )
            self.check_entities_on_snowflake_server(
                self.conn, bool(ignore_missing_objects)
            )
        else:
            click.secho(
                "Skipping entity validation checks (--skip-validation flag set)",
                fg="yellow",
            )
            # Application roles are only told apart from database roles by asking
            # Snowflake, so that one lookup still runs when validation is skipped.
            self.resolve_application_role_refs(self.conn)

        # Get the privileges granted to users and roles in the Snowflake account
        # Used in order to figure out which permissions in the spec file are
        #  new ones and which already exist (and there is no need to re-grant them)
        self.grants_to_role: Dict[ObjectName, Any] = {}
        self.roles_granted_to_user: Dict[ObjectName, Any] = {}
        self.tags_on_user: Dict[ObjectName, Dict[ObjectName, str]] = {}
        self.account_grants_to_role: Dict[ObjectName, Dict[str, Any]] = {}

        if not spec_test:
            click.secho("Fetching granted privileges from Snowflake", fg="green")
            self.get_privileges_from_snowflake_server(
                self.conn,
                roles=roles,
                users=users,
                run_list=run_list,
                ignore_memberships=ignore_memberships,
            )
        else:
            # spec-test reads nothing more. A run reports its reads at the end of
            # generate_permission_queries, which lists the schemas of db.* entries.
            self.report_reads()

    def report_reads(self) -> None:
        """One line on how the listings so far were read: through the API or SQL."""
        stats = getattr(self.conn, "read_stats", None)
        if not isinstance(stats, ReadStats) or not stats.listings:
            return
        click.secho(
            stats.summary(sql_only=self.conn.sql_only),
            fg="yellow" if stats.fallbacks else "green",
        )

    @staticmethod
    def check_permissions_on_snowflake_server(
        conn: Optional[SnowflakeConnector] = None,
    ) -> None:
        if conn is None:
            conn = SnowflakeConnector()
        error_messages = []

        click.secho(f"  Current user is: {conn.get_current_user()}.", fg="green")

        current_role = conn.get_current_role()
        if current_role != "securityadmin" and not conn.is_role_in_session(
            "securityadmin"
        ):
            error_messages.append(
                "Current role is not securityadmin and does not inherit it! "
                "Tundra needs MANAGE GRANTS: run as securityadmin, or as a role that "
                "has been granted securityadmin."
            )
        click.secho(f"  Current role is: {current_role}.", fg="green")

        if error_messages:
            raise SpecLoadingError("\n".join(error_messages))

    def check_warehouse_entities(self, conn):
        error_messages = []
        if len(self.entities["warehouses"]) > 0:
            warehouses = set(map(ObjectName.single, conn.show_warehouses()))
            for warehouse in map(ObjectName.single, self.entities["warehouses"]):
                if warehouse not in warehouses:
                    self.missing_entities["warehouses"].add(warehouse)
                    error_messages.append(
                        f"Missing Entity Error: Warehouse {warehouse} was not found on"
                        " Snowflake Server. Please create it before continuing."
                    )
        else:
            logger.debug(
                "`warehouses` not found in spec, skipping SHOW WAREHOUSES call."
            )
        return error_messages

    def check_integration_entities(self, conn):
        error_messages = []
        if len(self.entities["integrations"]) > 0:
            integrations = set(map(ObjectName.single, conn.show_integrations()))
            for integration in map(ObjectName.single, self.entities["integrations"]):
                if integration not in integrations:
                    self.missing_entities["integrations"].add(integration)
                    error_messages.append(
                        f"Missing Entity Error: Integration {integration} was not found on"
                        " Snowflake Server. Please create it before continuing."
                    )
        else:
            logger.debug(
                "`integrations` not found in spec, skipping SHOW INTEGRATIONS call."
            )
        return error_messages

    def check_database_entities(self, conn):
        error_messages = []
        if len(self.entities["databases"]) > 0:
            databases = set(map(ObjectName.single, conn.show_databases()))
            for db in map(ObjectName.single, self.entities["databases"]):
                if db not in databases:
                    self.missing_entities["databases"].add(db)
                    error_messages.append(
                        f"Missing Entity Error: Database {db} was not found on"
                        " Snowflake Server. Please create it before continuing."
                    )
        else:
            logger.debug("`databases` not found in spec, skipping SHOW DATABASES call.")
        return error_messages

    def check_schema_ref_entities(self, conn):
        error_messages = []
        if len(self.entities["schema_refs"]) > 0:
            schemas = set(map(ObjectName, conn.show_schemas()))
            for schema in map(name_or_pattern, self.entities["schema_refs"]):
                if isinstance(schema, ObjectName) and schema not in schemas:
                    self.missing_entities["schemas"].add(schema)
                    error_messages.append(
                        f"Missing Entity Error: Schema {schema} was not found on"
                        " Snowflake Server. Please create it before continuing."
                    )
        else:
            logger.debug("`schemas` not found in spec, skipping SHOW SCHEMAS call.")

        return error_messages

    def check_table_ref_entities(self, conn):
        error_messages = []
        if len(self.entities["table_refs"]) > 0:
            views = set(map(ObjectName, conn.show_views()))
            databases = list(self.entities["tables_by_database"].keys())
            existing_by_db = dict(
                zip(
                    databases,
                    parallel_map(
                        lambda d: set(map(ObjectName, conn.show_tables(database=d))),
                        databases,
                        self.max_workers,
                    ),
                )
            )
            for db, tables in self.entities["tables_by_database"].items():
                existing_tables = existing_by_db[db]
                for table in map(name_or_pattern, tables):
                    if (
                        isinstance(table, ObjectName)
                        and table not in existing_tables
                        and table not in views
                    ):
                        self.missing_entities["tables"].add(table)
                        error_messages.append(
                            f"Missing Entity Error: Table/View {table} was not found on"
                            " Snowflake Server. Please create it before continuing."
                        )
        else:
            logger.debug("`tables` not found in spec, skipping SHOW TABLES/VIEWS call.")
        return error_messages

    def check_task_ref_entities(self, conn):
        error_messages = []
        if len(self.entities["task_refs"]) > 0:
            databases = list(self.entities["tasks_by_database"].keys())
            existing_by_db = dict(
                zip(
                    databases,
                    parallel_map(
                        lambda d: set(map(ObjectName, conn.show_tasks(database=d))),
                        databases,
                        self.max_workers,
                    ),
                )
            )
            for db, tasks in self.entities["tasks_by_database"].items():
                for task in map(name_or_pattern, tasks):
                    if isinstance(task, ObjectName) and task not in existing_by_db[db]:
                        self.missing_entities["tasks"].add(task)
                        error_messages.append(
                            f"Missing Entity Error: Task {task} was not found on"
                            " Snowflake Server. Please create it before continuing."
                        )
        else:
            logger.debug("`tasks` not found in spec, skipping SHOW TASKS call.")
        return error_messages

    def check_role_entities(self, conn):
        error_messages = []
        if len(self.entities["roles"]) > 0:
            roles = conn.show_roles()
            existing = {ObjectName.single(role): role for role in roles}
            for role in self.spec["roles"]:
                for role_name, config in role.items():
                    name = ObjectName.single(role_name)
                    if name not in existing:
                        self.missing_entities["roles"].add(name)
                        error_messages.append(
                            f"Missing Entity Error: Role {role_name} was not found on"
                            " Snowflake Server. Please create it before continuing."
                        )
                    elif "owner" in config:
                        owner_on_snowflake = roles[existing[name]]
                        owner_in_spec = config["owner"]
                        if not _same_role(owner_on_snowflake, owner_in_spec):
                            error_messages.append(
                                f"Role {role_name} has owner {owner_on_snowflake} on snowflake, "
                                f"but has owner {owner_in_spec} defined in the spec file."
                            )
        else:
            logger.debug("`roles` not found in spec, skipping SHOW ROLES call.")
        return error_messages

    def resolve_application_role_refs(self, conn) -> List[ObjectName]:
        """
        Sort the DB.ROLE references in member_of into database roles and application
        roles by asking Snowflake, and return the references that are neither.

        The spec spells both kinds the same way (snowflake.usage_viewer is a database
        role, snowflake.cortex_analyst_requests_viewer an application role), so a
        reference SHOW DATABASE ROLES does not know is looked up with SHOW APPLICATION
        ROLES for an application of the same name. Resolved application roles move to
        entities["application_role_refs"] so the grants generator emits GRANT
        APPLICATION ROLE for them.
        """
        unresolved: List[ObjectName] = []
        refs_by_database: Dict[ObjectName, Set[ObjectName]] = {}
        for database_role in map(
            ObjectName.role_reference, self.entities["database_role_refs"]
        ):
            refs_by_database.setdefault(database_role.database, set()).add(
                database_role
            )

        for database, database_roles in sorted(refs_by_database.items()):
            existing_database_roles = set(
                map(
                    ObjectName.role_reference,
                    conn.show_database_roles(database=database),
                )
            )
            not_database_roles = [
                database_role
                for database_role in sorted(database_roles)
                if database_role not in existing_database_roles
            ]
            if not not_database_roles:
                continue

            existing_application_roles = set(
                map(
                    ObjectName.role_reference,
                    conn.show_application_roles(application=database),
                )
            )
            for ref in not_database_roles:
                if ref in existing_application_roles:
                    self.entities["application_role_refs"].add(ref)
                    self.entities["database_role_refs"].discard(ref)
                else:
                    unresolved.append(ref)

        return unresolved

    def check_database_role_entities(self, conn):
        error_messages = []
        if len(self.entities["database_role_refs"]) > 0:
            for database_role in self.resolve_application_role_refs(conn):
                self.missing_entities["database_roles"].add(database_role)
                error_messages.append(
                    f"Missing Entity Error: Database role {database_role} was not"
                    " found on Snowflake Server, as a database role or an application"
                    " role. Please create it before continuing."
                )
        else:
            logger.debug(
                "no database roles referenced in spec, skipping SHOW DATABASE ROLES call."
            )
        return error_messages

    def check_users_entities(self, conn):
        error_messages = []
        if len(self.entities["users"]) > 0:
            users = set(map(ObjectName.single, conn.show_users()))
            for user in map(ObjectName.single, self.entities["users"]):
                if user not in users:
                    self.missing_entities["users"].add(user)
                    error_messages.append(
                        f"Missing Entity Error: User {user} was not found on"
                        " Snowflake Server. Please create it before continuing."
                    )
        else:
            logger.debug("`users` not found in spec, skipping SHOW USERS call.")
        return error_messages

    def check_entities_on_snowflake_server(  # noqa
        self, conn: Optional[SnowflakeConnector] = None, ignore_missing: bool = False
    ) -> None:
        """
        Make sure that all [warehouses, integrations, dbs, schemas, tables, users,
        roles, database roles] referenced in the spec are defined in Snowflake.

        If ignore_missing is True, stores missing entities in self.missing_entities
        instead of raising errors.

        Raises a SpecLoadingError with all the errors found while checking
        Snowflake for missing entities (only if ignore_missing is False).
        """
        error_messages = []

        if conn is None:
            conn = self.conn

        error_messages.extend(self.check_warehouse_entities(conn))
        error_messages.extend(self.check_integration_entities(conn))
        error_messages.extend(self.check_database_entities(conn))
        error_messages.extend(self.check_schema_ref_entities(conn))
        error_messages.extend(self.check_table_ref_entities(conn))
        error_messages.extend(self.check_task_ref_entities(conn))
        error_messages.extend(self.check_role_entities(conn))
        error_messages.extend(self.check_database_role_entities(conn))
        error_messages.extend(self.check_users_entities(conn))

        if error_messages:
            if ignore_missing:
                click.secho(
                    f"Warning: {len(error_messages)} missing entities found, ignoring them (--ignore-missing-objects flag set)",
                    fg="yellow",
                )
                for msg in error_messages:
                    logger.info(msg)
            else:
                raise SpecLoadingError("\n".join(error_messages))

    def _merge_single_role_grants(self, target, role, results):
        for privilege in results:
            for grant_on in results[privilege]:
                held = self.filter_to_database_refs(
                    grant_on=grant_on, filter_set=results[privilege][grant_on]
                )
                if held:
                    target.setdefault(role, {}).setdefault(privilege, {}).setdefault(
                        grant_on, []
                    ).extend(held)

    def get_role_privileges_from_snowflake_server(
        self,
        conn: SnowflakeConnector,
        roles: Optional[List[str]] = None,
        ignore_memberships: Optional[bool] = False,
    ) -> None:
        future_grants: Dict[ObjectName, Any] = {}
        wanted = _single_names(roles, "role")

        # A member_of * is in entities["roles"] as a pattern, not a role to fetch
        spec_roles = [
            role
            for role in self.entities["roles"]
            if not isinstance(role, NamePattern)
            and not (wanted and ObjectName.single(role) not in wanted)
        ]

        # --- Fetch phase (parallel) ---
        # Future grants are read per role, in every database, so they're read under
        # --ignore-memberships too. A role that's missing holds none, and the merge
        # drops the databases the spec doesn't track.
        future_by_role = dict(
            zip(
                spec_roles,
                parallel_map(
                    lambda r: conn.show_future_grants_to_role(r),
                    spec_roles,
                    self.max_workers,
                ),
            )
        )

        role_list = [] if ignore_memberships else spec_roles
        role_grants_by_role = dict(
            zip(
                role_list,
                parallel_map(
                    lambda r: conn.show_grants_to_role(r), role_list, self.max_workers
                ),
            )
        )

        # Account-level privileges are only reconciled for roles that declare
        # `account_privileges`, and need the grantor of each existing grant, which
        # grants_to_role does not carry.
        account_privilege_roles = self.entities.get("account_privilege_roles", set())
        declaring_roles = [
            role for role in role_list if role in account_privilege_roles
        ]
        self.account_grants_to_role = dict(
            zip(
                map(ObjectName.single, declaring_roles),
                parallel_map(
                    lambda r: conn.show_account_grants_to_role(r),
                    declaring_roles,
                    self.max_workers,
                ),
            )
        )

        # --- Merge phase (serial, deterministic, original order) ---
        for role in spec_roles:
            self._merge_single_role_grants(
                future_grants, ObjectName.single(role), future_by_role[role]
            )

        for role in role_list:
            self._merge_single_role_grants(
                future_grants, ObjectName.single(role), role_grants_by_role[role]
            )

        self.grants_to_role = future_grants

    def get_user_privileges_from_snowflake_server(
        self, conn: SnowflakeConnector, users: Optional[List[str]] = None
    ) -> None:
        wanted = _single_names(users, "user")
        user_entities = [
            user
            for user in self.entities["users"]
            if not (wanted and ObjectName.single(user) not in wanted)
        ]
        results = parallel_map(
            lambda u: conn.show_roles_granted_to_user(u),
            user_entities,
            self.max_workers,
        )
        for user, roles_granted in zip(user_entities, results):
            self.roles_granted_to_user[ObjectName.single(user)] = roles_granted

        # Every listed user's tags are fetched, not only those declaring `tags`: the
        # spec owns all of them, so a tag on a user with no `tags` key is unset.
        tag_results = parallel_map(
            lambda u: conn.show_tags_on_user(u),
            user_entities,
            self.max_workers,
        )
        self.tags_on_user = dict(
            zip(map(ObjectName.single, user_entities), tag_results)
        )

    def get_privileges_from_snowflake_server(
        self,
        conn: Optional[SnowflakeConnector] = None,
        roles: Optional[List[str]] = None,
        users: Optional[List[str]] = None,
        run_list: Optional[List[str]] = None,
        ignore_memberships: Optional[bool] = False,
    ) -> None:
        """
        Get the privileges granted to users and roles in the Snowflake account
        Gets the future privileges each spec role holds in the spec's databases
        Consolidates role and future privileges into a single object for self.grants_to_role
        """
        run_list = run_list or ["users", "roles"]
        if conn is None:
            conn = self.conn

        if "users" in run_list and not ignore_memberships:
            logger.info("Fetching user privileges from Snowflake")
            self.get_user_privileges_from_snowflake_server(conn=conn, users=users)

        if "roles" in run_list:
            logger.info("Fetching role privileges from Snowflake")
            self.get_role_privileges_from_snowflake_server(
                conn=conn, roles=roles, ignore_memberships=ignore_memberships
            )

    def filter_to_database_refs(
        self, grant_on: str, filter_set: Iterable[Any]
    ) -> List[ObjectName]:
        """
        Filter out grants to databases that are not tracked in the configuration file
        :param grant_on: entity to be granted on. e.g. GRANT SOMETHING ON {DATABASE|WAREHOUSE|INTEGRATION|SCHEMA}...
        :param filter_set: the names granted on, as ObjectNames or as SHOW GRANTS spells them
        :return: the names, less those of kinds tundra doesn't read and those in
            non-tracked databases.
        """
        database_refs = set(map(ObjectName.single, self.entities["database_refs"]))
        warehouse_refs = set(map(ObjectName.single, self.entities["warehouse_refs"]))
        integration_refs = set(
            map(ObjectName.single, self.entities["integration_refs"])
        )

        names: List[ObjectName] = []
        for item in filter_set:
            if not item:
                continue
            try:
                names.append(
                    item
                    if isinstance(item, ObjectName)
                    else granted_name(grant_on, item)
                )
            except InvalidNameError:
                continue

        # Databases is the simple case. Just return items that are also in the database_refs list
        if grant_on == "database":
            return [name for name in names if name in database_refs]
        # Warehouses are also a simple case.
        elif grant_on == "warehouse":
            return [name for name in names if name in warehouse_refs]
        # Integrations are also a simple case.
        elif grant_on == "integration":
            return [name for name in names if name in integration_refs]
        # Database and application roles are db.role / app.role FQNs, but membership
        # of them is managed the same way as account roles: the spec is the source of
        # truth regardless of whether the owning database is tracked. Pass through as-is.
        elif grant_on in (DATABASE_ROLE_GRANT_TYPE, APPLICATION_ROLE_GRANT_TYPE):
            return names
        else:
            # A single-part name, such as a role in `grant ownership on role
            # role_name to role grantee`, isn't in a database, so it's kept. Anything
            # else is in its first part's database, and kept only if that's tracked.
            return [
                name
                for name in names
                if (len(name.parts) == 1 and not name.is_future)
                or name.database in database_refs
            ]

    def filter_config_for_missing_entities(
        self, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Filter out missing entities from a role/user config.
        Returns a new config with missing entities removed.
        """
        if not any(self.missing_entities.values()):
            # No missing entities, return config as-is
            return config

        filtered_config = config.copy()

        # Filter warehouses
        if "warehouses" in filtered_config:
            filtered_config["warehouses"] = [
                w
                for w in filtered_config["warehouses"]
                if ObjectName.single(w) not in self.missing_entities["warehouses"]
            ]

        # Filter integrations
        if "integrations" in filtered_config:
            filtered_config["integrations"] = [
                i
                for i in filtered_config["integrations"]
                if ObjectName.single(i) not in self.missing_entities["integrations"]
            ]

        # Filter privileges.<family>: the missing_entities key matches the spec key
        privileges = filtered_config.get("privileges", {})
        for family in ["databases", "schemas", *PRIVILEGE_FAMILIES]:
            if family not in privileges:
                continue
            read = ObjectName.single if family == "databases" else name_or_pattern
            for access_type in ["read", "write"]:
                if access_type in privileges[family]:
                    privileges[family][access_type] = [
                        ref
                        for ref in privileges[family][access_type]
                        if read(ref) not in self.missing_entities[family]
                    ]

        return filtered_config

    def generate_permission_queries(
        self,
        roles: Optional[List[str]] = None,
        users: Optional[List[str]] = None,
        run_list: Optional[List[str]] = None,
        ignore_memberships: Optional[bool] = False,
    ) -> List[Dict]:
        """
        Starting point to generate all the permission queries.

        For each entity type (e.g. user or role) that is affected by the spec,
        the proper sql permission queries are generated.

        Returns all the SQL commands as a list.
        """
        run_list = run_list or ["users", "roles"]
        sql_commands: List[Dict] = []
        role_filter = _single_names(roles, "role")
        user_filter = _single_names(users, "user")

        generator = SnowflakeGrantsGenerator(
            self.grants_to_role,
            self.roles_granted_to_user,
            ignore_memberships=ignore_memberships,
            conn=self.conn,
            account_grants_to_role=self.account_grants_to_role,
            application_roles=self.entities["application_role_refs"],
            tags_on_user=self.tags_on_user,
        )

        click.secho("Generating permission Queries:", fg="green")

        # For each permission in the spec, check if we have to generate an
        #  SQL command granting that permission

        for entity_type, entry in self.spec.items():
            if entity_type in [
                "require-owner",
                "databases",
                "warehouses",
                "integrations",
                "external_volumes",
                "version",
            ]:
                continue

            # Generate list of all entities (used for roles currently)
            entry = cast(List, entry)
            all_entities = [list(entity.keys())[0] for entity in entry]

            for entity_dict in entry:
                entity_configs = [
                    (entity_name, config)
                    for entity_name, config in entity_dict.items()
                    if config
                ]
                for entity_name, config in entity_configs:
                    name = (
                        ObjectName.single(entity_name)
                        if entity_type in ("roles", "users")
                        else None
                    )
                    # Skip missing roles/users
                    if (
                        entity_type == "roles"
                        and name in self.missing_entities["roles"]
                    ):
                        logger.info(f"Skipping missing role: {entity_name}")
                        continue
                    if (
                        entity_type == "users"
                        and name in self.missing_entities["users"]
                    ):
                        logger.info(f"Skipping missing user: {entity_name}")
                        continue

                    # Filter out missing entities from config
                    filtered_config = self.filter_config_for_missing_entities(config)

                    if (
                        entity_type == "roles"
                        and "roles" in run_list
                        and (not role_filter or name in role_filter)
                    ):
                        sql_commands.extend(
                            self.process_roles(
                                generator,
                                entity_type,
                                entity_name,
                                filtered_config,
                                all_entities,
                            )
                        )
                    elif (
                        entity_type == "users"
                        and "users" in run_list
                        and (not user_filter or name in user_filter)
                    ):
                        sql_commands.extend(
                            self.process_users(
                                generator, entity_type, entity_name, filtered_config
                            )
                        )

        self.report_reads()
        return self.remove_duplicate_queries(sql_commands)

    # TODO: These functions are part of a refactor of the previous module,
    # but this still requires a fair bit of attention to cleanup
    def process_roles(self, generator, entity_type, entity_name, config, all_entities):
        sql_commands = []
        click.secho(f"     Processing role {entity_name}", fg="green")
        sql_commands.extend(
            generator.generate_grant_roles(
                entity_type, entity_name, config, all_entities
            )
        )

        sql_commands.extend(generator.generate_grant_ownership(entity_name, config))

        sql_commands.extend(
            generator.generate_grant_privileges_to_role(
                entity_name,
                config,
                self.entities["shared_databases"],
                self.entities["databases"],
            )
        )
        return sql_commands

    @staticmethod
    def process_users(generator, entity_type, entity_name, config):
        sql_commands = []
        click.secho(f"     Processing user {entity_name}", fg="green")
        sql_commands.extend(generator.generate_alter_user(entity_name, config))
        sql_commands.extend(generator.generate_user_tags(entity_name, config))

        sql_commands.extend(
            generator.generate_grant_roles(entity_type, entity_name, config)
        )
        return sql_commands

    @staticmethod
    def remove_duplicate_queries(sql_commands: List[Dict]) -> List[Dict]:
        grants = []
        revokes = []

        for i, command in reversed(list(enumerate(sql_commands))):
            # Find all "GRANT OWNERSHIP commands"
            if command["sql"].startswith("GRANT OWNERSHIP ON"):
                grant = command["sql"].split("TO ROLE", 1)[0]

                if grant in grants:
                    # If there is already a GRANT OWNERSHIP for the same
                    #  DB/SCHEMA/TABLE --> remove the one before it
                    #  (only keep the last one)
                    del sql_commands[i]
                else:
                    grants.append(grant)

            if command["sql"].startswith("REVOKE ALL"):
                revoke = command["sql"]
                if revoke in revokes:
                    # If there is already a REVOKE ALL for the same
                    #  DB/SCHEMA/TABLE --> remove the one before it
                    #  (only keep the last one)
                    del sql_commands[i]
                else:
                    revokes.append(revoke)

        return sql_commands
