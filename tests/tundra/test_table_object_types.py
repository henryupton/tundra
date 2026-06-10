from tundra.table_object_types import (
    DYNAMIC_TABLE,
    ICEBERG_TABLE,
    TABLE,
    TABLE_OBJECT_TYPES,
    VIEW,
)


class TestTableObjectTypes:
    def test_registry_contents_and_order(self):
        # Order matters: it determines SQL statement emission order.
        assert TABLE_OBJECT_TYPES == [TABLE, VIEW, ICEBERG_TABLE, DYNAMIC_TABLE]

    def test_future_placeholder(self):
        assert TABLE.future_placeholder == "<table>"
        assert ICEBERG_TABLE.future_placeholder == "<iceberg table>"

    def test_is_writable(self):
        assert TABLE.is_writable
        assert ICEBERG_TABLE.is_writable
        assert not VIEW.is_writable
        assert not DYNAMIC_TABLE.is_writable

    def test_write_partial_privileges_strips_read(self):
        assert (
            TABLE.write_partial_privileges
            == "insert, update, delete, truncate, references"
        )
        assert VIEW.write_partial_privileges == ""

    def test_dynamic_table_has_no_schema_create_privilege(self):
        assert DYNAMIC_TABLE.schema_create_privilege is None
        assert TABLE.schema_create_privilege == "create table"
