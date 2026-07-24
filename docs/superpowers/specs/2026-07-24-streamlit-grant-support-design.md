# Streamlit grant support

**Date:** 2026-07-24
**Status:** Approved

## Goal

Let tundra manage `USAGE` grants on Snowflake Streamlit apps through the existing
`tables` read list (FQN and `db.schema.*` wildcards), and grant the schema-level
`CREATE STREAMLIT` privilege to roles with schema write access.

## Background

Streamlit apps in Snowflake are schema-scoped objects (`db.schema.app`). Their
only grantable privilege is `USAGE` (run/view the app). They are listed with
`SHOW STREAMLITS` and support `GRANT USAGE ON ALL|FUTURE STREAMLITS IN SCHEMA`.
The schema-level create privilege is `CREATE STREAMLIT`.

tundra already drives all schema-scoped, table-like grants from the object type
registry in `src/tundra/table_object_types.py` (`TABLE_OBJECT_TYPES`). The
registry supplies the read/write privileges, the `SHOW` command, the future-grant
placeholder, and the schema create privilege. The read-grant machinery
(`_generate_table_read_grants`) iterates the registry and emits single-object,
`ALL ... IN SCHEMA`, and `FUTURE ... IN SCHEMA` grants plus revocations. Adding a
registry entry is enough to wire all of this up, as the dynamic-table work
(commit `669da76d`) demonstrated.

Streamlit fits the `TableObjectType` shape because `read_privileges` /
`write_privileges` are free-form strings: setting both to `usage` makes the type
non-writable (like `VIEW`), so only the `tables: read` list drives grants.

## Decision

Fold Streamlit into the existing `tables` registry rather than giving it a
dedicated `privileges.streamlits` config key.

**Accepted tradeoff:** because the registry is driven by the `tables` config key,
a `tables: read: [db.schema.*]` wildcard will also grant `USAGE` on all and future
Streamlit apps in that schema. A role granted read on tables in a schema will also
be able to run Streamlit apps there. This was chosen deliberately for the minimal,
uniform diff over the correctness of an isolated config key.

## Changes

### 1. Registry entry — `src/tundra/table_object_types.py`

```python
STREAMLIT = TableObjectType(
    name="streamlit",
    connector_method="show_streamlits",
    show_command="STREAMLITS",
    read_privileges="usage",
    write_privileges="usage",
    schema_create_privilege="create streamlit",
)

TABLE_OBJECT_TYPES = [TABLE, VIEW, ICEBERG_TABLE, DYNAMIC_TABLE, STREAMLIT]
```

- `is_writable` is `False` (read == write), so only the `tables: read` list emits
  grants; `write_partial_privileges` is `""`, matching `VIEW`.
- Grant templates pluralize `name`: `ON streamlit db.s.app`,
  `ON ALL streamlits IN schema ...`, `ON FUTURE streamlits IN schema ...`.
- `future_placeholder` is `<streamlit>`; `snowflaky`'s
  `FUTURE_PLACEHOLDER_PATTERN` derives from the registry, so no `snowflaky`
  change is needed.

### 2. Connector — `src/tundra/snowflake_connector.py`

Add `show_streamlits`, delegating to the generic `show_table_objects`:

```python
def show_streamlits(
    self, database: Optional[str] = None, schema: Optional[str] = None
) -> List[str]:
    return self.show_table_objects(STREAMLIT, database=database, schema=schema)
```

Import `STREAMLIT` alongside the other registry types. `SHOW STREAMLITS` returns
`database_name`, `schema_name`, and `name`, which `show_table_objects` already
uses to build the identifier.

### 3. Mock connector — `tests/tundra_test_utils/snowflake_connector.py`

Add `show_streamlits` returning `[]`, mirroring `show_dynamic_tables`.

### 4. Schema create privilege

No code change: `SCHEMA_PARTIAL_WRITE_PRIVILEGES` iterates the registry and picks
up `create streamlit` automatically. `schemas: write: [db.schema]` will now also
grant `CREATE STREAMLIT`.

### 5. Docs / CLI

- `src/tundra/cli/permissions.py`: add "streamlit" to the fork-support blurb in
  the `run` docstring.
- `README.md`: note Streamlit apps under the `tables` listing, including the
  `USAGE`-only privilege and the `CREATE STREAMLIT` schema privilege.

### 6. Tests

Mirror the dynamic-table additions:

- `test_table_object_types.py`: registry contents/order, `future_placeholder`,
  `is_writable` (False), `write_partial_privileges` (`""`),
  `schema_create_privilege` (`"create streamlit"`).
- `test_snowflake_connector.py`: `show_streamlits` delegation, a `SHOW STREAMLITS`
  query assertion, and a `<STREAMLIT>` case in the `snowflaky` placeholder test.
- `test_snowflake_grants.py`: extend the `TestGenerateTableAndViewGrants`
  fixtures so expected grant lists include the streamlit `USAGE` ALL/FUTURE grants,
  and add a streamlit-focused `*` wildcard fixture parallel to
  `dynamic_tables_r_star_schema_config`. Patch `show_streamlits` in the test setup.
- `test_snowflake_spec_loader.py`: stub `show_streamlits` to `[]` where the other
  `show_*` methods are stubbed.

## Behaviour changes

Both fall out of reusing the registry and are expected:

1. `tables: read: [db.schema.*]` now also grants `USAGE` on all + future
   Streamlit apps in the schema.
2. `schemas: write: [db.schema]` now also grants `CREATE STREAMLIT`.

Existing specs will emit these new grants on the next run.

## Out of scope

- A dedicated `privileges.streamlits` config key.
- Granting privileges other than `USAGE` on Streamlit apps.
- Streamlit ownership management (handled by the existing `owns` mechanism if
  needed).
