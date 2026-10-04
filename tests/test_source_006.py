from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from test_selenium_search import Driver, Element

from petrovaradin_archive.cli import choose_sites
from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.models import DiscoveredURL
from petrovaradin_archive.providers.selenium_search import SeleniumInternalSearchProvider


@pytest.mark.parametrize('full_scan', [False, True])
def test_repeated_canonical_urls_stop_whole_site(tmp_path, monkeypatch, capsys, full_scan):
    db = ArchiveDB(tmp_path / 'archive.sqlite')
    db.add(DiscoveredURL('https://example.rs/a', 'test', 'test'))
    site = {'id': 'test', 'domains': ['example.rs'], 'internal_search': {
        'enabled': True, 'start_url': 'https://example.rs/?s=petrovaradin',
        'result_link_css': 'results', 'page_url_template': 'https://example.rs/page/{page}',
        'max_pages': 200,
    }}
    driver = Driver([
        {'results': [Element('https://example.rs/a/'), Element('https://example.rs/b')]},
        {'results': [Element('https://example.rs/b?utm_source=page2'),
                     Element('https://example.rs/a#section'), Element('https://example.rs/a')]},
        {'results': [Element('https://example.rs/c')]},
    ])
    provider = SeleniumInternalSearchProvider(
        {'selenium': {'timeout_seconds': 0, 'page_wait_seconds': 0}}, db, full_scan,
    )
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    items = list(provider.discover(site))
    assert [item.url for item in items] == ['https://example.rs/b']
    assert len(driver.visited) == 2  # No page 3 and no second script/query.
    assert driver.closed
    assert db.connection.execute('SELECT COUNT(*) FROM search_coverage').fetchone()[0] == 0
    log = capsys.readouterr().out
    assert 'status=repeated_page; strana=2' in log
    assert 'verovatno nepostojeća stranica' in log
    assert 'URL=https://example.rs/page/2' in log
    db.close()


def test_partial_overlap_does_not_stop(monkeypatch):
    site = {'id': 'test', 'domains': ['example.rs'], 'internal_search': {
        'enabled': True, 'start_url': 'https://example.rs/search',
        'result_link_css': 'results', 'page_url_template': 'https://example.rs/page/{page}',
        'max_pages': 3,
    }}
    driver = Driver([
        {'results': [Element('https://example.rs/a')]},
        {'results': [Element('https://example.rs/a'), Element('https://example.rs/b')]},
        {'results': [Element('https://example.rs/c')]},
    ])
    provider = SeleniumInternalSearchProvider({'selenium': {'timeout_seconds': 0, 'page_wait_seconds': 0}})
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 3
    assert len(driver.visited) == 3


@pytest.mark.parametrize('selected', [None, ['republika', 'b', 'a']])
def test_republika_runs_last_even_when_listed_first(selected):
    sites = [{'id': 'republika', 'run_last': True}, {'id': 'a'}, {'id': 'b'}]
    db = Mock()
    db.source_rows.return_value = [dict(s, enabled=True, manual_review=False, blocklist_json='[]') for s in sites]
    assert [s['id'] for s in choose_sites(sites, selected, db)] == ['a', 'b', 'republika']


def test_republika_configuration_is_last_and_targets_search_only():
    sites = yaml.safe_load((Path(__file__).parents[1] / 'config/sites.yaml').read_text(encoding='utf-8'))['sites']
    assert sites[-1]['id'] == 'republika'
    assert sites[-1]['run_last'] is True
    assert sites[-1]['internal_search']['result_link_css'] == '.search-page .news-list .news-item-title a[href]'
