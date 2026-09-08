from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from selenium import webdriver

from .content import content_kind, decode_html, extract_text, safe_extension, url_extension
from .http import PoliteClient
from .keywords import detect_locations
from .urltools import canonicalize_url


CONTENT_TYPES = {
    "news", "historical_press", "official_document", "public_notice", "advertisement",
    "obituary", "book_or_publication", "archive_record", "image", "document", "unknown",
}
FINAL_STATUSES = {
    "pending", "archived", "no_keyword", "retry", "unavailable", "blocked",
    "manual_capture_needed",
}

SPECIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS special_sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
  base_url TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS special_discovery_sessions (
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT, status TEXT NOT NULL DEFAULT 'running', requested_pages INTEGER NOT NULL,
  results_found INTEGER NOT NULL DEFAULT 0, unique_records INTEGER NOT NULL DEFAULT 0,
  error TEXT, FOREIGN KEY(source_id) REFERENCES special_sources(id)
);
CREATE TABLE IF NOT EXISTS special_discovery_pages (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, keyword TEXT NOT NULL,
  script TEXT NOT NULL, page_number INTEGER NOT NULL, search_url TEXT NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0, total_results INTEGER,
  html_sha256 TEXT, archive_path TEXT, discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, keyword, page_number),
  FOREIGN KEY(session_id) REFERENCES special_discovery_sessions(id)
);
CREATE TABLE IF NOT EXISTS special_records (
  id INTEGER PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  description TEXT, record_date TEXT, estimated_period TEXT, original_source_name TEXT,
  content_type TEXT NOT NULL DEFAULT 'unknown', page_label TEXT,
  duplicate_group_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_special_records_type ON special_records(content_type);
CREATE INDEX IF NOT EXISTS idx_special_records_group ON special_records(duplicate_group_id);
CREATE TABLE IF NOT EXISTS special_discoveries (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, page_id INTEGER NOT NULL,
  record_id INTEGER NOT NULL, keyword TEXT NOT NULL, script TEXT NOT NULL,
  page_number INTEGER NOT NULL, position INTEGER NOT NULL,
  search_result_url TEXT, original_url TEXT, discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, keyword, page_number, position),
  FOREIGN KEY(record_id) REFERENCES special_records(id)
);
CREATE INDEX IF NOT EXISTS idx_special_discoveries_record ON special_discoveries(record_id);
CREATE TABLE IF NOT EXISTS special_copies (
  id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL, url TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE, portal_name TEXT, existing_url_id INTEGER,
  download_status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER, final_url TEXT, content_type TEXT, content_sha256 TEXT,
  text_fingerprint TEXT, archive_path TEXT, extracted_text_path TEXT, size_bytes INTEGER,
  downloaded_at TEXT, error TEXT, FOREIGN KEY(record_id) REFERENCES special_records(id),
  FOREIGN KEY(existing_url_id) REFERENCES urls(id)
);
CREATE INDEX IF NOT EXISTS idx_special_copies_status ON special_copies(download_status);
CREATE TABLE IF NOT EXISTS special_assets (
  id INTEGER PRIMARY KEY, copy_id INTEGER NOT NULL, url TEXT NOT NULL,
  canonical_url TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, content_type TEXT, size_bytes INTEGER,
  content_sha256 TEXT, archive_path TEXT, error TEXT,
  UNIQUE(copy_id, canonical_url), FOREIGN KEY(copy_id) REFERENCES special_copies(id)
);
CREATE TABLE IF NOT EXISTS special_duplicate_groups (
  id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(slots=True)
class SpecialResult:
    title: str
    description: str
    record_date: str | None
    page_label: str | None
    search_result_url: str | None
    original_url: str | None
    original_source_name: str | None
    content_type: str


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def canonical_special_url(url: str) -> str:
    """Ignore search highlighting while preserving issue/page identity."""
    parsed = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k.casefold() != "query"])
    fragment = re.sub(r"(?:%7C|\|)query:[^|]+", "", parsed.fragment, flags=re.I)
    path = re.sub(r"/{2,}", "/", parsed.path) or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, query, fragment))


