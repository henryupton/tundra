import json
import logging
import os
import re
import subprocess
import threading
from collections import Counter
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)
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
    api_granted_name,
    canonical_from_api,
    granted_name,
    name_or_pattern,
)
from tundra.logger import GLOBAL_LOGGER as logger
from tundra.operations import Operation, SqlStatement, routes_to_api
from tundra.snowflake_api import SnowflakeApi
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

# The error code snowflake.core's 400 carries when Snowflake can't parse the SQL it built
# from a request, as for application role grants
SYNTAX_ERROR = "001003"

# SHOW ... IN SCHEMA against a schema that is gone fails with this rather than naming it.
MISSING_CONTAINER_ERROR = "Object does not exist, or operation cannot be performed"

# SHOW FUTURE GRANTS TO ROLE for a role that doesn't exist
MISSING_ROLE_PATTERN = re.compile(
    r"\bRole '[^']+' does not exist or not authorized", re.IGNORECASE
)

# The error code snowflake.core's 404 carries for an object that doesn't exist, the one
# SQL reports as "does not exist or not authorized"
DOES_NOT_EXIST = "002003"

SQL_ONLY_ENV = "PERMISSION_BOT_SQL_ONLY"
API_STRICT_ENV = "PERMISSION_BOT_API_STRICT"

API = "api"
SQL = "sql"

# Why a REST listing was read again through SQL, as the summary line words each reason
FALLBACK_REASONS = {
    "limit": "at the row limit",
    "validation": "invalid model",
    "api error": "API error",
    "non-json": "non-JSON error",
    "transport": "transport error",
    "timeout": "retry timeout",
}

# After one of these the rest of the run reads and writes through SQL, rather than wait
# out each request's own retries against an API that isn't answering
STICKY_REASONS = frozenset({"transport", "timeout"})

# Why a statement sent through snowflake.core ran again as its SQL, as the summary
# line after apply words each reason
WRITE_FALLBACK_REASONS = {
    "syntax": "syntax error",
    "server": "server error",
    "non-json": "non-JSON error",
    "transport": "transport error",
    "timeout": "retry timeout",
    "unmatched": "unmatched 404",
}

T = TypeVar("T")

# Don't show all the info log messages from Snowflake
for logger_name in ["snowflake.connector", "bot", "boto3"]:
    log = logging.getLogger(logger_name)
    log.setLevel(logging.WARNING)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split())


class ApiFallbackError(Exception):
    """A REST read that would have fallen back to SQL, raised under PERMISSION_BOT_API_STRICT."""

    def __init__(self, reader: str, reason: str) -> None:
        super().__init__(
            f"{reader} would read through SQL instead of the API"
            f" ({FALLBACK_REASONS[reason]}), and {API_STRICT_ENV} is set"
        )
        self.reader = reader
        self.reason = reason


def _request_info(error: BaseException) -> Optional[Dict[str, Any]]:
    """The message and error code of a snowflake.core APIError, or None for any other."""
    from snowflake.core.exceptions import APIError

    if not isinstance(error, APIError):
        return None
    try:
        return error.get_request_info()
    except ValueError:
        return None


def _is_missing(error: BaseException) -> bool:
    """Whether a snowflake.core error is a 404 for an object that doesn't exist."""
    info = _request_info(error)
    return (
        info is not None
        and getattr(error, "status", None) == 404
        and str(info["error_code"]) == DOES_NOT_EXIST
    )


def api_error_detail(error: BaseException) -> Optional[str]:
    """
    A snowflake.core error on one line as `error code (HTTP status): message`, in the
    shape SQL errors print as `errno (sqlstate): message`. None for any other error.
    """
    info = _request_info(error)
    if info is None or not info["message"]:
        return None
    status = getattr(error, "status", None)
    return " ".join(f"{info['error_code']} ({status}): {info['message']}".split())


def _fallback_reason(error: BaseException) -> Optional[str]:
    """Why a failed REST listing should be read through SQL, or None to raise it."""
    import urllib3
    from pydantic import ValidationError
    from snowflake.core.exceptions import APIError, RetryTimeoutError

    if isinstance(error, ValidationError):
        return "validation"
    # Raised by APIError's constructor itself, for an error body that isn't JSON
    if isinstance(error, json.JSONDecodeError):
        return "non-json"
    if isinstance(error, RetryTimeoutError):
        return "timeout"
    if isinstance(error, APIError):
        # snowflake.core reports an SSL failure as status 0
        return "api error" if error.status else "transport"
    if isinstance(
        error,
        (urllib3.exceptions.HTTPError, OSError, snowflake.connector.errors.Error),
    ):
        return "transport"
    # snowflake.core raises some transport failures again as its own error, such as
    # an SSL hostname mismatch as a SnowflakePythonError from the MaxRetryError
    if error.__cause__ is not None and _fallback_reason(error.__cause__) == "transport":
        return "transport"
    return None


