from unittest.mock import Mock

import httpx
import pytest
from selenium.common.exceptions import ElementClickInterceptedException
from test_selenium_search import Driver, Element

from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.models import DiscoveredURL
from petrovaradin_archive.providers.search_progress import SearchProgress
from petrovaradin_archive.providers.selenium_search import SeleniumInternalSearchProvider
from petrovaradin_archive.providers.sitemap_content import SitemapContentProvider
from petrovaradin_archive.providers.wordpress_search import WordPressSearchProvider

SITE = {'id': 'test', 'domains': ['example.rs']}
SETTINGS = {'keywords': ['Petrovaradin'], 'selenium': {'timeout_seconds': 0, 'page_wait_seconds': 0}}


def add(db, slug):
    db.add(DiscoveredURL('https://example.rs/' + slug, 'test', 'test'))


def test_coverage_survives_reopen_and_requires_same_query_and_config(tmp_path):
    path = tmp_path / 'db.sqlite'
    db = ArchiveDB(path)
    add(db, 'a')
    cfg = {'newest_first': True}
    first = SearchProgress(db, SITE, 'test', 'q', cfg)
    assert not first.should_stop(['https://example.rs/a'])
    assert not first.should_stop(['https://example.rs/a'])
    first.complete()
    db.close()
    db = ArchiveDB(path)
    again = SearchProgress(db, SITE, 'test', 'q', cfg)
    assert not again.should_stop(['https://example.rs/a/?utm_source=x'])
    assert again.should_stop(['https://example.rs/a'])
    assert not SearchProgress(db, SITE, 'test', 'other', cfg).incremental
    assert not SearchProgress(db, SITE, 'test', 'q', dict(cfg, result_link_css='changed')).incremental
    assert not SearchProgress(db, SITE, 'test', 'q', cfg, full_scan=True).incremental
    assert not SearchProgress(db, SITE, 'test', 'q', dict(cfg, pagination_mode='gsc')).incremental
    db.close()


def test_new_and_empty_pages_reset_streak_and_current_run_is_not_history(tmp_path):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    add(db, 'a')
    progress = SearchProgress(db, SITE, 'test', 'q', {'newest_first': True})
    progress.complete()
    progress = SearchProgress(db, SITE, 'test', 'q', {'newest_first': True})
    add(db, 'b')
    for urls in [['https://example.rs/a'], [], ['https://example.rs/a'],
                 ['https://example.rs/b'], ['https://example.rs/a']]:
        assert not progress.should_stop(urls)
    assert progress.should_stop(['https://example.rs/a'])
    db.close()


def test_selenium_stops_at_first_all_duplicate_page(tmp_path, monkeypatch):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    site = dict(SITE, internal_search={
        'enabled': True, 'start_url': 'https://example.rs/?s=Petrovaradin',
        'search_both_scripts': False, 'result_link_css': 'results', 'next_css': 'next',
        'max_pages': 10, 'newest_first': True,
    })
    pages = [{'results': [Element('https://example.rs/' + slug)],
              **({'next': [Element('/page/' + str(i + 2))]} if i < 2 else {})}
             for i, slug in enumerate(['a', 'b', 'c'])]
    for expected_pages, full in [(3, False), (1, False), (1, True)]:
        provider = SeleniumInternalSearchProvider(SETTINGS, db, full)
        driver = Driver(pages)
        monkeypatch.setattr(provider, '_driver', lambda _, current=driver: current)
        for item in provider.discover(site):
            db.add(item)
        assert len(driver.visited) == expected_pages
    assert len(db.pending(100)) == 3  # Known but pending articles remain downloadable.
    db.close()


def test_page_limit_does_not_create_completed_coverage(tmp_path, monkeypatch):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    site = dict(SITE, internal_search={
        'enabled': True, 'start_url': 'https://example.rs/?s=Petrovaradin',
        'result_link_css': 'results', 'max_pages': 1, 'newest_first': True,
    })
    provider = SeleniumInternalSearchProvider(SETTINGS, db)
    monkeypatch.setattr(provider, '_driver', lambda _: Driver([{'results': [Element('https://example.rs/a')]}]))
    list(provider.discover(site))
    assert db.connection.execute('SELECT COUNT(*) FROM search_coverage').fetchone()[0] == 0
    db.close()


def test_gsc_intercepted_click_loads_second_page(monkeypatch):
    provider = SeleniumInternalSearchProvider(SETTINGS)
    site = dict(SITE, internal_search={
        'enabled': True, 'start_url': 'https://example.rs/search',
        'result_link_css': 'results', 'pagination_mode': 'gsc', 'max_pages': 3,
    })
    driver = Driver([{'results': [Element('https://example.rs/a')]},
                     {'results': [Element('https://example.rs/b')]}])
    cursor = Element(None, '2', lambda: setattr(driver, 'index', 1))
    cursor.click = Mock(side_effect=ElementClickInterceptedException('overlay'))
    driver.pages[0]['.gsc-cursor-page'] = [cursor]
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 2


def test_scan_never_opens_known_article(tmp_path):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    add(db, 'known')
    client = Mock()
    url = 'https://example.rs/sitemap.xml'
    client.get.return_value = httpx.Response(200, text='<urlset><url><loc>https://example.rs/known/?utm_source=x</loc></url></urlset>', request=httpx.Request('GET', url))
    assert list(SitemapContentProvider(client, db, ['Petrovaradin'], {}).discover(dict(SITE, sitemaps=[url]))) == []
    client.get.assert_called_once_with(url)
    db.close()


def test_wordpress_pages_existing_filter_and_completion(tmp_path):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    add(db, 'a')
    client = Mock()
    client.allowed.return_value = True
    payloads = [[{'link': 'https://example.rs/a'}], [{'link': 'https://example.rs/b'}]]
    client.get.side_effect = lambda url: httpx.Response(200, json=payloads.pop(0), headers={'X-WP-TotalPages': '2'}, request=httpx.Request('GET', url))
    site = dict(SITE, wordpress_search={'endpoint': 'https://example.rs/wp-json/wp/v2/posts'})
    items = list(WordPressSearchProvider(client, SETTINGS, db).discover(site))
    assert items == []
    assert client.get.call_count == 1
    assert 'page=1' in client.get.call_args.args[0]
    assert 'orderby=date&order=desc' in client.get.call_args.args[0]
    assert db.connection.execute('SELECT COUNT(*) FROM search_coverage').fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize('status,body', [(403, 'challenge'), (429, 'rate limit'), (200, '<html>Just a moment</html>')])
def test_wordpress_blocked_is_not_empty_or_completed(tmp_path, status, body):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    client = Mock()
    client.allowed.return_value = True
    client.get.side_effect = lambda url: httpx.Response(status, text=body, request=httpx.Request('GET', url))
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        list(WordPressSearchProvider(client, SETTINGS, db).discover(dict(SITE, wordpress_search={'endpoint': 'https://example.rs/wp-json/wp/v2/posts'})))
    assert db.connection.execute('SELECT COUNT(*) FROM search_coverage').fetchone()[0] == 0
    assert client.get.call_count == 1
    db.close()
