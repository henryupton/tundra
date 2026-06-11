from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class TableObjectType:
    """A Snowflake table-like object type that tundra manages grants for.

    name: grant keyword and the key used in grants_to_role, e.g. "iceberg table"
    connector_method: SnowflakeConnector method that lists these objects
    show_command: SHOW command fragment, e.g. "TERSE TABLES"
    schema_create_privilege: schema-level create privilege, None if not granted
    """

    name: str
    connector_method: str
    show_command: str
    read_privileges: str
    write_privileges: str
    schema_create_privilege: Optional[str] = None

    @property
    def future_placeholder(self) -> str:
        return f"<{self.name}>"

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

# Order matters: it determines SQL statement emission order.
TABLE_OBJECT_TYPES: List[TableObjectType] = [TABLE, VIEW, ICEBERG_TABLE]
