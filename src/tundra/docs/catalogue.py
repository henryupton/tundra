"""
The catalogue `tundra docs` renders: a spec's roles, users and objects as nodes, and
who is a member of, uses or owns what as edges.

Memberships and object usage come from SnowflakeGrantsGenerator, the planner `run` uses,
so `member_of: ["*"]`, `include`/`exclude` and the skipped grants between Snowflake's
default roles resolve exactly as `run` applies them. On a spec alone the planner works
against an account that holds nothing and has every role the spec defines. With
`--live` it works against the snapshot SnowflakeSpecLoader read, and each edge is
classified the way `run` would act on it: a grant already held is in both, a grant still
to make is spec only, and a revoke is live only.
"""

import fnmatch
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    cast,
)

import tundra
from tundra.account_privileges import normalise_account_privilege
from tundra.entities import EntityGenerator
from tundra.identifiers import (
    InvalidNameError,
    NamePattern,
    ObjectName,
    member_or_pattern,
    name_or_pattern,
)
from tundra.operations import GRANT, Privileges, RoleMembership, SqlStatement
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_grants import SNOWFLAKE_DEFAULT_ROLES, SnowflakeGrantsGenerator
from tundra.spec_file_loader import load_spec
from tundra.table_object_types import PRIVILEGE_FAMILIES

CATALOGUE_VERSION = 1

SYSTEM_ROLES = SNOWFLAKE_DEFAULT_ROLES | frozenset(
    map(ObjectName.single, ("orgadmin", "globalorgadmin"))
)

# Inheritance Snowflake sets up itself, which no spec declares and run never plans
BUILTIN_MEMBERSHIPS = [
    ("accountadmin", "securityadmin"),
    ("accountadmin", "sysadmin"),
    ("securityadmin", "useradmin"),
]

SERVICE_TYPES = {"SERVICE", "LEGACY_SERVICE"}

# Spec section, node type, and the key SnowflakeSpecLoader.missing_entities files it under
OBJECT_SECTIONS = [
    ("warehouses", "warehouse", "warehouses"),
    ("databases", "database", "databases"),
    ("integrations", "integration", "integrations"),
    ("external_volumes", "external_volume", None),
]
NODE_TYPE_ORDER = [
    "role",
    "user",
    "warehouse",
    "database",
    "integration",
    "external_volume",
]
MISSING_KEYS = {
    "role": "roles",
    "database_role": "database_roles",
    "application_role": "database_roles",
    "user": "users",
    **{node_type: key for _, node_type, key in OBJECT_SECTIONS if key},
}

# The securables a role uses, keyed as tundra's Privileges operations name them
USAGE_TARGETS = {
    "warehouse": "warehouse",
    "database": "database",
    "integration": "integration",
    "external volume": "external_volume",
}

PRIVILEGE_SECTIONS = ["databases", "schemas", *PRIVILEGE_FAMILIES]
OWNS_SECTIONS = ["databases", "schemas", "tables"]

# Fields from a user's spec shown on its page. Names and email addresses stay out.
USER_FIELDS = [
    "can_login",
    "default_namespace",
    "default_secondary_roles",
    "parameters",
]

# Families in the order the key lists them; custom ones go after the system roles
FAMILIES_BEFORE_CUSTOM = [
    ("people", "People"),
    ("service", "Service users"),
    ("system", "System roles"),
]
FAMILIES_AFTER_CUSTOM = [
    ("functional", "Functional roles"),
    ("managed", "Database and application roles"),
    ("warehouse", "Warehouses"),
    ("database", "Databases"),
    ("integration", "Integrations"),
    ("external_volume", "External volumes"),
]
BUILTIN_FAMILY_IDS = {
    family_id for family_id, _ in FAMILIES_BEFORE_CUSTOM + FAMILIES_AFTER_CUSTOM
}

APPLICATION_ROLE_STATEMENT = re.compile(
    r"(GRANT|REVOKE) APPLICATION ROLE (.+) (?:TO|FROM) ROLE (.+)"
)


