from tundra.table_object_types import (
    ICEBERG_TABLE,
    TABLE,
    TABLE_OBJECT_TYPES,
    VIEW,
)


class TestTableObjectTypes:
    def test_registry_contents_and_order(self):
        # Order matters: it determines SQL statement emission order.
        assert TABLE_OBJECT_TYPES == [TABLE, VIEW, ICEBERG_TABLE]

    def test_future_placeholder(self):
        assert TABLE.future_placeholder == "<table>"
        assert ICEBERG_TABLE.future_placeholder == "<iceberg table>"

    def test_is_writable(self):
        assert TABLE.is_writable
        assert ICEBERG_TABLE.is_writable
        assert not VIEW.is_writable

    def test_write_partial_privileges_strips_read(self):
        assert (
            TABLE.write_partial_privileges
            == "insert, update, delete, truncate, references"
        )
        assert VIEW.write_partial_privileges == ""

    def test_schema_create_privileges(self):
        assert TABLE.schema_create_privilege == "create table"
        assert VIEW.schema_create_privilege == "create view"
        assert ICEBERG_TABLE.schema_create_privilege == "create iceberg table"