def classify_result(title: str, description: str, url: str | None) -> str:
    text = _normalized(f"{title} {description}")
    ext = url_extension(url or "")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "svg"}:
        return "image"
    if ext in {"pdf", "doc", "docx", "odt", "rtf", "xls", "xlsx"}:
        return "document"
    rules = (
        ("obituary", ("citulja", "умрли", "in memoriam")),
        ("advertisement", ("oglas", "ценовник", "prodaje", "nekretnin")),
        ("public_notice", ("javni poziv", "javni oglas", "obavestenje", "објав")),
        ("official_document", ("sluzbeni", "службени", "zakon", "izvestaj", "statisticki")),
        ("book_or_publication", ("bibliografija", "knjiga", "recnik", "годишњак")),
    )
    for kind, needles in rules:
        if any(needle in text for needle in needles):
            return kind
    if re.search(r"\b(18|19|20)\d{2}\b", text):
        return "historical_press"
    return "archive_record"


def parse_pretraziva_results(html: bytes | str) -> tuple[list[SpecialResult], str | None, int | None]:
    soup = BeautifulSoup(decode_html(html), "html.parser")
    results: list[SpecialResult] = []
    for item in soup.select("#results-list > li"):
        heading = item.select_one("h3")
        if not heading:
            continue
        title_node = heading.select_one("span span, a > span")
        title = _clean(title_node.get_text(" ") if title_node else heading.get_text(" "))
        time_node = heading.select_one("time")
        date = time_node.get("datetime") if time_node else None
        spans = heading.select("span")
        page_label = _clean(spans[-1].get_text(" ")) if spans else None
        snippet_node = item.select_one("p")
        description = _clean(snippet_node.get_text(" ") if snippet_node else "")
        result_link = item.select_one(".link-text a[href]")
        original_link = heading.select_one("a[href]")
        if original_link is None and snippet_node is not None:
            original_link = snippet_node.select_one("a[href]")
        search_url = urljoin("https://pretraziva.rs/", result_link.get("href")) if result_link else None
        original_url = urljoin("https://pretraziva.rs/", original_link.get("href")) if original_link else None
        host = urlsplit(original_url).hostname if original_url else None
        results.append(SpecialResult(
            title, description, date, page_label, search_url, original_url, host,
            classify_result(title, description, original_url),
        ))
    next_link = soup.select_one("#next-link-bottom[href], #next-link-top[href]")
    next_url = urljoin("https://pretraziva.rs/", next_link.get("href")) if next_link else None
    message = soup.select_one("#results-messages-top")
    total = None
    if message:
        match = re.search(r"od\s+([\d.]+)\s+prona", message.get_text(" "), re.I)
        if match:
            total = int(match.group(1).replace(".", ""))
    return results, next_url, total


