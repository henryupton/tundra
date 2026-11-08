"""
Snowflake object names, compared by Snowflake identity whatever their spelling.

An ObjectName holds the canonical name of each part (what Snowflake stores) and an
optional future-grant placeholder. lab_results.aggregate, LAB_RESULTS."AGGREGATE" and
ObjectName.from_canonical("LAB_RESULTS", "AGGREGATE") are one name, and str() of it is
the SQL tundra writes: lab_results.aggregate.

Names arrive in three forms, each with its own constructors. Spellings, as a spec writes
them and SHOW GRANTS and SHOW FUTURE GRANTS print them, go through ObjectName(),
ObjectName.single(), ObjectName.role_reference() or granted_name(). Canonical names, as
SHOW <objects>, TAG_REFERENCES and the grantee and owner columns report them, go through
ObjectName.from_canonical(). Parts of snowflake.core's REST models, quoted only where a
part isn't a plain identifier, go through ObjectName.from_api() or api_granted_name().

str() is a one-to-one encoding of identity, so a name also equals, hashes and sorts like
the plain string of its rendering: ObjectName("RAW.PUBLIC") == "raw.public". A string in
any other spelling has to be parsed first: ObjectName("raw.public") != "RAW.PUBLIC".

Spec entries with an unquoted * (db.*, db.prefix_*, db.*.*, db.schema.*, "*" in
member_of) are NamePatterns, matched part by part against canonical names.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    NoReturn,
    Self,
    TypeAlias,
    TypeVar,
    final,
    overload,
)

from snowflake.core import CanonicalName, Identifier, QualifiedIdentifier
from snowflake.core.role import ContainingScope, Securable

from tundra.table_object_types import ALL_OBJECT_TYPES

if TYPE_CHECKING:
    from pydantic import BaseModel

_Model = TypeVar("_Model", bound="BaseModel")

# Snowflake's reserved keywords, which can't be unquoted identifiers
# (https://docs.snowflake.com/en/sql-reference/reserved-keywords). SNOWFLAKE isn't one,
# but it stays so the system database always renders as "SNOWFLAKE".
RESERVED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "account",
        "all",
        "alter",
        "and",
        "any",
        "as",
        "between",
        "by",
        "case",
        "cast",
        "check",
        "column",
        "connect",
        "connection",
        "constraint",
        "create",
        "cross",
        "current",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "database",
        "delete",
        "distinct",
        "drop",
        "else",
        "exists",
        "false",
        "following",
        "for",
        "from",
        "full",
        "grant",
        "group",
        "gscluster",
        "having",
        "ilike",
        "in",
        "increment",
        "inner",
        "insert",
        "intersect",
        "into",
        "is",
        "issue",
        "join",
        "lateral",
        "left",
        "like",
        "localtime",
        "localtimestamp",
        "minus",
        "natural",
        "not",
        "null",
        "of",
        "on",
        "or",
        "order",
        "organization",
        "qualify",
        "regexp",
        "revoke",
        "right",
        "rlike",
        "row",
        "rows",
        "sample",
        "schema",
        "select",
        "set",
        "snowflake",
        "some",
        "start",
        "table",
        "tablesample",
        "then",
        "to",
        "trigger",
        "true",
        "try_cast",
        "union",
        "unique",
        "update",
        "using",
        "values",
        "view",
        "when",
        "whenever",
        "where",
        "window",
        "with",
    }
)

# Can't be granted on, so no wildcard in the schema position matches it
INFORMATION_SCHEMA: Final = "INFORMATION_SCHEMA"

MAX_PART_LENGTH: Final = 255


class InvalidNameError(ValueError):
    """A spelling or canonical name that isn't a name of the expected shape."""


_UNQUOTED = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_PLAIN_UPPER = re.compile(r"[A-Z_][A-Z0-9_$]*")
_UPPER_FRAGMENT = re.compile(r"[A-Z0-9_$]*")
_QUOTED = re.compile(r'"((?:[^"]|"")+)"')
_FRAGMENT = re.compile(r'"((?:[^"]|"")*)"|[^"]+')
_BARE_RUN = re.compile(r"([A-Z0-9_$]+)|[^A-Z0-9_$]+")

# The kinds a future-grant placeholder can name: schema and each object type's grant
# key. Any other <...> is a name, as in raw.public."<tables>".
_FUTURE_KINDS: Final = frozenset({t.grant_key for t in ALL_OBJECT_TYPES} | {"schema"})

_CACHE_SIZE: Final = 1 << 16

# SHOW GRANTS granted_on kinds, lowercase with spaces, by how their name column is spelt
_SINGLE_PART_KINDS = frozenset(
    {"database", "warehouse", "integration", "external volume", "role", "user"}
)
_ROLE_REFERENCE_KINDS = frozenset({"database role", "application role"})
_DOTTED_KINDS = frozenset({"schema"} | {t.name for t in ALL_OBJECT_TYPES})

