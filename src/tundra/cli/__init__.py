from .cli import cli  # isort:skip

from tundra.cli import docs, permissions


def main():
    cli()


__all__ = ["docs", "permissions"]
