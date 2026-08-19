from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class TableObjectType:
    """A Snowflake table-like object type that tundra manages grants for.

    name: SQL grant keyword, e.g. "iceberg table"
    connector_method: SnowflakeConnector method that lists these objects
    show_command: SHOW command fragment, e.g. "TERSE TABLES"
    schema_create_privilege: schema-level create privilege, None if not granted
    supports_bulk_grants: whether Snowflake accepts ON ALL and ON FUTURE for this type.
        When it does, a wildcard spec entry is fully covered by those two statements and
        the schema's objects never need listing. A type without bulk-grant support has to
        fall back to enumerating them.
    """

    name: str
    connector_method: str
    show_command: str
    read_privileges: str
    write_privileges: str
    schema_create_privilege: Optional[str] = None
    supports_bulk_grants: bool = True

    @property
    def grant_key(self) -> str:
        """The form Snowflake itself uses on the wire, e.g. "iceberg_table".

        `SHOW GRANTS` reports `granted_on` as ICEBERG_TABLE and names future grants
        DB.SCHEMA.<ICEBERG_TABLE>, both underscore-separated, whereas the SQL grant
        keyword in `name` is space-separated. Every lookup against fetched grant
        state keys off this, never off `name`, or multi-word types never match what
        Snowflake reported and get re-granted on every run.
        """
        return self.name.replace(" ", "_")

    @property
    def future_placeholder(self) -> str:
        return f"<{self.grant_key}>"

    @property
    def is_writable(self) -> bool:
        return self.write_privileges != self.read_privileges

    @property
    def write_partial_privileges(self) -> str:
        read = self.read_privileges.split(", ")
        return ", ".join(
            p for p in self.write_privileges.split(", ") if p not in read
        )


FULL_WRITE_PRIVILEGES = "select, insert, update, delete, truncate, references"

TABLE = TableObjectType(
    name="table",
    connector_method="show_tables",
    show_command="TERSE TABLES",
    read_privileges="select",
    write_privileges=FULL_WRITE_PRIVILEGES,
    schema_create_privilege="create table",
)

VIEW = TableObjectType(
    name="view",
    connector_method="show_views",
    show_command="TERSE VIEWS",
    read_privileges="select",
    write_privileges="select",
    schema_create_privilege="create view",
)

ICEBERG_TABLE = TableObjectType(
    name="iceberg table",
    connector_method="show_iceberg_tables",
    show_command="ICEBERG TABLES",
    read_privileges="select",
    write_privileges=FULL_WRITE_PRIVILEGES,
    schema_create_privilege="create iceberg table",
)

# DML against dynamic tables is invalid; write access grants refresh control
# (monitor, operate) instead. `create dynamic table` schema grants are
# deliberately excluded (see spec).
DYNAMIC_TABLE = TableObjectType(
    name="dynamic table",
    connector_method="show_dynamic_tables",
    show_command="DYNAMIC TABLES",
    read_privileges="select",
    write_privileges="select, monitor, operate",
    schema_create_privilege=None,
)

# Streamlit apps expose only USAGE (run/view). read == write makes the type
# non-writable, so grants come from the `tables` read list. `create streamlit`
# is derived as a schema write privilege via the registry.
STREAMLIT = TableObjectType(
    name="streamlit",
    connector_method="show_streamlits",
    show_command="STREAMLITS",
    read_privileges="usage",
    write_privileges="usage",
    schema_create_privilege="create streamlit",
)

# Order matters: it determines SQL statement emission order.
TABLE_OBJECT_TYPES: List[TableObjectType] = [
    TABLE,
    VIEW,
    ICEBERG_TABLE,
    DYNAMIC_TABLE,
    STREAMLIT,
]
