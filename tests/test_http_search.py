from unittest.mock import Mock

import httpx
import pytest

from petrovaradin_archive.providers.http_search import HTTPInternalSearchProvider


def make_site():
    return {
        "id": "blic",
        "domains": ["example.rs"],
        "internal_search": {
            "enabled": True,
            "engine": "http",
            "search_both_scripts": False,
            "start_url": "https://example.rs/search?q=Petrovaradin",
            "result_link_css": "article h2 a",
            "next_css": "#next",
            "pagination_mode": "template",
            "require_next_link": True,
            "page_url_template": "https://example.rs/search?q=Petrovaradin&strana={page}",
            "max_pages": 3,
        },
    }


def test_http_search_uses_template_for_form_pagination_and_skips_duplicates():
    client = Mock()
    client.allowed.return_value = True
    htmls = [
        '<article><h2><a href="/a">Prvi članak</a></h2></article><a id="next" href="#">Sledeća</a>',
        '<article><h2><a href="/a">Prvi članak</a></h2><h2><a href="/b">Drugi članak</a></h2></article>',
    ]
    client.get.side_effect = lambda url: httpx.Response(
        200, text=htmls.pop(0), request=httpx.Request("GET", url)
    )
    items = list(HTTPInternalSearchProvider(client, {}).discover(make_site()))
    assert [item.url for item in items] == ["https://example.rs/a", "https://example.rs/b"]
    assert client.get.call_args.args[0].endswith("q=Petrovaradin&strana=2")
    assert items[1].metadata["search_page_number"] == 2


def test_http_search_respects_robots():
    client = Mock()
    client.allowed.return_value = False
    with pytest.raises(ValueError, match="robots_disallowed"):
        list(HTTPInternalSearchProvider(client, {}).discover(make_site()))
    client.get.assert_not_called()


def test_http_search_reports_rate_limit_without_browser_fallback():
    client = Mock()
    client.allowed.return_value = True
    client.get.return_value = httpx.Response(
        429, request=httpx.Request("GET", "https://example.rs/search")
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(HTTPInternalSearchProvider(client, {}).discover(make_site()))
    assert client.get.call_count == 1


def test_http_search_missing_selector_is_not_a_success():
    client = Mock()
    client.allowed.return_value = True
    client.get.return_value = httpx.Response(
        200, text="<h1>Loading...</h1>", request=httpx.Request("GET", "https://example.rs/search")
    )
    with pytest.raises(ValueError, match="results_missing"):
        list(HTTPInternalSearchProvider(client, {}).discover(make_site()))


def test_cli_routes_http_only_for_configured_sites(monkeypatch):
    from argparse import Namespace
    from pathlib import Path

    from petrovaradin_archive import cli

    sites = [make_site(), {"id": "other", "internal_search": {"engine": "selenium"}}]
    db, client = Mock(), Mock()
    browser, http = Mock(), Mock()
    browser.name = "selenium_internal_search"
    http.name = "http_internal_search"
    browser.discover.return_value = []
    http.discover.return_value = []
    monkeypatch.setattr(cli, "context", lambda _: (Path("."), {}, sites, db, client))
    monkeypatch.setattr(cli, "choose_sites", lambda *args: sites)
    monkeypatch.setattr(cli, "SeleniumInternalSearchProvider", lambda _: browser)
    monkeypatch.setattr(cli, "HTTPInternalSearchProvider", lambda *args: http)
    cli.cmd_discover(Namespace(config_dir=None, provider=["selenium"], site=None, max_pages=None))
    http.discover.assert_called_once_with(sites[0])
    browser.discover.assert_called_once_with(sites[1])