_SECURABLE_FIELDS = {
    1: ("name",),
    2: ("database", "name"),
    3: ("database", "schema", "name"),
}


class _Text:
    """
    Immutable values ordered by their rendered text, which doesn't follow
    PYTHONHASHSEED. A str is ordered as text too, the way a name compares equal to one.
    """

    __slots__ = ()
    _text: str

    @property
    def sort_key(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text

    def __lt__(self, other: object) -> bool:
        text = _text_of(other)
        return NotImplemented if text is None else self._text < text

    def __le__(self, other: object) -> bool:
        text = _text_of(other)
        return NotImplemented if text is None else self._text <= text

    def __gt__(self, other: object) -> bool:
        text = _text_of(other)
        return NotImplemented if text is None else self._text > text

    def __ge__(self, other: object) -> bool:
        text = _text_of(other)
        return NotImplemented if text is None else self._text >= text

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: object) -> Self:
        return self


@final
class ObjectName(_Text):
    """
    One to three canonical parts plus an optional future-grant placeholder, such as
    raw.<schema> or raw.public.<iceberg_table>. Immutable, hashable, ordered and
    interned, so equal names are usually the same object.

    ObjectName(spelling) parses a dotted name as a spec or SHOW GRANTS writes it,
    splitting on unquoted dots: unquoted identifiers resolve to uppercase, quoted ones
    keep their case, and a part that isn't a valid identifier (gitlab-ci, 1234raw,
    DEPARTMENT - DATA) is taken as the exact canonical name. A final part such as
    <TABLE> or <ICEBERG_TABLE>, naming a kind tundra grants on, is the placeholder; any
    other <...> is a name. Pass arity to require exactly that many parts and no
    placeholder.

    Every constructor hands back an ObjectName it's given unchanged, so coercion costs
    an isinstance check.

    A future-grant scope isn't SQL, so formatting one raises, the way a NamePattern
    does. Format its parent instead, or use str() or !s to show it.
    """

    __slots__ = (
        "parts",
        "placeholder",
        "_text",
        "_hash",
        "_database",
        "_parent",
        "_identifiers",
    )

    parts: tuple[str, ...]
    placeholder: str | None
    _hash: int
    _database: ObjectName
    _parent: ObjectName | None
    _identifiers: tuple[Identifier, ...]

    def __new__(cls, spelling: NameLike, *, arity: int | None = None) -> ObjectName:
        name = _parse(spelling) if isinstance(spelling, str) else _from_object(spelling)
        if arity is not None:
            _require_arity(name, arity)
        return name

    @classmethod
    def single(cls, spelling: NameLike) -> ObjectName:
        """
        A name that never splits on dots: a role, user, warehouse, integration, external
        volume or database. heidi.bond@example.com is one user. These are never
        patterns or future-grant scopes, so an unquoted * or a <...> is part of the
        name: svc*bot is "svc*bot" and <admin> is "<admin>". A member_of "*" goes
        through member_or_pattern().
        """
        if isinstance(spelling, str):
            return _parse_single(spelling)
        name = _from_object(spelling)
        if name.placeholder or len(name.parts) != 1:
            raise InvalidNameError(f"{name!s} isn't a single-part name")
        return name

    @classmethod
    def role_reference(cls, spelling: NameLike) -> ObjectName:
        """
        A role as member_of and SHOW GRANTS name one: an account role, or a database or
        application role written DB.ROLE. Only the first unquoted dot separates the two,
        because application role names can have dots in them:
        snowflake.CORTEX-MODEL-ROLE-GEMINI-2.5-FLASH is two parts. A role is never a
        future-grant scope, so a <...> part is a name, as in single().
        """
        if isinstance(spelling, str):
            return _parse_role_reference(spelling)
        name = _from_object(spelling)
        if name.placeholder or len(name.parts) > 2:
            raise InvalidNameError(f"{name!s} isn't ROLE or DB.ROLE")
        return name

    @classmethod
    def from_canonical(cls, *parts: str | CanonicalName | ObjectName) -> ObjectName:
        """
        A name from the canonical parts SHOW <objects> reports: RAW, Mixed-Case. A lone
        ObjectName comes back unchanged; one among other parts raises TypeError.
        """
        if len(parts) == 1 and isinstance(parts[0], ObjectName):
            return parts[0]
        return _intern(tuple(map(_canonical_argument, parts)), None)

    @classmethod
    def from_api(cls, *parts: str) -> ObjectName:
        """
        A name from the parts of a snowflake.core model, each read by
        canonical_from_api(): Database(name="RAW"), Securable(database="RAW",
        name='"Mixed-Case"').
        """
        return cls.from_canonical(*map(canonical_from_api, parts))

    @property
    def database(self) -> ObjectName:
        """The first part: a table's database, an application role's application."""
        try:
            return self._database
        except AttributeError:
            database = (
                self
                if len(self.parts) == 1 and not self.placeholder
                else _intern(self.parts[:1], None)
            )
            object.__setattr__(self, "_database", database)
            return database

    @property
    def parent(self) -> ObjectName | None:
        """
        What the name is in: raw.public for raw.public.events and raw.public.<table>,
        raw for raw.public and raw.<schema>, None for raw.
        """
        try:
            return self._parent
        except AttributeError:
            if self.placeholder:
                parent: ObjectName | None = _intern(self.parts, None)
            elif len(self.parts) == 1:
                parent = None
            else:
                parent = _intern(self.parts[:-1], None)
            object.__setattr__(self, "_parent", parent)
            return parent

    @property
    def name(self) -> str:
        """The canonical name of the last part, as IS_ROLE_IN_SESSION('...') takes it."""
        self._require_object("a name")
        return self.parts[-1]

    @property
    def is_future(self) -> bool:
        return self.placeholder is not None

    @property
    def future_kind(self) -> str | None:
        """The placeholder as TableObjectType.grant_key spells it: iceberg_table."""
        return self.placeholder[1:-1] if self.placeholder else None

    @property
    def is_information_schema(self) -> bool:
        return (
            not self.placeholder
            and len(self.parts) == 2
            and self.parts[1] == INFORMATION_SCHEMA
        )

    def child(self, canonical: str | CanonicalName) -> ObjectName:
        """
        This name with a canonical part added, for a role SHOW DATABASE ROLES or SHOW
        APPLICATION ROLES reports on its own: ObjectName.single(db).child(row["name"]).
        """
        self._require_object("a container")
        return _intern(self.parts + (_canonical_argument(canonical),), None)

    def future(self, kind: str) -> ObjectName:
        """
        The future-grant scope for <kind> in this database or schema. <kind> is schema
        or a TableObjectType's grant key, SQL keyword or placeholder: iceberg_table,
        iceberg table, <ICEBERG_TABLE>.
        """
        self._require_object("a database or schema")
        return _intern(self.parts, _placeholder(kind))

    @property
    def quoted(self) -> str:
        """
        Every part quoted with its case kept. User properties (DEFAULT_ROLE,
        DEFAULT_WAREHOUSE, DEFAULT_NAMESPACE) are stored as text, not resolved, so
        monitor has to be sent as "MONITOR" to match the role.
        """
        self._require_object("a user property")
        return ".".join(map(_quote, self.parts))

    @property
    def string_literal(self) -> str:
        """
        The SQL as a string literal, for functions that take a name as text, such as
        TAG_REFERENCES('"heidi.bond@example.com"', 'USER'). Snowflake reads backslash
        escapes in string constants, so backslashes are doubled as well as quotes.
        """
        self._require_object("a string literal")
        return "'" + self._text.replace("\\", "\\\\").replace("'", "''") + "'"

    @property
    def api_parts(self) -> tuple[str, ...]:
        """Each part spelt the way snowflake.core's REST models are sent it."""
        return tuple(map(_api_part, self.parts))

    @property
    def api_name(self) -> str:
        """A single-part name for root.roles[...], root.users[...] or Grantees.role()."""
        if self.placeholder or len(self.parts) != 1:
            raise InvalidNameError(f"{self!s} isn't a single-part name")
        return _api_part(self.parts[0])

    @property
    def identifiers(self) -> tuple[Identifier, ...]:
        """The parts as snowflake.core Identifiers, spelt as api_parts."""
        try:
            return self._identifiers
        except AttributeError:
            identifiers = tuple(map(Identifier, self.api_parts))
            object.__setattr__(self, "_identifiers", identifiers)
            return identifiers

    @overload
    def to_securable(self) -> Securable: ...  # noqa: E704

    @overload
    def to_securable(self, model: type[_Model]) -> _Model: ...  # noqa: E704

    def to_securable(self, model: type[Any] = Securable) -> Any:
        """
        The object as a Securable: name, database and name, or database, schema and
        name. Two parts are a schema, or a database or application role, where
        QualifiedIdentifier would read them as schema and name. `model` picks the
        database role or user resource's own Securable class.
        """
        self._require_object("a securable")
        api = self.api_parts
        return model(**dict(zip(_SECURABLE_FIELDS[len(api)], api)))

    @overload
    def to_containing_scope(self) -> ContainingScope: ...  # noqa: E704

    @overload
    def to_containing_scope(self, model: type[_Model]) -> _Model: ...  # noqa: E704

    def to_containing_scope(self, model: type[Any] = ContainingScope) -> Any:
        """
        The database or schema for an ON ALL or ON FUTURE grant: the name itself, or the
        container of a future-grant scope (raw.public for raw.public.<table>).
        """
        if len(self.parts) > 2:
            raise InvalidNameError(f"{self!s} isn't a database or schema")
        return model(**dict(zip(("database", "schema"), self.api_parts)))

    def _require_object(self, use: str) -> None:
        if self.placeholder:
            raise InvalidNameError(f"{self!s} is a future-grant scope, not {use}")

    def __eq__(self, other: object) -> bool:
        if other is self:
            return True
        if isinstance(other, ObjectName):
            return self._text == other._text
        if isinstance(other, str):
            return self._text == other
        return NotImplemented

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"ObjectName({self._text!r})"

    def __format__(self, format_spec: str) -> str:
        if self.placeholder:
            raise TypeError(
                f"{self._text!r} is a future-grant scope and can't be formatted into"
                " SQL; format its parent, or use str() or !s to show it"
            )
        return format(self._text, format_spec)

    def __reduce__(self) -> tuple[Any, ...]:
        return _intern, (self.parts, self.placeholder)


