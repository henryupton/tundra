from .cli import cli  # isort:skip

from tundra.cli import permissions


def main():
    cli()


__all__ = ["permissions"]
