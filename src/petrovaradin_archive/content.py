from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup


DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "odt", "rtf", "txt", "csv", "xls", "xlsx"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "tif", "tiff", "svg"}


def decode_html(content: bytes | str) -> str:
    """Decode HTML without BeautifulSoup emitting byte-decoding warnings."""
    if isinstance(content, str):
        return content
    for encoding in ("utf-8", "windows-1250", "windows-1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def url_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower().lstrip(".")


def content_kind(url: str, content_type: str = "") -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    extension = url_extension(url)
    if media_type == "application/pdf" or extension == "pdf":
        return "pdf"
    if extension in DOCUMENT_EXTENSIONS or media_type in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.oasis.opendocument.text",
        "application/rtf",
        "text/rtf",
    }:
        return "document"
    if media_type.startswith("image/") or extension in IMAGE_EXTENSIONS:
        return "image"
    if media_type.startswith("text/html") or extension in {"", "html", "htm"}:
        return "html"
    return "other"


def extract_text(content: bytes, kind: str, url: str = "") -> str:
    extension = url_extension(url)
    if kind == "html":
        return BeautifulSoup(decode_html(content), "html.parser").get_text(" ", strip=True)
    if kind == "pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content), strict=False)
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            return ""
    if extension == "docx":
        return _xml_zip_text(content, "word/document.xml")
    if extension == "odt":
        return _xml_zip_text(content, "content.xml")
    if extension in {"txt", "csv", "rtf"}:
        for encoding in ("utf-8", "utf-16", "windows-1250", "latin-1"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
    return ""


def _xml_zip_text(content: bytes, member: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read(member))
        return " ".join(text.strip() for text in root.itertext() if text.strip())
    except Exception:
        return ""


def safe_extension(url: str, content_type: str, kind: str) -> str:
    extension = url_extension(url)
    if extension and re.fullmatch(r"[a-z0-9]{1,8}", extension):
        return extension
    if kind == "pdf":
        return "pdf"
    if kind == "image":
        media_type = content_type.split(";", 1)[0].lower()
        return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                "image/gif": "gif", "image/svg+xml": "svg"}.get(media_type, "img")
    return "bin"
