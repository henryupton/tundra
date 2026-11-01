import os
import sys

import click

from tundra import SpecLoadingError
from tundra.operations import SqlStatement, api_capable
from tundra.snowflake_connector import (
    API,
    ApiFallbackError,
    InvalidSettingError,
    SnowflakeConnector,
    WriteStats,
    api_error_detail,
)
from tundra.snowflake_spec_loader import SnowflakeSpecLoader

from . import cli


def exit_with_error(error: Exception) -> None:
    for line in str(error).splitlines():
        click.secho(line, fg="red")
    sys.exit(1)


def print_command(command, diff, dry=False):
    """Prints the queries to the command line with prefixes"""
    diff_prefix = ""
    if command["already_granted"]:
        if diff:
            diff_prefix = "  "
        else:
            pass
    else:
        if diff:
            diff_prefix = "+ "

    if command.get("run_status"):
        foreground_color = "green"
        run_prefix = "[SUCCESS] "
    elif command.get("run_status") is None and dry:
        foreground_color = "cyan"
        run_prefix = "[PENDING] "
    elif command.get("run_status") is None:
        foreground_color = "cyan"
        run_prefix = "[SKIPPED] "
    elif command.get("vanished"):
        foreground_color = "yellow"
        run_prefix = "[VANISHED] "
    else:
        foreground_color = "red"
        run_prefix = "[ERROR] "

    click.secho(f"{diff_prefix}{run_prefix}{command['sql']};", fg=foreground_color)
    if command.get("error"):
        click.secho(f"    {command['error']}", fg=foreground_color)


def error_detail(error: Exception) -> str:
    """
    Snowflake's own message on one line: without SQLAlchemy's wrapper and SQL echo from
    SQL, and as `error code (HTTP status): message` from snowflake.core.
    """
    return api_error_detail(error) or " ".join(
        str(getattr(error, "orig", error)).split()
    )


def apply_query(conn, query):
    """
    Run one generated statement, recording run_status and, on failure, the error.

    The connector runs it through snowflake.core or as its SQL (see
    SnowflakeConnector.execute). A plain dict, or a planned command whose `sql` a caller
    has changed since planning, runs its `sql` as SQL, so what runs is what prints.

    Statements are generated from a snapshot of the account, so an object something else
    drops between planning and applying (a CI job recreating its schema, say) fails every
    statement still queued against it. A fresh lookup separates the two outcomes: still
    gone marks the statement vanished, as there is nothing left to grant on; back again
    means it was recreated, so the statement is retried once against the new object.
    The retry goes the way the first try ended: a statement the API couldn't answer, and
    ran as SQL, is retried as SQL.
    """
    sql = query.get("sql", "")
    operation = getattr(query, "operation", None)
    if operation is None or operation.sql != sql:
        operation = SqlStatement(sql)
    executed = conn.execute(operation)

    missing = (
        SnowflakeConnector.missing_object(executed.error, operation)
        if executed.error
        else None
    )
    if missing and conn.object_exists(*missing):
        executed = conn.execute(operation, through_api=executed.transport == API)
    elif missing:
        query["vanished"] = True

    if api_capable(operation):
        conn.write_stats.applied(executed.transport)
    query["run_status"] = executed.error is None
    if executed.error is not None:
        query["error"] = error_detail(executed.error)


def report_writes(conn) -> None:
    """
    One line on how the statements apply ran went: through the API or SQL, and why any
    ran as SQL when the API was meant to take them, including a listing's failure that
    sent the rest of the run through SQL.
    """
    stats = getattr(conn, "write_stats", None)
    if not isinstance(stats, WriteStats) or not stats.statements:
        return
    click.secho(
        stats.summary(sql_only=conn.sql_only, api_failure=conn.api_failure),
        fg="yellow" if stats.any_sent_to_sql else "green",
    )


@cli.command()  # type: ignore
@click.argument("spec")
@click.option(
    "--dry", "--dryrun", help="Do not actually run, just check.", is_flag=True
)
@click.option(
    "--diff", help="Show full diff, both new and existing permissions.", is_flag=True
)
@click.option(
    "--role",
    multiple=True,
    default=[],
    help="Run grants for specific roles. Usage: --role testrole --role testrole2.",
)
@click.option(
    "--user",
    multiple=True,
    default=[],
    help="Run grants for specific users. Usage: --user testuser --user testuser2.",
)
@click.option(
    "--ignore-memberships",
    help="Do not handle role membership grants/revokes",
    is_flag=True,
)
@click.option(
    "--skip-validation",
    help="Skip validation checks for entity existence in Snowflake",
    is_flag=True,
)
@click.option(
    "--ignore-missing-objects",
    help="Ignore grants for objects that don't exist in Snowflake instead of failing",
    is_flag=True,
)
@click.option(
    "--max-workers",
    type=click.IntRange(min=1),
    default=None,
    help="Number of parallel Snowflake fetch workers (default 8, env PERMISSION_BOT_MAX_WORKERS).",
)
@click.option(
    "--sql-only",
    help=(
        "Read and write through SQL only, not snowflake.core's REST API"
        " (env PERMISSION_BOT_SQL_ONLY=true)."
    ),
    is_flag=True,
)
@click.pass_context
def run(
    ctx,
    spec,
    dry,
    diff,
    role,
    user,
    ignore_memberships,
    skip_validation,
    ignore_missing_objects,
    max_workers,
    sql_only,
    print_skipped=False,
):
    """
    Grant the permissions provided in the provided specification file for specific users and roles.
    This fork includes support for Iceberg tables, dynamic tables, streamlit apps, semantic views, external volumes, and catalog integrations.

    Reads go through Snowflake's REST API where snowflake.core covers them, and any
    listing it can't give in full is read again through SQL. Some kinds of statement go
    through the API too (the README lists them), and the rest run as SQL, as does any
    statement the API can't answer. A listing or statement with no answer after
    PERMISSION_BOT_API_TIMEOUT seconds (default 120) goes through SQL, and so does the
    rest of the run. PERMISSION_BOT_SQL_ONLY=true does what --sql-only does, and
    PERMISSION_BOT_API_STRICT=true fails the run instead of reading through SQL.
    """
    if role and user:
        run_list = ["roles", "users"]
    elif role:
        run_list = ["roles"]
    elif user:
        run_list = ["users"]
    else:
        run_list = ["roles", "users"]
    if ctx.parent.params.get("verbose", 0) >= 1:
        print_skipped = True
    if max_workers is not None:
        os.environ["PERMISSION_BOT_MAX_WORKERS"] = str(max_workers)
    tundra_grants(
        spec=spec,
        dry=dry,
        diff=diff,
        roles=role,
        users=user,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
        skip_validation=skip_validation,
        ignore_missing_objects=ignore_missing_objects,
        print_skipped=print_skipped,
        sql_only=sql_only,
    )


