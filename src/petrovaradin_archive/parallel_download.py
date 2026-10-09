from __future__ import annotations

import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from .config import env
from .database import ArchiveDB
from .downloader import Downloader
from .http import PoliteClient
from .logging_utils import log


@contextmanager
def download_lock(database: Path, operation: str = "download"):
    """OS-owned lock: released even if the process crashes; never delete the file."""
    lock_path = database.with_suffix(database.suffix + f'.{operation}.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SystemExit(f'{operation.capitalize()} već radi nad ovom bazom. Koristi --workers u jednom pokretanju.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _path(root, value):
    return root / Path(value)


def _download_site(root, settings, site_id, rows, mode, stop_event):
    if stop_event.is_set():
        return {}
    # Each thread owns its SQLite connection, HTTP client and browser.
    db = ArchiveDB(_path(root, settings['database']))
    client = None
    downloader = None
    try:
        client = PoliteClient(settings, env('ARCHIVE_USER_AGENT', 'PetrovaradinInfoArchive/0.1') or '')
        downloader = Downloader(
            db, client, _path(root, settings['html_dir']), settings.get('keywords', []),
            int(settings.get('request', {}).get('max_retries', 3)),
            settings.get('selenium', {}),
            _path(root, settings.get('file_dir', 'data/files')),
            _path(root, settings.get('text_dir', 'data/text')),
            settings.get('archive', {}), settings.get('candidate_keywords', []),
        )
        return downloader.run(len(rows), site_id, mode, rows=rows, stop_event=stop_event)
    finally:
        try:
            if downloader is not None:
                downloader.close()
        finally:
            db.close()
            if client is not None:
                client.close()


def run_download(root, settings, db, limit, site_id, mode, workers):
    if workers < 1:
        raise ValueError('workers must be positive')
    rows = [dict(row) for row in db.pending(limit, site_id)]
    priorities = {row['id']: bool(row['manual_review']) for row in db.source_rows()}
    counts = Counter()
    log(f'Download: ukupno={len(rows)}; workers={workers}; manual_review izvori poslednji', flush=True)
    # A barrier between tiers prevents review sites starting while normal sites run.
    stop_event = Event()
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        try:
            for review in (False, True):
                groups = {}
                for row in rows:
                    if priorities.get(row['site_id'], bool(row['requires_manual_review'])) == review:
                        groups.setdefault(row['site_id'], []).append(row)
                futures = [executor.submit(_download_site, root, settings, source, batch, mode, stop_event)
                           for source, batch in groups.items()]
                for future in as_completed(futures):
                    counts.update(future.result())
        except BaseException:
            stop_event.set()
            for future in futures:
                future.cancel()
            raise
    return dict(counts)
