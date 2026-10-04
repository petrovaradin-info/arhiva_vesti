from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from selenium.common.exceptions import (
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)

from petrovaradin_archive.providers.selenium_search import SeleniumInternalSearchProvider


class Element:
    def __init__(self, href, text="Petrovaradin", click=None):
        self.href, self.text, self.callback = href, text, click

    def get_attribute(self, name):
        return self.href if name == "href" else ""

    def find_element(self, *args):
        return self

    def is_displayed(self):
        return True

    def click(self):
        self.callback()


class Driver:
    title = "Pretraga"

    def __init__(self, pages):
        self.pages, self.index, self.visited = pages, 0, []
        self.current_url = "https://example.rs/?s=Petrovaradin"
        self.closed = False

    def get(self, url):
        self.current_url = url
        self.visited.append(url)
        self.index = len(self.visited) - 1

    def find_elements(self, by, selector):
        return self.pages[min(self.index, len(self.pages) - 1)].get(selector, [])

    def quit(self):
        self.closed = True

    def execute_script(self, script, element):
        if 'click()' in script:
            element.callback()


@pytest.fixture
def provider():
    return SeleniumInternalSearchProvider({"selenium": {"timeout_seconds": 0, "page_wait_seconds": 0}})


@pytest.fixture
def site():
    return {"id": "test", "domains": ["example.rs"], "internal_search": {
        "enabled": True, "start_url": "https://example.rs/?s=Petrovaradin",
        "search_both_scripts": False, "result_link_css": "results", "next_css": "next",
        "max_pages": 5,
    }}


def test_fresh_profile_retry_keeps_original_and_cleans_temp(provider, site, monkeypatch, capsys):
    site['internal_search'].update(user_data_dir="existing", retry_with_fresh_profile=True)
    driver = Driver([{"results": [Element("https://example.rs/article")]}])
    configs = []

    def create(config):
        configs.append(dict(config))
        if len(configs) == 1:
            raise SessionNotCreatedException("DevToolsActivePort")
        assert Path(config['user_data_dir']).is_dir()
        return driver

    monkeypatch.setattr(provider, "_driver", create)
    assert len(list(provider.discover(site))) == 1
    assert configs[0]['user_data_dir'] == 'existing'
    assert not Path(configs[1]['user_data_dir']).exists()
    assert driver.closed
    assert 'nijedan URL nije otvoren' in capsys.readouterr().out


def test_absolute_profile_resolved_from_project(provider, tmp_path, monkeypatch):
    provider.root = tmp_path
    driver = Mock()
    chrome = Mock(return_value=driver)
    monkeypatch.setattr('petrovaradin_archive.providers.selenium_search.webdriver.Chrome', chrome)
    provider._driver({'user_data_dir': 'data/profile'})
    assert f'--user-data-dir={(tmp_path / "data/profile").resolve()}' in chrome.call_args.kwargs['options'].arguments


def test_three_pages_and_candidate_skip_reasons(provider, site, monkeypatch, capsys):
    site['blocklist'] = ['https://example.rs/skip']
    driver = Driver([
        {'results': [Element('https://example.rs/one'), Element('https://example.rs/skip'),
                     Element('https://other.rs/news')], 'next': [Element('/page/2')]},
        {'results': [Element('https://example.rs/one'), Element('https://example.rs/two')],
         'next': [Element('/page/3')]},
        {'results': [Element('https://example.rs/three')]},
    ])
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert [i.url for i in provider.discover(site)] == [f'https://example.rs/{p}' for p in ['one', 'two', 'three']]
    assert driver.visited[-1] == 'https://example.rs/page/3'
    logs = capsys.readouterr().out
    for status in ['blocklist', 'outside_domain', 'duplicate', 'accepted', 'no_next_link']:
        assert f'status={status}' in logs


def test_repeated_page_stops_even_when_order_changes(provider, site, monkeypatch, capsys):
    a, b = Element('https://example.rs/a'), Element('https://example.rs/b')
    driver = Driver([{'results': [a, b], 'next': [Element('/page/2')]}, {'results': [b, a]}])
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 2
    assert 'status=repeated_page' in capsys.readouterr().out