@dataclass(frozen=True)
class FamilyRule:
    """Roles whose names match `pattern`, a case-insensitive glob, get their own colour."""

    label: str
    pattern: str

    @classmethod
    def parse(cls, text: str) -> "FamilyRule":
        label, equals, pattern = (part.strip() for part in text.partition("="))
        if not equals or not label or not pattern:
            raise ValueError(
                f"{text!r} isn't LABEL=PATTERN, such as 'Loading roles=load*'"
            )
        rule = cls(label, pattern)
        if not rule.id or rule.id in BUILTIN_FAMILY_IDS:
            raise ValueError(
                f"{label!r} can't be told apart from tundra's own families"
            )
        return rule

    @property
    def id(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.label.lower()).strip("-")

    def matches(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name.strip('"').lower(), self.pattern.lower())


class _SpecRoles:
    """SHOW ROLES for an account that has every role the spec defines."""

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self._roles = [
            ObjectName.single(name) for name, _ in _definitions(spec, "roles")
        ]

    def show_roles(self) -> Dict[ObjectName, None]:
        return dict.fromkeys(self._roles)


class SpecSource:
    """
    A spec read on its own, with what SnowflakeSpecLoader would add after reading the
    account left empty. Loading it runs tundra's spec checks, so a spec `run` would
    refuse fails here with the same SpecLoadingError.
    """

    def __init__(self, spec_path: str) -> None:
        self.spec = load_spec(spec_path)
        self.entities = EntityGenerator(spec=self.spec).inspect_entities()
        self.conn = _SpecRoles(self.spec)
        self.grants_to_role: Dict[ObjectName, Any] = {}
        self.roles_granted_to_user: Dict[ObjectName, Any] = {}
        self.missing_entities: Dict[str, Any] = {}


def _definitions(spec: Mapping[str, Any], section: str) -> Iterable[Tuple[str, Dict]]:
    for entry in spec.get(section) or []:
        for name, config in entry.items():
            yield name, config or {}


def _id(node_type: str, name: Any) -> str:
    prefix = "role" if node_type in ("database_role", "application_role") else node_type
    return f"{prefix}:{name}"


def _unique(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(items))


@dataclass
class _PendingEdge:
    grants: List[Tuple[Tuple[str, ...], bool]] = field(default_factory=list)
    revokes: List[Tuple[str, ...]] = field(default_factory=list)


