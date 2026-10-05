import json
from pathlib import Path
from urllib.parse import urljoin

import pytest
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/source_002"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
SITES = {
    site["id"]: site
    for site in yaml.safe_load((ROOT / "config/sites.yaml").read_text(encoding="utf-8"))["sites"]
}


@pytest.mark.parametrize("site_id", sorted(MANIFEST))
def test_observed_result_markup_excludes_sidebar(site_id):
    config = SITES[site_id]["internal_search"]
    fixture = BeautifulSoup(
        (FIXTURES / f"{site_id}.html").read_text(encoding="utf-8"), "html.parser"
    )
    matches = fixture.select(config["result_link_css"])
    urls = [urljoin(config["start_url"], a["href"]) for a in matches]
    assert urls == [MANIFEST[site_id]["expected_url"]]
