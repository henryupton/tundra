import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from tundra.docs.catalogue import SpecSource, build_catalogue
from tundra.docs.site import make_server, write_site

SPEC = Path(__file__).parents[1] / "specs" / "docs_site"


def embedded_catalogue(html):
    match = re.search(
        r'<script id="tundra-catalogue" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match, "index.html carries no catalogue"
    return json.loads(match.group(1))


@pytest.fixture
def site(tmp_path):
    catalogue = build_catalogue(SpecSource(str(SPEC)), name="docs_site")
    write_site(catalogue, tmp_path / "target")
    return tmp_path / "target", catalogue


def test_writes_the_catalogue_and_a_page_that_carries_it(site):
    target, catalogue = site
    assert json.loads((target / "catalogue.json").read_text()) == catalogue
    assert embedded_catalogue((target / "index.html").read_text()) == catalogue


def test_the_page_loads_nothing_from_the_network(site):
    target, _ = site
    html = (target / "index.html").read_text()
    references = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert references, "index.html loads no assets"
    for reference in references:
        if reference.startswith("#"):
            continue
        assert not re.match(r"[a-z]+:|//", reference), reference
        assert (target / reference).is_file(), reference


def test_the_layout_engine_ships_with_the_site(site):
    target, _ = site
    viz = target / "assets" / "vendor" / "viz-global.js"
    assert viz.stat().st_size > 1_000_000
    assert viz.read_text().startswith("/*!\nViz.js 3.31.0")


def test_spec_text_cannot_close_the_script_that_carries_it(tmp_path):
    spec = tmp_path / "spec.yml"
    spec.write_text(
        "roles:\n"
        "  - reader:\n"
        "      meta:\n"
        "        comment: \"</script><script>alert('reader')</script> <!--\"\n"
        "      warehouses: [loading]\n"
        "warehouses:\n"
        "  - loading:\n"
        "      size: x-small\n"
    )
    catalogue = build_catalogue(SpecSource(str(spec)), name="spec")
    write_site(catalogue, tmp_path / "target")

    html = (tmp_path / "target" / "index.html").read_text()
    assert "alert('reader')</script>" not in html
    assert "<!--" not in html.split('id="tundra-catalogue"', 1)[1]
    assert embedded_catalogue(html) == catalogue


def test_regenerating_replaces_the_previous_site(site):
    target, catalogue = site
    (target / "catalogue.json").write_text("{}")
    write_site(catalogue, target)
    assert json.loads((target / "catalogue.json").read_text()) == catalogue


def test_the_server_serves_the_site(site):
    target, _ = site
    server = make_server(target, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            assert response.status == 200
            assert 'id="tundra-catalogue"' in response.read().decode()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/catalogue.json"
        ) as response:
            assert json.loads(response.read())["metadata"]["spec"] == "docs_site"
    finally:
        server.shutdown()
        server.server_close()