NameLike: TypeAlias = (
    str | ObjectName | Identifier | CanonicalName | QualifiedIdentifier
)

# One part of a pattern: its readings, each the canonical text between its *s. Most
# parts read one way; *1234raw reads two, as *1234RAW and as *"1234raw".
_Glob: TypeAlias = tuple[tuple[str, ...], ...]


@final
class NamePattern(_Text):
    """
    A spec entry with an unquoted *: raw.*, raw.loading_*, raw.*_raw, raw.*.*,
    raw.public.*, or "*" in member_of. `prefix` is the literal leading parts as an
    ObjectName (None for "*") and `globs` every part from the first wildcard on, in
    canonical form: raw.*.* has prefix raw and globs ("*", "*").

    A glob reads like the names it spells. Its unquoted text resolves to uppercase
    when the glob with its *s filled in is an unquoted identifier: loading_* matches
    LOADING_ORDERS but not "loading_lower", and *2024_backup matches
    SALES_2024_BACKUP. Where the glob with its *s taken out isn't an identifier, the
    text also keeps its case, like the name it then spells: my-* matches "my-x", and
    *1234raw matches "1234raw" as well as X1234RAW. Quoted text matches exactly, so
    "loading_"* matches "loading_lower". A * matches any run of characters, and never
    INFORMATION_SCHEMA in the schema position, which a member_of pattern doesn't have.

    It equals the string of its canonical form, like ObjectName, but it isn't a name:
    formatting one into a string raises, so a wildcard can't end up in a GRANT. Use
    str() or !s to show it.
    """

    __slots__ = ("prefix", "globs", "_parts", "_regexes", "_roles", "_text", "_hash")

    prefix: ObjectName | None
    globs: tuple[str, ...]
    _parts: tuple[_Glob, ...]
    _regexes: tuple[re.Pattern[str], ...]
    _roles: bool
    _hash: int

    def __new__(cls, spelling: str | NamePattern) -> NamePattern:
        if isinstance(spelling, NamePattern):
            return spelling
        if not isinstance(spelling, str):
            raise TypeError(f"Expected a pattern spelling, got {spelling!r}")
        return _parse_pattern(spelling)

    @property
    def database(self) -> ObjectName:
        if self.prefix is None:
            raise InvalidNameError(f"{self!s} has no database")
        return self.prefix.database

    @property
    def parent(self) -> ObjectName | NamePattern | None:
        """raw.* for raw.*.*, raw.public for raw.public.*, raw for raw.*."""
        if len(self.globs) == 1:
            return self.prefix
        return _make_pattern(self.prefix, self._parts[:-1], roles=self._roles)

    def matches(self, name: ObjectName) -> bool:
        """
        Whether `name` has as many parts, the same prefix and each part matching its
        glob. A str has to be parsed first, with ObjectName() or ObjectName.single(),
        because only the caller knows whether its dots split it.
        """
        if not isinstance(name, ObjectName):
            raise TypeError(
                f"NamePattern.matches takes an ObjectName, not {name!r}; parse it with"
                " ObjectName() or ObjectName.single() first"
            )
        prefix = self.prefix.parts if self.prefix else ()
        offset = len(prefix)
        if name.placeholder or len(name.parts) != offset + len(self._regexes):
            return False
        if name.parts[:offset] != prefix:
            return False
        for index, (regex, glob, part) in enumerate(
            zip(self._regexes, self._parts, name.parts[offset:]), offset
        ):
            if not regex.fullmatch(part):
                return False
            if (
                index == 1
                and not self._roles
                and _has_star(glob)
                and part == INFORMATION_SCHEMA
            ):
                return False
        return True

    def __eq__(self, other: object) -> bool:
        if other is self:
            return True
        if isinstance(other, NamePattern):
            # A member_of db.* matches a database role named INFORMATION_SCHEMA, where
            # a schema entry's db.* doesn't, so the two are different patterns
            return self._text == other._text and self._roles == other._roles
        if isinstance(other, str):
            return self._text == other
        return NotImplemented

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"NamePattern({self._text!r})"

    def __format__(self, format_spec: str) -> NoReturn:
        raise TypeError(
            f"NamePattern {self._text!r} isn't a name and can't be formatted into SQL;"
            " expand it with matches(), or use str() or !s to show it"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (_parse_role_pattern if self._roles else _parse_pattern), (self._text,)


def name_or_pattern(
    spelling: str | ObjectName | NamePattern, *, arity: int | None = None
) -> ObjectName | NamePattern:
    """
    A spec entry: a NamePattern when a part has an unquoted *, otherwise a dotted
    ObjectName. Pass arity to require that many parts, wildcards included: 2 for schema
    entries, 3 for table and task entries.
    """
    entry: ObjectName | NamePattern
    if isinstance(spelling, NamePattern) and spelling._roles:
        entry = _parse_pattern(spelling._text)
    elif isinstance(spelling, (ObjectName, NamePattern)):
        entry = spelling
    elif isinstance(spelling, str):
        entry = _parse_pattern(spelling) if _has_glob(spelling) else _parse(spelling)
    else:
        raise TypeError(f"Expected a spec entry, got {spelling!r}")
    if arity is not None:
        _require_arity(entry, arity)
    return entry


def member_or_pattern(
    spelling: str | ObjectName | NamePattern,
) -> ObjectName | NamePattern:
    """
    A member_of entry: a NamePattern for an unquoted * ("*" is every role), otherwise
    ObjectName.role_reference(), an account role or a database or application role.
    Either way only the first unquoted dot splits it, as role_reference() does.
    """
    if isinstance(spelling, NamePattern):
        pattern = spelling
    elif isinstance(spelling, str) and _has_glob(spelling):
        pattern = _parse_role_pattern(spelling)
    else:
        return ObjectName.role_reference(spelling)
    if _pattern_size(pattern) > 2:
        raise InvalidNameError(f"{pattern!s} isn't ROLE or DB.ROLE")
    # A rendered pattern quotes any dot inside a part, so its text splits the same way
    return pattern if pattern._roles else _parse_role_pattern(pattern._text)


def granted_name(granted_on: str, name: str) -> ObjectName:
    """
    The name column of a SHOW GRANTS row, parsed the way its granted_on kind spells it.
    Kinds tundra doesn't read names for (ACCOUNT, FUNCTION, STAGE...) raise
    InvalidNameError, which the reader can catch to skip the row.
    """
    if not isinstance(granted_on, str):
        raise TypeError(f"Expected a granted_on kind, got {granted_on!r}")
    kind = granted_on.lower().replace("_", " ")
    if kind in _SINGLE_PART_KINDS:
        return ObjectName.single(name)
    if kind in _ROLE_REFERENCE_KINDS:
        return ObjectName.role_reference(name)
    if kind in _DOTTED_KINDS:
        return ObjectName(name)
    raise InvalidNameError(f"tundra doesn't read the names of {granted_on} grants")


def canonical_from_api(part: str) -> str:
    """
    The canonical name one part of a snowflake.core model holds. REST quotes a part
    that isn't a plain identifier ("Mixed-Case", "UTIL_PARSE(VARCHAR)", "<TABLE>") and
    sends the rest bare, keywords included, so a bare part is canonical as it stands:
    it isn't resolved to uppercase the way a bare spelling is.
    """
    if part[:1] != '"':
        return part
    match = _QUOTED.fullmatch(part)
    if match is None:
        raise InvalidNameError(f"{part!r} isn't a quoted identifier")
    return match.group(1).replace('""', '"')


def api_granted_name(
    securable_type: str, securable: Any, *, future: bool = False
) -> ObjectName | None:
    """
    The name a snowflake.core Grant is on, or None for a kind tundra doesn't read the
    names of (ACCOUNT, FUNCTION, STAGE...). `securable` is the Grant's Securable, whose
    parts are read by canonical_from_api(). A future grant's name is the quoted
    placeholder for its kind, "<ICEBERG_TABLE>", so the grant is on its container's
    future(kind). Parts that don't fit the kind raise InvalidNameError.
    """
    kind = securable_type.lower().replace("_", " ")
    if not future and kind in _SINGLE_PART_KINDS:
        arities: tuple[int, ...] = (1,)
    elif (not future and kind in _ROLE_REFERENCE_KINDS) or kind == "schema":
        arities = (2,)
    elif kind in _DOTTED_KINDS:
        arities = (2, 3) if future else (3,)
    else:
        return None

    given = {
        "database": securable.database,
        "schema": securable.var_schema,
        "name": securable.name,
    }
    fields = tuple(field for field, part in given.items() if part is not None)
    if fields not in [_SECURABLE_FIELDS[arity] for arity in arities]:
        raise InvalidNameError(
            f"A {securable_type} grant isn't on {', '.join(fields) or 'nothing'}"
        )
    parts = [given[field] for field in fields]
    if not future:
        return ObjectName.from_api(*parts)

    *container, name = parts
    if name != _quote(_placeholder(kind).upper()):
        raise InvalidNameError(
            f"{name!r} isn't the placeholder of a future {securable_type} grant"
        )
    return ObjectName.from_api(*container).future(kind)


@lru_cache(maxsize=_CACHE_SIZE)
def _intern(parts: tuple[str, ...], placeholder: str | None) -> ObjectName:
    """The ObjectName for canonical parts and a placeholder, built once while cached."""
    if not 1 <= len(parts) <= (2 if placeholder else 3):
        if placeholder:
            raise InvalidNameError(
                f"A future-grant scope is in a database or schema, not {parts!r}"
            )
        raise InvalidNameError(f"A name has one to three parts, not {parts!r}")
    if placeholder == "<schema>" and len(parts) != 1:
        raise InvalidNameError(f"Future schemas are in a database, not in {parts!r}")
    for part in parts:
        if not part:
            raise InvalidNameError(f"A name can't have an empty part: {parts!r}")
        if len(part) > MAX_PART_LENGTH:
            raise InvalidNameError(
                f"{part[:40]!r}... is longer than {MAX_PART_LENGTH} characters"
            )

    text = ".".join(map(_render_part, parts))
    if placeholder:
        text = f"{text}.{placeholder}"

    name = object.__new__(ObjectName)
    object.__setattr__(name, "parts", parts)
    object.__setattr__(name, "placeholder", placeholder)
    object.__setattr__(name, "_text", text)
    object.__setattr__(name, "_hash", hash(text))
    return name


@lru_cache(maxsize=_CACHE_SIZE)
def _parse(spelling: str) -> ObjectName:
    parts = _split(spelling)
    placeholder = _placeholder_of(parts[-1]) if len(parts) > 1 else None
    if placeholder:
        parts.pop()
    if len(parts) > (2 if placeholder else 3):
        raise InvalidNameError(f"{spelling!r} has too many parts")
    if any(map(_placeholder_of, parts)):
        raise InvalidNameError(
            f"{spelling!r}: a future-grant placeholder can only follow a database or"
            " schema, as the last part"
        )
    return _intern(tuple(_canonical_part(p, spelling) for p in parts), placeholder)


@lru_cache(maxsize=_CACHE_SIZE)
def _parse_single(spelling: str) -> ObjectName:
    return _intern((_canonical_part(spelling, spelling, wildcards=False),), None)


@lru_cache(maxsize=_CACHE_SIZE)
def _parse_role_reference(spelling: str) -> ObjectName:
    parts = _split_role_reference(spelling)
    return _intern(tuple(_canonical_part(p, spelling) for p in parts), None)


@lru_cache(maxsize=_CACHE_SIZE)
def _parse_pattern(spelling: str) -> NamePattern:
    return _pattern_from_parts(_split(spelling), spelling, roles=False)


@lru_cache(maxsize=_CACHE_SIZE)
def _parse_role_pattern(spelling: str) -> NamePattern:
    return _pattern_from_parts(_split_role_reference(spelling), spelling, roles=True)


def _pattern_from_parts(parts: list[str], spelling: str, *, roles: bool) -> NamePattern:
    if len(parts) > 3:
        raise InvalidNameError(f"{spelling!r} has more than three parts")
    first = next((i for i, part in enumerate(parts) if _is_glob(part)), None)
    if first is None:
        raise InvalidNameError(f"{spelling!r} has no unquoted *, so it's a name")
    if not roles and any(map(_placeholder_of, parts)):
        raise InvalidNameError(f"{spelling!r}: a pattern can't have a placeholder")
    prefix = (
        _intern(tuple(_canonical_part(p, spelling) for p in parts[:first]), None)
        if first
        else None
    )
    globs = tuple(
        (
            _glob(part, spelling)
            if _is_glob(part)
            else ((_canonical_part(part, spelling),),)
        )
        for part in parts[first:]
    )
    return _make_pattern(prefix, globs, roles=roles)


def _make_pattern(
    prefix: ObjectName | None, parts: tuple[_Glob, ...], *, roles: bool
) -> NamePattern:
    globs = tuple(map(_render_glob, parts))
    text = ".".join(((str(prefix),) if prefix else ()) + globs)
    pattern = object.__new__(NamePattern)
    object.__setattr__(pattern, "prefix", prefix)
    object.__setattr__(pattern, "globs", globs)
    object.__setattr__(pattern, "_parts", parts)
    object.__setattr__(pattern, "_regexes", tuple(map(_glob_regex, parts)))
    object.__setattr__(pattern, "_roles", roles)
    object.__setattr__(pattern, "_text", text)
    object.__setattr__(pattern, "_hash", hash(text))
    return pattern


def _from_object(value: object) -> ObjectName:
    if isinstance(value, ObjectName):
        return value
    if isinstance(value, Identifier):
        return _parse_single(str(value))
    if isinstance(value, CanonicalName):
        return _intern((str(value),), None)
    if isinstance(value, QualifiedIdentifier):
        spelling = str(value)
        if value.database is not None and value.schema is None:
            # Dropping the missing schema would make database..name the schema
            # database.name, so refuse it the way the str spelling is refused
            raise InvalidNameError(
                f"{spelling!r} has no schema: Snowflake reads database..name in the"
                " PUBLIC schema, so spell the schema out"
            )
        identifiers = (value.database, value.schema, value.name)
        return _intern(
            tuple(
                _canonical_part(str(i), spelling) for i in identifiers if i is not None
            ),
            None,
        )
    raise TypeError(f"Expected a name or a name spelling, got {value!r}")


def _canonical_argument(part: object) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, CanonicalName):
        return str(part)
    raise TypeError(f"A canonical name is a str or CanonicalName, not {part!r}")


