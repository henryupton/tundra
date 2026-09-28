from tundra.table_object_types import (
    DYNAMIC_TABLE,
    ICEBERG_TABLE,
    SEMANTIC_VIEW,
    STREAMLIT,
    TABLE,
    TABLE_OBJECT_TYPES,
    VIEW,
)


class TestTableObjectTypes:
    def test_registry_contents_and_order(self):
        # Order matters: it determines SQL statement emission order.
        assert TABLE_OBJECT_TYPES == [
            TABLE,
            VIEW,
            ICEBERG_TABLE,
            DYNAMIC_TABLE,
            STREAMLIT,
            SEMANTIC_VIEW,
        ]

    def test_grant_key_is_the_wire_form(self):
        # `name` is the SQL keyword; `grant_key` is what Snowflake reports in
        # `granted_on` and inside future-grant names. They differ for multi-word types.
        assert TABLE.grant_key == "table"
        assert VIEW.grant_key == "view"
        assert ICEBERG_TABLE.grant_key == "iceberg_table"
        assert DYNAMIC_TABLE.grant_key == "dynamic_table"
        assert STREAMLIT.grant_key == "streamlit"
        assert SEMANTIC_VIEW.grant_key == "semantic_view"

    def test_future_placeholder(self):
        assert TABLE.future_placeholder == "<table>"
        assert ICEBERG_TABLE.future_placeholder == "<iceberg_table>"
        assert DYNAMIC_TABLE.future_placeholder == "<dynamic_table>"
        assert STREAMLIT.future_placeholder == "<streamlit>"
        assert SEMANTIC_VIEW.future_placeholder == "<semantic_view>"

    def test_is_writable(self):
        assert TABLE.is_writable
        assert ICEBERG_TABLE.is_writable
        assert DYNAMIC_TABLE.is_writable
        assert not VIEW.is_writable
        assert not STREAMLIT.is_writable
        assert not SEMANTIC_VIEW.is_writable

    def test_write_partial_privileges_strips_read(self):
        assert (
            TABLE.write_partial_privileges
            == "insert, update, delete, truncate, references"
        )
        assert DYNAMIC_TABLE.write_partial_privileges == "monitor, operate"
        assert VIEW.write_partial_privileges == ""
        assert STREAMLIT.write_partial_privileges == ""
        assert SEMANTIC_VIEW.write_partial_privileges == ""

    def test_schema_create_privileges(self):
        assert TABLE.schema_create_privilege == "create table"
        assert VIEW.schema_create_privilege == "create view"
        assert ICEBERG_TABLE.schema_create_privilege == "create iceberg table"
        assert DYNAMIC_TABLE.schema_create_privilege is None
        assert STREAMLIT.schema_create_privilege == "create streamlit"
        assert SEMANTIC_VIEW.schema_create_privilege == "create semantic view"
