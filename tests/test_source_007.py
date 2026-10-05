import re
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from test_selenium_search import Driver, Element

from petrovaradin_archive import cli, logging_utils
from petrovaradin_archive.providers.google_site_search import GoogleSiteSearchProvider
from petrovaradin_archive.providers.http_search import HTTPInternalSearchProvider
from petrovaradin_archive.providers.selenium_search import SeleniumInternalSearchProvider


def test_timestamp_on_every_line_and_elapsed(capsys, monkeypatch):
    logging_utils.log('prva\ndruga')
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(re.match(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}\]', line) for line in lines)
    monkeypatch.setattr(logging_utils, 'perf_counter', lambda: 12.345)
    assert logging_utils.elapsed(10) == 'trajanje=2.345s'


@pytest.mark.parametrize('statuses,stop', [([], False), (['accepted', 'duplicate'], False),
                                         (['existing'], True), (['duplicate', 'existing'], True)])
def test_duplicate_page_rule(statuses, stop):
    provider = SeleniumInternalSearchProvider({})
    assert provider._stop_duplicates({'id': 'test'}, statuses, 1, 'https://example.rs') == stop
    assert provider.stop_site == stop


def test_cli_moves_to_next_site_without_google_or_other_providers(monkeypatch, capsys):
    sites = [{'id': 'first', 'google_supplement': True}, {'id': 'second'}]
    db, client = Mock(), Mock()
    provider = Mock(name='provider')
    provider.name = 'selenium_internal_search'
    provider.stop_site = True
    provider.discover.side_effect = lambda site: iter([])
    google = Mock()
    google.name = 'google_site_search'
    monkeypatch.setattr(cli, 'context', lambda _: (Path('.'), {}, sites, db, client))
    monkeypatch.setattr(cli, 'choose_sites', lambda *args: sites)
    monkeypatch.setattr(cli, 'SeleniumInternalSearchProvider', lambda *args: provider)
    monkeypatch.setattr(cli, 'GoogleSiteSearchProvider', lambda *args: google)
    cli.cmd_discover(Namespace(config_dir=None, provider=['selenium', 'google'], site=None, max_pages=None))
    assert [call.args[0]['id'] for call in provider.discover.call_args_list] == ['first', 'second']
    google.discover.assert_not_called()
    assert 'trajanje=' in capsys.readouterr().out


def test_google_existing_page_stops_before_next_page(monkeypatch):
    url = 'https://example.rs/petrovaradin'
    driver = Driver([{'a[href]': [Element(url)]}])
    provider = GoogleSiteSearchProvider({'google_search': {'page_wait_seconds': 0}})
    provider.known_urls = {url}
    monkeypatch.setattr(provider, '_driver', lambda: driver)
    assert list(provider.discover({'id': 'test', 'domains': ['example.rs']})) == []
    assert provider.stop_site and driver.closed
    assert len(driver.visited) == 1


def test_http_existing_page_stops_before_next_page():
    url = 'https://example.rs/petrovaradin'
    client, db = Mock(), Mock()
    db.known_urls.return_value = {url}
    client.allowed.return_value = True
    client.get.return_value = httpx.Response(200, text=f'<article><a href="{url}">Petrovaradin</a></article><a rel="next" href="/page/2">next</a>', request=httpx.Request('GET', 'https://example.rs/search'))
    provider = HTTPInternalSearchProvider(client, {}, db)
    site = {'id': 'test', 'domains': ['example.rs'], 'internal_search': {
        'enabled': True, 'start_url': 'https://example.rs/search', 'result_link_css': 'article a',
    }}
    assert list(provider.discover(site)) == []
    assert provider.stop_site
    assert client.get.call_count == 1