def _require_arity(entry: ObjectName | NamePattern, arity: int) -> None:
    if isinstance(entry, ObjectName):
        entry._require_object("a name")
        size = len(entry.parts)
    else:
        size = _pattern_size(entry)
    if size != arity:
        raise InvalidNameError(f"{entry!s} isn't a {arity}-part name")


def _pattern_size(pattern: NamePattern) -> int:
    return (len(pattern.prefix.parts) if pattern.prefix else 0) + len(pattern.globs)


def _split_role_reference(spelling: str) -> list[str]:
    """ROLE or DB.ROLE, split at the first unquoted dot only."""
    parts = _split(spelling)
    if len(parts) > 2:
        tail = parts[1:]
        if any(not part or '"' in part for part in tail):
            raise InvalidNameError(f"{spelling!r} isn't ROLE or DB.ROLE")
        parts = [parts[0], ".".join(tail)]
    return parts


def _split(spelling: str) -> list[str]:
    """Split on dots outside double quotes, keeping each part as written."""
    if '"' not in spelling:
        return spelling.split(".")
    parts, start, quoted = [], 0, False
    for index, char in enumerate(spelling):
        if char == '"':
            quoted = not quoted
        elif char == "." and not quoted:
            parts.append(spelling[start:index])
            start = index + 1
    if quoted:
        raise InvalidNameError(f"{spelling!r} has an unterminated quote")
    parts.append(spelling[start:])
    return parts


