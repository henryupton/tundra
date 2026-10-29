import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import quote_plus

import snowflake.connector
import sqlalchemy

# To support key pair authentication
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from snowflake.sqlalchemy import URL

from tundra.identifiers import (
    RESERVED_KEYWORDS,
    InvalidNameError,
    NameLike,
    NamePattern,
    ObjectName,
    granted_name,
    name_or_pattern,
)
from tundra.logger import GLOBAL_LOGGER as logger
from tundra.table_object_types import (
    ALL_OBJECT_TYPES,
    DYNAMIC_TABLE,
    ICEBERG_TABLE,
    SEMANTIC_VIEW,
    STREAMLIT,
    TABLE,
    TASK,
    VIEW,
    TableObjectType,
)

# The key database role grants are stored under in grants_to_role. Snowflake reports
# granted_on as either DATABASE_ROLE or DATABASE ROLE depending on the surface, so both
# spellings are collapsed to this on the way in. Unlike the table object types, which key
# on the underscore wire form, there is no SQL keyword/wire-form split to straddle here:
# this constant is both sides, and the spec side reaches it via the same normalisation.
DATABASE_ROLE_GRANT_TYPE = "database role"

# Application roles (app.role) are reported as APPLICATION_ROLE / APPLICATION ROLE and
# keyed under this, mirroring DATABASE_ROLE_GRANT_TYPE. The SNOWFLAKE application ships
# its telemetry viewers (CORTEX_ANALYST_REQUESTS_VIEWER, AI_OBSERVABILITY_READER, ...)
# as application roles, which GRANT DATABASE ROLE cannot reach.
APPLICATION_ROLE_GRANT_TYPE = "application role"

# External volume grants are keyed under this whether the row reports EXTERNAL_VOLUME or
# EXTERNAL VOLUME, like the two role kinds above.
EXTERNAL_VOLUME_GRANT_TYPE = "external volume"

# Snowflake's wording for a statement whose target is gone, e.g. `Schema 'DB.SCHEMA' does
# not exist or not authorized.` A missing parent is reported as the parent, so a grant on a
# table in a dropped schema names the schema. Only databases, schemas and registry objects
# are matched: roles, warehouses and users are spec-managed, so their absence is a real
# failure rather than something another process dropped mid-run.
MISSING_OBJECT_PATTERN = re.compile(
    r"\b(?P<kind>{}) '(?P<name>[^']+)' does not exist or not authorized".format(
        "|".join(
            sorted(
                ["database", "schema"] + [t.name for t in ALL_OBJECT_TYPES],
                key=len,
                reverse=True,
            )
        )
    ),
    re.IGNORECASE,
)

# SHOW ... IN SCHEMA against a schema that is gone fails with this rather than naming it.
MISSING_CONTAINER_ERROR = "Object does not exist, or operation cannot be performed"

# Don't show all the info log messages from Snowflake
for logger_name in ["snowflake.connector", "bot", "boto3"]:
    log = logging.getLogger(logger_name)
    log.setLevel(logging.WARNING)


