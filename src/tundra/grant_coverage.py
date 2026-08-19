from dataclasses import dataclass, field
from typing import Iterable, Set

from tundra.snowflake_connector import SnowflakeConnector


@dataclass
class GrantCoverage:
    """Which objects a role's spec entries legitimately cover, used by the revoke pass.

    A wildcard entry (`db.schema.*`, `db.*.*`) is granted with ON ALL plus ON FUTURE, so
    every object in the schema is covered by construction, present and future alike.
    Recording the schema is therefore equivalent to recording each of its objects, and it
    means the schema never has to be listed.

    That matters because listing it was the dominant cost of query generation: a
    `SHOW <objects> IN SCHEMA` per schema, per object type, per role, none of which ever
    influenced a GRANT. The enumeration existed only to populate this allowlist.

    Names are normalised through `snowflaky` on the way in and on the way out, because the
    two sides being compared arrive differently quoted: fetched grant state comes from
    `SHOW GRANTS`, whereas an exact schema reference comes verbatim from the spec file.
    """

    objects: Set[str] = field(default_factory=set)
    schemas: Set[str] = field(default_factory=set)

    def add_object(self, name: str) -> None:
        self.objects.add(SnowflakeConnector.snowflaky(name))

    def add_objects(self, names: Iterable[str]) -> None:
        for name in names:
            self.add_object(name)

    def add_schema(self, schema: str) -> None:
        self.schemas.add(SnowflakeConnector.snowflaky(schema))

    def add_schemas(self, schemas: Iterable[str]) -> None:
        for schema in schemas:
            self.add_schema(schema)

    def covers(self, name: str) -> bool:
        normalised = SnowflakeConnector.snowflaky(name)

        if normalised in self.objects:
            return True

        # Everything before the final dot: for `db.schema.object` that is the owning
        # schema, and for a database-level `db.<placeholder>` it is the database, which
        # is never a member of `schemas` and so correctly falls through to False.
        parent = normalised.rpartition(".")[0]

        return bool(parent) and parent in self.schemas

    def merge(self, other: "GrantCoverage") -> "GrantCoverage":
        return GrantCoverage(
            objects=self.objects | other.objects,
            schemas=self.schemas | other.schemas,
        )