class SpecialRepository:
    def __init__(self, connection: sqlite3.Connection, candidate_keywords: list[str] | None = None):
        self.connection = connection
        self.candidate_keywords = candidate_keywords or []
        self.connection.executescript(SPECIAL_SCHEMA)
        self._migrate()
        self.connection.execute(
            "INSERT OR IGNORE INTO special_sources(id,name,category,base_url) VALUES(?,?,?,?)",
            ("pretraziva", "Pretraživa", "specialized_database", "https://pretraziva.rs/"),
        )
        self._reconcile_copies()
        self.connection.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(special_records)")}
        if "lokacija" not in columns:
            self.connection.execute("ALTER TABLE special_records ADD COLUMN lokacija TEXT")
        self.connection.commit()

    def rows_for_recategorize(self, limit: int) -> list[sqlite3.Row]:
        """title+description only — special_copies don't keep full extracted text in the DB."""
        return list(self.connection.execute(
            "SELECT id, title, description FROM special_records ORDER BY id LIMIT ?", (limit,),
        ))

    def set_lokacija(self, record_id: int, lokacija: str | None) -> None:
        self.connection.execute(
            "UPDATE special_records SET lokacija=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (lokacija, record_id),
        )
        self.connection.commit()

    def _reconcile_copies(self) -> None:
        """Backfill portal copies from discoveries after canonicalization changes."""
        for row in self.connection.execute(
            "SELECT id,url,canonical_url FROM special_copies"
        ).fetchall():
            canonical = canonical_special_url(row["url"])
            if canonical != row["canonical_url"]:
                self.connection.execute(
                    "UPDATE OR IGNORE special_copies SET canonical_url=? WHERE id=?",
                    (canonical, row["id"]),
                )
        discoveries = self.connection.execute(
            """SELECT DISTINCT d.record_id,d.original_url,d.search_result_url,r.original_source_name
            FROM special_discoveries d JOIN special_records r ON r.id=d.record_id"""
        ).fetchall()
        for row in discoveries:
            candidates = []
            if row["search_result_url"]:
                candidates.append((row["search_result_url"], "pretraziva.rs"))
            if row["original_url"] and row["original_url"] != row["search_result_url"]:
                candidates.append((row["original_url"], row["original_source_name"]))
            for url, portal_name in candidates:
                existing = self.connection.execute(
                    "SELECT id FROM urls WHERE canonical_url=?", (canonicalize_url(url),)
                ).fetchone()
                self.connection.execute(
                    """INSERT OR IGNORE INTO special_copies
                    (record_id,url,canonical_url,portal_name,existing_url_id) VALUES(?,?,?,?,?)""",
                    (row["record_id"], url, canonical_special_url(url), portal_name,
                     existing[0] if existing else None),
                )

    def start_session(self, pages: int) -> int:
        cur = self.connection.execute(
            "INSERT INTO special_discovery_sessions(source_id,requested_pages) VALUES('pretraziva',?)",
            (pages,),
        )
        self.connection.commit()
        return int(cur.lastrowid)

    def finish_session(self, session_id: int, status: str, error: str | None = None) -> None:
        counts = self.connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT record_id) FROM special_discoveries WHERE session_id=?",
            (session_id,),
        ).fetchone()
        self.connection.execute(
            """UPDATE special_discovery_sessions SET finished_at=CURRENT_TIMESTAMP,status=?,
            results_found=?,unique_records=?,error=? WHERE id=?""",
            (status, counts[0], counts[1], error, session_id),
        )
        self.connection.commit()

    def add_page(self, session_id: int, keyword: str, script: str, page_number: int,
                 url: str, html: bytes, archive_path: str, total: int | None) -> int:
        digest = hashlib.sha256(html).hexdigest()
        cur = self.connection.execute(
            """INSERT INTO special_discovery_pages
            (session_id,keyword,script,page_number,search_url,total_results,html_sha256,archive_path)
            VALUES(?,?,?,?,?,?,?,?)""",
            (session_id, keyword, script, page_number, url, total, digest, archive_path),
        )
        self.connection.commit()
        return int(cur.lastrowid)

    def add_result(self, session_id: int, page_id: int, keyword: str, script: str,
                   page_number: int, position: int, result: SpecialResult) -> tuple[int, bool]:
        identity_url = result.search_result_url or result.original_url
        canonical_key = canonical_special_url(identity_url) if identity_url else "meta:" + hashlib.sha256(
            f"{_normalized(result.title)}|{result.record_date}|{result.page_label}".encode()
        ).hexdigest()
        before = self.connection.total_changes
        lokacija = detect_locations(
            f"{result.title} {result.description}", self.candidate_keywords
        ) if self.candidate_keywords else None
        self.connection.execute(
            """INSERT OR IGNORE INTO special_records
            (canonical_key,title,description,record_date,estimated_period,original_source_name,
             content_type,page_label,lokacija) VALUES(?,?,?,?,?,?,?,?,?)""",
            (canonical_key, result.title, result.description, result.record_date,
             result.record_date[:4] if result.record_date else None,
             result.original_source_name, result.content_type, result.page_label, lokacija),
        )
        created = self.connection.total_changes > before
        record_id = int(self.connection.execute(
            "SELECT id FROM special_records WHERE canonical_key=?", (canonical_key,)
        ).fetchone()[0])
        self.connection.execute(
            """INSERT OR IGNORE INTO special_discoveries
            (session_id,page_id,record_id,keyword,script,page_number,position,search_result_url,original_url)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (session_id, page_id, record_id, keyword, script, page_number, position,
             result.search_result_url, result.original_url),
        )
        copy_urls = []
        if result.search_result_url:
            copy_urls.append((result.search_result_url, "pretraziva.rs"))
        if result.original_url and result.original_url != result.search_result_url:
            copy_urls.append((result.original_url, result.original_source_name))
        for copy_url, portal_name in copy_urls:
            canonical = canonical_special_url(copy_url)
            existing = self.connection.execute(
                "SELECT id FROM urls WHERE canonical_url=?", (canonicalize_url(copy_url),)
            ).fetchone()
            self.connection.execute(
                """INSERT OR IGNORE INTO special_copies
                (record_id,url,canonical_url,portal_name,existing_url_id) VALUES(?,?,?,?,?)""",
                (record_id, copy_url, canonical, portal_name,
                 existing[0] if existing else None),
            )
        self.connection.execute(
            "UPDATE special_discovery_pages SET result_count=result_count+1 WHERE id=?", (page_id,)
        )
        self.connection.commit()
        return record_id, created

    def pending(self, limit: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT c.*,r.title record_title,r.description record_description
            FROM special_copies c JOIN special_records r ON r.id=c.record_id
            WHERE c.download_status IN ('pending','retry') ORDER BY c.id LIMIT ?""",
            (limit,),
        ))

    def stats(self) -> dict[str, object]:
        by_status = {r[0]: r[1] for r in self.connection.execute(
            "SELECT download_status,COUNT(*) FROM special_copies GROUP BY download_status"
        )}
        by_type = {r[0]: r[1] for r in self.connection.execute(
            "SELECT content_type,COUNT(*) FROM special_records GROUP BY content_type"
        )}
        overlap = self.connection.execute(
            """SELECT COUNT(*) FROM (SELECT record_id FROM special_discoveries
            GROUP BY record_id HAVING COUNT(DISTINCT script)>1)"""
        ).fetchone()[0]
        return {
            "records": self.connection.execute("SELECT COUNT(*) FROM special_records").fetchone()[0],
            "discoveries": self.connection.execute("SELECT COUNT(*) FROM special_discoveries").fetchone()[0],
            "copies": self.connection.execute("SELECT COUNT(*) FROM special_copies").fetchone()[0],
            "script_overlap": overlap, "by_status": by_status, "by_type": by_type,
        }

    def list_records(self, limit: int, status: str | None = None,
                     query: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if status:
            clauses.append("c.download_status=?"); params.append(status)
        if query:
            clauses.append("(r.title LIKE ? OR r.description LIKE ? OR c.url LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        return list(self.connection.execute(
            f"""SELECT r.*,c.id copy_id,c.url,c.download_status,c.archive_path
            FROM special_records r LEFT JOIN special_copies c ON c.record_id=r.id
            {where} ORDER BY r.id DESC LIMIT ?""", params,
        ))


