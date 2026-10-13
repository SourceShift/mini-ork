"""mini_ork.documents — read-only document preview extraction.

One entry point, :func:`extract`, returns a per-format *preview model* for a
file on disk. Preview in-app; the agent does the writing — nothing in this
package writes to the input, and no extractor mutates the filesystem.

    from mini_ork.documents import extract

    preview = extract("book.xlsx")     # -> GridPreview
    preview = extract("brief.docx")    # -> TextPreview
    preview = extract("paper.pdf")     # -> RasterisedPreview

Per format:

* **CSV / TSV / XLSX** → :class:`GridPreview`, via pandas (+ the pyarrow
  engine), reading ``.tsv`` with a tab delimiter so it is not silently flattened
  to one column, with a stdlib ``zipfile`` fallback for XLSX so a host without a
  pandas Excel engine still gets a grid.
* **DOCX / PPTX** → :class:`TextPreview`, via stdlib ``zipfile`` + ``xml.etree``
  (both are ZIP+XML; a faithful read-only preview needs nothing more).
* **PDF** → :class:`RasterisedPreview`, host binary first (``qlmanage``/``sips``
  on macOS, ``pdftoppm`` if present), with pinned ``pymupdf`` behind the
  ``mini-ork[documents]`` extra. When no rasteriser resolves it *fails closed*
  with :class:`RasteriserUnavailableError` naming what to install — never a
  blank pane.

Import cost: pandas/pyarrow/pymupdf are imported lazily inside the extractors,
so ``import mini_ork.documents`` never pulls them in. The required runtime
dependency list is unchanged (PyYAML + jsonschema).
"""
from __future__ import annotations

import os

from mini_ork.documents.errors import (
    CorruptDocumentError,
    DocumentExtractionError,
    RasteriserUnavailableError,
    UnsupportedFormatError,
)
from mini_ork.documents.models import (
    GridPreview,
    RasterisedPreview,
    RasterPage,
    TextPreview,
)
from mini_ork.documents.pdf import available_rasteriser, host_rasteriser
from mini_ork.documents.tabular import DEFAULT_MAX_ROWS

DEFAULT_MAX_PAGES = 10

#: Suffix → extractor kind. The single source of truth for what is supported.
_FORMATS: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".docx": "docx",
    ".pptx": "pptx",
    ".pdf": "pdf",
}

SUPPORTED_FORMATS = tuple(sorted(_FORMATS))

#: The union of every preview model :func:`extract` can return.
Preview = GridPreview | TextPreview | RasterisedPreview

__all__ = [
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_ROWS",
    "SUPPORTED_FORMATS",
    "CorruptDocumentError",
    "DocumentExtractionError",
    "GridPreview",
    "Preview",
    "RasterPage",
    "RasterisedPreview",
    "RasteriserUnavailableError",
    "TextPreview",
    "UnsupportedFormatError",
    "available_rasteriser",
    "extract",
    "host_rasteriser",
]


def extract(
    path: str | os.PathLike[str],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_blocks: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Preview:
    """Return a preview model for ``path``, dispatching on its extension.

    ``max_rows`` bounds a grid, ``max_pages`` a rasterised PDF, and
    ``max_blocks`` a text preview (defaults applied per format). A file whose
    format has no extractor raises :class:`UnsupportedFormatError`; an
    unreadable file raises the format's typed error rather than returning an
    empty model.
    """
    target = os.fspath(path)
    suffix = os.path.splitext(target)[1].lower()
    kind = _FORMATS.get(suffix)
    if kind is None:
        raise UnsupportedFormatError(target, SUPPORTED_FORMATS)
    if not os.path.isfile(target):
        raise DocumentExtractionError(f"{target!r} is not a readable file")

    # Imported here, not at module scope: keeps `import mini_ork.documents` free
    # of pandas/pyarrow (and their transitive cost).
    if kind == "csv":
        from mini_ork.documents.tabular import extract_csv

        return extract_csv(target, max_rows=max_rows)
    if kind == "xlsx":
        from mini_ork.documents.tabular import extract_xlsx

        return extract_xlsx(target, max_rows=max_rows)
    if kind == "pdf":
        from mini_ork.documents.pdf import extract_pdf

        return extract_pdf(target, max_pages=max_pages)

    from mini_ork.documents.ooxml import (
        DEFAULT_MAX_BLOCKS,
        extract_docx,
        extract_pptx,
    )

    blocks = DEFAULT_MAX_BLOCKS if max_blocks is None else max_blocks
    if kind == "docx":
        return extract_docx(target, max_blocks=blocks)
    return extract_pptx(target, max_blocks=blocks)
