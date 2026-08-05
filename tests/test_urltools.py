from petrovaradin_archive.urltools import (
    canonicalize_url, host_matches, is_non_article_url, url_hash,
)


def test_canonicalize_removes_tracking_and_fragment():
    assert canonicalize_url("HTTPS://WWW.021.RS/a/?utm_source=x&b=2&a=1#top") == (
        "https://www.021.rs/a?a=1&b=2"
    )


def test_hash_is_stable_for_tracking_variants():
    assert url_hash("https://021.rs/vest") == url_hash("https://021.rs/vest/?utm_medium=social")


def test_host_matching_does_not_accept_lookalike_domain():
    assert host_matches("https://www.021.rs/a", ["021.rs"])
    assert not host_matches("https://021.rs.example.com/a", ["021.rs"])


def test_comment_urls_are_not_articles():
    assert is_non_article_url("https://nova.rs/vest/abc/komentari/")
    assert is_non_article_url("https://blic.rs/vest/abc?strana=komentari")
    assert not is_non_article_url("https://nova.rs/vest/petrovaradin/")