def _canonical_part(part: str, spelling: str, *, wildcards: bool = True) -> str:
    """The canonical name a part spells. With wildcards off, a * is a plain character."""
    if '"' in part:
        if part == '""':
            raise InvalidNameError(f"{spelling!r} has an empty part")
        match = _QUOTED.fullmatch(part)
        if match is None:
            raise InvalidNameError(
                f"{spelling!r}: {part!r} isn't a quoted identifier; quote the whole part"
            )
        return match.group(1).replace('""', '"')
    if _UNQUOTED.fullmatch(part):
        return part.upper()
    if not part:
        raise InvalidNameError(f"{spelling!r} has an empty part")
    if wildcards and "*" in part:
        raise InvalidNameError(
            f"{spelling!r} is a wildcard pattern, not a name; quote a name with a * in it"
        )
    return part


@lru_cache(maxsize=_CACHE_SIZE)
def _render_part(canonical: str) -> str:
    if _PLAIN_UPPER.fullmatch(canonical) and canonical.lower() not in RESERVED_KEYWORDS:
        return canonical.lower()
    return _quote(canonical)


# The spelling sent to snowflake.core's REST models. Plain uppercase names go bare, in
# the case REST reports them, and SNOWFLAKE isn't quoted (only tundra's SQL quotes it).
# This is the one place to change if the dev probe shows REST paths want another form.
_API_RESERVED = RESERVED_KEYWORDS - {"snowflake"}


