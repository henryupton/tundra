# Streamlit Grant Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manage `USAGE` grants on Snowflake Streamlit apps (FQN + `db.schema.*` wildcards) and the schema-level `CREATE STREAMLIT` privilege, by adding one entry to the table object type registry.

**Architecture:** Streamlit is a schema-scoped, `USAGE`-only object. It rides the existing `tables` config key by being added to `TABLE_OBJECT_TYPES`, so the registry-driven read/wildcard/future/revoke machinery and the registry-derived schema create privileges cover it with no new generation code. Mirrors the dynamic-table work in commit `669da76d`.

**Tech Stack:** Python 3.10, pytest, pytest-mock, cerberus (spec validation).

## Global Constraints

- Streamlit's only grantable privilege is `usage`; `read_privileges == write_privileges == "usage"`, making the type non-writable (grants come from the `tables: read` list only).
- Registry order determines SQL emission order; append `STREAMLIT` last, matching how `DYNAMIC_TABLE` was appended.
- The grant test (`test_snowflake_grants.py`) sorts actual and expected SQL before comparing; `usage` lines sort after all `select` lines.
- Full suite must stay green after every task's commit. Because adding a type to the registry makes the read loop call `conn.show_streamlits(...)`, the connector method and mock must exist before the type is registered — hence Task 1 (method) precedes Task 2 (registry).
- Commit message trailer for every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File map

- `src/tundra/table_object_types.py` — define `STREAMLIT` (Task 1), append to `TABLE_OBJECT_TYPES` (Task 2).
- `src/tundra/snowflake_connector.py` — import `STREAMLIT`, add `show_streamlits` (Task 1).
- `tests/tundra_test_utils/snowflake_connector.py` — mock `show_streamlits` (Task 1).
- `tests/tundra/test_snowflake_connector.py` — delegation test (Task 1), `<STREAMLIT>` placeholder assertion (Task 2).
- `tests/tundra/test_table_object_types.py` — registry unit tests (Task 2).
- `tests/tundra/test_snowflake_grants.py` — fixture updates + new streamlit wildcard fixture + `show_streamlits` patch (Task 2).
- `tests/tundra/test_snowflake_spec_loader.py` — `show_streamlits` stub (Task 2).
- `src/tundra/cli/permissions.py` — docstring blurb (Task 3).
- `README.md` — tables-section note (Task 3).

---

## Task 1: Connector method + mock (`show_streamlits`)

Add the `STREAMLIT` constant and the connector method **without** registering the type, so the method exists before anything calls it. This task is behaviourally inert (nothing iterates over it yet) and keeps the suite green.

**Files:**
- Modify: `src/tundra/table_object_types.py`
- Modify: `src/tundra/snowflake_connector.py`
- Modify: `tests/tundra_test_utils/snowflake_connector.py`
- Test: `tests/tundra/test_snowflake_connector.py`

**Interfaces:**
- Produces: `STREAMLIT` (a `TableObjectType` with `name="streamlit"`, `connector_method="show_streamlits"`, `show_command="STREAMLITS"`, `read_privileges="usage"`, `write_privileges="usage"`, `schema_create_privilege="create streamlit"`); `SnowflakeConnector.show_streamlits(database=None, schema=None) -> List[str]`; `MockSnowflakeConnector.show_streamlits(...) -> List[str]` returning `[]`.

- [ ] **Step 1: Write the failing delegation test**

In `tests/tundra/test_snowflake_connector.py`, add `STREAMLIT` to the existing import from `tundra.table_object_types` (line 6: `from tundra.table_object_types import DYNAMIC_TABLE, ICEBERG_TABLE, TABLE, VIEW`) and add an assertion inside `test_wrappers_delegate_to_show_table_objects` (the test where `show_dynamic_tables`/`show_views`/`show_iceberg_tables` delegation is asserted against the patched `show_table_objects`):

```python
        conn.show_streamlits(database="db")
        generic.assert_called_with(STREAMLIT, database="db", schema=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/tundra/test_snowflake_connector.py -k delegate -v`
Expected: FAIL — `ImportError: cannot import name 'STREAMLIT'` (or `AttributeError: ... no attribute 'show_streamlits'`).

- [ ] **Step 3: Define the `STREAMLIT` constant**

In `src/tundra/table_object_types.py`, after the `DYNAMIC_TABLE` definition and **before** the `TABLE_OBJECT_TYPES` list, add:

```python
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
```

Do **not** add it to `TABLE_OBJECT_TYPES` yet.

- [ ] **Step 4: Add the connector method**

In `src/tundra/snowflake_connector.py`, add `STREAMLIT` to the import block from `tundra.table_object_types` (keep alphabetical: after `ICEBERG_TABLE`, before `TABLE`), then add this method next to `show_dynamic_tables`:

```python
    def show_streamlits(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return self.show_table_objects(STREAMLIT, database=database, schema=schema)
```

- [ ] **Step 5: Add the mock method**

In `tests/tundra_test_utils/snowflake_connector.py`, next to `show_dynamic_tables`, add:

```python
    def show_streamlits(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return []
```

- [ ] **Step 6: Run the delegation test to verify it passes**

