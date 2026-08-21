from petrovaradin_archive.article_adapters import (
    content_fingerprint, extract_article, hamming_distance, simhash,
)
from petrovaradin_archive.providers.selenium_search import SeleniumInternalSearchProvider


def test_wordpress_adapter_extracts_article_and_original_source():
    html = """
    <html><head>
      <meta property="og:title" content="Vest iz Petrovaradina">
      <meta property="article:published_time" content="2025-02-01T10:00:00+01:00">
      <link rel="canonical" href="/vest/petrovaradin">
    </head><body><article><div class="entry-content">
      <p>Petrovaradin je tema ovog dovoljno dugog probnog teksta.</p>
      <p>Drugi pasus članka koji treba da ostane u izdvojenom tekstu.</p>
      <p>Izvor: <a href="https://original.example/vest">Originalni portal</a></p>
      <aside>Preporučujemo neku drugu vest.</aside>
    </div></article></body></html>
    """
    article = extract_article(html, "https://kopija.example/neka-vest", "wordpress")
    assert article.title == "Vest iz Petrovaradina"
    assert article.canonical_url == "https://kopija.example/vest/petrovaradin"
    assert article.published_at == "2025-02-01T10:00:00+01:00"
    assert article.original_source_url == "https://original.example/vest"
    assert "Drugi pasus" in article.body_text
    assert "Preporučujemo" not in article.body_text


def test_fingerprints_ignore_case_spacing_and_punctuation():
    base = "Petrovaradin ima veoma važnu vest " * 12
    variant = "  PETROVARADIN, ima veoma važnu vest! " * 12
    assert content_fingerprint(base) == content_fingerprint(variant)
    assert hamming_distance(simhash(base), simhash(variant)) == 0


def test_selenium_search_builds_latin_and_cyrillic_variants():
    provider = SeleniumInternalSearchProvider({
        "keywords": ["Petrovaradin", "Петроварадин"], "selenium": {}
    })
    variants = provider._search_variants({
        "start_url": "https://example.rs/?s=petrovaradin",
        "page_url_template": "https://example.rs/page/{page}/?s=petrovaradin",
    })
    assert [url for url, _ in variants] == [
        "https://example.rs/?s=Petrovaradin",
        "https://example.rs/?s=%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0%D1%80%D0%B0%D0%B4%D0%B8%D0%BD",
    ]
    assert variants[1][1]["page_url_template"].endswith(
        "?s=%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2%D0%B0%D1%80%D0%B0%D0%B4%D0%B8%D0%BD"
    )
