import os
import sys
from pathlib import Path

import click

from tundra import SpecLoadingError
from tundra.caching_connector import CachingSnowflakeConnector
from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_spec_loader import SnowflakeSpecLoader
from tundra.state_cache import StateCache

from . import cli


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
    else:
        foreground_color = "red"
        run_prefix = "[ERROR] "

    click.secho(f"{diff_prefix}{run_prefix}{command['sql']};", fg=foreground_color)


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
    "--no-cache",
    is_flag=True,
    help="Bypass the local state cache (always fetch fresh).",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Ignore cached state and overwrite it with a fresh fetch.",
)
@click.option(
    "--cache-ttl",
    type=float,
    default=3600,
    help="Cache freshness window in seconds (default 3600).",
)
@click.option(
    "--cache-path",
    type=str,
    default=None,
    help="Path to the SQLite cache file (default ~/.cache/tundra/<account>.db).",
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
    no_cache,
    refresh,
    cache_ttl,
    cache_path,
    print_skipped=False,
):
    """
    Grant the permissions provided in the provided specification file for specific users and roles.
    This fork includes support for Iceberg tables, dynamic tables, external volumes, and catalog integrations.
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
        no_cache=no_cache,
        refresh=refresh,
        cache_ttl=cache_ttl,
        cache_path=cache_path,
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
def spec_test(spec, role, user, run_list, ignore_memberships):
    """
    Load SnowFlake spec based on the roles.yml provided. CLI use only for confirming specifications are valid.
    This fork includes validation for Iceberg tables, external volumes, and catalog integrations.
    """
    load_specs(
        spec,
        role=role,
        user=user,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
        do_spec_test=True,
    )


def _build_connector(no_cache, refresh, cache_ttl, cache_path):
    conn = SnowflakeConnector()
    if no_cache:
        return conn
    account = os.getenv("PERMISSION_BOT_ACCOUNT", "default")
    path = cache_path or str(Path.home() / ".cache" / "tundra" / f"{account}.db")
    cache = StateCache(path=path, account=account, ttl_seconds=cache_ttl)
    return CachingSnowflakeConnector(conn, cache, refresh=refresh)


def load_specs(
    spec,
    role,
    user,
    run_list,
    ignore_memberships,
    do_spec_test,
    skip_validation=False,
    ignore_missing_objects=False,
    conn=None,
):
    """
    Load specs separately.
    """
    try:
        click.secho("Confirming spec loads successfully")
        spec_loader = SnowflakeSpecLoader(
            spec,
            conn=conn,
            roles=role,
            users=user,
            run_list=run_list,
            ignore_memberships=ignore_memberships,
            spec_test=do_spec_test,
            skip_validation=skip_validation,
            ignore_missing_objects=ignore_missing_objects,
        )
        click.secho("Snowflake specs successfully loaded", fg="green")
    except SpecLoadingError as exc:
        for line in str(exc).splitlines():
            click.secho(line, fg="red")
        sys.exit(1)

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
    no_cache=False,
    refresh=False,
    cache_ttl=3600,
    cache_path=None,
):
    """Grant the permissions provided in the provided specification file."""
    conn = _build_connector(no_cache, refresh, cache_ttl, cache_path)

    spec_loader = load_specs(
        spec,
        role=roles,
        user=users,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
        skip_validation=skip_validation,
        ignore_missing_objects=ignore_missing_objects,
        do_spec_test=False,
        conn=conn,
    )

    sql_grant_queries = spec_loader.generate_permission_queries(
        roles=roles,
        users=users,
        run_list=run_list,
        ignore_memberships=ignore_memberships,
    )

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
            status = None
            if not query.get("already_granted"):
                try:
                    conn.run_query(query.get("sql", ""))
                    status = True
                except Exception:
                    status = False

                ran_query = query
                ran_query["run_status"] = status
                print_command(ran_query, diff)
            # If already granted, print command
            elif print_skipped:
                print_command(query, diff)
        # If dry, print commands
        else:
            if not query.get("already_granted") or print_skipped:
                print_command(query, diff, dry=True)


cli.add_command(spec_test)  # type: ignore