Run: `pytest tests/tundra/test_snowflake_connector.py -k delegate -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite to confirm nothing regressed**

Run: `pytest -q`
Expected: PASS (STREAMLIT is defined but unregistered, so no grant behaviour changed).

- [ ] **Step 8: Commit**

```bash
git add src/tundra/table_object_types.py src/tundra/snowflake_connector.py \
        tests/tundra_test_utils/snowflake_connector.py tests/tundra/test_snowflake_connector.py
git commit -m "feat: add show_streamlits connector method and STREAMLIT type

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Register STREAMLIT and cover the grant behaviour

Append `STREAMLIT` to the registry. This activates: `USAGE` grants on the `tables` read list (incl. `db.schema.*` ALL/FUTURE), the `<streamlit>` future placeholder in `snowflaky`, and `create streamlit` in `SCHEMA_PARTIAL_WRITE_PRIVILEGES`.

**Files:**
- Modify: `src/tundra/table_object_types.py`
- Test: `tests/tundra/test_table_object_types.py`
- Test: `tests/tundra/test_snowflake_connector.py`
- Test: `tests/tundra/test_snowflake_grants.py`
- Test: `tests/tundra/test_snowflake_spec_loader.py`

**Interfaces:**
- Consumes: `STREAMLIT`, `show_streamlits` from Task 1.
- Produces: `TABLE_OBJECT_TYPES` ending with `[..., DYNAMIC_TABLE, STREAMLIT]`.

- [ ] **Step 1: Update the registry unit tests (failing)**

In `tests/tundra/test_table_object_types.py`, add `STREAMLIT` to the import, then update the assertions:

```python
    def test_registry_contents_and_order(self):
        # Order matters: it determines SQL statement emission order.
        assert TABLE_OBJECT_TYPES == [TABLE, VIEW, ICEBERG_TABLE, DYNAMIC_TABLE, STREAMLIT]
```

Add to `test_future_placeholder`:

```python
        assert STREAMLIT.future_placeholder == "<streamlit>"
```

Add to `test_is_writable` (streamlit is NOT writable, read == write):

```python
        assert not STREAMLIT.is_writable
```

Add to `test_write_partial_privileges_strips_read`:

```python
        assert STREAMLIT.write_partial_privileges == ""
```

Add to `test_schema_create_privileges`:

```python
        assert STREAMLIT.schema_create_privilege == "create streamlit"
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `pytest tests/tundra/test_table_object_types.py -v`
Expected: FAIL — `test_registry_contents_and_order` (STREAMLIT not in list) and others.

- [ ] **Step 3: Register STREAMLIT**

In `src/tundra/table_object_types.py`, append `STREAMLIT` to the list:

```python
# Order matters: it determines SQL statement emission order.
TABLE_OBJECT_TYPES: List[TableObjectType] = [
    TABLE, VIEW, ICEBERG_TABLE, DYNAMIC_TABLE, STREAMLIT
]
```

- [ ] **Step 4: Run the registry tests to verify they pass**

Run: `pytest tests/tundra/test_table_object_types.py -v`
Expected: PASS.

- [ ] **Step 5: Add the `<STREAMLIT>` placeholder assertion (failing, then verify)**

In `tests/tundra/test_snowflake_connector.py`, in `TestSnowflakyFuturePlaceholders`, add an assertion alongside the existing `<DYNAMIC TABLE>` one:

```python
        assert (
            SnowflakeConnector.snowflaky("db_1.schema_1.<STREAMLIT>")
            == "db_1.schema_1.<streamlit>"
        )
```

Run: `pytest tests/tundra/test_snowflake_connector.py -k Placeholder -v`
Expected: PASS (the pattern derives from the now-updated registry).

- [ ] **Step 6: Add the `show_streamlits` patch to the grant test harness**

In `tests/tundra/test_snowflake_grants.py`, in `test_generate_table_and_view_grants` (around the block that patches `show_dynamic_tables`), add:

```python
        mocker.patch(
            "tundra.snowflake_grants.SnowflakeConnector.show_streamlits",
            mock_connector.show_streamlits,
        )
```

- [ ] **Step 7: Add a dedicated streamlit wildcard fixture (failing)**

In `tests/tundra/test_snowflake_grants.py`, in `TestGenerateTableAndViewGrants`, add this fixture next to `dynamic_tables_r_star_schema_config`:

```python
    def streamlits_r_star_schema_config(mocker):
        """
        Read access on database_1.schema_1.* must also cover streamlit apps:
        future + all USAGE grants.
        """
        mocker.patch.object(MockSnowflakeConnector, "show_tables", return_value=[])
        mocker.patch.object(MockSnowflakeConnector, "show_views", return_value=[])
        mocker.patch.object(
            MockSnowflakeConnector, "show_iceberg_tables", return_value=[]
        )
        mocker.patch.object(
            MockSnowflakeConnector, "show_dynamic_tables", return_value=[]
        )
        mocker.patch.object(
            MockSnowflakeConnector,
            "show_streamlits",
            return_value=["database_1.schema_1.streamlit_1"],
        )

        config = {
            "read": ["database_1.schema_1.*"],
            "write": [],
        }

        role = "functional_role"

        expected = [
            "GRANT select ON ALL dynamic tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON ALL iceberg tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON ALL tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON ALL views IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON FUTURE dynamic tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON FUTURE iceberg tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON FUTURE tables IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT select ON FUTURE views IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT usage ON ALL streamlits IN schema database_1.schema_1 TO ROLE functional_role",
            "GRANT usage ON FUTURE streamlits IN schema database_1.schema_1 TO ROLE functional_role",
        ]

        return [MockSnowflakeConnector, config, role, expected]