@lru_cache(maxsize=_CACHE_SIZE)
def _api_part(canonical: str) -> str:
    if _PLAIN_UPPER.fullmatch(canonical) and canonical.lower() not in _API_RESERVED:
        return canonical
    return _quote(canonical)


def _quote(canonical: str) -> str:
    # Not CanonicalName.to_identifier_string(), which passes a name that starts and ends
    # with a quote through as if it were already quoted, so it resolves to another name.
    return '"' + canonical.replace('"', '""') + '"'


def _placeholder(kind: str) -> str:
    if not isinstance(kind, str):
        raise TypeError(f"Expected an object type, got {kind!r}")
    word = kind[1:-1] if kind[:1] == "<" and kind[-1:] == ">" else kind
    word = word.lower().replace(" ", "_")
    if word not in _FUTURE_KINDS:
        raise InvalidNameError(f"{kind!r} isn't a kind tundra grants future grants on")
    return f"<{word}>"


def _placeholder_of(part: str) -> str | None:
    """The placeholder a part is, as SHOW FUTURE GRANTS spells it (<ICEBERG_TABLE>)."""
    if part[:1] == "<" and part[-1:] == ">" and part[1:-1].lower() in _FUTURE_KINDS:
        return f"<{part[1:-1].lower()}>"
    return None