class _Builder:
    def __init__(self, source: Any, live: bool, families: Sequence[FamilyRule]) -> None:
        self.spec = source.spec
        self.entities = source.entities
        self.live = live
        self.families = list(families)
        self.missing_entities = source.missing_entities if live else {}
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.spellings: Dict[str, str] = {}
        self.owners: Dict[str, ObjectName] = {}
        self.pending: Dict[Tuple[str, str, str], _PendingEdge] = defaultdict(
            _PendingEdge
        )
        self.edges: List[Dict[str, Any]] = []

        conn = source.conn if live else _SpecRoles(self.spec)
        self.generator = SnowflakeGrantsGenerator(
            source.grants_to_role if live else {},
            defaultdict(set, source.roles_granted_to_user if live else {}),
            conn=cast(SnowflakeConnector, conn),
            application_roles=self.entities["application_role_refs"],
        )

    def build(self, name: str) -> Dict[str, Any]:
        self.collect_spellings()
        self.define_objects()
        self.define_roles()
        self.define_users()
        self.plan_roles()
        self.plan_users()
        self.finish_planned_edges()
        self.add_builtin_edges()
        self.add_owner_edges()
        if self.live:
            self.flag_missing()

        type_order = {node_type: i for i, node_type in enumerate(NODE_TYPE_ORDER)}
        nodes = dict(
            sorted(
                self.nodes.items(),
                key=lambda item: (type_order[item[0].split(":", 1)[0]], item[0]),
            )
        )
        return {
            "metadata": {
                "catalogue_version": CATALOGUE_VERSION,
                "tundra_version": tundra.__version__,
                "generated_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "spec": name,
                "live": self.live,
            },
            "families": self.present_families(nodes),
            "nodes": nodes,
            "edges": sorted(
                self.edges, key=lambda e: (e["source"], e["type"], e["target"])
            ),
            "user_groups": self.user_groups(nodes),
        }

    # Names

    def collect_spellings(self) -> None:
        """
        How the spec spells each name, so a page says `snowflake.usage_viewer` where
        tundra renders "SNOWFLAKE".usage_viewer. A definition's spelling wins.
        """
        for section, node_type in [
            ("roles", "role"),
            ("users", "user"),
            *((section, node_type) for section, node_type, _ in OBJECT_SECTIONS),
        ]:
            for name, _ in _definitions(self.spec, section):
                self.spellings[_id(node_type, ObjectName.single(name))] = name

        for section in ("roles", "users"):
            for _, config in _definitions(self.spec, section):
                for spelling in self.member_of_entries(config):
                    member = member_or_pattern(spelling)
                    if isinstance(member, ObjectName):
                        self.spellings.setdefault(_id("role", member), spelling)
                if config.get("owner"):
                    owner = ObjectName.single(config["owner"])
                    self.spellings.setdefault(_id("role", owner), config["owner"])

    @staticmethod
    def member_of_entries(config: Dict[str, Any]) -> List[str]:
        member_of = config.get("member_of") or []
        if isinstance(member_of, dict):
            return list(member_of.get("include") or []) + list(
                member_of.get("exclude") or []
            )
        return list(member_of)

    # Nodes

    def node(self, node_type: str, name: ObjectName) -> Dict[str, Any]:
        node_id = _id(node_type, name)
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "id": node_id,
                "type": node_type,
                "name": self.spellings.get(node_id, str(name)),
                "family": self.family(node_type, node_id, name),
                "defined": False,
                "comment": None,
                "owner": None,
                "meta": {},
            }
            if node_type in ("role", "database_role", "application_role"):
                self.nodes[node_id].update(
                    {
                        "privileges": {},
                        "warehouses": [],
                        "integrations": [],
                        "external_volumes": [],
                        "account_privileges": [],
                        "owns": {},
                    }
                )
        return self.nodes[node_id]

    def family(self, node_type: str, node_id: str, name: ObjectName) -> str:
        if node_type in ("database_role", "application_role"):
            return "managed"
        if node_type != "role":
            return node_type
        if name in SYSTEM_ROLES:
            return "system"
        display = self.spellings.get(node_id, str(name))
        for rule in self.families:
            if rule.matches(display):
                return rule.id
        return "functional"

    def describe(self, node: Dict[str, Any], config: Dict[str, Any]) -> None:
        meta = dict(config.get("meta") or {})
        node["defined"] = True
        node["comment"] = config.get("comment") or meta.pop("comment", None)
        meta.pop("comment", None)
        node["meta"] = meta
        if config.get("owner"):
            owner = self.owners[node["id"]] = ObjectName.single(config["owner"])
            node["owner"] = _id("role", owner)

    def define_objects(self) -> None:
        for section, node_type, _ in OBJECT_SECTIONS:
            for name, config in _definitions(self.spec, section):
                node = self.node(node_type, ObjectName.single(name))
                self.describe(node, config)
                for key in ("size", "shared", "category", "storage_provider"):
                    if key in config:
                        node[key] = config[key]

    def define_roles(self) -> None:
        for name, config in _definitions(self.spec, "roles"):
            node = self.node("role", ObjectName.single(name))
            self.describe(node, config)
            privileges = config.get("privileges") or {}
            node["privileges"] = {
                section: {
                    access: self.entries(section, privileges[section][access])
                    for access in ("read", "write")
                    if (privileges[section] or {}).get(access)
                }
                for section in PRIVILEGE_SECTIONS
                if privileges.get(section)
            }
            node["owns"] = {
                section: self.entries(section, (config.get("owns") or {})[section])
                for section in OWNS_SECTIONS
                if (config.get("owns") or {}).get(section)
            }
            for key, node_type in [
                ("warehouses", "warehouse"),
                ("integrations", "integration"),
                ("external_volumes", "external_volume"),
            ]:
                node[key] = [
                    _id(node_type, ObjectName.single(name))
                    for name in config.get(key) or []
                ]
            node["account_privileges"] = [
                normalise_account_privilege(privilege)
                for privilege in config.get("account_privileges") or []
            ]

    @staticmethod
    def entries(section: str, spellings: Sequence[str]) -> List[Dict[str, Any]]:
        """Privilege or owns entries as the spec spells them, each with its database."""
        found = []
        for spelling in spellings:
            entry = (
                ObjectName.single(spelling)
                if section == "databases"
                else name_or_pattern(spelling)
            )
            try:
                database: Optional[str] = _id("database", entry.database)
            except InvalidNameError:
                database = None
            found.append(
                {
                    "name": spelling,
                    "database": database,
                    "pattern": isinstance(entry, NamePattern),
                }
            )
        return found

    def define_users(self) -> None:
        for name, config in _definitions(self.spec, "users"):
            node = self.node("user", ObjectName.single(name))
            self.describe(node, config)
            node["family"] = (
                "service"
                if str(config.get("type", "")).upper() in SERVICE_TYPES
                else "people"
            )
            for key, node_type in [
                ("default_role", "role"),
                ("default_warehouse", "warehouse"),
            ]:
                node[key] = (
                    _id(node_type, ObjectName.single(config[key]))
                    if config.get(key)
                    else None
                )
            node.update({key: config.get(key) for key in USER_FIELDS})
            node["tags"] = dict(config.get("tags") or {})

    # Edges

    def plan_roles(self) -> None:
        """The memberships and usage grants run plans for each role, as run plans them."""
        all_roles = [name for name, _ in _definitions(self.spec, "roles")]
        for name, config in _definitions(self.spec, "roles"):
            # run plans nothing for a role defined with no settings, revokes included
            if not config:
                continue
            generator = self.generator
            planned = generator.generate_grant_roles("roles", name, config, all_roles)
            if "warehouses" in config:
                planned += generator.generate_warehouse_grants(
                    role=name, warehouses=config["warehouses"]
                )
            if "integrations" in config:
                planned += generator.generate_integration_grants(
                    role=name, integrations=config["integrations"]
                )
            if "external_volumes" in config:
                planned += generator.generate_external_volume_grants(
                    role=name, external_volumes=config["external_volumes"]
                )
            databases = (config.get("privileges") or {}).get("databases") or {}
            planned += generator.generate_database_grants(
                role=name,
                databases={
                    "read": databases.get("read") or [],
                    "write": databases.get("write") or [],
                },
                shared_dbs=self.entities["shared_databases"],
                spec_dbs=self.entities["databases"],
            )
            for command in planned:
                self.pend(command)

    def plan_users(self) -> None:
        for name, config in _definitions(self.spec, "users"):
            for command in self.generator.generate_grant_roles("users", name, config):
                self.pend(command)

    def pend(self, command: Dict[str, Any]) -> None:
        """File a planned grant or revoke under the edge it adds or takes away."""
        operation = getattr(command, "operation", None)
        action: str
        privileges: Tuple[str, ...]
        if isinstance(operation, RoleMembership):
            grantee_type = "user" if operation.grantee_type == "user" else "role"
            member_type = (
                "database_role" if operation.member_type == "database role" else "role"
            )
            edge = self.edge_for(
                (grantee_type, operation.grantee),
                (member_type, operation.member),
                "member",
            )
            action, privileges = operation.action, ()
        elif (
            isinstance(operation, Privileges)
            and operation.target is not None
            and operation.securable_type in USAGE_TARGETS
        ):
            edge = self.edge_for(
                ("role", operation.role),
                (USAGE_TARGETS[operation.securable_type], operation.target),
                "usage",
            )
            action, privileges = operation.action, operation.privileges
        elif isinstance(operation, SqlStatement):
            match = APPLICATION_ROLE_STATEMENT.fullmatch(operation.sql)
            if not match:
                return
            verb, member, grantee = match.groups()
            edge = self.edge_for(
                ("role", ObjectName.single(grantee)),
                ("application_role", ObjectName.role_reference(member)),
                "member",
            )
            action, privileges = verb.lower(), ()
        else:
            return

        if action == GRANT:
            edge.grants.append((tuple(privileges), bool(command["already_granted"])))
        else:
            edge.revokes.append(tuple(privileges))

    def edge_for(
        self,
        source: Tuple[str, ObjectName],
        target: Tuple[str, ObjectName],
        edge_type: str,
    ) -> _PendingEdge:
        source_node = self.node(*source)
        target_node = self.node(*target)
        if target_node["type"] == "role" and target[0] != "role":
            # Only Snowflake can say whether a DB.ROLE is a database or application role
            target_node["type"] = target[0]
            target_node["family"] = "managed"
        return self.pending[(source_node["id"], target_node["id"], edge_type)]

    def finish_planned_edges(self) -> None:
        for (source, target, edge_type), pending in self.pending.items():
            granted = [p for p, already in pending.grants if already]
            to_grant = [p for p, already in pending.grants if not already]
            edge: Dict[str, Any] = {
                "source": source,
                "target": target,
                "type": edge_type,
                "origin": "spec" if pending.grants else "live",
                "status": None,
                "privileges": _unique(
                    privilege
                    for privileges in (
                        [p for p, _ in pending.grants] or pending.revokes
                    )
                    for privilege in privileges
                ),
            }
            if self.live:
                if not pending.grants:
                    edge["status"] = "live_only"
                else:
                    edge["status"] = "both" if granted else "spec_only"
                edge["missing"] = _unique(
                    p for privileges in to_grant for p in privileges
                )
                edge["extra"] = _unique(
                    p for privileges in pending.revokes for p in privileges
                )
            self.edges.append(edge)

    def add_builtin_edges(self) -> None:
        planned = {(e["source"], e["target"]) for e in self.edges}
        for grantee, member in BUILTIN_MEMBERSHIPS:
            source = _id("role", ObjectName.single(grantee))
            target = _id("role", ObjectName.single(member))
            if source in self.nodes and target in self.nodes:
                if (source, target) not in planned:
                    self.edges.append(
                        self.plain_edge(source, target, "member", "builtin")
                    )

    def add_owner_edges(self) -> None:
        for owned, owner in self.owners.items():
            source = self.node("role", owner)["id"]
            self.edges.append(self.plain_edge(source, owned, "owner", "spec"))

    def plain_edge(
        self, source: str, target: str, edge_type: str, origin: str
    ) -> Dict[str, Any]:
        """An edge that isn't compared with the account: Snowflake's own, or an owner."""
        edge: Dict[str, Any] = {
            "source": source,
            "target": target,
            "type": edge_type,
            "origin": origin,
            "status": None,
            "privileges": [],
        }
        if self.live:
            edge.update({"missing": [], "extra": []})
        return edge

    def flag_missing(self) -> None:
        for node in self.nodes.values():
            missing = self.missing_entities.get(MISSING_KEYS.get(node["type"], ""), ())
            name = node["id"].split(":", 1)[1]
            node["missing"] = any(str(entry) == name for entry in missing)

    # Summaries

    def user_groups(self, nodes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """People who hold exactly the same roles, as the graph draws them, biggest first."""
        roles_of: Dict[str, List[str]] = {
            node_id: []
            for node_id, node in nodes.items()
            if node["type"] == "user" and node["family"] == "people"
        }
        for edge in self.edges:
            if edge["source"] in roles_of and edge["type"] == "member":
                roles_of[edge["source"]].append(edge["target"])

        groups: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
        for user, roles in roles_of.items():
            groups[tuple(sorted(set(roles)))].append(user)
        ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
        return [
            {"id": f"group:{index}", "roles": list(roles), "users": sorted(users)}
            for index, (roles, users) in enumerate(ordered, 1)
        ]

    def present_families(
        self, nodes: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        present = {node["family"] for node in nodes.values()}
        listed = [
            {"id": family_id, "label": label, "custom": False}
            for family_id, label in FAMILIES_BEFORE_CUSTOM
        ]
        listed += [
            {"id": rule.id, "label": rule.label, "custom": True}
            for rule in self.families
        ]
        listed += [
            {"id": family_id, "label": label, "custom": False}
            for family_id, label in FAMILIES_AFTER_CUSTOM
        ]
        seen = set()
        families = []
        for family in listed:
            if family["id"] in present and family["id"] not in seen:
                seen.add(family["id"])
                families.append(family)
        return families


def build_catalogue(
    source: Any,
    *,
    name: str,
    live: bool = False,
    families: Sequence[FamilyRule] = (),
) -> Dict[str, Any]:
    """
    The catalogue for `source`: a SpecSource, or with `live` a SnowflakeSpecLoader that
    has read the account (anything with its spec, entities, conn, grants_to_role,
    roles_granted_to_user and missing_entities). Nothing here writes to Snowflake: the
    planner is only asked what it would do.
    """
    return _Builder(source, live, families).build(name)
