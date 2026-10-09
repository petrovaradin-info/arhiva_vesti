from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from threading import Event

from .logging_utils import log
from .parallel_download import download_lock


def dispatch_sites(sites, workers, run_site, stop_event):
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        try:
            seen = set()
            for review in (False, True):
                futures = []
                for site in sites:
                    if bool(site.get('manual_review')) != review or site['id'] in seen:
                        continue
                    seen.add(site['id'])
                    futures.append(executor.submit(run_site, site['id']))
                for future in as_completed(futures):
                    future.result()
        except BaseException:
            stop_event.set()
            for future in futures:
                future.cancel()
            raise


def run_discovery(args):
    from .cli import context, choose_sites, resolve_path, _cmd_discover_serial
    root, settings, sites, db, client = context(args.config_dir)
    try:
        selected = choose_sites(sites, args.site, db)
        database = resolve_path(root, settings['database'])
    finally:
        db.close()
        client.close()
    stop_event = Event()
    def run_site(site_id):
        if stop_event.is_set():
            return
        local = copy(args)
        local.site = [site_id]
        local.stop_event = stop_event
        return _cmd_discover_serial(local)
    with download_lock(database, operation='discover'):
        log(f'Discovery: izvora={len(selected)}; workers={args.workers}; manual_review poslednji', flush=True)
        dispatch_sites(selected, args.workers, run_site, stop_event)
        log('Discovery svih izabranih izvora završen.', flush=True)
    return 0
