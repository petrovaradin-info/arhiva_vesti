from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from .config import env, load_config
from .database import ArchiveDB
from .downloader import Downloader
from .http import PoliteClient
from .importer import import_urls
from .providers import SitemapProvider
from .providers.selenium_search import SeleniumInternalSearchProvider
from .providers.google_site_search import GoogleSiteSearchProvider
from .providers.sitemap_content import SitemapContentProvider
from .providers.duckduckgo_site_search import DuckDuckGoSiteSearchProvider
from .article_adapters import extract_article


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def context(config_dir: Path | None = None):
    root = project_root()
    settings, sites = load_config(config_dir or root / "config")
    db = ArchiveDB(resolve_path(root, settings["database"]))
    db.sync_sources(sites)
    user_agent = env("ARCHIVE_USER_AGENT", "PetrovaradinInfoArchive/0.1") or ""
    client = PoliteClient(settings, user_agent)
    return root, settings, sites, db, client


def choose_sites(sites: list[dict], site_ids: list[str] | None, db: ArchiveDB) -> list[dict]:
    source_rows = {row["id"]: row for row in db.source_rows()}
    enabled = []
    for site in sites:
        source = source_rows.get(site["id"])
        if source is None or not source["enabled"]:
            continue
        merged = dict(site)
        if source:
            merged["manual_review"] = bool(source["manual_review"])
            merged["blocklist"] = json.loads(source["blocklist_json"] or "[]")
        enabled.append(merged)
    if not site_ids:
        return enabled
    selected = [site for site in enabled if site["id"] in site_ids]
    missing = set(site_ids) - {site["id"] for site in selected}
    if missing:
        raise SystemExit(f"Nepoznati ili isključeni sajtovi: {', '.join(sorted(missing))}")
    return selected


