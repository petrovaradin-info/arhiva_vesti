import gzip
from argparse import Namespace
from unittest.mock import Mock

from petrovaradin_archive.article_adapters import extract_article
from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.models import DiscoveredURL
from petrovaradin_archive.source_hunter import SourceLink
from petrovaradin_archive import cli


def test_sources_citations_redirects_and_noise():
    article = extract_article('''<head><link rel="canonical" href="https://original.rs/a">
      <meta http-equiv="refresh" content="0; URL=https://new.rs/a"></head>
      <article><p>Извор: <a href="https://source.rs/a?utm_source=x">Portal</a></p>
      <a href="/other">Navod</a><blockquote cite="https://quote.rs/a">Citat</blockquote>
      <p>https://plain.rs/a</p><a href="javascript:alert(1)">bad</a>
      <a href="https://bad.rs:abc/a">bad port</a><a href="#part">self</a>
      <aside><a href="https://ad.rs">ad</a></aside></article>
      <nav><a href="https://menu.rs">menu</a></nav>''', 'https://news.rs/a')
    urls = {link.url for link in article.source_links}
    assert urls == {'https://original.rs/a', 'https://new.rs/a', 'https://source.rs/a',
                    'https://news.rs/other', 'https://quote.rs/a', 'https://plain.rs/a'}
    assert any(x.kind == 'source' for x in article.source_links)


def test_persistence_dedup_and_provenance(tmp_path):
    path = tmp_path / 'db.sqlite'
    db = ArchiveDB(path)
    db.add(DiscoveredURL('https://news.rs/a', 'news', 'test'))
    db.add(DiscoveredURL('https://news.rs/b', 'news', 'test'))
    ids = [r[0] for r in db.connection.execute('SELECT id FROM urls ORDER BY id')]
    links = [SourceLink('https://source.rs/a?utm_source=x', 'source')]
    assert db.store_source_links(ids[0], links) == 1
    assert db.store_source_links(ids[0], links) == 0
    assert db.store_source_links(ids[1], links) == 1
    db.close()
    db = ArchiveDB(path)
    assert len(db.source_link_rows(domain='source.rs')) == 2
    assert db.source_link_rows(domain='missing.rs') == []
    db.close()


def test_backfill_already_analyzed_archive(tmp_path, monkeypatch, capsys):
    db = ArchiveDB(tmp_path / 'db.sqlite')
    db.add(DiscoveredURL('https://news.rs/a', 'news', 'test'))
    archive = tmp_path / 'a.html.gz'
    archive.write_bytes(gzip.compress(b'<article><a href="https://source.rs/a">Source</a></article>'))
    db.connection.execute("UPDATE urls SET archive_path=?, article_text='already analyzed', final_url='https://news.rs/new'", (str(archive),))
    db.connection.commit()
    monkeypatch.setattr(cli, 'context', lambda _: (tmp_path, {}, [], db, Mock()))
    monkeypatch.setattr(db, 'close', lambda: None)
    args = Namespace(config_dir=None, list=False, after_id=0, site=None, limit=100)
    assert cli.cmd_hunt_sources(args) == 0
    assert len(db.source_link_rows()) == 2
    assert cli.cmd_hunt_sources(args) == 0
    assert len(db.source_link_rows()) == 2
    assert '"added": 0' in capsys.readouterr().out


def test_cli_parser():
    args = cli.build_parser().parse_args(['hunt-sources', '--list', '--domain', 'source.rs'])
    assert args.list and args.domain == 'source.rs'


def test_download_collects_sources_and_external_redirect(tmp_path):
    import httpx
    from petrovaradin_archive.downloader import Downloader
    db = ArchiveDB(tmp_path / 'db.sqlite')
    db.sync_sources([{'id': 'news', 'name': 'News', 'domains': ['news.rs']}])
    db.add(DiscoveredURL('https://news.rs/a', 'news', 'test'))
    client = Mock()
    client.allowed.return_value = True
    client.max_bytes = 1000000
    client.get.return_value = httpx.Response(200, text='<article>Petrovaradin <a href="https://source.rs/a">Izvor</a></article>',
        headers={'content-type': 'text/html'}, request=httpx.Request('GET', 'https://news.rs/a'))
    downloader = Downloader(db, client, tmp_path / 'html', ['Petrovaradin'])
    assert downloader.run(mode='http')['archived'] == 1
    assert db.source_link_rows()[0]['url'] == 'https://source.rs/a'
    db.add(DiscoveredURL('https://news.rs/b', 'news', 'test'))
    client.get.return_value = httpx.Response(200, text='Petrovaradin',
        headers={'content-type': 'text/html'}, request=httpx.Request('GET', 'https://redirect.rs/b'))
    assert downloader.run(mode='http')['bad_redirect'] == 1
    assert any(r['url'] == 'https://redirect.rs/b' for r in db.source_link_rows())
    db.reset_discovery()
    assert db.connection.execute('SELECT COUNT(*) FROM source_links').fetchone()[0] == 0
    db.close()