def _write_fallback_reason(error: BaseException) -> Optional[str]:
    """
    Why a statement snowflake.core failed to run should run again as its SQL, or None
    to report the error: SQL Snowflake built from the request and couldn't parse, a
    5xx, an error body that isn't JSON, a transport failure, or a retry timeout.
    snowflake.core resends a request answered 429, 503 or 504 (or polls a 202) until
    its retry window closes, so those end as a RetryTimeoutError, never an APIError.
    GRANT and REVOKE are idempotent, so running the SQL after a request that may have
    landed late is safe. Anything else is Snowflake refusing the statement, which its
    SQL would be refused for too.
    """
    from snowflake.core.exceptions import APIError

    if isinstance(error, json.JSONDecodeError):
        return "non-json"
    if isinstance(error, APIError):
        if not error.status:
            return "transport"
        if error.status >= 500:
            return "server"
        info = _request_info(error)
        if info is not None and str(info["error_code"]) == SYNTAX_ERROR:
            return "syntax"
        return None
    reason = _fallback_reason(error)
    return reason if reason in STICKY_REASONS else None


def _transport_summary(
    noun: str,
    by_transport: Mapping[str, int],
    by_reason: Mapping[str, int],
    reasons: Mapping[str, str],
    sql_only: bool,
) -> str:
    line = (
        f"  API-capable {noun}: {by_transport.get(API, 0)} through the API,"
        f" {by_transport.get(SQL, 0)} through SQL"
    )
    if by_reason:
        line += " (fallbacks: {})".format(
            ", ".join(
                f"{by_reason[reason]} {description}"
                for reason, description in reasons.items()
                if by_reason.get(reason)
            )
            + (
                ", then SQL for the rest of the run"
                if STICKY_REASONS & by_reason.keys()
                else ""
            )
        )
    elif sql_only:
        line += " (SQL only)"
    return line + "."


