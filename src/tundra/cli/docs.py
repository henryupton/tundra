import webbrowser
from pathlib import Path

import click

from tundra import SpecLoadingError
from tundra.docs import site
from tundra.docs.catalogue import FamilyRule, SpecSource, build_catalogue
from tundra.docs.live import load_live
from tundra.snowflake_connector import ApiFallbackError, InvalidSettingError

from . import cli
from .permissions import exit_with_error


def spec_name(spec_path: str) -> str:
    """What the site calls a spec: its directory's name, or its file's without the extension."""
    path = Path(spec_path).resolve()
    return path.stem if path.is_file() else path.name


def family_rules(ctx, param, values):
    try:
        return [FamilyRule.parse(value) for value in values]
    except ValueError as error:
        raise click.BadParameter(str(error), ctx=ctx, param=param)


@cli.group()  # type: ignore
def docs():
    """
    Generate and serve a browsable site of a spec's roles, users and grants.
    """


@docs.command()
@click.option(
    "--spec",
    required=True,
    type=click.Path(exists=True),
    help="Spec file, or directory of spec fragments, as run takes it.",
)
@click.option(
    "--target-dir",
    default="target",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Where to write the site.",
)
@click.option(
    "--live",
    is_flag=True,
    help=(
        "Also read the account's grants, as run does, and mark each edge as in both,"
        " live only or spec only. Read only: it never grants or revokes. Connects with"
        " the PERMISSION_BOT_* settings, as securityadmin or a role that inherits it."
    ),
)
@click.option(
    "--family",
    "families",
    multiple=True,
    metavar="LABEL=PATTERN",
    callback=family_rules,
    help=(
        "Give the roles whose names match PATTERN, a glob, their own colour and row"
        " in the key, e.g. --family 'Finance roles=finance_*'. Repeatable; the"
        " first match wins."
    ),
)
@click.option(
    "--sql-only",
    is_flag=True,
    help="With --live, read through SQL only, not snowflake.core's REST API.",
)
def generate(spec, target_dir, live, families, sql_only):
    """
    Write a static site for a spec: catalogue.json, plus index.html and its assets,
    which open from disk with no network. By default it reads the spec alone and
    never connects to Snowflake.
    """
    try:
        source = load_live(spec, sql_only=sql_only) if live else SpecSource(spec)
        catalogue = build_catalogue(
            source, name=spec_name(spec), live=live, families=families
        )
    except (SpecLoadingError, ApiFallbackError, InvalidSettingError) as exc:
        exit_with_error(exc)

    index = site.write_site(catalogue, target_dir)
    click.secho(f"Wrote {index}", fg="green")


@docs.command()
@click.option(
    "--target-dir",
    default="target",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The directory tundra docs generate wrote.",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option(
    "--browser/--no-browser", default=True, help="Open the site in a browser."
)
def serve(target_dir, host, port, browser):
    """Serve the generated site locally and open it. Ctrl-C stops it."""
    if not (Path(target_dir) / "index.html").is_file():
        exit_with_error(
            SpecLoadingError(
                f"No docs site in {target_dir}. Run `tundra docs generate` first."
            )
        )

    server = site.make_server(target_dir, host, port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    click.secho(f"Serving {target_dir} at {url} (Ctrl-C to stop)", fg="green")
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
