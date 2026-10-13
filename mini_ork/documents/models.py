"""Preview models returned by :func:`mini_ork.documents.extract`.

Plain frozen dataclasses of stdlib types only. Nothing here imports pandas,
pyarrow or pymupdf, so ``import mini_ork.documents`` stays cheap (the plan's
``c:0`` import smoke must not pull the heavy readers in).

A model is a *value*: the caller renders it and drops it. None of these types
holds an open file handle or a path the package will write back to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GridPreview:
    """A tabular preview (CSV/XLSX): header cells plus the leading rows.

    ``rows`` is truncated to the requested ``max_rows``; ``total_rows`` is the
    full row count so a caller can render "showing N of M". ``truncated`` is
    the explicit signal — the grid is never silently shortened.
    """

    path: str
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int
    total_columns: int
    truncated: bool = False
    sheet: str | None = None
    kind: str = "grid"


@dataclass(frozen=True)
class TextPreview:
    """Extracted text (DOCX/PPTX) as a whole plus per-block lines.

    ``blocks`` preserves the document's own paragraph/bullet boundaries so a
    caller can render structure rather than one run-on paragraph.
    """

    path: str
    text: str
    blocks: list[str] = field(default_factory=list)
    kind: str = "text"


@dataclass(frozen=True)
class RasterPage:
    """One rendered PDF page. ``page`` is 1-based, matching what a reader sees."""

    page: int
    image: bytes
    media_type: str = "image/png"
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class RasterisedPreview:
    """Rendered PDF pages plus the name of the rasteriser that produced them.

    ``rasteriser`` is recorded because host thumbnail tools (``qlmanage``,
    ``sips``) render the first page only, while ``pdftoppm``/``pymupdf`` render
    the requested page range. A caller that needs every page can check it.
    """

    path: str
    pages: list[RasterPage]
    rasteriser: str
    kind: str = "rasterised"