class ReadStats:
    """
    How many of the listings snowflake.core can give each transport answered, and why
    any fell back to SQL, by reader.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.listings: Counter = Counter()
        self.fallbacks: Counter = Counter()

    def listed(self, reader: str, transport: str) -> None:
        with self._lock:
            self.listings[reader, transport] += 1

    def fell_back(self, reader: str, reason: str) -> None:
        with self._lock:
            self.fallbacks[reader, reason] += 1

    def by_transport(self) -> Counter:
        return self._totals(self.listings)

    def by_reason(self) -> Counter:
        return self._totals(self.fallbacks)

    def _totals(self, counts: Counter) -> Counter:
        totals: Counter = Counter()
        with self._lock:
            for (_, key), count in counts.items():
                totals[key] += count
        return totals

    def summary(self, sql_only: bool = False) -> str:
        """
        One line for the run: the listings that can go through the API, by transport,
        and fallbacks by reason. Reads that are always SQL, like SHOW ROLES, aren't
        counted.
        """
        return _transport_summary(
            "listings",
            self.by_transport(),
            self.by_reason(),
            FALLBACK_REASONS,
            sql_only,
        )


class WriteStats:
    """
    How many API-capable statements apply ran through each transport, and why any it
    sent through snowflake.core ran again as SQL. Apply runs one statement at a time.
    """

    def __init__(self) -> None:
        self.statements: Counter = Counter()
        self.fallbacks: Counter = Counter()

    def applied(self, transport: str) -> None:
        self.statements[transport] += 1

    def fell_back(self, reason: str) -> None:
        self.fallbacks[reason] += 1

    def summary(self, sql_only: bool = False) -> str:
        """
        One line for the apply, in the read summary's form. Statements only SQL can
        express, like GRANT OWNERSHIP, aren't counted.
        """
        return _transport_summary(
            "statements",
            self.statements,
            self.fallbacks,
            WRITE_FALLBACK_REASONS,
            sql_only,
        )


class Executed(NamedTuple):
    """How a planned statement ran: the transport that ran it last, and its error."""

    transport: str
    error: Optional[Exception] = None


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
    # Defaults for the read transport, so a connector built without __init__ (the test
    # fakes, MockSnowflakeConnector) reads the same way
    _sql_only = False
    _api_failed = False
    _api: Optional[SnowflakeApi] = None
    _api_error: Optional[Exception] = None
    _api_connection: Any = None
    _api_lock = threading.Lock()

    def __init__(self, config: Optional[Dict] = None, sql_only: bool = False) -> None:
        self._sql_only = sql_only
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

    @property
    def sql_only(self) -> bool:
        """
        Whether every read and write goes through SQL rather than snowflake.core: set by
        --sql-only or sql_only=True, or for the whole process by PERMISSION_BOT_SQL_ONLY.
        """
        return self._sql_only or _env_flag(SQL_ONLY_ENV)

    @sql_only.setter
    def sql_only(self, value: bool) -> None:
        self._sql_only = value

    @property
    def api_strict(self) -> bool:
        """PERMISSION_BOT_API_STRICT: a REST read that would fall back to SQL raises."""
        return _env_flag(API_STRICT_ENV)

    @property
    def api(self) -> SnowflakeApi:
        """
        snowflake.core, built the first time a reader needs it over a connection checked
        out of this connector's pool, and kept for the run. Root runs SELECT
        SYSTEM$CLIENT_VERSION_INFO() as it's built.
        """
        if self._api is None:
            with self._api_lock:
                # Readers waiting on the lock don't log in again to fail the same way
                if self._api_error is not None:
                    raise self._api_error
                if self._api is None:
                    try:
                        self._api = self._build_api()
                    except Exception as error:
                        self._api_error = error
                        raise
        return self._api

    def _build_api(self) -> SnowflakeApi:
        from snowflake.core import Root

        pooled = self.engine.raw_connection()
        try:
            root = Root(pooled.driver_connection)
        except BaseException:
            pooled.close()
            raise
        # Root holds the DBAPI connection, and the pool takes it back once its wrapper
        # is collected, so the wrapper is held too
        self._api_connection = pooled
        return SnowflakeApi(root)

    @property
    def read_stats(self) -> ReadStats:
        stats = self.__dict__.get("_read_stats")
        if stats is None:
            stats = self.__dict__.setdefault("_read_stats", ReadStats())
        return stats

    @property
    def write_stats(self) -> WriteStats:
        stats = self.__dict__.get("_write_stats")
        if stats is None:
            stats = self.__dict__.setdefault("_write_stats", WriteStats())
        return stats

    def _read(
        self,
        reader: str,
        listing: Callable[[SnowflakeApi], Sequence[Any]],
        parse: Callable[[Sequence[Any]], T],
        sql: Callable[[], T],
    ) -> T:
        """
        Read through snowflake.core: `listing` fetches the models and `parse` reads
        them. The SQL reader `sql` answers instead when the connector is SQL only, after
        a transport error, and for any listing that falls back (see _fall_back). A 404
        for an object that doesn't exist is raised, as SQL's "does not exist" is.
        """
        if self.sql_only or self._api_failed:
            self.read_stats.listed(reader, SQL)
            return sql()

        try:
            api = self.api
        except Exception as error:
            return self._fall_back(reader, "transport", sql, error)

        try:
            models = listing(api)
        except Exception as error:
            if _is_missing(error):
                self.read_stats.listed(reader, API)
                raise
            reason = _fallback_reason(error)
            if reason is None:
                raise
            return self._fall_back(reader, reason, sql, error)

        if len(models) >= api.list_limit:
            return self._fall_back(reader, "limit", sql)

        try:
            result = parse(models)
        except InvalidNameError as error:
            return self._fall_back(reader, "validation", sql, error)

        self.read_stats.listed(reader, API)
        return result

    def _fall_back(
        self,
        reader: str,
        reason: str,
        sql: Callable[[], T],
        error: Optional[BaseException] = None,
    ) -> T:
        """
        Read through SQL what the API couldn't give in full: a listing at the row limit
        (it may have left rows out), a model snowflake.core or tundra can't read, an API
        error, or a transport failure, after which the rest of the run reads through SQL.
        """
        if self.api_strict:
            raise ApiFallbackError(reader, reason) from error
        detail = f": {error}" if error is not None else ""
        if reason in STICKY_REASONS and not self._api_failed:
            self._api_failed = True
            logger.warning(
                f"{reader} through snowflake.core failed ({FALLBACK_REASONS[reason]}),"
                f" so the rest of the run reads through SQL{detail}"
            )
        else:
            logger.info(
                f"{reader} reads through SQL ({FALLBACK_REASONS[reason]}){detail}"
            )
        self.read_stats.fell_back(reader, reason)
        self.read_stats.listed(reader, SQL)
        return sql()

    @staticmethod
    def _api_granted_on(securable_type: str) -> str:
        """The grants_to_role key of a snowflake.core Grant's securable_type."""
        return SnowflakeConnector.normalise_granted_on(securable_type.replace(" ", "_"))

    @staticmethod
    def _api_granted(
        securable_type: str, grant: Any, future: bool = False
    ) -> Optional[ObjectName]:
        """The name a snowflake.core Grant is on, or None for a kind tundra doesn't read."""
        if grant.securable is None:
            raise InvalidNameError(f"A {securable_type} grant has no securable")
        return api_granted_name(securable_type, grant.securable, future=future)

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

    @staticmethod
    def _api_names(models: Sequence[Any]) -> List[ObjectName]:
        return [ObjectName.from_api(model.name) for model in models]

    def show_databases(self) -> List[ObjectName]:
        return self._read(
            "show_databases",
            lambda api: api.databases(),
            self._api_names,
            lambda: self.show_query("DATABASES"),
        )

    def show_warehouses(self) -> List[ObjectName]:
        return self._read(
            "show_warehouses",
            lambda api: api.warehouses(),
            self._api_names,
            lambda: self.show_query("WAREHOUSES"),
        )

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
        return self._read(
            "show_users",
            lambda api: api.users(),
            self._api_names,
            lambda: self.show_query("USERS"),
        )

    def show_schemas(self, database: Optional[NameLike] = None) -> List[ObjectName]:
        # REST lists schemas a database at a time, so the account's stay one SHOW
        if not database:
            return self._sql_schemas("SHOW TERSE SCHEMAS IN ACCOUNT")

        container = ObjectName.single(database)
        return self._read(
            "show_schemas",
            lambda api: api.schemas(container.api_name),
            lambda models: [
                container.child(canonical_from_api(model.name)) for model in models
            ],
            lambda: self._sql_schemas(f"SHOW TERSE SCHEMAS IN DATABASE {container}"),
        )

    def _sql_schemas(self, query: str) -> List[ObjectName]:
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

    def show_future_grants_to_role(
        self, role: NameLike
    ) -> Dict[str, Dict[str, Set[ObjectName]]]:
        """
        The future grants held by <role>, in every database, keyed like
        show_grants_to_role: privilege, then grant_on (table, iceberg_table, schema...),
        then the future-grant scopes. A role that doesn't exist holds none.
        """
        name = ObjectName.single(role)
        try:
            return self._read(
                "show_future_grants_to_role",
                lambda api: api.future_grants_to_role(name.api_name),
                self._api_future_grants,
                lambda: self._sql_future_grants_to_role(name),
            )
        except Exception as error:
            if _is_missing(error) or MISSING_ROLE_PATTERN.search(str(error)):
                logger.info(f"Role {name} doesn't exist, so it holds no future grants")
                return {}
            raise

    def _api_future_grants(
        self, models: Sequence[Any]
    ) -> Dict[str, Dict[str, Set[ObjectName]]]:
        grants: Dict[str, Any] = {}
        for grant in models:
            name = self._api_granted(grant.securable_type, grant, future=True)
            if name is None:
                continue
            granted_on = self._api_granted_on(grant.securable_type)
            for privilege in grant.privileges or ():
                grants.setdefault(privilege.lower(), {}).setdefault(
                    granted_on, set()
                ).add(name)
        return grants

    def _sql_future_grants_to_role(
        self, role: ObjectName
    ) -> Dict[str, Dict[str, Set[ObjectName]]]:
        grants: Dict[str, Any] = {}
        query = f"SHOW FUTURE GRANTS TO ROLE {role}"

        for result in self.run_query(query).fetchall():
            name = self._granted(result["grant_on"], result["name"])
            if name is None:
                continue
            grants.setdefault(result["privilege"].lower(), {}).setdefault(
                result["grant_on"].lower(), set()
            ).add(name)

        return grants

    def show_grants_to_role(self, role) -> Dict[str, Dict[str, Set[ObjectName]]]:
        if role in ["*", '"*"']:
            return {}

        name = ObjectName.single(role)
        return self._read(
            "show_grants_to_role",
            lambda api: api.grants_to_role(name.api_name),
            self._api_grants_to_role,
            lambda: self._sql_grants_to_role(name),
        )

    def _api_grants_to_role(
        self, models: Sequence[Any]
    ) -> Dict[str, Dict[str, Set[ObjectName]]]:
        grants: Dict[str, Any] = {}
        for grant in models:
            name = self._api_granted(grant.securable_type, grant)
            if name is None:
                continue
            granted_on = self._api_granted_on(grant.securable_type)

            # As in SQL, the application's own grants aren't recorded
            if granted_on == APPLICATION_ROLE_GRANT_TYPE and (
                not grant.granted_by
                or ObjectName.from_api(grant.granted_by) == name.database
            ):
                continue

            for privilege in grant.privileges or ():
                grants.setdefault(privilege.lower(), {}).setdefault(
                    granted_on, set()
                ).add(name)
        return grants

    def _sql_grants_to_role(
        self, role: ObjectName
    ) -> Dict[str, Dict[str, Set[ObjectName]]]:
        grants: Dict[str, Any] = {}
        query = f"SHOW GRANTS TO ROLE {role}"

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
        name = ObjectName.single(role)
        return self._read(
            "show_account_grants_to_role",
            lambda api: api.grants_to_role(name.api_name),
            self._api_account_grants,
            lambda: self._sql_account_grants_to_role(name),
        )

    @staticmethod
    def _api_account_grants(models: Sequence[Any]) -> Dict[str, Union[ObjectName, str]]:
        grants: Dict[str, Union[ObjectName, str]] = {}
        for grant in models:
            if grant.securable_type.lower() != "account":
                continue
            for privilege in grant.privileges or ():
                grants[privilege.lower()] = (
                    ObjectName.from_api(grant.granted_by) if grant.granted_by else ""
                )
        return grants

    def _sql_account_grants_to_role(
        self, role: ObjectName
    ) -> Dict[str, Union[ObjectName, str]]:
        grants: Dict[str, Union[ObjectName, str]] = {}

        query = f"SHOW GRANTS TO ROLE {role}"

        for result in self.run_query(query).fetchall():
            if result["granted_on"].lower() != "account":
                continue
            granted_by = result["granted_by"]
            grants[result["privilege"].lower()] = (
                ObjectName.from_canonical(granted_by) if granted_by else ""
            )

        return grants

    def show_grants_to_role_with_grant_option(self, role) -> Dict[str, Any]:
        name = ObjectName.single(role)
        return self._read(
            "show_grants_to_role_with_grant_option",
            lambda api: api.grants_to_role(name.api_name),
            self._api_grants_with_grant_option,
            lambda: self._sql_grants_to_role_with_grant_option(name),
        )

    def _api_grants_with_grant_option(self, models: Sequence[Any]) -> Dict[str, Any]:
        grants: Dict[str, Any] = {}
        for grant in models:
            # Keyed by granted_on as SHOW GRANTS spells it, lowercased: external_volume
            granted_on = grant.securable_type.replace(" ", "_").lower()
            name: Optional[ObjectName]
            if granted_on == "account":
                if grant.securable is None:
                    raise InvalidNameError("An account grant has no securable")
                name = ObjectName.from_api(grant.securable.name)
            else:
                name = self._api_granted(grant.securable_type, grant)
                if name is None:
                    continue

            for privilege in grant.privileges or ():
                grants.setdefault(privilege.lower(), {}).setdefault(
                    granted_on, {}
                ).setdefault(name, {}).update(
                    {"grant_option": bool(grant.grant_option)}
                )
        return grants

    def _sql_grants_to_role_with_grant_option(self, role: ObjectName) -> Dict[str, Any]:
        grants: Dict[str, Any] = {}

        query = f"SHOW GRANTS TO ROLE {role}"
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
        name = ObjectName.single(user)
        return self._read(
            "show_roles_granted_to_user",
            lambda api: api.grants_to_user(name.api_name),
            self._api_user_roles,
            lambda: self._sql_roles_granted_to_user(name),
        )

    def _api_user_roles(self, models: Sequence[Any]) -> Set[ObjectName]:
        roles = set()
        for grant in models:
            if grant.securable_type.lower() == "role":
                role = self._api_granted(grant.securable_type, grant)
                if role is not None:
                    roles.add(role)
        return roles

    def _sql_roles_granted_to_user(self, user: ObjectName) -> Set[ObjectName]:
        query = f"SHOW GRANTS TO USER {user}"
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
        container = ObjectName.single(database)
        return self._read(
            "show_database_roles",
            lambda api: api.database_roles(container.api_name),
            lambda models: [
                container.child(canonical_from_api(model.name)) for model in models
            ],
            lambda: self._sql_database_roles(container),
        )

    def _sql_database_roles(self, database: ObjectName) -> List[ObjectName]:
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

    def writes_through_api(self, operation: Operation) -> bool:
        """
        Whether apply sends a planned statement through snowflake.core: ROUTES sends
        its kind there, the connector isn't SQL only, and the API hasn't failed this
        run. PERMISSION_BOT_API_STRICT is for reads, so it can't stop a run halfway.
        """
        return not (self.sql_only or self._api_failed) and routes_to_api(operation)

    def execute(self, operation: Operation) -> Executed:
        """
        Run one planned statement, through snowflake.core where writes_through_api
        says so and as its SQL otherwise. A statement snowflake.core gets no usable
        answer for (see _write_fallback_reason) runs once more as its SQL, and the
        fallback is logged. The error the statement ends with is returned, not raised.
        """
        if isinstance(operation, SqlStatement) or not self.writes_through_api(
            operation
        ):
            return self._execute_sql(operation)

        try:
            api = self.api
        except Exception as error:
            return self._write_fall_back(operation, "transport", error)

        try:
            operation.send(api)
        except Exception as error:
            reason = _write_fallback_reason(error)
            # The REST wording of a missing schema or database is modelled on a missing
            # table's, so a 404 that doesn't name the statement's own target gets SQL's
            # answer, which apply knows how to read
            if (
                reason is None
                and _is_missing(error)
                and self.missing_object(error, operation) is None
            ):
                reason = "unmatched"
            if reason is None:
                return Executed(API, error)
            return self._write_fall_back(operation, reason, error)
        return Executed(API)

    def _execute_sql(self, operation: Operation) -> Executed:
        try:
            self.run_query(operation.sql)
        except Exception as error:
            return Executed(SQL, error)
        return Executed(SQL)

    def _write_fall_back(
        self, operation: Operation, reason: str, error: BaseException
    ) -> Executed:
        then = "it runs as SQL"
        if reason in STICKY_REASONS and not self._api_failed:
            self._api_failed = True
            then = "it and the rest of the run go through SQL"
        logger.warning(
            f"{operation.sql} through snowflake.core failed"
            f" ({WRITE_FALLBACK_REASONS[reason]}), so {then}: {_one_line(error)}"
        )
        self.write_stats.fell_back(reason)
        return self._execute_sql(operation)

    @staticmethod
    def missing_object(
        error: Exception, operation: Optional[Operation] = None
    ) -> Optional[Tuple[str, NameLike]]:
        """
        The (kind, name) a failed statement reports as missing, or None when it failed
        for any other reason. See MISSING_OBJECT_PATTERN for which kinds count.

        SQL's message names the object. snowflake.core's 404 lowercases the name in its
        message, so for an error from the API the name is the operation's own, and only
        when the message names that part of the operation's target.
        """
        info = _request_info(error)
        if operation is None or info is None:
            match = MISSING_OBJECT_PATTERN.search(str(error))
            if match is None:
                return None
            return match.group("kind").lower(), match.group("name")

        if not _is_missing(error) or isinstance(operation, SqlStatement):
            return None
        match = MISSING_OBJECT_PATTERN.search(info["message"] or "")
        if match is None:
            return None
        kind = match.group("kind").lower()
        name = operation.missing(kind)
        reported = match.group("name").replace('"', "").lower()
        if name is None or ".".join(name.parts).lower() != reported:
            return None
        return kind, name

    def object_exists(self, kind: str, name: NameLike) -> bool:
        """
        Look an object up with the same listing that discovered it at plan time. A
        parent that is gone too (in SQL's words or as snowflake.core's 404) means the
        object is gone. A lookup that fails for any other reason reports the object as
        present, since its absence can't be confirmed. That includes strict mode's
        ApiFallbackError, so a lookup during apply can't stop a run halfway.
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
            return not (
                self.missing_object(exc)
                or MISSING_CONTAINER_ERROR in str(exc)
                or _is_missing(exc)
            )
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
