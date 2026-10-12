import snowflake.connector
import sqlalchemy

from tundra.cli.permissions import apply_query, print_command
from tundra_test_utils.snowflake_connector import MockSnowflakeConnector

SQL = "GRANT select ON FUTURE views IN schema test_analytics.pr_1__data_1 TO ROLE developer"
SCHEMA_GONE = "Schema 'TEST_ANALYTICS.PR_1__DATA_1' does not exist or not authorized."


def snowflake_error(message: str) -> sqlalchemy.exc.DBAPIError:
    orig = snowflake.connector.errors.ProgrammingError(
        msg=f"SQL compilation error:\n{message}", errno=2003, sqlstate="02000"
    )
    return sqlalchemy.exc.ProgrammingError(SQL, None, orig)


def connector(mocker, run_query_effects, schemas):
    conn = MockSnowflakeConnector()
    run_query = mocker.patch.object(conn, "run_query", side_effect=run_query_effects)
    mocker.patch.object(conn, "show_schemas", return_value=schemas)
    return conn, run_query


class TestApplyQuery:
    def test_success(self, mocker):
        conn, run_query = connector(mocker, [None], [])
        query = {"sql": SQL, "already_granted": False}

        apply_query(conn, query)

        assert query["run_status"] is True
        assert "error" not in query
        run_query.assert_called_once_with(SQL)

    def test_failure_records_snowflake_message(self, mocker):
        error = snowflake_error("Insufficient privileges to operate on schema 'S'.")
        conn, run_query = connector(mocker, [error], [])
        query = {"sql": SQL, "already_granted": False}

        apply_query(conn, query)

        assert query["run_status"] is False
        assert not query.get("vanished")
        assert query["error"] == (
            "002003 (02000): SQL compilation error: "
            "Insufficient privileges to operate on schema 'S'."
        )
        run_query.assert_called_once_with(SQL)

    def test_object_dropped_mid_run_is_vanished(self, mocker):
        conn, run_query = connector(mocker, [snowflake_error(SCHEMA_GONE)], [])
        query = {"sql": SQL, "already_granted": False}

        apply_query(conn, query)

        assert query["run_status"] is False
        assert query["vanished"] is True
        assert SCHEMA_GONE in query["error"]
        run_query.assert_called_once_with(SQL)

    def test_object_recreated_mid_run_is_retried(self, mocker):
        conn, run_query = connector(
            mocker,
            [snowflake_error(SCHEMA_GONE), None],
            ["test_analytics.pr_1__data_1"],
        )
        query = {"sql": SQL, "already_granted": False}

        apply_query(conn, query)

        assert query["run_status"] is True
        assert not query.get("vanished")
        assert run_query.call_count == 2

    def test_retry_failure_is_an_error(self, mocker):
        conn, _ = connector(
            mocker,
            [snowflake_error(SCHEMA_GONE), snowflake_error(SCHEMA_GONE)],
            ["test_analytics.pr_1__data_1"],
        )
        query = {"sql": SQL, "already_granted": False}

        apply_query(conn, query)

        assert query["run_status"] is False
        assert not query.get("vanished")
        assert SCHEMA_GONE in query["error"]


class TestPrintCommand:
    def test_error_prints_detail_under_statement(self, capsys):
        print_command(
            {
                "sql": SQL,
                "already_granted": False,
                "run_status": False,
                "error": "boom",
            },
            diff=False,
        )

        assert capsys.readouterr().out == f"[ERROR] {SQL};\n    boom\n"

    def test_vanished_is_not_an_error(self, capsys):
        print_command(
            {
                "sql": SQL,
                "already_granted": False,
                "run_status": False,
                "vanished": True,
                "error": SCHEMA_GONE,
            },
            diff=False,
        )

        assert capsys.readouterr().out == f"[VANISHED] {SQL};\n    {SCHEMA_GONE}\n"
