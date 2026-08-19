from tundra.grant_coverage import GrantCoverage


class TestGrantCoverage:
    def test_explicit_object_is_covered(self):
        coverage = GrantCoverage()
        coverage.add_object("database_1.schema_1.table_1")

        assert coverage.covers("database_1.schema_1.table_1")
        assert not coverage.covers("database_1.schema_1.table_2")

    def test_schema_scope_covers_every_object_in_it(self):
        # This is the substitution that removes the need to list a schema: an ON ALL plus
        # ON FUTURE pair covers present and future objects alike, so the scope says
        # everything the enumerated list used to.
        coverage = GrantCoverage()
        coverage.add_schema("database_1.schema_1")

        assert coverage.covers("database_1.schema_1.table_1")
        assert coverage.covers("database_1.schema_1.some_table_added_later")
        assert coverage.covers("database_1.schema_1.<table>")

    def test_schema_scope_does_not_leak_to_other_schemas(self):
        coverage = GrantCoverage()
        coverage.add_schema("database_1.schema_1")

        assert not coverage.covers("database_1.schema_2.table_1")
        assert not coverage.covers("database_2.schema_1.table_1")

    def test_schema_scope_does_not_cover_a_database_level_future_grant(self):
        # `db.<table>` has a database, not a schema, as its parent. A database-level
        # future grant is only ever covered by being recorded explicitly.
        coverage = GrantCoverage()
        coverage.add_schema("database_1.schema_1")

        assert not coverage.covers("database_1.<table>")

        coverage.add_object("database_1.<table>")

        assert coverage.covers("database_1.<table>")

    def test_names_are_normalised_on_both_sides(self):
        # A schema reference read verbatim from the spec and one read back from
        # SHOW GRANTS are quoted differently; both have to resolve to the same key.
        coverage = GrantCoverage()
        coverage.add_schema("DATABASE_1.SCHEMA_1")

        assert coverage.covers("database_1.schema_1.table_1")
        assert coverage.covers("DATABASE_1.SCHEMA_1.TABLE_1")

    def test_merge_unions_objects_and_schemas(self):
        read = GrantCoverage()
        read.add_schema("database_1.schema_1")
        write = GrantCoverage()
        write.add_object("database_1.schema_2.table_1")

        merged = read.merge(write)

        assert merged.covers("database_1.schema_1.anything")
        assert merged.covers("database_1.schema_2.table_1")
        # merge returns a new value rather than mutating either side
        assert not read.covers("database_1.schema_2.table_1")

    def test_empty_coverage_covers_nothing(self):
        assert not GrantCoverage().covers("database_1.schema_1.table_1")
