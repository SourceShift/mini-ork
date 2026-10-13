"""Typed errors for :mod:`mini_ork.documents`.

Extraction is a *preview* surface: a caller renders whatever it gets. So every
failure is explicit and typed, and every message carries the remediation —
there is no code path that returns an empty model for a file it could not read.
That is the "fail closed and loudly" requirement, expressed in the type system.
"""
from __future__ import annotations

from collections.abc import Iterable


class DocumentExtractionError(RuntimeError):
    """Base class for every failure raised by :mod:`mini_ork.documents`."""


class UnsupportedFormatError(DocumentExtractionError):
    """The path's format has no extractor. Names the formats that *do*."""

    def __init__(self, path: str, supported: Iterable[str]) -> None:
        self.path = path
        self.supported = tuple(sorted(supported))
        super().__init__(
            f"no preview extractor for {path!r}; supported formats: "
            + ", ".join(self.supported)
        )


class CorruptDocumentError(DocumentExtractionError):
    """The bytes could not be parsed as the format the extension claims.

    Raised instead of returning a partially-populated model, so a corrupt file
    surfaces as an error rather than a preview that silently drops content.
    """


class RasteriserUnavailableError(DocumentExtractionError):
    """No PDF rasteriser could be resolved.

    Fail closed: neither a host binary (``qlmanage``/``sips``/``pdftoppm``) nor
    the optional ``pymupdf`` extra is available, so the page images simply do
    not exist. Returning an empty :class:`~mini_ork.documents.models.RasterisedPreview`
    here would render as a blank pane, which is exactly what this class exists
    to prevent. The message names what to install.
    """
