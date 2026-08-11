"""Ephemeral document-body extraction for reviews.

Hard rules (AGENTS.md): the body exists only in worker memory for the
duration of one review — never written to disk, database, logs or error
messages. Everything returned to callers is plain extracted text; callers
persist only derived, structured results.

Office formats are unpacked with the standard library (a .docx/.xlsx/.pptx
is a zip of XML), PDF uses pypdf when available, plain-text families are
decoded directly. Legacy .doc and unknown formats return empty text and the
review stays at the metadata_only scope.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

from .config import Settings
from .db import Document
from .integrations import DingtalkClient, IntegrationError

MAX_CHARS = 60000
PLAIN_TEXT_EXTENSIONS = {"txt", "md", "markdown", "csv", "html", "htm", "json", "xml", "log"}
OFFICE_EXTENSIONS = {"docx", "xlsx", "pptx"}
EXTRACTABLE_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | OFFICE_EXTENSIONS | {"pdf"}


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="ignore")


def _strip_tags(xml_text: str) -> str:
    return re.sub(r"<[^>]+>", "", xml_text)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with archive.open("word/document.xml") as handle:
            xml_text = handle.read().decode("utf-8", errors="ignore")
    # Paragraph ends become newlines so structure checks (headings, paragraph
    # length) see the document the way a reader does.
    xml_text = xml_text.replace("</w:p>", "</w:p>\n")
    return _strip_tags(xml_text)


def _xlsx_text(data: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    parts.append(node.text)
        for name in sorted(names):
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                root = ElementTree.fromstring(archive.read(name))
                for node in root.iter():
                    if node.tag.endswith("}t") and node.text:
                        parts.append(node.text)
    return "\n".join(parts)


def _pptx_text(data: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                root = ElementTree.fromstring(archive.read(name))
                for node in root.iter():
                    if node.tag.endswith("}t") and node.text:
                        parts.append(node.text)
                parts.append("")
    return "\n".join(parts)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages[:100]]
        return "\n".join(pages)
    except Exception:
        return ""  # encrypted/corrupt/scanned PDFs degrade to metadata scope


def extract_text(extension: str, data: bytes) -> str:
    ext = (extension or "").lower()
    try:
        if ext in PLAIN_TEXT_EXTENSIONS:
            text = _decode(data)
        elif ext == "docx":
            text = _docx_text(data)
        elif ext == "xlsx":
            text = _xlsx_text(data)
        elif ext == "pptx":
            text = _pptx_text(data)
        elif ext == "pdf":
            text = _pdf_text(data)
        else:
            return ""
    except Exception:
        return ""  # malformed archives degrade to metadata scope, never crash a review
    return text[:MAX_CHARS].strip()


async def fetch_document_content(settings: Settings, doc: Document) -> tuple[str, str]:
    """Return (text, source). Source names the channel for the review record;
    the text itself is the caller's ephemeral copy and must not be persisted."""
    if doc.is_folder or (doc.extension or "").lower() not in EXTRACTABLE_EXTENSIONS:
        return "", "unsupported"
    if not settings.content_extract_enabled or not settings.wiki_storage_space_id:
        return "", "disabled"
    if not doc.storage_dentry_id:
        # The numeric key arrives with the file's audit event; stock files
        # that predate the trail stay at metadata scope until touched.
        return "", "no_numeric_id"
    if doc.size and doc.size > settings.content_max_bytes:
        return "", "too_large"
    client = DingtalkClient(settings)
    data = await client.download_file_bytes(settings.wiki_storage_space_id, doc.storage_dentry_id)
    if not data:
        return "", "empty_download"
    text = extract_text(doc.extension, data)
    return text, ("storage_download" if text else "extract_empty")