class PretrazivaDiscoverer:
    QUERIES = (("Petrovaradin", "latin"), ("Петроварадин", "cyrillic"))

    def __init__(self, repository: SpecialRepository, client: PoliteClient,
                 discovery_dir: Path):
        self.repository = repository
        self.client = client
        self.discovery_dir = discovery_dir

    def run(self, max_pages: int, results_per_page: int = 100) -> dict[str, int]:
        session_id = self.repository.start_session(max_pages)
        found = created = 0
        try:
            for keyword, script in self.QUERIES:
                url = "https://pretraziva.rs/pretraga?" + urlencode(
                    {"search": keyword, "advanced": "1", "results": results_per_page,
                     "sort": "score", "path": "*"}
                )
                page_number = 0
                while url and (max_pages <= 0 or page_number < max_pages):
                    page_number += 1
                    started = time.monotonic()
                    print(f"[{script}] [{page_number}/{max_pages}] otvaram: {url}", flush=True)
                    response = self.client.get(url)
                    response.raise_for_status()
                    payload = response.content
                    results, next_url, total = parse_pretraziva_results(payload)
                    digest = hashlib.sha256(payload).hexdigest()
                    folder = self.discovery_dir / f"session-{session_id}" / script
                    folder.mkdir(parents=True, exist_ok=True)
                    archive = folder / f"page-{page_number}-{digest}.html.gz"
                    with gzip.open(archive, "wb") as handle:
                        handle.write(payload)
                    page_id = self.repository.add_page(
                        session_id, keyword, script, page_number, url, payload, str(archive), total
                    )
                    for position, result in enumerate(results, 1):
                        found += 1
                        _, is_new = self.repository.add_result(
                            session_id, page_id, keyword, script, page_number, position, result
                        )
                        created += int(is_new)
                    elapsed = time.monotonic() - started
                    expected_pages = ((total or 0) + results_per_page - 1) // results_per_page
                    pct = page_number / expected_pages * 100 if expected_pages else 0
                    print(f"  -> archived {len(results)} rezultata ({pct:.0f}%, {elapsed:.2f}s)", flush=True)
                    if not next_url:
                        break
                    url = next_url
            self.repository.finish_session(session_id, "completed")
        except Exception as exc:
            self.repository.finish_session(session_id, "failed", str(exc)[:2000])
            raise
        return {"session_id": session_id, "found": found, "new": created}


