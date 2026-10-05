from threading import Barrier, Lock

import pytest

from petrovaradin_archive.cli import choose_sites, build_parser
from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.models import DiscoveredURL
from petrovaradin_archive import parallel_download as parallel


def populate(tmp_path):
    db = ArchiveDB(tmp_path / 'archive.db')
    sites = [{'id': 'a-review', 'name': 'Review', 'manual_review': True},
             {'id': 'b-normal', 'name': 'Normal'},
             {'id': 'c-normal', 'name': 'Normal 2', 'run_last': True}]
    db.sync_sources(sites)
    for site in sites:
        db.add(DiscoveredURL(f"https://{site['id']}.rs/vest", site['id'], 'test'))
    return db, sites


def test_review_last_before_limit_and_source_override(tmp_path):
    db, sites = populate(tmp_path)
    assert [s['id'] for s in choose_sites(sites, None, db)] == ['b-normal', 'c-normal', 'a-review']
    for result in (db.source_rows(), db.coverage_rows()):
        assert result[-1]['id'] == 'a-review'
    assert db.pending(1)[0]['site_id'] == 'b-normal'
    assert db.pending(1, 'a-review')[0]['site_id'] == 'a-review'
    assert db.list_urls(None, 1)[0]['site_id'] == 'b-normal'
    assert db.list_urls('pending', 1)[0]['site_id'] == 'b-normal'
    assert db.stats()[-1]['site_id'] == 'a-review'
    for row in db.pending(10):
        db.add_asset(row['id'], row['url'] + '.pdf', 'pdf')
    assert db.pending_assets(1)[0]['site_id'] == 'b-normal'
    assert db.asset_rows(limit=10)[-1]['site_id'] == 'a-review'
    db.set_source_manual_review('b-normal', True)
    assert db.pending(1)[0]['site_id'] == 'c-normal'
    db.close()


def test_parallel_overlap_unique_rows_and_review_barrier(tmp_path, monkeypatch):
    db, sites = populate(tmp_path)
    barrier = Barrier(2)
    completed = set()
    seen = []
    lock = Lock()
    def worker(root, settings, source, rows, mode, stop_event):
        if source != 'a-review':
            barrier.wait(timeout=5)  # Fails if normal sources are serial.
        else:
            assert completed == {'b-normal', 'c-normal'}
        with lock:
            completed.add(source)
            seen.extend(row['id'] for row in rows)
        return {'archived': len(rows)}
    monkeypatch.setattr(parallel, '_download_site', worker)
    assert parallel.run_download(tmp_path, {}, db, 3, None, 'selenium', 2) == {'archived': 3}
    assert len(seen) == len(set(seen)) == 3
    db.close()


def test_lock_released_after_failure(tmp_path):
    path = tmp_path / 'archive.db'
    with pytest.raises(RuntimeError):
        with parallel.download_lock(path):
            with pytest.raises(SystemExit, match='Download već radi'):
                with parallel.download_lock(path):
                    pytest.fail('lock acquired twice')
            raise RuntimeError('worker failed')
    with parallel.download_lock(path):
        pass


def test_workers_validation():
    assert build_parser().parse_args(['download', '--workers', '3']).workers == 3
    with pytest.raises(SystemExit):
        build_parser().parse_args(['download', '--workers', '0'])


def test_real_worker_uses_own_db_and_processes_selected_rows(tmp_path, monkeypatch):
    import httpx
    from petrovaradin_archive.http import PoliteClient
    db, _ = populate(tmp_path)
    monkeypatch.setattr(PoliteClient, 'allowed', lambda self, url: True)
    monkeypatch.setattr(PoliteClient, 'get', lambda self, url, **kwargs: httpx.Response(
        200, text='<html><body>Petrovaradin vest</body></html>',
        headers={'content-type': 'text/html'}, request=httpx.Request('GET', url)))
    settings = {'database': 'archive.db', 'html_dir': 'html', 'keywords': ['Petrovaradin']}
    result = parallel.run_download(tmp_path, settings, db, 3, None, 'http', 2)
    assert result['archived'] == 3
    assert [r['download_status'] for r in db.list_urls(None, 3)] == ['archived', 'archived', 'awaiting_review']
    db.close()

