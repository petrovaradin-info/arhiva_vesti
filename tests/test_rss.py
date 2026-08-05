from types import SimpleNamespace

from petrovaradin_archive.providers.rss import RSSProvider


class FakeClient:
    def __init__(self, xml: bytes):
        self.xml = xml

    def allowed(self, url):
        return True

    def get(self, url):
        return SimpleNamespace(
            content=self.xml, url=url, raise_for_status=lambda: None,
        )


def test_rss_keeps_keyword_derivatives():
    xml = b"""<rss><channel><item><title>Petrovaradinski turnir</title>
    <link>https://example.rs/vest</link><description>Sport</description></item>
    <item><title>Druga vest</title><link>https://example.rs/drugo</link></item>
    </channel></rss>"""
    provider = RSSProvider(FakeClient(xml), ["Petrovaradin"])
    items = list(provider.discover({
        "id": "example", "domains": ["example.rs"],
        "feeds": ["https://example.rs/rss"], "rss_auto_discover": False,
    }))
    assert [item.url for item in items] == ["https://example.rs/vest"]
