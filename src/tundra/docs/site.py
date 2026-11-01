"""
The static site `tundra docs` writes: index.html with the catalogue inside it, the
catalogue again as catalogue.json, and the assets the page loads. The page needs no
server and no network, so it opens from disk, and `serve` only saves the double-click.
"""

import html
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Dict, Union

STATIC = files("tundra.docs") / "static"


def _copy_tree(source: Traversable, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_dir():
            _copy_tree(item, target / item.name)
        else:
            (target / item.name).write_bytes(item.read_bytes())


def _embedded(catalogue: Dict[str, Any]) -> str:
    """
    The catalogue as JSON a <script> element can hold. Escaping <, > and & keeps spec
    text such as "</script>" or "<!--" from ending the element early, and JSON.parse
    reads the escapes back as the characters they stand for.
    """
    text = json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def write_site(catalogue: Dict[str, Any], target_dir: Union[str, Path]) -> Path:
    """Write the site for `catalogue` into target_dir and return its index.html."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    _copy_tree(STATIC / "assets", target / "assets")
    (target / "catalogue.json").write_text(
        json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    title = f"{catalogue['metadata']['spec']} · tundra docs"
    page = (
        (STATIC / "index.html")
        .read_text(encoding="utf-8")
        .replace("__TITLE__", html.escape(title))
        .replace("__CATALOGUE__", _embedded(catalogue))
    )
    index = target / "index.html"
    index.write_text(page, encoding="utf-8")
    return index


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def make_server(
    target_dir: Union[str, Path], host: str, port: int
) -> ThreadingHTTPServer:
    """An HTTP server for the site in target_dir. Port 0 picks a free port."""
    handler = partial(_QuietHandler, directory=str(target_dir))
    return ThreadingHTTPServer((host, port), handler)