def cmd_init(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    resolve_path(root, settings["html_dir"]).mkdir(parents=True, exist_ok=True)
    print(f"Baza spremna: {resolve_path(root, settings['database'])}")
    print(f"Konfigurisano sajtova: {len(sites)}")
    db.close()
    client.close()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    keywords = settings.get("keywords", ["Petrovaradin"])
    providers = []
    if "sitemap" in args.provider:
        providers.append(SitemapProvider(
            client, keywords,
            int(settings.get("request", {}).get("max_sitemaps_per_site", 100)),
        ))
    if "selenium" in args.provider:
        providers.append(SeleniumInternalSearchProvider(settings))
    if "google" in args.provider:
        providers.append(GoogleSiteSearchProvider(settings))
    if "scan" in args.provider:
        providers.append(SitemapContentProvider(client, db, keywords, settings))
    if "ddg" in args.provider:
        providers.append(DuckDuckGoSiteSearchProvider(client, keywords))
    added = seen = 0
    try:
        for site in choose_sites(sites, args.site, db):
            if args.max_pages:
                site = dict(site)
                site["internal_search"] = dict(site.get("internal_search", {}))
                site["internal_search"]["max_pages"] = args.max_pages
                site["google_max_pages"] = args.max_pages
            for provider in providers:
                print(f"[{site['id']}] discovery: {provider.name}", flush=True)
                provider_seen = 0
                try:
                    for item in provider.discover(site):
                        provider_seen += 1
                        seen += 1
                        added += int(db.add(item))
                except Exception as exc:
                    print(f"[{site['id']}] {provider.name} greška: {exc}", file=sys.stderr)
                if (provider.name == "selenium_internal_search"
                        and provider_seen == 0 and site.get("google_fallback")
                        and "google" not in args.provider):
                    fallback = GoogleSiteSearchProvider(settings)
                    print(f"[{site['id']}] interna pretraga je prazna; fallback: google_site_search")
                    try:
                        for item in fallback.discover(site):
                            seen += 1
                            added += int(db.add(item))
                    except Exception as exc:
                        print(f"[{site['id']}] Google fallback nije uspeo: {exc}", file=sys.stderr)
    finally:
        db.close()
        client.close()
    print(f"Pronađeno: {seen}; novo u bazi: {added}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    downloader = Downloader(
        db, client, resolve_path(root, settings["html_dir"]), settings.get("keywords", []),
        int(settings.get("request", {}).get("max_retries", 3)),
        settings.get("selenium", {}),
        resolve_path(root, settings.get("file_dir", "data/files")),
        resolve_path(root, settings.get("text_dir", "data/text")),
        settings.get("archive", {}),
    )
    try:
        counts = downloader.run(args.limit, args.site, args.mode)
    finally:
        downloader.close()
        db.close()
        client.close()
    print("; ".join(f"{name}={count}" for name, count in counts.items()))
    return 0


def cmd_download_assets(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    downloader = Downloader(
        db, client, resolve_path(root, settings["html_dir"]), settings.get("keywords", []),
        int(settings.get("request", {}).get("max_retries", 3)), settings.get("selenium", {}),
        resolve_path(root, settings.get("file_dir", "data/files")),
        resolve_path(root, settings.get("text_dir", "data/text")), settings.get("archive", {}),
    )
    try:
        counts = downloader.run_assets(args.limit, args.site, args.kind)
    finally:
        downloader.close()
        db.close()
        client.close()
    print("; ".join(f"{name}={count}" for name, count in counts.items()))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    analyzed = duplicates = failed = 0
    try:
        for row in db.unanalyzed_html_rows(args.limit, args.site):
            try:
                path = Path(row["archive_path"])
                if path.suffix == ".gz":
                    with gzip.open(path, "rb") as handle:
                        content = handle.read()
                else:
                    content = path.read_bytes()
                config = json.loads(row["config_json"] or "{}")
                article = extract_article(
                    content, row["final_url"] or row["url"], config.get("adapter", "generic")
                )
                group_id = db.store_article_analysis(
                    row["id"], title=article.title, body_text=article.body_text,
                    published_at=article.published_at,
                    canonical_url=article.canonical_url,
                    original_source_url=article.original_source_url,
                    adapter_name=article.adapter_name,
                )
                analyzed += 1
                duplicates += int(group_id != row["id"])
            except Exception as exc:
                failed += 1
                print(f"#{row['id']} analiza nije uspela: {exc}", file=sys.stderr)
    finally:
        db.close()
        client.close()
    print(f"analizirano={analyzed}; povezano_kao_kopija={duplicates}; neuspešno={failed}")
    return 0


def cmd_duplicates(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.duplicate_groups(args.limit)
    finally:
        db.close()
        client.close()
    for row in rows:
        print(f"grupa #{row['id']}: {row['copies']} primeraka [{row['sites']}]")
        print(f"  {row['article_title'] or ''}")
        print(f"  {row['url']}")
    print(f"Grupa duplikata: {len(rows)}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        count = import_urls(args.path, db, sites, args.source)
    finally:
        db.close()
        client.close()
    print(f"Uvezeno novih URL-ova: {count}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.stats()
        counts = db.counts()
        asset_stats = db.asset_stats()
    finally:
        db.close()
        client.close()
    if args.json:
        print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['site_id']:16} {row['download_status']:12} {row['count']:8}")
        print(
            f"UKUPNO: urls={counts['urls']}; discoveries={counts['discoveries']}; "
            f"sources={counts['sources']}; bad_urls={counts['bad_urls']}; "
            f"assets={counts['assets']}; assets_archived={counts['assets_archived']}; "
            f"scan_checks={counts['scan_checks']}"
        )
        for row in asset_stats:
            print(
                f"  ASSETS {row['kind']:10} {row['status']:14} "
                f"{row['count']:8} {row['size_bytes'] / 1024 / 1024:10.1f} MB"
            )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.list_urls(args.status, args.limit)
    finally:
        db.close()
        client.close()
    for row in rows:
        print(f"#{row['id']} [{row['site_id']}] {row['download_status']} HTTP={row['http_status']}")
        print(f"  {row['url']}")
        if row["archive_path"]:
            print(f"  arhiva: {row['archive_path']}")
        if row["error"]:
            print(f"  greška: {row['error']}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        row_ids = db.irrelevant_selenium_ids(settings["keywords"])
        removed = db.delete_urls(row_ids) if args.apply else 0
    finally:
        db.close()
        client.close()
    if args.apply:
        print(f"Uklonjeno nerelevantnih pending Selenium URL-ova: {removed}")
    else:
        print(f"Nerelevantnih pending Selenium URL-ova za uklanjanje: {len(row_ids)}")
        print("Pokrenite prune --apply za potvrdu.")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        raise SystemExit("Reset zahteva --yes jer briše sve redove iz urls i discoveries.")
    root, settings, sites, db, client = context(args.config_dir)
    database_path = resolve_path(root, settings["database"])
    try:
        url_count, discovery_count = db.reset_discovery()
    finally:
        db.close()
        client.close()
    print(f"Baza: {database_path}")
    print(f"Obrisano: urls={url_count}; discoveries={discovery_count}")
    return 0


def cmd_archive_source(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        row = db.archived_url(args.id)
    finally:
        db.close()
        client.close()
    if row is None:
        raise SystemExit(f"URL ID {args.id} ne postoji.")
    if row["download_status"] not in {"archived", "awaiting_review"} or not row["archive_path"]:
        raise SystemExit(f"URL ID {args.id} nema sačuvan page source.")
    stored = Path(row["archive_path"])
    default_suffix = ".html" if stored.name.endswith(".html.gz") else stored.suffix
    output = args.output or resolve_path(root, settings["data_dir"]) / "view" / f"{args.id}{default_suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    if stored.suffix == ".gz":
        with gzip.open(stored, "rb") as source:
            output.write_bytes(source.read())
    else:
        shutil.copy2(stored, output)
    print(f"Original: {row['url']}")
    print(f"Arhivski page source: {output.resolve()}")
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        count = db.reclassify_robots_blocks()
        bad_count = db.backfill_bad_urls()
    finally:
        db.close()
        client.close()
    print(f"Robots blokade prebačene u status manual_capture_needed: {count}")
    print(f"Postojeći no_keyword URL-ovi dodati u bad_urls: {bad_count}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        count = db.requeue(args.status, args.site)
    finally:
        db.close()
        client.close()
    print(f"Vraćeno u pending: {count}")
    return 0


def cmd_robots_recheck(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.robots_blocked_rows(args.site)
        allowed_ids = [row["id"] for row in rows if client.allowed(row["url"])]
        requeued = db.requeue_ids(allowed_ids)
    finally:
        db.close()
        client.close()
    print(f"Provereno robots URL-ova: {len(rows)}")
    print(f"Vraćeno u pending: {requeued}")
    print(f"I dalje zabranjeno: {len(rows) - requeued}")
    return 0


def cmd_clean_urls(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        count = db.classify_non_articles()
    finally:
        db.close()
        client.close()
    print(f"Označeno kao ignored_non_article: {count}")
    return 0


def cmd_audit_sources(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    selected = choose_sites(sites, args.site, db)
    user_agent = env("ARCHIVE_USER_AGENT", "PetrovaradinInfoArchive/0.1") or ""

    def check(site: dict) -> tuple[str, str, str, int | None, str | None, str | None]:
        search = site.get("internal_search", {})
        targets = list(search.get("start_urls", []))
        if search.get("start_url"):
            targets.append(search["start_url"])
        targets.extend(site.get("sitemaps", []))
        target = targets[0] if targets else f"https://{site['domains'][0]}/"
        insecure_hosts = {
            host.lower() for host in settings.get("request", {}).get("tls_insecure_hosts", [])
        }
        try:
            with httpx.Client(
                follow_redirects=True, timeout=args.timeout,
                headers={"User-Agent": user_agent, "Accept": "text/html,application/xml,*/*"},
                verify=(urlsplit(target).hostname or "").lower() not in insecure_hosts,
            ) as audit_client:
                response = audit_client.get(target)
            status = "reachable" if response.status_code < 400 else "http_error"
            return (site["id"], target, status, response.status_code, str(response.url), None)
        except Exception as exc:
            return (site["id"], target, "unreachable", None, None, str(exc))

    results = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(check, site): site["id"] for site in selected}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                db.record_source_audit(*result)
    finally:
        db.close()
        client.close()
    for source_id, target, status, http_status, final_url, error in sorted(results):
        detail = f"HTTP {http_status}" if http_status is not None else (error or "")
        print(f"{source_id:22} {status:12} {detail}")
    totals = {status: sum(row[2] == status for row in results)
              for status in ("reachable", "http_error", "unreachable")}
    print("; ".join(f"{key}={value}" for key, value in totals.items()))
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    if args.action != "list" and not args.id:
        raise SystemExit("Ova akcija zahteva ID izvora.")
    if args.action in {"block-add", "block-remove"} and not args.prefix:
        raise SystemExit("Blocklist akcija zahteva URL prefiks.")
    root, settings, sites, db, client = context(args.config_dir)
    try:
        if args.action in {"enable", "disable"}:
            if not db.set_source_enabled(args.id, args.action == "enable"):
                raise SystemExit(f"Nepoznat izvor: {args.id}")
        elif args.action in {"review-on", "review-off"}:
            if not db.set_source_manual_review(args.id, args.action == "review-on"):
                raise SystemExit(f"Nepoznat izvor: {args.id}")
        elif args.action in {"public-on", "public-off"}:
            if not db.set_source_public(args.id, args.action == "public-on"):
                raise SystemExit(f"Nepoznat izvor: {args.id}")
        elif args.action in {"block-add", "block-remove"}:
            try:
                values = db.update_source_blocklist(
                    args.id, args.prefix, remove=args.action == "block-remove"
                )
            except KeyError:
                raise SystemExit(f"Nepoznat izvor: {args.id}") from None
            print(f"Blocklist [{args.id}]: {json.dumps(values, ensure_ascii=False)}")
        rows = db.source_rows()
    finally:
        db.close()
        client.close()
    if args.action == "list":
        for row in rows:
            flags = ["ON" if row["enabled"] else "OFF"]
            flags.append("PUBLIC" if row["public_enabled"] else "HIDDEN")
            if row["manual_review"]:
                flags.append("MANUAL_REVIEW")
            blocked = len(json.loads(row["blocklist_json"] or "[]"))
            print(f"{row['id']:20} {' '.join(flags):20} blocklist={blocked:2}  {row['name']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.public_search(args.query, args.limit)
    finally:
        db.close()
        client.close()
    for row in rows:
        copies = f" (+{row['duplicate_copies']} kopija)" if row["duplicate_copies"] else ""
        print(f"#{row['id']} [{row['site_id']}] {row['title'] or ''}{copies}")
        print(f"  {row['url']}")
    print(f"Rezultata dostupnih redovnim korisnicima: {len(rows)}")
    return 0


def cmd_bad_urls(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.bad_url_rows(args.limit)
    finally:
        db.close()
        client.close()
    for row in rows:
        print(f"#{row['id']} [{row['site_id']}] {row['reason']} x{row['occurrences']}")
        print(f"  {row['url']}")
        if row["final_url"]:
            print(f"  vodi na: {row['final_url']}")
    print(f"Prikazano loših URL-ova: {len(rows)}")
    return 0


def cmd_assets(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.asset_rows(args.status, args.limit)
    finally:
        db.close()
        client.close()
    for row in rows:
        size = row["size_bytes"] or 0
        print(f"#{row['id']} [{row['site_id']}] {row['kind']} {row['status']} {size} B")
        print(f"  {row['url']}")
        if row["archive_path"]:
            print(f"  arhiva: {row['archive_path']}")
        if row["error"]:
            print(f"  greška: {row['error']}")
    print(f"Prikazano priloga: {len(rows)}")
    return 0


def cmd_assets_requeue(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        count = db.requeue_assets(args.status, args.kind)
    finally:
        db.close()
        client.close()
    print(f"Vraćeno priloga u retry: {count}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        rows = db.coverage_rows()
    finally:
        db.close()
        client.close()
    for row in rows:
        flag = "OK" if row["captured"] else "ZERO"
        print(
            f"{row['id']:22} {flag:4} audit={row['audit_status']:12} "
            f"found={row['discovered']:4} saved={row['captured']:4} "
            f"robots={row['robots_blocked'] or 0:3} no_keyword={row['no_keyword'] or 0:3}"
        )
    zero = sum(not row["captured"] for row in rows)
    print(f"Izvora: {len(rows)}; sa sačuvanim sadržajem: {len(rows) - zero}; bez sadržaja: {zero}")
    return 0


def cmd_manual_import(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        row = db.connection.execute("SELECT * FROM urls WHERE id=?", (args.id,)).fetchone()
        if row is None:
            raise SystemExit(f"URL ID {args.id} ne postoji.")
        content = args.path.read_bytes()
        text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True).casefold()
        keywords = [word.casefold() for word in settings.get("keywords", [])]
        if not any(word in text for word in keywords):
            raise SystemExit("Dati HTML ne sadrži aktivnu ključnu reč; nije arhiviran.")
        digest = hashlib.sha256(content).hexdigest()
        host = urlsplit(row["url"]).hostname or row["site_id"]
        folder = resolve_path(root, settings["html_dir"]) / host
        folder.mkdir(parents=True, exist_ok=True)
        archive_path = folder / f"{digest}.html.gz"
        if not archive_path.exists():
            with gzip.open(archive_path, "wb") as handle:
                handle.write(content)
        db.mark_archived(
            row["id"], http_status=None, final_url=row["url"], content_type="text/html",
            content_sha256=digest, archive_path=str(archive_path),
        )
    finally:
        db.close()
        client.close()
    print(f"Ručno sačuvan page source za URL #{args.id}: {archive_path}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    root, settings, sites, db, client = context(args.config_dir)
    try:
        changed = db.review(args.id, args.decision == "approve")
    finally:
        db.close()
        client.close()
    if not changed:
        raise SystemExit("Red ne postoji ili nije u statusu awaiting_review.")
    print(f"URL #{args.id}: {'odobren' if args.decision == 'approve' else 'odbijen'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="petrovaradin-archive")
    parser.add_argument("--config-dir", type=Path, help="Alternativni config direktorijum")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Kreira bazu i data direktorijume")
    init.set_defaults(func=cmd_init)

    discover = sub.add_parser("discover", help="Otkriva istorijske URL-ove")
    discover.add_argument("--provider", action="append",
                          choices=["sitemap", "selenium", "google", "ddg", "scan"],
                          required=True)
    discover.add_argument("--site", action="append", help="ID sajta; izostaviti za sve")
    discover.add_argument(
        "--max-pages", type=int,
        help="Privremeni limit stranica po izvoru, koristan za probu adaptera",
    )
    discover.set_defaults(func=cmd_discover)

    download = sub.add_parser("download", help="Preuzima pending HTML stranice")
    download.add_argument("--limit", type=int, default=100)
    download.add_argument("--site", help="Obrađuje samo jedan site ID")
    download.add_argument(
        "--mode", choices=["selenium", "http", "hybrid"], default="selenium",
        help="Način otvaranja; podrazumevano Selenium page_source",
    )
    download.set_defaults(func=cmd_download)

    download_assets = sub.add_parser(
        "download-assets", help="Preuzima pending dokumente i slike odvojeno od HTML-a"
    )
    download_assets.add_argument("--limit", type=int, default=100)
    download_assets.add_argument("--site")
    download_assets.add_argument("--kind", choices=["document", "image"])
    download_assets.set_defaults(func=cmd_download_assets)

    analyze = sub.add_parser(
        "analyze", help="Izvlači podatke iz sačuvanih članaka i grupiše prenete vesti"
    )
    analyze.add_argument("--limit", type=int, default=1000)
    analyze.add_argument("--site")
    analyze.set_defaults(func=cmd_analyze)

    duplicates = sub.add_parser("duplicates", help="Prikazuje grupe prenetih/duplih vesti")
    duplicates.add_argument("--limit", type=int, default=100)
    duplicates.set_defaults(func=cmd_duplicates)

    importer = sub.add_parser("import", help="Uvozi URL kolonu iz CSV-a ili URL-ove iz TXT-a")
    importer.add_argument("path", type=Path)
    importer.add_argument("--source", default="manual_import")
    importer.set_defaults(func=cmd_import)

    stats = sub.add_parser("stats", help="Prikazuje stanje arhive")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    listing = sub.add_parser("list", help="Prikazuje URL-ove, statuse i greške")
    listing.add_argument("--status", help="Filtrira status, npr. unavailable ili archived")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=cmd_list)

    prune = sub.add_parser("prune", help="Nalazi Selenium rezultate bez traženog korena reči")
    prune.add_argument("--apply", action="store_true", help="Uklanja pronađene pending redove")
    prune.set_defaults(func=cmd_prune)

    reset = sub.add_parser("reset", help="Briše sve URL i discovery redove")
    reset.add_argument("--yes", action="store_true", help="Potvrđuje nepovratno brisanje redova")
    reset.set_defaults(func=cmd_reset)

    source = sub.add_parser("archive-source", help="Izvlači sačuvani page source u .html")
    source.add_argument("id", type=int, help="ID reda iz urls tabele")
    source.add_argument("--output", type=Path)
    source.set_defaults(func=cmd_archive_source)

    reclassify = sub.add_parser("reclassify", help="Ispravlja stare robots statuse")
    reclassify.set_defaults(func=cmd_reclassify)

    requeue = sub.add_parser("requeue", help="Vraća odabrane statuse u pending")
    requeue.add_argument("--status", action="append", required=True)
    requeue.add_argument("--site")
    requeue.set_defaults(func=cmd_requeue)

    robots_recheck = sub.add_parser(
        "robots-recheck",
        help="Ponovo proverava robots.txt i vraća dozvoljene URL-ove u pending",
    )
    robots_recheck.add_argument("--site", help="Proverava samo jedan site ID")
    robots_recheck.set_defaults(func=cmd_robots_recheck)

    clean_urls = sub.add_parser(
        "clean-urls", help="Označava komentare i druge nečlanke bez brisanja"
    )
    clean_urls.set_defaults(func=cmd_clean_urls)

    audit = sub.add_parser("audit-sources", help="Proverava dostupnost svakog izvora")
    audit.add_argument("--site", action="append", help="ID izvora; izostaviti za sve")
    audit.add_argument("--timeout", type=float, default=12.0)
    audit.add_argument("--workers", type=int, default=6)
    audit.set_defaults(func=cmd_audit_sources)

    sources = sub.add_parser("sources", help="Upravlja tabelom izvora")
    sources.add_argument(
        "action",
        choices=["list", "enable", "disable", "review-on", "review-off",
                 "public-on", "public-off",
                 "block-add", "block-remove"],
    )
    sources.add_argument("id", nargs="?")
    sources.add_argument("prefix", nargs="?")
    sources.set_defaults(func=cmd_sources)

    review = sub.add_parser("review", help="Ručna provera arhive sa rizičnog izvora")
    review.add_argument("id", type=int)
    review.add_argument("decision", choices=["approve", "reject"])
    review.set_defaults(func=cmd_review)

    search = sub.add_parser("search", help="Pretražuje samo javno dostupne arhive")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=100)
    search.set_defaults(func=cmd_search)

    bad_urls = sub.add_parser("bad-urls", help="Prikazuje kolekciju loših URL-ova")
    bad_urls.add_argument("--limit", type=int, default=100)
    bad_urls.set_defaults(func=cmd_bad_urls)

    assets = sub.add_parser("assets", help="Prikazuje arhivirane dokumente i slike")
    assets.add_argument("--status", help="Filtrira status priloga")
    assets.add_argument("--limit", type=int, default=100)
    assets.set_defaults(func=cmd_assets)

    assets_requeue = sub.add_parser("assets-requeue", help="Vraća neuspele priloge u retry")
    assets_requeue.add_argument("--status", default="failed")
    assets_requeue.add_argument("--kind", choices=["document", "image"])
    assets_requeue.set_defaults(func=cmd_assets_requeue)

    coverage = sub.add_parser("coverage", help="Prikazuje pokrivenost svakog izvora")
    coverage.set_defaults(func=cmd_coverage)

    manual_import = sub.add_parser(
        "manual-import", help="Uvozi ručno sačuvan HTML za robots-blokirani URL"
    )
    manual_import.add_argument("id", type=int)
    manual_import.add_argument("path", type=Path)
    manual_import.set_defaults(func=cmd_manual_import)

    return parser


def main() -> int:
    # Windows PowerShell can inherit cp1252 even when project files are UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