def _has_glob(spelling: str) -> bool:
    return "*" in spelling and any(map(_is_glob, _split(spelling)))


def _is_glob(part: str) -> bool:
    """Whether a part has a * outside quotes."""
    if "*" not in part:
        return False
    if '"' not in part:
        return True
    return any(
        match.group(1) is None and "*" in match.group()
        for match in _FRAGMENT.finditer(part)
    )


def _glob(part: str, spelling: str) -> _Glob:
    """The readings of a wildcard part."""
    fragments = list(_FRAGMENT.finditer(part))
    if sum(len(match.group()) for match in fragments) != len(part):
        raise InvalidNameError(f"{spelling!r}: {part!r} has a malformed quoted part")
    if any(match.group(1) == "" for match in fragments):
        raise InvalidNameError(f"{spelling!r}: {part!r} has an empty quoted part")
    # Unquoted text resolves to uppercase if the glob with its *s filled in is an
    # identifier, so *2024_backup reads like sales_2024_backup. If the glob with its
    # *s taken out isn't one, the text also keeps its case, so *1234raw matches the
    # name 1234raw spells. Quoted text stands in as a letter.
    probe = "".join(
        "A" if match.group(1) is not None else match.group() for match in fragments
    )
    readings = []
    if _UNQUOTED.fullmatch(probe.replace("*", "A")):
        readings.append(_reading(fragments, upper=True))
    if not _UNQUOTED.fullmatch(probe.replace("*", "")):
        readings.append(_reading(fragments, upper=False))
    return tuple(dict.fromkeys(readings))


