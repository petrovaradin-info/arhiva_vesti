from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.models import DiscoveredURL


def test_database_deduplicates_canonical_urls(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    first = DiscoveredURL("https://021.rs/vest/", "021", "sitemap")
    second = DiscoveredURL("https://021.rs/vest?utm_source=x", "021", "google_api")
    assert db.add(first)
    db.add(second)
    count = db.connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    discoveries = db.connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
    assert count == 1
    assert discoveries == 2


def test_verification_statuses_are_distinct(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.add(DiscoveredURL("https://021.rs/relevantna", "021", "selenium_internal_search"))
    row_id = db.connection.execute("SELECT id FROM urls").fetchone()[0]
    db.mark_no_keyword(
        row_id, http_status=200, final_url="https://021.rs/relevantna", content_type="text/html"
    )
    row = db.connection.execute("SELECT * FROM urls WHERE id=?", (row_id,)).fetchone()
    assert row["download_status"] == "no_keyword"
    assert row["keyword_match"] == 0
    assert row["archive_path"] is None


def test_source_flags_and_blocklist_are_stored_in_database(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.sync_sources([{
        "id": "tabloid", "name": "Tabloid", "domains": ["example.rs"],
        "enabled": True, "manual_review": True,
        "blocklist": ["https://example.rs/komentari/"],
        "internal_search": {"start_url": "https://example.rs/?s=petrovaradin"},
    }])
    source = db.source("tabloid")
    assert source["enabled"] == 1
    assert source["manual_review"] == 1
    assert source["public_enabled"] == 0
    db.set_source_enabled("tabloid", False)
    assert db.source("tabloid")["enabled"] == 0
    values = db.update_source_blocklist("tabloid", "https://example.rs/tag/")
    assert "https://example.rs/tag/" in values


def test_manual_review_source_waits_for_approval(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.sync_sources([{
        "id": "tabloid", "name": "Tabloid", "domains": ["example.rs"],
        "enabled": True, "manual_review": True,
    }])
    db.add(DiscoveredURL("https://example.rs/vest", "tabloid", "selenium_internal_search"))
    row_id = db.connection.execute("SELECT id FROM urls").fetchone()[0]
    db.mark_archived(
        row_id, http_status=200, final_url="https://example.rs/vest",
        content_type="text/html", content_sha256="abc", archive_path="snapshot.html.gz",
    )
    assert db.connection.execute(
        "SELECT download_status FROM urls WHERE id=?", (row_id,)
    ).fetchone()[0] == "awaiting_review"
    assert db.review(row_id, approved=True)
    assert db.connection.execute(
        "SELECT download_status FROM urls WHERE id=?", (row_id,)
    ).fetchone()[0] == "archived"


def test_public_search_hides_non_public_source(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.sync_sources([{
        "id": "hidden", "name": "Hidden", "domains": ["example.rs"],
        "enabled": True, "manual_review": True, "public_enabled": False,
    }])
    db.add(DiscoveredURL("https://example.rs/petrovaradin", "hidden", "selenium_internal_search"))
    row_id = db.connection.execute("SELECT id FROM urls").fetchone()[0]
    db.mark_archived(
        row_id, http_status=200, final_url="https://example.rs/petrovaradin",
        content_type="text/html", content_sha256="abc", archive_path="snapshot.html.gz",
    )
    assert db.review(row_id, approved=True)
    assert db.public_search("petrovaradin") == []
    db.set_source_public("hidden", True)
    assert len(db.public_search("petrovaradin")) == 1


def test_requeue_only_allowed_robots_rows(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    for suffix in ("dozvoljen", "zabranjen"):
        db.add(DiscoveredURL(
            f"https://example.rs/{suffix}", "example", "selenium_internal_search"
        ))
    rows = db.connection.execute("SELECT id FROM urls ORDER BY id").fetchall()
    for row in rows:
        db.mark_blocked(row["id"], "Blocked by robots.txt")

    assert len(db.robots_blocked_rows()) == 2
    assert db.requeue_ids([rows[0]["id"]]) == 1
    statuses = [
        row[0] for row in db.connection.execute(
            "SELECT download_status FROM urls ORDER BY id"
        )
    ]
    assert statuses == ["pending", "manual_capture_needed"]


def test_public_search_finds_text_inside_archived_document(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.sync_sources([{"id": "city", "name": "City", "domains": ["example.rs"]}])
    db.add(DiscoveredURL("https://example.rs/plan.pdf", "city", "sitemap"))
    row_id = db.connection.execute("SELECT id FROM urls").fetchone()[0]
    db.mark_archived(
        row_id, http_status=200, final_url="https://example.rs/plan.pdf",
        content_type="application/pdf", content_sha256="abc", archive_path="plan.pdf",
        media_kind="pdf", size_bytes=100, extracted_text="Plan za Petrovaradin",
    )
    assert len(db.public_search("Petrovaradin")) == 1


def test_public_search_finds_text_inside_linked_asset(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    db.sync_sources([{"id": "city", "name": "City", "domains": ["example.rs"]}])
    db.add(DiscoveredURL("https://example.rs/vest", "city", "sitemap"))
    row_id = db.connection.execute("SELECT id FROM urls").fetchone()[0]
    db.mark_archived(
        row_id, http_status=200, final_url="https://example.rs/vest",
        content_type="text/html", content_sha256="abc", archive_path="vest.html.gz",
    )
    asset_id = db.add_asset(row_id, "https://example.rs/plan.pdf", "pdf")
    db.mark_asset_archived(
        asset_id, final_url="https://example.rs/plan.pdf", content_type="application/pdf",
        size_bytes=100, content_sha256="def", archive_path="plan.pdf",
        extracted_text="Budžet za Petrovaradin",
    )
    assert len(db.public_search("Petrovaradin")) == 1
