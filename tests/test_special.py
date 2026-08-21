import sqlite3

from petrovaradin_archive.database import ArchiveDB
from petrovaradin_archive.special import (
    SpecialRepository,
    canonical_special_url,
    parse_pretraziva_results,
)


def page(keyword="Petrovaradin", query="petrovaradin", start=10):
    return f"""
    <div id="results"><p id="results-messages-top">Rezultati 1-10 od 3.605 pronađenih.</p>
    <ul id="results-list"><li><div><h3><a href="https://istorijskenovine.unilib.rs/view/index.html#panel:pp%7Cissue:UB_1%7Cpage:25%7Cquery:{query}">
    <span>Službeni vojni list</span>, <time datetime="1925-05-09">9. 5. 1925.</time>,
    <span>strana 25</span></a></h3><span class="link-text">[<a href="/prikaz/sluzbeni-vojni-list/1925-05-09/25">Tekst</a>]</span></div>
    <p><strong>{keyword}</strong> službeni zapis</p></li></ul>
    <a id="next-link-bottom" href="/pretraga?search={keyword}&amp;advanced=&amp;startrow={start}">Sledeći</a></div>
    """


def add_fixture_result(repo, session, keyword, script, html):
    results, _, total = parse_pretraziva_results(html)
    page_id = repo.add_page(session, keyword, script, 1, "https://pretraziva.rs/pretraga", html.encode(), "page.gz", total)
    return repo.add_result(session, page_id, keyword, script, 1, 1, results[0])


def test_parses_latin_and_cyrillic_results_and_original_source():
    for keyword, query in (("Petrovaradin", "petrovaradin"), ("Петроварадин", "петроварадин")):
        results, next_url, total = parse_pretraziva_results(page(keyword, query))
        assert len(results) == 1
        assert results[0].title == "Službeni vojni list"
        assert results[0].record_date == "1925-05-09"
        assert results[0].original_source_name == "istorijskenovine.unilib.rs"
        assert results[0].search_result_url.endswith("/prikaz/sluzbeni-vojni-list/1925-05-09/25")
        assert "startrow=10" in next_url
        assert total == 3605


def test_canonical_url_ignores_highlight_query():
    latin = "https://istorijskenovine.unilib.rs/view/index.html#panel:pp%7Cissue:UB_1%7Cpage:25%7Cquery:petrovaradin"
    cyrillic = "https://istorijskenovine.unilib.rs/view/index.html#panel:pp%7Cissue:UB_1%7Cpage:25%7Cquery:%D0%BF%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0%D1%80%D0%B0%D0%B4%D0%B8%D0%BD"
    assert canonical_special_url(latin) == canonical_special_url(cyrillic)
    other_page = latin.replace("page:25", "page:26")
    assert canonical_special_url(latin) != canonical_special_url(other_page)


def test_both_scripts_share_record_but_keep_discoveries(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    repo = SpecialRepository(db.connection)
    session = repo.start_session(1)
    first_id, first_new = add_fixture_result(repo, session, "Petrovaradin", "latin", page())
    second_id, second_new = add_fixture_result(
        repo, session, "Петроварадин", "cyrillic", page("Петроварадин", "петроварадин")
    )
    assert first_id == second_id
    assert first_new and not second_new
    assert repo.stats()["discoveries"] == 2
    assert repo.stats()["records"] == 1
    assert repo.stats()["script_overlap"] == 1


def test_pending_includes_retry_for_resume(tmp_path):
    db = ArchiveDB(tmp_path / "archive.sqlite3")
    repo = SpecialRepository(db.connection)
    session = repo.start_session(1)
    add_fixture_result(repo, session, "Petrovaradin", "latin", page())
    copy_id = repo.connection.execute("SELECT id FROM special_copies").fetchone()[0]
    repo.connection.execute(
        "UPDATE special_copies SET download_status='retry', attempts=1 WHERE id=?", (copy_id,)
    )
    repo.connection.commit()
    pending = repo.pending(10)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
