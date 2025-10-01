import logging

import click

import tundra
from tundra.logger import GLOBAL_LOGGER as logger


@click.group(invoke_without_command=True, no_args_is_help=True)
@click.option(
    "-v", "--verbose", help="Increases log level with count, e.g -vv", count=True
)
@click.version_option(
    version=tundra.__version__, prog_name="tundra",
    message="%(prog)s %(version)s - Snowflake permissions with Iceberg table support"
)
@click.pass_context
def cli(ctx, verbose):
    logger.setLevel(logging.WARNING)
    if verbose == 1:
        logger.setLevel(logging.INFO)
    if verbose >= 2:
        logger.setLevel(logging.DEBUG)

    ctx.ensure_object(dict)