def test_gsc_clicks_actual_cursor_and_does_not_invent_page(provider, site, monkeypatch):
    site['internal_search']['pagination_mode'] = 'gsc'
    driver = Driver([{'results': [Element('https://example.rs/a')]},
                     {'results': [Element('https://example.rs/b')]}])
    driver.pages[0]['.gsc-cursor-page'] = [Element(None, '2', lambda: setattr(driver, 'index', 1))]
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 2
    assert len(driver.visited) == 1


@pytest.mark.parametrize('title,status', [('Just a moment...', 'challenge'), ('Pretraga', 'results_timeout')])
def test_missing_results_not_reported_as_empty(provider, site, monkeypatch, title, status):
    driver = Driver([{}])
    driver.title = title
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    with pytest.raises(TimeoutException, match=f'status={status}'):
        list(provider.discover(site))
    assert driver.closed


def test_navigation_failure_logs_attempted_url(provider, site, monkeypatch, capsys):
    driver = Driver([{}])
    driver.get = Mock(side_effect=WebDriverException('failed'))
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    with pytest.raises(WebDriverException):
        list(provider.discover(site))
    assert 'status=navigation_error URL=https://example.rs/?s=Petrovaradin' in capsys.readouterr().out


def test_limit_does_not_navigate_extra_page(provider, site, monkeypatch):
    site['internal_search']['max_pages'] = 1
    driver = Driver([{'results': [Element('https://example.rs/a')], 'next': [Element('/page/2')]}])
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 1
    assert len(driver.visited) == 1


def test_script_variants_with_identical_first_page_still_paginate(provider, site, monkeypatch):
    site['internal_search']['search_both_scripts'] = True
    a = Element('https://example.rs/a')
    driver = Driver([{'results': [a]}, {'results': [a], 'next': [Element('/page/2')]},
                     {'results': [Element('https://example.rs/b')]}])
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 2
    assert '%D0%9F' in driver.visited[1]


def test_source_configuration():
    sites = yaml.safe_load((Path(__file__).parents[1] / 'config/sites.yaml').read_text(encoding='utf-8'))['sites']
    sites = {s['id']: s for s in sites}
    assert sites['masina']['internal_search']['start_url'] == 'https://www.masina.rs/?s=petrovaradin'
    assert sites['021']['google_supplement']
    assert sites['buka_6yka']['internal_search']['retry_with_fresh_profile']


def test_bootstrap_before_search(provider, site, monkeypatch):
    site['internal_search'].update(bootstrap_url='https://example.rs/', bootstrap_wait_seconds=0)
    driver = Driver([{}, {'results': [Element('https://example.rs/a')]}])
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    assert len(list(provider.discover(site))) == 1
    assert driver.visited == ['https://example.rs/', site['internal_search']['start_url']]


def test_rate_limit_is_not_empty_result(provider, site, monkeypatch):
    driver = Driver([{}])
    driver.title = 'Too Many Requests'
    monkeypatch.setattr(provider, '_driver', lambda _: driver)
    with pytest.raises(WebDriverException, match='status=rate_limited'):
        list(provider.discover(site))
    assert driver.closed


@pytest.mark.parametrize('providers,expected', [(['selenium'], 1), (['selenium', 'google'], 1)])
def test_google_supplement_runs_with_existing_internal_results(monkeypatch, providers, expected):
    from argparse import Namespace

    from petrovaradin_archive import cli
    from petrovaradin_archive.models import DiscoveredURL

    db, client = Mock(), Mock()
    item = DiscoveredURL('https://021.rs/article', '021', 'selenium_internal_search')
    site = {'id': '021', 'domains': ['021.rs'], 'google_supplement': True}
    monkeypatch.setattr(cli, 'context', lambda _: (Path('.'), {}, [site], db, client))
    monkeypatch.setattr(cli, 'choose_sites', lambda *args: [site])
    selenium = Mock(name='selenium_provider')
    selenium.name = 'selenium_internal_search'
    selenium.discover.return_value = iter([item])
    google = Mock()
    google.name = 'google_site_search'
    google.discover.return_value = iter([])
    monkeypatch.setattr(cli, 'SeleniumInternalSearchProvider', lambda *args: selenium)
    monkeypatch.setattr(cli, 'GoogleSiteSearchProvider', lambda _: google)
    db.add.return_value = True
    cli.cmd_discover(Namespace(config_dir=None, provider=providers, site=None, max_pages=None))
    assert google.discover.call_count == expected
