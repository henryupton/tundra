from dataclasses import dataclass, field
from typing import Iterable, Set

from tundra.identifiers import NameLike, ObjectName


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

    Both sides are ObjectNames compared by Snowflake identity, so it doesn't matter that
    fetched grant state comes from `SHOW GRANTS` spelt one way and an exact schema
    reference comes verbatim from the spec file spelt another.
    """

    objects: Set[ObjectName] = field(default_factory=set)
    schemas: Set[ObjectName] = field(default_factory=set)

    def add_object(self, name: NameLike) -> None:
        self.objects.add(ObjectName(name))

    def add_objects(self, names: Iterable[NameLike]) -> None:
        for name in names:
            self.add_object(name)

    def add_schema(self, schema: NameLike) -> None:
        self.schemas.add(ObjectName(schema))

    def add_schemas(self, schemas: Iterable[NameLike]) -> None:
        for schema in schemas:
            self.add_schema(schema)

    def covers(self, name: NameLike) -> bool:
        name = ObjectName(name)

        if name in self.objects:
            return True

        # For `db.schema.object` and `db.schema.<placeholder>` the parent is the owning
        # schema. For a database-level `db.<placeholder>` it is the database, which is
        # never a member of `schemas` and so correctly falls through to False.
        parent = name.parent

        return parent is not None and parent in self.schemas

    def merge(self, other: "GrantCoverage") -> "GrantCoverage":
        return GrantCoverage(
            objects=self.objects | other.objects,
            schemas=self.schemas | other.schemas,
        )