def _reading(fragments: list[re.Match[str]], *, upper: bool) -> tuple[str, ...]:
    """The canonical text between the *s, with unquoted text uppercased or as written."""
    segments = [""]
    for match in fragments:
        if match.group(1) is not None:
            segments[-1] += match.group(1).replace('""', '"')
            continue
        first, *rest = (match.group().upper() if upper else match.group()).split("*")
        segments[-1] += first
        segments.extend(rest)
    # Stars next to each other match what one does
    return (segments[0], *(s for s in segments[1:-1] if s), segments[-1])


def _has_star(glob: _Glob) -> bool:
    return len(glob[0]) > 1


def _render_glob(glob: _Glob) -> str:
    if len(glob) == 2:
        return _render_both_readings(*glob)
    (segments,) = glob
    if len(segments) == 1:
        return _render_part(segments[0])
    if all(map(_UPPER_FRAGMENT.fullmatch, segments)) and _UNQUOTED.fullmatch(
        "".join(segments)
    ):
        return "*".join(segments).lower()
    return "*".join(_quote(segment) if segment else "" for segment in segments)


def _render_both_readings(upper: tuple[str, ...], exact: tuple[str, ...]) -> str:
    """
    A glob that reads two ways, like *1234raw. The readings differ only where unquoted
    text has a lowercase letter, so those go bare, as do digits, _, $ and uppercase
    letters, which read the same either way. Anything else came from quoted text.
    """
    rendered = []
    for upper_segment, exact_segment in zip(upper, exact):
        runs = (
            (exact_segment[slice(*run.span())], run.group(1))
            for run in _BARE_RUN.finditer(upper_segment)
        )
        rendered.append("".join(text if bare else _quote(text) for text, bare in runs))
    return "*".join(rendered)


@lru_cache(maxsize=_CACHE_SIZE)
def _glob_regex(glob: _Glob) -> re.Pattern[str]:
    if len(glob) == 1:
        return re.compile(_reading_regex(glob[0]), re.DOTALL)
    upper, exact = glob
    # The uppercase reading's leading * stands for an identifier's first letter, so it
    # matches at least one character: *1234raw reads as x1234raw, never as 1234RAW
    return re.compile(f".{_reading_regex(upper)}|{_reading_regex(exact)}", re.DOTALL)


def _reading_regex(segments: tuple[str, ...]) -> str:
    if len(segments) == 1:
        return re.escape(segments[0])
    first, *inner, last = map(re.escape, segments)
    # Each inner segment matches atomically at its first place, as fnmatch.translate
    # does, so a glob with many stars can't backtrack through every split of a name
    return (
        "(?:"
        + first
        + "".join(f"(?>.*?{segment})" for segment in inner)
        + ".*"
        + last
        + ")"
    )


def _text_of(other: object) -> str | None:
    if isinstance(other, _Text):
        return other._text
    return other if isinstance(other, str) else None
