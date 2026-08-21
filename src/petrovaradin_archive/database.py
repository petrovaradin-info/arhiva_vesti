from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import DiscoveredURL
from .keywords import contains_keyword
from .urltools import canonicalize_url, is_non_article_url
from .article_adapters import content_fingerprint, hamming_distance, simhash

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS urls (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  site_id TEXT NOT NULL,
  discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  discovered_by TEXT NOT NULL,
  query TEXT,
  expected_date TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  download_status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER,
  final_url TEXT,
  content_type TEXT,
  content_sha256 TEXT,
  archive_path TEXT,
  downloaded_at TEXT,
  keyword_match INTEGER,
  live_status TEXT,
  last_checked_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(download_status);
CREATE INDEX IF NOT EXISTS idx_urls_site ON urls(site_id);
CREATE TABLE IF NOT EXISTS discoveries (
  id INTEGER PRIMARY KEY,
  canonical_url TEXT NOT NULL,
  source TEXT NOT NULL,
  query TEXT,
  discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(canonical_url, source, query)
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domains_json TEXT NOT NULL DEFAULT '[]',
  search_url TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  manual_review INTEGER NOT NULL DEFAULT 0,
  public_enabled INTEGER NOT NULL DEFAULT 1,
  blocklist_json TEXT NOT NULL DEFAULT '[]',
  config_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bad_urls (
  id INTEGER PRIMARY KEY,
  canonical_url TEXT NOT NULL,
  site_id TEXT NOT NULL,
  url TEXT NOT NULL,
  reason TEXT NOT NULL,
  final_url TEXT,
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  occurrences INTEGER NOT NULL DEFAULT 1,
  UNIQUE(canonical_url, reason, final_url)
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY,
  url_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  final_url TEXT,
  kind TEXT NOT NULL,
  content_type TEXT,
  size_bytes INTEGER,
  content_sha256 TEXT,
  archive_path TEXT,
  extracted_text_path TEXT,
  extracted_text TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  downloaded_at TEXT,
  FOREIGN KEY(url_id) REFERENCES urls(id),
  UNIQUE(url_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_assets_url_id ON assets(url_id);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE TABLE IF NOT EXISTS source_audits (
  source_id TEXT PRIMARY KEY,
  checked_url TEXT NOT NULL,
  status TEXT NOT NULL,
  http_status INTEGER,
  final_url TEXT,
  error TEXT,
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scan_checks (
  site_id TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  matched INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(site_id, canonical_url)
);
"""


class ArchiveDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(urls)")}
        additions = {
            "keyword_match": "INTEGER",
            "live_status": "TEXT",
            "last_checked_at": "TEXT",
            "requires_manual_review": "INTEGER NOT NULL DEFAULT 0",
            "review_status": "TEXT NOT NULL DEFAULT 'not_required'",
            "access_status": "TEXT NOT NULL DEFAULT 'visible'",
            "media_kind": "TEXT NOT NULL DEFAULT 'html'",
            "size_bytes": "INTEGER",
            "extracted_text_path": "TEXT",
            "extracted_text": "TEXT",
            "article_title": "TEXT",
            "article_text": "TEXT",
            "published_at": "TEXT",
            "canonical_from_page": "TEXT",
            "original_source_url": "TEXT",
            "adapter_name": "TEXT",
            "content_fingerprint": "TEXT",
            "simhash": "TEXT",
            "duplicate_group_id": "INTEGER",
            "is_primary": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE urls ADD COLUMN {name} {sql_type}")
        asset_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(assets)")
        }
        if "extracted_text" not in asset_columns:
            self.connection.execute("ALTER TABLE assets ADD COLUMN extracted_text TEXT")
        if "attempts" not in asset_columns:
            self.connection.execute(
                "ALTER TABLE assets ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        source_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(sources)")
        }
        if "public_enabled" not in source_columns:
            self.connection.execute(
                "ALTER TABLE sources ADD COLUMN public_enabled INTEGER NOT NULL DEFAULT 1"
            )
            self.connection.execute(
                "UPDATE sources SET public_enabled=0 WHERE manual_review=1"
            )
        self.connection.commit()
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_urls_fingerprint ON urls(content_fingerprint)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_urls_duplicate_group ON urls(duplicate_group_id)"
        )
        self.connection.commit()

    def sync_sources(self, sites: list[dict]) -> None:
        for site in sites:
            search = site.get("internal_search", {})
            self.connection.execute(
                """INSERT INTO sources
                (id, name, domains_json, search_url, enabled, manual_review, public_enabled,
                 blocklist_json, config_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  domains_json=excluded.domains_json,
                  search_url=excluded.search_url,
                  config_json=excluded.config_json,
                  updated_at=CURRENT_TIMESTAMP""",
                (site["id"], site["name"], json.dumps(site.get("domains", [])),
                 search.get("start_url"), int(site.get("enabled", True)),
                 int(site.get("manual_review", False)),
                 int(site.get("public_enabled", not site.get("manual_review", False))),
                 json.dumps(site.get("blocklist", []), ensure_ascii=False),
                 json.dumps(site, ensure_ascii=False)),
            )
        self.connection.commit()

    def source_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM sources ORDER BY id"))

    def coverage_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT s.id, s.name, s.enabled, s.manual_review,
                      COALESCE(a.status, 'not_audited') audit_status,
                      a.http_status, a.error audit_error,
                      COUNT(u.id) discovered,
                      SUM(CASE WHEN u.download_status IN ('archived','awaiting_review')
                               THEN 1 ELSE 0 END) captured,
                      SUM(CASE WHEN u.download_status='manual_capture_needed'
                               THEN 1 ELSE 0 END) robots_blocked,
                      SUM(CASE WHEN u.download_status='no_keyword' THEN 1 ELSE 0 END) no_keyword
               FROM sources s
               LEFT JOIN source_audits a ON a.source_id=s.id
               LEFT JOIN urls u ON u.site_id=s.id
               GROUP BY s.id, s.name, s.enabled, s.manual_review,
                        a.status, a.http_status, a.error
               ORDER BY s.id"""
        ))

    def source(self, source_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def record_source_audit(self, source_id: str, checked_url: str, status: str,
                            http_status: int | None = None, final_url: str | None = None,
                            error: str | None = None) -> None:
        self.connection.execute(
            """INSERT INTO source_audits
            (source_id, checked_url, status, http_status, final_url, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET checked_url=excluded.checked_url,
            status=excluded.status, http_status=excluded.http_status,
            final_url=excluded.final_url, error=excluded.error,
            checked_at=CURRENT_TIMESTAMP""",
            (source_id, checked_url, status, http_status, final_url,
             error[:2000] if error else None),
        )
        self.connection.commit()

    def was_content_scanned(self, site_id: str, url: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM scan_checks WHERE site_id=? AND canonical_url=?",
            (site_id, canonicalize_url(url)),
        ).fetchone() is not None

    def record_content_scan(self, site_id: str, url: str, matched: bool,
                            error: str | None = None) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO scan_checks
            (site_id, canonical_url, matched, error, checked_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (site_id, canonicalize_url(url), int(matched), error[:2000] if error else None),
        )
        self.connection.commit()

    def set_source_enabled(self, source_id: str, enabled: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE sources SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(enabled), source_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def set_source_manual_review(self, source_id: str, required: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE sources SET manual_review=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(required), source_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def set_source_public(self, source_id: str, enabled: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE sources SET public_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(enabled), source_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def update_source_blocklist(self, source_id: str, prefix: str, remove: bool = False) -> list[str]:
        row = self.source(source_id)
        if row is None:
            raise KeyError(source_id)
        values = set(json.loads(row["blocklist_json"] or "[]"))
        values.discard(prefix) if remove else values.add(prefix)
        result = sorted(values)
        self.connection.execute(
            "UPDATE sources SET blocklist_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(result, ensure_ascii=False), source_id),
        )
        self.connection.commit()
        return result

    def add(self, item: DiscoveredURL) -> bool:
        canonical = canonicalize_url(item.url)
        source = self.source(item.site_id)
        manual_review = bool(source["manual_review"]) if source else False
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO urls
            (url, canonical_url, site_id, discovered_by, query, expected_date, metadata_json,
             requires_manual_review, review_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.url, canonical, item.site_id, item.discovered_by, item.query,
             item.expected_date, json.dumps(item.metadata, ensure_ascii=False),
             int(manual_review), "pending" if manual_review else "not_required"),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO discoveries(canonical_url, source, query) VALUES (?, ?, ?)",
            (canonical, item.discovered_by, item.query or ""),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def pending(self, limit: int, site_id: str | None = None) -> list[sqlite3.Row]:
        if site_id:
            return list(self.connection.execute(
                """SELECT * FROM urls WHERE download_status IN ('pending', 'retry')
                AND site_id=? ORDER BY id LIMIT ?""",
                (site_id, limit),
            ))
        return list(self.connection.execute(
            "SELECT * FROM urls WHERE download_status IN ('pending', 'retry') ORDER BY id LIMIT ?",
            (limit,),
        ))

    def mark_archived(self, row_id: int, **values: object) -> None:
        self.connection.execute(
            """UPDATE urls SET download_status=CASE
              WHEN requires_manual_review=1 THEN 'awaiting_review' ELSE 'archived' END,
            attempts=attempts+1,
            http_status=?, final_url=?, content_type=?, content_sha256=?, archive_path=?,
            media_kind=?, size_bytes=?, extracted_text_path=?, extracted_text=?,
            downloaded_at=CURRENT_TIMESTAMP, keyword_match=1, live_status='active',
            access_status=CASE WHEN requires_manual_review=1 THEN 'hidden' ELSE 'visible' END,
            last_checked_at=CURRENT_TIMESTAMP, error=NULL WHERE id=?""",
            (values.get("http_status"), values.get("final_url"), values.get("content_type"),
             values.get("content_sha256"), values.get("archive_path"),
             values.get("media_kind", "html"), values.get("size_bytes"),
             values.get("extracted_text_path"), values.get("extracted_text"), row_id),
        )
        self.connection.commit()

    def store_article_analysis(self, row_id: int, *, title: str | None, body_text: str,
                               published_at: str | None, canonical_url: str | None,
                               original_source_url: str | None, adapter_name: str) -> int:
        fingerprint = content_fingerprint(body_text)
        article_simhash = simhash(body_text)
        row = self.connection.execute(
            "SELECT site_id FROM urls WHERE id=?", (row_id,)
        ).fetchone()
        primary_id = row_id
        if fingerprint:
            exact = self.connection.execute(
                """SELECT id, duplicate_group_id FROM urls
                   WHERE id<>? AND content_fingerprint=? ORDER BY id LIMIT 1""",
                (row_id, fingerprint),
            ).fetchone()
            if exact:
                primary_id = exact["duplicate_group_id"] or exact["id"]
        if primary_id == row_id and article_simhash and len(body_text) >= 300:
            candidates = self.connection.execute(
                """SELECT id, duplicate_group_id, simhash, length(article_text) AS text_length
                   FROM urls WHERE id<>? AND site_id<>? AND simhash IS NOT NULL
                   AND length(article_text) BETWEEN ? AND ?""",
                (row_id, row["site_id"], int(len(body_text) * 0.85), int(len(body_text) * 1.15)),
            ).fetchall()
            match = next(
                (candidate for candidate in candidates
                 if hamming_distance(article_simhash, candidate["simhash"]) <= 4), None
            )
            if match:
                primary_id = match["duplicate_group_id"] or match["id"]
        self.connection.execute(
            """UPDATE urls SET article_title=?, article_text=?, published_at=?,
               canonical_from_page=?, original_source_url=?, adapter_name=?,
               content_fingerprint=?, simhash=?, duplicate_group_id=?, is_primary=?
               WHERE id=?""",
            (title, body_text, published_at, canonical_url, original_source_url, adapter_name,
             fingerprint, article_simhash, primary_id, int(primary_id == row_id), row_id),
        )
        if primary_id != row_id:
            self.connection.execute(
                "UPDATE urls SET duplicate_group_id=?, is_primary=1 WHERE id=?",
                (primary_id, primary_id),
            )
        self.connection.commit()
        return primary_id

    def unanalyzed_html_rows(self, limit: int, site_id: str | None = None) -> list[sqlite3.Row]:
        parameters: list[object] = []
        site_filter = ""
        if site_id:
            site_filter = " AND u.site_id=?"
            parameters.append(site_id)
        parameters.append(limit)
        return list(self.connection.execute(
            """SELECT u.*, s.config_json FROM urls u JOIN sources s ON s.id=u.site_id
               WHERE u.download_status IN ('archived','awaiting_review')
               AND u.media_kind='html' AND u.archive_path IS NOT NULL
               AND u.article_text IS NULL""" + site_filter + " ORDER BY u.id LIMIT ?",
            parameters,
        ))

    def duplicate_groups(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT p.id, p.site_id, p.url, p.article_title,
                      COUNT(c.id) AS copies,
                      GROUP_CONCAT(DISTINCT c.site_id) AS sites
               FROM urls p JOIN urls c ON c.duplicate_group_id=p.id
               WHERE p.id=p.duplicate_group_id
               GROUP BY p.id HAVING COUNT(c.id)>1
               ORDER BY copies DESC, p.id LIMIT ?""", (limit,)
        ))

    def add_asset(self, url_id: int, url: str, kind: str) -> int | None:
        canonical = canonicalize_url(url)
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO assets(url_id, url, canonical_url, kind)
            VALUES (?, ?, ?, ?)""",
            (url_id, url, canonical, kind),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return int(cursor.lastrowid)

    def mark_asset_archived(self, asset_id: int, **values: object) -> None:
        self.connection.execute(
            """UPDATE assets SET status='archived', final_url=?, content_type=?,
            size_bytes=?, content_sha256=?, archive_path=?, extracted_text_path=?, extracted_text=?,
            downloaded_at=CURRENT_TIMESTAMP, attempts=attempts+1, error=NULL WHERE id=?""",
            (values.get("final_url"), values.get("content_type"), values.get("size_bytes"),
             values.get("content_sha256"), values.get("archive_path"),
             values.get("extracted_text_path"), values.get("extracted_text"), asset_id),
        )
        self.connection.commit()

    def mark_asset_failed(self, asset_id: int, status: str, error: str) -> None:
        self.connection.execute(
            "UPDATE assets SET status=?, attempts=attempts+1, error=? WHERE id=?",
            (status, error[:2000], asset_id),
        )
        self.connection.commit()

    def review(self, row_id: int, approved: bool) -> bool:
        status = "archived" if approved else "rejected"
        review_status = "approved" if approved else "rejected"
        cursor = self.connection.execute(
            """UPDATE urls SET download_status=?, review_status=?, access_status=?
            WHERE id=? AND requires_manual_review=1 AND download_status='awaiting_review'""",
            (status, review_status, "visible" if approved else "hidden", row_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def public_search(self, query: str, limit: int = 100) -> list[sqlite3.Row]:
        pattern = f"%{query}%"
        return list(self.connection.execute(
            """SELECT u.id, u.site_id, u.url, u.downloaded_at, u.archive_path,
                      COALESCE(u.article_title, json_extract(u.metadata_json, '$.title')) AS title,
                      CASE WHEN u.duplicate_group_id IS NULL THEN 0 ELSE
                        (SELECT COUNT(*)-1 FROM urls d
                         WHERE d.duplicate_group_id=u.duplicate_group_id) END AS duplicate_copies
               FROM urls u JOIN sources s ON s.id=u.site_id
               WHERE u.download_status='archived'
                 AND u.access_status='visible'
                 AND s.public_enabled=1
                 AND (u.duplicate_group_id IS NULL OR u.id=u.duplicate_group_id)
                 AND (u.url LIKE ? OR u.metadata_json LIKE ? OR u.article_text LIKE ?
                      OR u.extracted_text LIKE ?
                      OR EXISTS (SELECT 1 FROM assets a
                                 WHERE a.url_id=u.id AND a.status='archived'
                                   AND a.extracted_text LIKE ?))
               ORDER BY u.downloaded_at DESC LIMIT ?""",
            (pattern, pattern, pattern, pattern, pattern, limit),
        ))

    def mark_no_keyword(self, row_id: int, **values: object) -> None:
        self.connection.execute(
            """UPDATE urls SET download_status='no_keyword', attempts=attempts+1,
            http_status=?, final_url=?, content_type=?, keyword_match=0,
            live_status='active', last_checked_at=CURRENT_TIMESTAMP,
            archive_path=NULL, content_sha256=NULL, error=NULL WHERE id=?""",
            (values.get("http_status"), values.get("final_url"),
             values.get("content_type"), row_id),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT site_id, url, canonical_url FROM urls WHERE id=?", (row_id,)
        ).fetchone()
        self.record_bad_url(
            row["site_id"], row["url"], "no_keyword", values.get("final_url")
        )

    def mark_failed(self, row_id: int, error: str, retry: bool, http_status: int | None = None) -> None:
        status = "retry" if retry else "unavailable"
        self.connection.execute(
            """UPDATE urls SET download_status=?, attempts=attempts+1, error=?,
            http_status=COALESCE(?, http_status), live_status=?,
            last_checked_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, error[:2000], http_status,
             "temporary_error" if retry else "unavailable", row_id),
        )
        self.connection.commit()

    def mark_blocked(self, row_id: int, error: str) -> None:
        self.connection.execute(
            """UPDATE urls SET download_status='manual_capture_needed', attempts=attempts+1,
            error=?, live_status='robots_blocked', last_checked_at=CURRENT_TIMESTAMP WHERE id=?""",
            (error[:2000], row_id),
        )
        self.connection.commit()

    def mark_bad_redirect(self, row_id: int, final_url: str) -> None:
        row = self.connection.execute(
            "SELECT site_id, url FROM urls WHERE id=?", (row_id,)
        ).fetchone()
        self.record_bad_url(row["site_id"], row["url"], "external_redirect", final_url)
        self.connection.execute(
            """UPDATE urls SET download_status='bad_redirect', final_url=?,
            live_status='redirected', last_checked_at=CURRENT_TIMESTAMP,
            error='Redirected outside configured source domains' WHERE id=?""",
            (final_url, row_id),
        )
        self.connection.commit()

    def record_bad_url(self, site_id: str, url: str, reason: str,
                       final_url: object = None) -> None:
        canonical = canonicalize_url(url)
        final = str(final_url) if final_url else ""
        self.connection.execute(
            """INSERT INTO bad_urls(canonical_url, site_id, url, reason, final_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url, reason, final_url) DO UPDATE SET
              last_seen_at=CURRENT_TIMESTAMP, occurrences=occurrences+1""",
            (canonical, site_id, url, reason, final),
        )
        self.connection.commit()

    def bad_url_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM bad_urls ORDER BY last_seen_at DESC, id DESC LIMIT ?", (limit,)
        ))

    def asset_rows(self, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if status:
            return list(self.connection.execute(
                """SELECT a.*, u.site_id FROM assets a JOIN urls u ON u.id=a.url_id
                WHERE a.status=? ORDER BY a.id DESC LIMIT ?""",
                (status, limit),
            ))
        return list(self.connection.execute(
            """SELECT a.*, u.site_id FROM assets a JOIN urls u ON u.id=a.url_id
            ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        ))

    def pending_assets(self, limit: int, site_id: str | None = None,
                       kind: str | None = None) -> list[sqlite3.Row]:
        clauses = ["a.status IN ('pending', 'retry')"]
        params: list[object] = []
        if site_id:
            clauses.append("u.site_id=?")
            params.append(site_id)
        if kind:
            clauses.append("a.kind=?")
            params.append(kind)
        params.append(limit)
        return list(self.connection.execute(
            f"""SELECT a.*, u.site_id FROM assets a JOIN urls u ON u.id=a.url_id
            WHERE {' AND '.join(clauses)} ORDER BY a.id LIMIT ?""",
            params,
        ))

    def archived_asset_bytes(self) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM assets WHERE status='archived'"
        ).fetchone()[0])

    def asset_stats(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT kind, status, COUNT(*) count, COALESCE(SUM(size_bytes), 0) size_bytes
            FROM assets GROUP BY kind, status ORDER BY kind, status"""
        ))

    def requeue_assets(self, status: str, kind: str | None = None) -> int:
        params: list[object] = [status]
        kind_clause = ""
        if kind:
            kind_clause = " AND kind=?"
            params.append(kind)
        cursor = self.connection.execute(
            f"""UPDATE assets SET status='retry', error=NULL
            WHERE status=?{kind_clause}""",
            params,
        )
        self.connection.commit()
        return cursor.rowcount

    def backfill_bad_urls(self) -> int:
        added = 0
        rows = self.connection.execute(
            """SELECT site_id, url, final_url FROM urls
            WHERE download_status='no_keyword'"""
        ).fetchall()
        before = self.connection.execute("SELECT COUNT(*) FROM bad_urls").fetchone()[0]
        for row in rows:
            self.record_bad_url(row["site_id"], row["url"], "no_keyword", row["final_url"])
        after = self.connection.execute("SELECT COUNT(*) FROM bad_urls").fetchone()[0]
        return int(after - before)

    def archived_url(self, row_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT id, url, archive_path, download_status FROM urls WHERE id=?", (row_id,)
        ).fetchone()

    def reclassify_robots_blocks(self) -> int:
        cursor = self.connection.execute(
            """UPDATE urls SET download_status='manual_capture_needed',
            live_status='robots_blocked'
            WHERE download_status IN ('unavailable', 'blocked')
              AND error='Blocked by robots.txt'"""
        )
        self.connection.commit()
        return cursor.rowcount

    def robots_blocked_rows(self, site_id: str | None = None) -> list[sqlite3.Row]:
        if site_id:
            return list(self.connection.execute(
                """SELECT id, site_id, url FROM urls
                WHERE download_status='manual_capture_needed' AND site_id=? ORDER BY id""",
                (site_id,),
            ))
        return list(self.connection.execute(
            """SELECT id, site_id, url FROM urls
            WHERE download_status='manual_capture_needed' ORDER BY id"""
        ))

    def requeue_ids(self, row_ids: list[int]) -> int:
        if not row_ids:
            return 0
        placeholders = ",".join("?" for _ in row_ids)
        cursor = self.connection.execute(
            f"""UPDATE urls SET download_status='pending', attempts=0,
            live_status=NULL, last_checked_at=CURRENT_TIMESTAMP, error=NULL
            WHERE id IN ({placeholders})
              AND download_status='manual_capture_needed'""",
            row_ids,
        )
        self.connection.commit()
        return cursor.rowcount

    def requeue(self, statuses: list[str], site_id: str | None = None) -> int:
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        params: list[object] = list(statuses)
        site_clause = ""
        if site_id:
            site_clause = " AND site_id=?"
            params.append(site_id)
        cursor = self.connection.execute(
            f"""UPDATE urls SET download_status='pending', attempts=0,
            http_status=NULL, final_url=NULL, content_type=NULL, content_sha256=NULL,
            archive_path=NULL, downloaded_at=NULL, keyword_match=NULL,
            live_status=NULL, last_checked_at=NULL, error=NULL
            WHERE download_status IN ({placeholders}){site_clause}""",
            params,
        )
        self.connection.commit()
        return cursor.rowcount

    def classify_non_articles(self) -> int:
        rows = self.connection.execute(
            """SELECT u.id, u.url, s.config_json FROM urls u
            LEFT JOIN sources s ON s.id=u.site_id
            WHERE u.download_status IN ('pending', 'retry', 'manual_capture_needed')"""
        ).fetchall()
        row_ids = []
        for row in rows:
            config = json.loads(row["config_json"] or "{}")
            if is_non_article_url(row["url"], config.get("exclude_url_patterns", [])):
                row_ids.append(row["id"])
        if not row_ids:
            return 0
        placeholders = ",".join("?" for _ in row_ids)
        cursor = self.connection.execute(
            f"""UPDATE urls SET download_status='ignored_non_article',
            live_status='ignored', error='Filtered as category/comment/non-article URL'
            WHERE id IN ({placeholders})""",
            row_ids,
        )
        self.connection.commit()
        return cursor.rowcount

    def stats(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT site_id, download_status, COUNT(*) count FROM urls GROUP BY site_id, download_status"
        ))

    def counts(self) -> dict[str, int]:
        return {
            "urls": int(self.connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]),
            "discoveries": int(
                self.connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
            ),
            "sources": int(self.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]),
            "bad_urls": int(self.connection.execute("SELECT COUNT(*) FROM bad_urls").fetchone()[0]),
            "assets": int(self.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]),
            "assets_archived": int(self.connection.execute(
                "SELECT COUNT(*) FROM assets WHERE status='archived'"
            ).fetchone()[0]),
            "scan_checks": int(self.connection.execute(
                "SELECT COUNT(*) FROM scan_checks"
            ).fetchone()[0]),
        }

    def list_urls(self, status: str | None, limit: int) -> list[sqlite3.Row]:
        if status:
            return list(self.connection.execute(
                """SELECT id, site_id, url, download_status, http_status,
                archive_path, error FROM urls WHERE download_status=? ORDER BY id LIMIT ?""",
                (status, limit),
            ))
        return list(self.connection.execute(
            """SELECT id, site_id, url, download_status, http_status,
            archive_path, error FROM urls ORDER BY id LIMIT ?""",
            (limit,),
        ))

    def irrelevant_selenium_ids(self, keywords: list[str]) -> list[int]:
        rows = self.connection.execute(
            """SELECT id, url, metadata_json FROM urls
            WHERE discovered_by='selenium_internal_search' AND download_status!='downloaded'"""
        )
        irrelevant = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            searchable = " ".join(
                str(value) for value in (
                    row["url"], metadata.get("title", ""),
                    metadata.get("search_result_text", ""),
                )
            )
            if not contains_keyword(searchable, keywords):
                irrelevant.append(int(row["id"]))
        return irrelevant

    def delete_urls(self, row_ids: list[int]) -> int:
        if not row_ids:
            return 0
        placeholders = ",".join("?" for _ in row_ids)
        canonical_rows = self.connection.execute(
            f"SELECT canonical_url FROM urls WHERE id IN ({placeholders})", row_ids
        ).fetchall()
        canonicals = [row["canonical_url"] for row in canonical_rows]
        if canonicals:
            canonical_placeholders = ",".join("?" for _ in canonicals)
            self.connection.execute(
                f"DELETE FROM discoveries WHERE canonical_url IN ({canonical_placeholders})", canonicals
            )
        cursor = self.connection.execute(
            f"DELETE FROM urls WHERE id IN ({placeholders})", row_ids
        )
        self.connection.commit()
        return cursor.rowcount

    def reset_discovery(self) -> tuple[int, int]:
        """Remove all URL/discovery rows while keeping the database schema."""
        url_count = self.connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
        discovery_count = self.connection.execute(
            "SELECT COUNT(*) FROM discoveries"
        ).fetchone()[0]
        self.connection.execute("DELETE FROM discoveries")
        self.connection.execute("DELETE FROM urls")
        self.connection.execute("DELETE FROM bad_urls")
        self.connection.commit()
        return int(url_count), int(discovery_count)

    def close(self) -> None:
        self.connection.close()
