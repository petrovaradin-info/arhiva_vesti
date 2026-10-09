from threading import Barrier, Event
import pytest
from petrovaradin_archive.parallel_discover import dispatch_sites
from petrovaradin_archive.parallel_download import download_lock
from petrovaradin_archive.cli import build_parser


def test_parallel_unique_and_review_last():
    barrier = Barrier(2)
    completed = []
    def run(site):
        if site != 'review':
            barrier.wait(timeout=5)
        else:
            assert set(completed) == {'one', 'two'}
        completed.append(site)
    dispatch_sites([{'id': 'review', 'manual_review': True}, {'id': 'one'},
                    {'id': 'two'}, {'id': 'one'}], 2, run, Event())
    assert len(completed) == 3


def test_discovery_lock_exclusive_but_download_independent(tmp_path):
    path = tmp_path / 'archive.db'
    with download_lock(path, operation='discover'):
        with pytest.raises(SystemExit, match='Discover'):
            with download_lock(path, operation='discover'):
                pass
        with download_lock(path):
            pass
    with download_lock(path, operation='discover'):
        pass


def test_discovery_workers():
    assert build_parser().parse_args(['discover', '--provider', 'selenium', '--workers', '3']).workers == 3
    with pytest.raises(SystemExit):
        build_parser().parse_args(['discover', '--provider', 'selenium', '--workers', '0'])