@click.command()
@click.argument("spec")
@click.option(
    "--role",
    multiple=True,
    default=[],
    help="Run grants for specific roles. Usage: --role testrole --role testrole2.",
)
@click.option(
    "--user",
    multiple=True,
    default=[],
    help="Run grants for specific users. Usage: --user testuser --user testuser2.",
)
@click.option(
    "--ignore-memberships",
    help="Do not handle role membership grants/revokes",
    is_flag=True,
)
@click.option(
    "--run-list",
    multiple=True,
    default=["roles", "users"],
    help="Run grants for specific users. Usage: --user testuser --user testuser2.",
)
@click.option(
    "--sql-only",
    help=(
        "Read and write through SQL only, not snowflake.core's REST API"
        " (env PERMISSION_BOT_SQL_ONLY=true)."
    ),
    is_flag=True,
)
def spec_test(spec, role, user, run_list, ignore_memberships, sql_only):
    """
    Load SnowFlake spec based on the roles.yml provided. CLI use only for confirming specifications are valid.
    This fork includes validation for Iceberg tables, external volumes, and catalog integrations.

    Reads go through Snowflake's REST API where snowflake.core covers them, and any
    listing it can't give in full is read again through SQL.
    PERMISSION_BOT_SQL_ONLY=true does what --sql-only does, and
    PERMISSION_BOT_API_STRICT=true fails the run instead of falling back to SQL.
    A listing with no answer after PERMISSION_BOT_API_TIMEOUT seconds (default 120)
    is read through SQL, and so is the rest of the run.
    """
    load_specs(
        spec,
        role=role,
        user=user,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
        do_spec_test=True,
        sql_only=sql_only,
    )


def load_specs(
    spec,
    role,
    user,
    run_list,
    ignore_memberships,
    do_spec_test,
    skip_validation=False,
    ignore_missing_objects=False,
    sql_only=False,
):
    """
    Load specs separately.
    """
    try:
        click.secho("Confirming spec loads successfully")
        spec_loader = SnowflakeSpecLoader(
            spec,
            roles=role,
            users=user,
            run_list=run_list,
            ignore_memberships=ignore_memberships,
            spec_test=do_spec_test,
            skip_validation=skip_validation,
            ignore_missing_objects=ignore_missing_objects,
            sql_only=sql_only,
        )
        click.secho("Snowflake specs successfully loaded", fg="green")
    except (SpecLoadingError, ApiFallbackError, InvalidSettingError) as exc:
        exit_with_error(exc)

    return spec_loader


def tundra_grants(
    spec,
    dry,
    diff,
    roles,
    users,
    run_list,
    ignore_memberships,
    skip_validation,
    ignore_missing_objects,
    print_skipped,
    sql_only=False,
):
    """Grant the permissions provided in the provided specification file."""
    spec_loader = load_specs(
        spec,
        role=roles,
        user=users,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
        skip_validation=skip_validation,
        ignore_missing_objects=ignore_missing_objects,
        do_spec_test=False,
        sql_only=sql_only,
    )

    try:
        sql_grant_queries = spec_loader.generate_permission_queries(
            roles=roles,
            users=users,
            run_list=run_list,
            ignore_memberships=ignore_memberships,
        )
    except ApiFallbackError as exc:
        exit_with_error(exc)

    click.secho()
    if diff:
        click.secho(
            "SQL Commands generated for given spec file (Full diff with both new and already granted commands):"
        )
    else:
        click.secho("SQL Commands generated for given spec file:")
    click.secho()

    for query in sql_grant_queries:
        if not dry:
            if not query.get("already_granted"):
                apply_query(spec_loader.conn, query)
                print_command(query, diff)
            # If already granted, print command
            elif print_skipped:
                print_command(query, diff)
        # If dry, print commands
        else:
            if not query.get("already_granted") or print_skipped:
                print_command(query, diff, dry=True)

    if not dry:
        report_writes(spec_loader.conn)


cli.add_command(spec_test)  # type: ignore
