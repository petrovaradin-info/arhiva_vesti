import httpx

from petrovaradin_archive.http import PoliteClient


def make_client(handler):
    client = PoliteClient(
        {"request": {"respect_robots_txt": True, "delay_seconds": 0}},
        "PetrovaradinInfoArchive/0.1",
    )
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "PetrovaradinInfoArchive/0.1"},
    )
    return client


def test_robots_is_fetched_with_configured_client_and_parsed():
    def handler(request):
        assert request.headers["user-agent"] == "PetrovaradinInfoArchive/0.1"
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin/\n")

    client = make_client(handler)
    assert client.allowed("https://example.rs/vest/petrovaradin")
    assert not client.allowed("https://example.rs/admin/petrovaradin")
    client.close()


def test_explicit_robots_403_remains_disallowed():
    client = make_client(lambda request: httpx.Response(403))
    assert not client.allowed("https://example.rs/vest/petrovaradin")
    client.close()