class _BufferedResult:
    """Holds fully-fetched rows so callers can read them after the DB
    connection has been returned to the pool. Preserves the subset of the
    SQLAlchemy result interface the connector relies on."""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class SnowflakeConnector:
    def __init__(self, config: Optional[Dict] = None) -> None:
        max_workers = int(os.getenv("PERMISSION_BOT_MAX_WORKERS", "8"))
        pool_kwargs = {"pool_size": max_workers, "max_overflow": max_workers}

        if not config:
            config = {
                "user": os.getenv("PERMISSION_BOT_USER"),
                "password": quote_plus(os.getenv("PERMISSION_BOT_PASSWORD", "")),
                "account": os.getenv("PERMISSION_BOT_ACCOUNT"),
                "database": os.getenv("PERMISSION_BOT_DATABASE"),
                "role": os.getenv("PERMISSION_BOT_ROLE"),
                "warehouse": os.getenv("PERMISSION_BOT_WAREHOUSE"),
                "oauth_token": os.getenv("PERMISSION_BOT_OAUTH_TOKEN"),
                "key_path": os.getenv("PERMISSION_BOT_KEY_PATH"),
                "key_passphrase": os.getenv("PERMISSION_BOT_KEY_PASSPHRASE"),
                "authenticator": os.getenv("PERMISSION_BOT_AUTHENTICATOR"),
                "workload_identity_provider": os.getenv(
                    "PERMISSION_BOT_WORKLOAD_IDENTITY_PROVIDER"
                ),
                "token_command": os.getenv("PERMISSION_BOT_TOKEN_COMMAND"),
            }

        if config.get("token_command") is not None:
            # Workload identity federation with a short-lived token: the command is
            # rerun for every connection the pool opens, so a token that expired
            # mid-run is never presented.
            connect_kwargs = {
                key: value
                for key, value in {
                    "user": config["user"],
                    "account": config["account"],
                    "database": config["database"],
                    "role": config["role"],
                    "warehouse": config["warehouse"],
                    "authenticator": config["authenticator"] or "WORKLOAD_IDENTITY",
                    "workload_identity_provider": config["workload_identity_provider"],
                }.items()
                if value
            }
            token_command = config["token_command"]
            self.engine = sqlalchemy.create_engine(
                "snowflake://",
                creator=lambda: snowflake.connector.connect(
                    token=self.run_token_command(token_command), **connect_kwargs
                ),
                **pool_kwargs,
            )
        elif config["oauth_token"] is not None:
            self.engine = sqlalchemy.create_engine(
                URL(
                    user=config["user"],
                    account=config["account"],
                    authenticator="oauth",
                    token=config["oauth_token"],
                    warehouse=config["warehouse"],
                ),
                **pool_kwargs,
            )
        elif config["key_path"] is not None:
            pkb = self.generate_private_key(
                config["key_path"], config.get("key_passphrase")
            )
            self.engine = sqlalchemy.create_engine(
                URL(
                    user=config["user"],
                    account=config["account"],
                    database=config["database"],
                    role=config["role"],
                    warehouse=config["warehouse"],
                ),
                connect_args={"private_key": pkb},
                **pool_kwargs,
            )

        elif config["authenticator"] is not None:
            self.engine = sqlalchemy.create_engine(
                URL(
                    user=config["user"],
                    account=config["account"],
                    database=config["database"],
                    role=config["role"],
                    warehouse=config["warehouse"],
                    authenticator=config["authenticator"],
                ),
                **pool_kwargs,
            )
        else:
            if not config["user"]:
                raise Exception(
                    "Validation Error: PERMISSION_BOT_USER not set. Please ensure environment variables are set."
                )
            self.engine = sqlalchemy.create_engine(
                URL(
                    user=config["user"],
                    password=config["password"],
                    account=config["account"],
                    database=config["database"],
                    role=config["role"],
                    warehouse=config["warehouse"],
                    # Enable the insecure_mode if you get OCSP errors while testing
                    # insecure_mode=True,
                ),
                **pool_kwargs,
            )

    @staticmethod
    def run_token_command(command: str) -> str:
        """Run the shell command that prints a fresh auth token and return it."""
        result = subprocess.run(
            command, shell=True, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    @staticmethod
    def generate_private_key(key_path: str, key_passphrase: Union[str, None]) -> bytes:
        with open(key_path, "rb") as key:
            encoded_key = None
            if key_passphrase:
                encoded_key = key_passphrase.encode()
            p_key = serialization.load_pem_private_key(
                key.read(), password=encoded_key, backend=default_backend()
            )

        return p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    @staticmethod
    def _granted(granted_on: str, name: str) -> Optional[ObjectName]:
        """The name of a SHOW GRANTS row, or None for a kind tundra doesn't read."""
        try:
            return granted_name(granted_on, name)
        except InvalidNameError:
            return None

    def show_query(self, entity) -> List[ObjectName]:
        query = f"SHOW {entity}"
        results = self.run_query(query).fetchall()

        return [ObjectName.from_canonical(result["name"]) for result in results]

    def show_databases(self) -> List[ObjectName]:
        return self.show_query("DATABASES")

    def show_warehouses(self) -> List[ObjectName]:
        return self.show_query("WAREHOUSES")

    def show_integrations(self) -> List[ObjectName]:
        return self.show_query("INTEGRATIONS")

    def show_table_objects(
        self,
        object_type: TableObjectType,
        database: Optional[NameLike] = None,
        schema: Optional[NameLike] = None,
    ) -> List[ObjectName]:
        if schema:
            query = f"SHOW {object_type.show_command} IN SCHEMA {ObjectName(schema)}"
        elif database:
            query = (
                f"SHOW {object_type.show_command} IN DATABASE "
                f"{ObjectName.single(database)}"
            )
        else:
            query = f"SHOW {object_type.show_command} IN ACCOUNT"

        results = self.run_query(query).fetchall()

        return [
            ObjectName.from_canonical(
                result["database_name"], result["schema_name"], result["name"]
            )
            for result in results
        ]

    def show_tables(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(TABLE, database=database, schema=schema)

    def show_views(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(VIEW, database=database, schema=schema)

    def show_dynamic_tables(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(DYNAMIC_TABLE, database=database, schema=schema)

    def show_streamlits(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(STREAMLIT, database=database, schema=schema)

    def show_semantic_views(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(SEMANTIC_VIEW, database=database, schema=schema)

    def show_iceberg_tables(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(ICEBERG_TABLE, database=database, schema=schema)

    def show_tasks(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> List[ObjectName]:
        return self.show_table_objects(TASK, database=database, schema=schema)

    def show_users(self) -> List[ObjectName]:
        return self.show_query("USERS")

    def show_schemas(self, database: Optional[NameLike] = None) -> List[ObjectName]:
        if database:
            query = f"SHOW TERSE SCHEMAS IN DATABASE {ObjectName.single(database)}"
        else:
            query = "SHOW TERSE SCHEMAS IN ACCOUNT"

        results = self.run_query(query).fetchall()

        return [
            ObjectName.from_canonical(result["database_name"], result["name"])
            for result in results
        ]

    def show_future_grants(
        self, database: Optional[NameLike] = None, schema: Optional[NameLike] = None
    ) -> Dict[ObjectName, Dict[str, Dict[str, Set[ObjectName]]]]:
        future_grants: Dict[ObjectName, Any] = {}

        if schema:
            query = f"SHOW FUTURE GRANTS IN SCHEMA {ObjectName(schema)}"
        elif database:
            query = f"SHOW FUTURE GRANTS IN DATABASE {ObjectName.single(database)}"
        else:
            raise ValueError("show_future_grants needs a database or a schema")

        results = self.run_query(query).fetchall()

        for result in results:
            if result["grant_to"] != "ROLE":
                continue

            name = self._granted(result["grant_on"], result["name"])
            if name is None:
                continue

            role = ObjectName.from_canonical(result["grantee_name"])
            privilege = result["privilege"].lower()
            granted_on = result["grant_on"].lower()

            future_grants.setdefault(role, {}).setdefault(privilege, {}).setdefault(
                granted_on, set()
            ).add(name)

        return future_grants

    def show_grants_to_role(self, role) -> Dict[str, Dict[str, Set[ObjectName]]]:
        grants: Dict[str, Any] = {}
        if role in ["*", '"*"']:
            return grants

        query = f"SHOW GRANTS TO ROLE {ObjectName.single(role)}"

        results = self.run_query(query).fetchall()

        for result in results:
            privilege = result["privilege"].lower()
            granted_on = SnowflakeConnector.normalise_granted_on(result["granted_on"])

            name = self._granted(granted_on, result["name"])
            if name is None:
                continue

            # Grants the application made itself are system-managed: SECURITYADMIN
            # cannot revoke them and no spec should try, so they are not recorded at all.
            if (
                granted_on == APPLICATION_ROLE_GRANT_TYPE
                and SnowflakeConnector.is_system_application_role_grant(result)
            ):
                continue

            grants.setdefault(privilege, {}).setdefault(granted_on, set()).add(name)

        return grants

    def show_account_grants_to_role(self, role) -> Dict[str, Union[ObjectName, str]]:
        """
        The account-level privileges held by <role>, mapped to the role that granted
        each one. Snowflake's own privileges on its system-defined roles report an
        empty grantor, which is how the generator knows never to revoke them.
        """
        grants: Dict[str, Union[ObjectName, str]] = {}

        query = f"SHOW GRANTS TO ROLE {ObjectName.single(role)}"

        for result in self.run_query(query).fetchall():
            if result["granted_on"].lower() != "account":
                continue
            granted_by = result["granted_by"]
            grants[result["privilege"].lower()] = (
                ObjectName.from_canonical(granted_by) if granted_by else ""
            )

        return grants

    def show_grants_to_role_with_grant_option(self, role) -> Dict[str, Any]:
        grants: Dict[str, Any] = {}

        query = f"SHOW GRANTS TO ROLE {ObjectName.single(role)}"
        results = self.run_query(query).fetchall()

        for result in results:
            privilege = result["privilege"].lower()
            granted_on = result["granted_on"].lower()
            grant_option = result["grant_option"].lower() == "true"

            # An account grant's name column is the account, which is canonical
            name: Optional[ObjectName]
            if granted_on == "account":
                name = ObjectName.from_canonical(result["name"])
            else:
                name = self._granted(granted_on, result["name"])
                if name is None:
                    continue

            grants.setdefault(privilege, {}).setdefault(granted_on, {}).setdefault(
                name, {}
            ).update({"grant_option": grant_option})

        return grants

    def show_roles_granted_to_user(self, user) -> Set[ObjectName]:
        query = f"SHOW GRANTS TO USER {ObjectName.single(user)}"
        results = self.run_query(query).fetchall()

        return {
            ObjectName.from_canonical(result["role"])
            for result in results
            if result["role"]
        }

    def show_tags_on_user(self, user: NameLike) -> Dict[ObjectName, str]:
        """
        The tags set directly on <user>, keyed by tag name. Inherited tags (level
        differs from domain) are left out: they belong to the parent object, not the
        user. The SNOWFLAKE database is queried because it exists in every account and
        tundra runs with no session database.
        """
        user = ObjectName.single(user)
        query = (
            "SELECT tag_database, tag_schema, tag_name, tag_value FROM TABLE("
            f"SNOWFLAKE.INFORMATION_SCHEMA.TAG_REFERENCES({user.string_literal}, 'USER')"
            ") WHERE level = domain"
        )
        results = self.run_query(query).fetchall()

        return {
            ObjectName.from_canonical(
                result["tag_database"], result["tag_schema"], result["tag_name"]
            ): result["tag_value"]
            for result in results
        }

    def get_current_user(self) -> ObjectName:
        query = "SELECT CURRENT_USER() AS USER"
        result = self.run_query(query).fetchone()
        return ObjectName.from_canonical(result["user"])

    def get_current_role(self) -> ObjectName:
        query = "SELECT CURRENT_ROLE() AS ROLE"
        result = self.run_query(query).fetchone()
        return ObjectName.from_canonical(result["role"])

    def is_role_in_session(self, role: NameLike) -> bool:
        """True when <role> is the current role or one the current role inherits."""
        name = ObjectName.single(role).name.replace("\\", "\\\\").replace("'", "''")
        query = f"SELECT IS_ROLE_IN_SESSION('{name}') AS IN_SESSION"
        result = self.run_query(query).fetchone()
        return bool(result["in_session"])

    def show_roles(self) -> Dict[ObjectName, Union[ObjectName, str]]:
        query = "SHOW ROLES"
        results = self.run_query(query).fetchall()

        return {
            ObjectName.from_canonical(result["name"]): (
                ObjectName.from_canonical(result["owner"]) if result["owner"] else ""
            )
            for result in results
        }

    def show_database_roles(self, database: NameLike) -> List[ObjectName]:
        """List the database roles in a database as db.role names."""
        database = ObjectName.single(database)
        query = f"SHOW DATABASE ROLES IN DATABASE {database}"
        results = self.run_query(query).fetchall()

        # SHOW DATABASE ROLES reports the role name on its own
        return [database.child(result["name"]) for result in results]

    def show_application_roles(self, application: NameLike) -> List[ObjectName]:
        """
        List the application roles of an installed application as app.role names.

        Returns an empty list when <application> is not an installed application,
        so a DB.ROLE reference that turns out not to be a database role can fall
        through to this without failing the whole spec.
        """
        application = ObjectName.single(application)
        query = f"SHOW APPLICATION ROLES IN APPLICATION {application}"
        try:
            results = self.run_query(query).fetchall()
        except Exception as error:
            logger.debug(
                f"{application} is not an application, no application roles: {error}"
            )
            return []

        # SHOW APPLICATION ROLES reports the bare role name, which may itself contain
        # periods (CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH)
        return [application.child(result["name"]) for result in results]

    @staticmethod
    def is_system_application_role_grant(result: Dict[str, Any]) -> bool:
        """
        True when a SHOW GRANTS row for an application role was granted by the
        application itself (granted_by is empty or names the application), as the
        SNOWFLAKE application does for ACCOUNTADMIN. Those grants are not ours to manage.
        """
        application = granted_name(result["granted_on"], result["name"]).database
        granted_by = result.get("granted_by")
        return not granted_by or ObjectName.from_canonical(granted_by) == application

    def run_query(self, query: str) -> "_BufferedResult":
        from sqlalchemy import text

        with self.engine.connect() as connection:
            logger.debug(f"Running query: {query}")
            result = connection.execute(text(query))
            rows = result.mappings().all() if result.returns_rows else []

        return _BufferedResult(rows)

    @staticmethod
    def missing_object(error: Exception) -> Optional[Tuple[str, str]]:
        """
        The (kind, name) a failed statement reports as missing, or None when it failed
        for any other reason. See MISSING_OBJECT_PATTERN for which kinds count.
        """
        match = MISSING_OBJECT_PATTERN.search(str(error))
        if match is None:
            return None
        return match.group("kind").lower(), match.group("name")

    def object_exists(self, kind: str, name: str) -> bool:
        """
        Look an object up with the same SHOW that discovered it at plan time. A parent
        that is gone too means the object is gone. A lookup that fails for any other
        reason reports the object as present, since its absence can't be confirmed.
        """
        try:
            if kind == "database":
                identifier = ObjectName.single(name)
                found = set(map(ObjectName.single, self.show_databases()))
            elif kind == "schema":
                identifier = ObjectName(name)
                found = set(
                    map(ObjectName, self.show_schemas(database=identifier.parent))
                )
            else:
                identifier = ObjectName(name)
                object_type = next(t for t in ALL_OBJECT_TYPES if t.name == kind)
                found = set(
                    map(
                        ObjectName,
                        self.show_table_objects(object_type, schema=identifier.parent),
                    )
                )
        except Exception as exc:
            return not (self.missing_object(exc) or MISSING_CONTAINER_ERROR in str(exc))
        return identifier in found

    def full_schema_list(
        self, schema: Union[str, ObjectName, NamePattern]
    ) -> List[ObjectName]:
        """
        For a given schema entry, get all schemas it may be referencing.

        For example, if <db>.* is given then all schemas in the database
        will be returned. If <db>.<schema_partial>_* is given, then all
        schemas that match the schema partial pattern will be returned.
        If a full schema name is given, it will return that single schema
        as a list. INFORMATION_SCHEMA never matches a pattern.

        Returns a list of schema names.
        """
        entry = (
            schema
            if isinstance(schema, (ObjectName, NamePattern))
            else name_or_pattern(schema, arity=2)
        )

        if isinstance(entry, ObjectName):
            return [entry]

        return [
            db_schema
            for db_schema in map(ObjectName, self.show_schemas(entry.database))
            if entry.matches(db_schema)
        ]

    @staticmethod
    def normalise_granted_on(granted_on: str) -> str:
        """
        Normalise the granted_on column of a SHOW GRANTS result to the key it is
        stored under in grants_to_role.
        """
        normalised = granted_on.lower()

        if normalised in ("database_role", DATABASE_ROLE_GRANT_TYPE):
            return DATABASE_ROLE_GRANT_TYPE

        if normalised in ("application_role", APPLICATION_ROLE_GRANT_TYPE):
            return APPLICATION_ROLE_GRANT_TYPE

        if normalised in ("external_volume", EXTERNAL_VOLUME_GRANT_TYPE):
            return EXTERNAL_VOLUME_GRANT_TYPE

        return normalised

    @staticmethod
    def reserved_keywords() -> Set[str]:
        return set(RESERVED_KEYWORDS)
