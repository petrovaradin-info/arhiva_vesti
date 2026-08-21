import io
import zipfile

from petrovaradin_archive.content import content_kind, decode_html, extract_text


def test_content_kind_uses_mime_and_extension():
    assert content_kind("https://example.rs/plan.pdf", "application/octet-stream") == "pdf"
    assert content_kind("https://example.rs/photo", "image/jpeg") == "image"
    assert content_kind("https://example.rs/vest", "text/html; charset=utf-8") == "html"


def test_docx_text_is_extracted_without_external_program():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='urn:w'><w:p><w:t>Petrovaradin</w:t></w:p></w:document>",
        )
    assert "Petrovaradin" in extract_text(output.getvalue(), "document", "plan.docx")


def test_html_decode_handles_legacy_serbian_encoding():

    payload = "<p>Petrovaradin i železnička stanica</p>".encode("windows-1250")
    assert "železnička" in decode_html(payload)
    assert "Petrovaradin" in extract_text(payload, "html")
