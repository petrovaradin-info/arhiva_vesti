"""Run from project root using .venv/Scripts/python.exe scripts/resume_archive.py."""
import concurrent.futures
import datetime
import pathlib
import sqlite3
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
STAMP = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
LOG = ROOT / 'data' / ('archive-' + STAMP + '.log')


def run(*args):
    with LOG.with_name(LOG.stem + '-' + args[0] + '.log').open('a', encoding='utf-8') as out:
        out.write('\nCOMMAND: ' + ' '.join(args) + '\n')
        out.flush()
        return subprocess.call([sys.executable, '-u', '-m', 'petrovaradin_archive.cli', *args],
                               cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)


def pending():
    with sqlite3.connect('file:' + (ROOT / 'data/archive.sqlite3').as_posix() + '?mode=ro',
                         uri=True, timeout=60) as db:
        return db.execute("SELECT count(*) FROM urls WHERE download_status IN ('pending','retry')").fetchone()[0]


def main():
    print(str(LOG), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        discovery = pool.submit(run, 'discover', '--provider', 'sitemap', '--provider', 'selenium',
                                '--full-scan', '--workers', '6')
        while True:
            finished = discovery.done()
            if pending():
                if run('download', '--limit', '-1', '--mode', 'hybrid', '--workers', '6'):
                    raise SystemExit('Download failed; inspect ' + str(LOG))
            elif finished:
                break
            else:
                time.sleep(30)
        if discovery.result():
            raise SystemExit('Discovery failed; inspect ' + str(LOG))
    # Assets have independent retry state; each invocation takes a snapshot.
    for _ in range(3):
        if run('download-assets', '--limit', '-1'):
            raise SystemExit('Asset download failed; inspect ' + str(LOG))
    print('Archive run finished: ' + str(LOG), flush=True)


if __name__ == '__main__':
    from petrovaradin_archive.parallel_download import download_lock
    with download_lock(ROOT / 'data/archive.sqlite3', operation='archive-run'):
        main()