class SpecialDownloader:
    def __init__(self, repository: SpecialRepository, client: PoliteClient, data_dir: Path,
                 keywords: list[str], request_settings: dict, archive_settings: dict,
                 selenium_settings: dict):
        self.repository, self.client, self.data_dir = repository, client, data_dir
        self.keywords = [k.casefold() for k in keywords]
        self.max_retries = int(request_settings.get("max_retries", 3))
        self.max_document = int(archive_settings.get("max_document_mb", 30)) * 1024 * 1024
        self.max_image = int(archive_settings.get("max_image_mb", 8)) * 1024 * 1024
        self.selenium_settings = selenium_settings
        self.driver = None

    def close(self) -> None:
        if self.driver:
            self.driver.quit(); self.driver = None

    def _render(self, url: str) -> tuple[bytes, str]:
        if not self.driver:
            options = webdriver.ChromeOptions()
            if self.selenium_settings.get("headless", True): options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(int(self.selenium_settings.get("page_load_timeout_seconds", 30)))
        self.driver.get(url)
        return self.driver.page_source.encode(), self.driver.current_url

    def _store_payload(self, payload: bytes, final_url: str, content_type: str,
                       kind: str, text: str) -> tuple[str, str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        normalized_text = re.sub(r"\s+", " ", text.casefold()).strip()
        fingerprint = hashlib.sha256(normalized_text.encode()).hexdigest() if normalized_text else digest
        host = urlsplit(final_url).hostname or "unknown"
        folder = self.data_dir / host
        folder.mkdir(parents=True, exist_ok=True)
        if kind == "html":
            archive = folder / f"{digest}.html.gz"
            if not archive.exists():
                with gzip.open(archive, "wb") as handle:
                    handle.write(payload)
        else:
            archive = folder / f"{digest}.{safe_extension(final_url, content_type, kind)}"
            if not archive.exists():
                archive.write_bytes(payload)
        return digest, fingerprint, str(archive)

    def run(self, limit: int, mode: str = "hybrid") -> dict[str, int]:
        rows = self.repository.pending(limit)
        counts = {k: 0 for k in ("archived", "no_keyword", "retry", "unavailable", "blocked", "manual_capture_needed")}
        for pos, row in enumerate(rows, 1):
            started = time.monotonic(); pct = pos / len(rows) * 100 if rows else 100
            print(f"[{pos}/{len(rows)}] [{row['portal_name'] or 'unknown'}] {pct:.0f}% {row['url']}", flush=True)
            try:
                if not self.client.allowed(row["url"]):
                    raise PermissionError("Blocked by robots.txt")
                requested = content_kind(row["url"])
                if mode in {"selenium", "hybrid"} and requested == "html":
                    payload, final_url = self._render(row["url"])
                    content_type, http_status = "text/html; charset=utf-8", None
                else:
                    max_bytes = self.max_image if requested == "image" else self.max_document if requested in {"pdf", "document"} else None
                    response = self.client.get(row["url"], max_bytes=max_bytes)
                    response.raise_for_status(); payload, final_url = response.content, str(response.url)
                    content_type, http_status = response.headers.get("content-type", ""), response.status_code
                kind = content_kind(final_url, content_type)
                text = extract_text(payload, kind, final_url)
                searchable = (
                    f"{row['url']} {row['record_title']} {row['record_description']} {text}"
                ).casefold()
                matched = any(k in searchable for k in self.keywords)
                digest, fingerprint, archive_path = self._store_payload(
                    payload, final_url, content_type, kind, text
                )
                if not matched:
                    self.repository.connection.execute(
                        """UPDATE special_copies SET download_status='no_keyword',attempts=attempts+1,
                        http_status=?,final_url=?,content_type=?,content_sha256=?,text_fingerprint=?,
                        archive_path=?,size_bytes=?,downloaded_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?""",
                        (http_status, final_url, content_type, digest, fingerprint,
                         archive_path, len(payload), row["id"]),
                    ); counts["no_keyword"] += 1
                else:
                    group = self.repository.connection.execute(
                        "INSERT OR IGNORE INTO special_duplicate_groups(fingerprint) VALUES(?)", (fingerprint,)
                    )
                    group_id = self.repository.connection.execute(
                        "SELECT id FROM special_duplicate_groups WHERE fingerprint=?", (fingerprint,)
                    ).fetchone()[0]
                    self.repository.connection.execute(
                        """UPDATE special_copies SET download_status='archived',attempts=attempts+1,
                        http_status=?,final_url=?,content_type=?,content_sha256=?,text_fingerprint=?,
                        archive_path=?,size_bytes=?,downloaded_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?""",
                        (http_status, final_url, content_type, digest, fingerprint,
                         archive_path, len(payload), row["id"]),
                    )
                    self.repository.connection.execute(
                        "UPDATE special_records SET duplicate_group_id=? WHERE id=?", (group_id, row["record_id"])
                    ); counts["archived"] += 1
                self.repository.connection.commit()
                status = "archived" if matched else "no_keyword"
                print(f"  -> {status} ({time.monotonic()-started:.2f}s)", flush=True)
            except PermissionError as exc:
                self.repository.connection.execute(
                    "UPDATE special_copies SET download_status='manual_capture_needed',attempts=attempts+1,error=? WHERE id=?",
                    (str(exc), row["id"]),
                ); self.repository.connection.commit(); counts["manual_capture_needed"] += 1
                print(f"  -> manual_capture_needed ({time.monotonic()-started:.2f}s)", flush=True)
            except Exception as exc:
                http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retry = row["attempts"] + 1 < self.max_retries and http_status not in {404, 410}
                status = "retry" if retry else "unavailable"
                self.repository.connection.execute(
                    "UPDATE special_copies SET download_status=?,attempts=attempts+1,http_status=?,error=? WHERE id=?",
                    (status, http_status, str(exc)[:2000], row["id"]),
                ); self.repository.connection.commit(); counts[status] += 1
                print(f"  -> {status} ({time.monotonic()-started:.2f}s): {exc}", flush=True)
        return counts
