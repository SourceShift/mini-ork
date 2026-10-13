"""DOCX and PPTX → :class:`~mini_ork.documents.models.TextPreview`.

Both formats are OOXML: a ZIP containing XML with the real text inside. A
faithful read-only preview needs nothing beyond ``zipfile`` + ``xml.etree``, so
this module carries no dependency at all — which is why the roadmap pins it
this way rather than reaching for ``python-docx``/``python-pptx``.

Read-only: the archive is opened through :class:`zipfile.ZipFile` and never
extracted to disk.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

from mini_ork.documents.errors import CorruptDocumentError
from mini_ork.documents.models import TextPreview

DEFAULT_MAX_BLOCKS = 500

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

_SLIDE_XML = re.compile(r"ppt/slides/slide(\d+)\.xml\Z")


def extract_docx(path: str, *, max_blocks: int = DEFAULT_MAX_BLOCKS) -> TextPreview:
    """Preview a DOCX: one block per paragraph, in document order."""
    payload = _read_part(path, "word/document.xml", "DOCX")
    root = _parse(path, payload, "word/document.xml")
    blocks = []
    for paragraph in root.iter(f"{{{_W_NS}}}p"):
        line = "".join(node.text or "" for node in paragraph.iter(f"{{{_W_NS}}}t"))
        if line.strip():
            blocks.append(line)
    return _preview(path, blocks, max_blocks)


def extract_pptx(path: str, *, max_blocks: int = DEFAULT_MAX_BLOCKS) -> TextPreview:
    """Preview a PPTX: one block per text paragraph, slides in numeric order."""
    with _open_archive(path, "PPTX") as zf:
        slides: list[tuple[int, str]] = []
        for name in zf.namelist():
            match = _SLIDE_XML.match(name)
            if match:
                slides.append((int(match.group(1)), name))
        if not slides:
            raise CorruptDocumentError(f"{path!r} contains no ppt/slides/slideN.xml parts")
        slides.sort()
        blocks = []
        for _, name in slides:
            root = _parse(path, zf.read(name), name)
            for paragraph in root.iter(f"{{{_A_NS}}}p"):
                line = "".join(node.text or "" for node in paragraph.iter(f"{{{_A_NS}}}t"))
                if line.strip():
                    blocks.append(line)
    return _preview(path, blocks, max_blocks)


def _preview(path: str, blocks: list[str], max_blocks: int) -> TextPreview:
    shown = blocks[:max_blocks] if isinstance(max_blocks, int) and max_blocks > 0 else list(blocks)
    return TextPreview(path=str(path), text="\n".join(shown), blocks=shown)


def _open_archive(path: str, label: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise CorruptDocumentError(
            f"{path!r} is not a readable {label} (not a ZIP archive)"
        ) from exc


def _read_part(path: str, part: str, label: str) -> bytes:
    with _open_archive(path, label) as zf:
        try:
            return zf.read(part)
        except KeyError as exc:
            raise CorruptDocumentError(f"{path!r} has no {part!r} part (not a {label})") from exc


def _parse(path: str, payload: bytes, part: str) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CorruptDocumentError(f"{path!r}: {part} is not parsable XML: {exc}") from exc
