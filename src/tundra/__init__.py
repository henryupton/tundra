from importlib.metadata import PackageNotFoundError, version

from tundra.error import SpecLoadingError

try:
    __version__ = version("tundra-cli")
except PackageNotFoundError:
    # A source checkout that was never installed has no package metadata
    __version__ = "unknown"

__all__ = [
    "SpecLoadingError",
]