```

Then add `streamlits_r_star_schema_config` to the `@pytest.mark.parametrize("config", [...])` list for `test_generate_table_and_view_grants` (next to `dynamic_tables_r_star_schema_config`).

- [ ] **Step 8: Run the new fixture test to verify it passes**

Run: `pytest tests/tundra/test_snowflake_grants.py -k "streamlits_r_star" -v`
Expected: PASS.

- [ ] **Step 9: Reconcile the existing wildcard fixtures**

Registering STREAMLIT adds a `usage` ALL + FUTURE `streamlits` grant for every schema/database grouping that a read wildcard (`db.schema.*`, `db.*.*`, `*.*`) already produces `views` grants for. Run the grant tests:

Run: `pytest tests/tundra/test_snowflake_grants.py::TestGenerateTableAndViewGrants -v`
Expected: FAIL for fixtures with `.*` reads (e.g. `future_schemas_tables_views_config`, `partial_rw_future_schemas_tables_views_config`, `future_tables_w_multiple_schemas_existing_grants`, and the `*` fixtures). The assertion diff shows extra actual lines of the exact form:

```
GRANT usage ON ALL streamlits IN <schema|database> <name> TO ROLE <role>
GRANT usage ON FUTURE streamlits IN <schema|database> <name> TO ROLE <role>
```

For each failing fixture, add those two lines (one ALL, one FUTURE) per grouping shown in the diff to that fixture's `expected` list, keeping the list sorted (the `usage ...` lines sort after every `select ...` line, i.e. at the end). Verify each added line matches the format above before pasting — do not blind-copy. Fixtures that only grant on individually-named objects (no `.*`) get no streamlit lines and need no change.

- [ ] **Step 10: Add the spec-loader stub**

In `tests/tundra/test_snowflake_spec_loader.py`, wherever `show_iceberg_tables`/`show_dynamic_tables` are stubbed with `return_value=[]` (the `SpecFileLoading` end-to-end test), add:

```python
        mocker.patch.object(
            SnowflakeConnector,
            "show_streamlits",
            return_value=[],
        )
```

- [ ] **Step 11: Run the full suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add src/tundra/table_object_types.py tests/tundra/test_table_object_types.py \
        tests/tundra/test_snowflake_connector.py tests/tundra/test_snowflake_grants.py \
        tests/tundra/test_snowflake_spec_loader.py
git commit -m "feat: register STREAMLIT in the object type registry

Streamlit apps list under tables and are granted USAGE (read == write, so
grants come from the tables read list). create streamlit is derived as a
schema write privilege via the registry.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Documentation

**Files:**
- Modify: `src/tundra/cli/permissions.py`
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Update the CLI docstring**

In `src/tundra/cli/permissions.py`, in the `run` docstring, extend the fork blurb:

```python
    This fork includes support for Iceberg tables, dynamic tables, streamlit apps, external volumes, and catalog integrations.
```

- [ ] **Step 2: Update the README**

In `README.md`, in the paragraph that lists what rides under `tables` (currently "Tables, views, Iceberg tables, and dynamic tables are all listed under `tables` ..."), update to include streamlits and note the privilege model:

```markdown
Tables, views, Iceberg tables, dynamic tables, and Streamlit apps are all listed under `tables` and handled
properly behind the scenes. Streamlit apps expose only `USAGE`: read and write both grant `usage`, and a
`create streamlit` schema privilege is granted with schema write access. Because Streamlit apps ride the
`tables` read list, a `database.schema.*` read wildcard also grants `USAGE` on all and future Streamlit apps
in that schema. New table-like object types are defined in `src/tundra/table_object_types.py`.
```

- [ ] **Step 3: Run the full suite (docs shouldn't break anything)**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/tundra/cli/permissions.py README.md
git commit -m "docs: document streamlit grant support

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** registry entry (Task 1/2), connector method (Task 1), mock (Task 1), schema create privilege — automatic via `SCHEMA_PARTIAL_WRITE_PRIVILEGES`, exercised by reconciled fixtures (Task 2 Step 9), docs/CLI (Task 3), tests across all five test files (Tasks 1–2). All spec sections map to a task.
- **Behaviour changes** from the spec (schema-write now grants `create streamlit`; `tables` read wildcard now grants streamlit `usage`) surface as fixture diffs reconciled in Task 2 Step 9 and are documented in Task 3.
- **Type consistency:** `show_streamlits`, `STREAMLIT`, `name="streamlit"`, `<streamlit>` placeholder, and the `usage ... streamlits` SQL form are used identically across all tasks.
