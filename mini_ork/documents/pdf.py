"""PDF → :class:`~mini_ork.documents.models.RasterisedPreview`.

Two tiers, one probe path (the same shape as ``mini-ork vault`` and the
Crucible runtime backend):

* **Host binary first** — ``qlmanage`` then ``sips`` (both ship with macOS),
  then ``pdftoppm`` (poppler) if it is on ``PATH``. No Python dependency.
* **Pinned ``pymupdf`` behind the optional extra** — the predictable tier for a
  host with no rasteriser binary; installed with ``pip install 'mini-ork[documents]'``.

When neither resolves the call **fails closed**: it raises
:class:`~mini_ork.documents.errors.RasteriserUnavailableError` naming what to
install. It never returns an empty preview, because that would render as a
blank pane — the failure mode the roadmap calls out by name.

Read-only with one honest caveat: ``qlmanage``/``sips``/``pdftoppm`` rasterise
by writing image files, so each invocation owns a :class:`tempfile.TemporaryDirectory`
that is cleaned up on return. The *input document* is only ever read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from mini_ork.documents.errors import DocumentExtractionError, RasteriserUnavailableError
from mini_ork.documents.models import RasterisedPreview, RasterPage

DEFAULT_MAX_PAGES = 10
DEFAULT_DPI = 100

#: Host rasterisers, probed in this order. ``qlmanage``/``sips`` ship with
#: macOS; ``pdftoppm`` comes from poppler (``brew install poppler``). Order is
#: the roadmap's (macOS-native first) and is pinned by a unit test.
HOST_RASTERISERS = ("qlmanage", "sips", "pdftoppm")

#: Name reported when the optional pymupdf extra supplies the rasteriser.
PYMUPDF = "pymupdf"

_INSTALL_HINT = (
    "install a host rasteriser (macOS already ships 'qlmanage' and 'sips'; on "
    "Linux 'brew install poppler' or 'apt-get install poppler-utils' provides "
    "'pdftoppm'), or the optional Python extra with "
    "pip install 'mini-ork[documents]' (pymupdf)"
)


def extract_pdf(path: str, *, max_pages: int = DEFAULT_MAX_PAGES) -> RasterisedPreview:
    """Rasterise a PDF's pages, or raise if no rasteriser can be resolved."""
    name = resolve_rasteriser()
    if name == PYMUPDF:
        pages = _rasterise_pymupdf(path, max_pages=max_pages)
    else:
        pages = _rasterise_host(name, path, max_pages=max_pages)
    return RasterisedPreview(path=str(path), pages=pages, rasteriser=name)


# ── resolution (probe, then advertise only what actually runs) ──────────────


def _host_binary(name: str) -> str | None:
    """Indirection over :func:`shutil.which` so tests can mask the host."""
    return shutil.which(name)


def _load_pymupdf() -> object | None:
    """Import PyMuPDF, or ``None`` when the optional extra is absent."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None
    return fitz


def host_rasteriser() -> str:
    """First host rasteriser on ``PATH``, or ``""``. Order pinned by tests."""
    for candidate in HOST_RASTERISERS:
        if _host_binary(candidate):
            return candidate
    return ""


def available_rasteriser() -> str:
    """The rasteriser that will be used, or ``""`` when none is usable.

    A host binary is preferred; pymupdf is the fallback tier. A caller that
    only wants to *know* whether rasterisation is possible uses this rather
    than catching the error from :func:`extract_pdf`.
    """
    host = host_rasteriser()
    if host:
        return host
    if _load_pymupdf() is not None:
        return PYMUPDF
    return ""


def resolve_rasteriser() -> str:
    """Like :func:`available_rasteriser` but fails closed instead of returning ``""``."""
    name = available_rasteriser()
    if not name:
        raise RasteriserUnavailableError(
            f"no PDF rasteriser available. To preview PDFs, {_INSTALL_HINT}. "
            "Refusing to return an empty preview."
        )
    return name


# ── host-binary rasterisers ─────────────────────────────────────────────────


def _rasterise_host(name: str, path: str, *, max_pages: int) -> list[RasterPage]:
    builders: dict[str, Callable[[str, int], list[RasterPage]]] = {
        "pdftoppm": _rasterise_pdftoppm,
        "sips": _rasterise_sips,
        "qlmanage": _rasterise_qlmanage,
    }
    build = builders.get(name)
    if build is None:
        raise RasteriserUnavailableError(f"unknown host rasteriser {name!r}")
    return build(path, max_pages)


def _run(argv: list[str], *, rasteriser: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentExtractionError(f"{rasteriser} could not be run: {exc}") from exc


def _read_png(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _rasterise_pdftoppm(path: str, max_pages: int) -> list[RasterPage]:
    """Every page up to ``max_pages`` — the only host tier that is multi-page."""
    binary = _host_binary("pdftoppm")
    if not binary:
        raise RasteriserUnavailableError(
            f"'pdftoppm' is not on PATH. To preview PDFs, {_INSTALL_HINT}."
        )
    with tempfile.TemporaryDirectory(prefix="mini-ork-pdf-") as scratch:
        prefix = os.path.join(scratch, "page")
        argv = [binary, "-png", "-r", str(DEFAULT_DPI)]
        if max_pages and max_pages > 0:
            argv += ["-f", "1", "-l", str(max_pages)]
        argv += ["--", os.fspath(path), prefix]
        proc = _run(argv, rasteriser="pdftoppm")
        if proc.returncode != 0:
            raise DocumentExtractionError(
                f"pdftoppm failed on {path!r}: {proc.stderr.strip()[:400] or 'no diagnostics'}"
            )
        produced = sorted(n for n in os.listdir(scratch) if n.lower().endswith(".png"))
        if not produced:
            raise DocumentExtractionError(
                f"pdftoppm produced no page images for {path!r} (is it a valid PDF?)"
            )
        return [
            RasterPage(page=index, image=_read_png(os.path.join(scratch, name)))
            for index, name in enumerate(produced, start=1)
        ]


def _rasterise_sips(path: str, max_pages: int) -> list[RasterPage]:
    """First page only — ``sips`` converts a PDF's leading page to an image."""
    binary = _host_binary("sips")
    if not binary:
        raise RasteriserUnavailableError(
            f"'sips' is not on PATH. To preview PDFs, {_INSTALL_HINT}."
        )
    with tempfile.TemporaryDirectory(prefix="mini-ork-pdf-") as scratch:
        out = os.path.join(scratch, "page-1.png")
        proc = _run([binary, "-s", "format", "png", os.fspath(path), "--out", out], rasteriser="sips")
        if proc.returncode != 0 or not os.path.isfile(out):
            raise DocumentExtractionError(
                f"sips failed on {path!r}: {proc.stderr.strip()[:400] or 'no page image produced'}"
            )
        return [RasterPage(page=1, image=_read_png(out))]


def _rasterise_qlmanage(path: str, max_pages: int) -> list[RasterPage]:
    """First page only — ``qlmanage -t`` renders a thumbnail of the preview."""
    binary = _host_binary("qlmanage")
    if not binary:
        raise RasteriserUnavailableError(
            f"'qlmanage' is not on PATH. To preview PDFs, {_INSTALL_HINT}."
        )
    with tempfile.TemporaryDirectory(prefix="mini-ork-pdf-") as scratch:
        proc = _run([binary, "-t", "-s", "1024", "-o", scratch, os.fspath(path)], rasteriser="qlmanage")
        produced = sorted(n for n in os.listdir(scratch) if n.lower().endswith(".png"))
        if not produced:
            raise DocumentExtractionError(
                f"qlmanage produced no thumbnail for {path!r}: "
                f"{proc.stderr.strip()[:400] or 'no diagnostics'}"
            )
        return [RasterPage(page=1, image=_read_png(os.path.join(scratch, produced[0])))]


# ── optional pymupdf tier ───────────────────────────────────────────────────


def _rasterise_pymupdf(path: str, max_pages: int) -> list[RasterPage]:
    fitz = _load_pymupdf()
    if fitz is None:
        raise RasteriserUnavailableError(
            f"the pymupdf rasteriser is not installed. To preview PDFs, {_INSTALL_HINT}."
        )
    pages: list[RasterPage] = []
    try:
        document = fitz.open(os.fspath(path))
    except Exception as exc:
        raise DocumentExtractionError(f"pymupdf could not open {path!r}: {exc}") from exc
    try:
        for index, page in enumerate(document, start=1):
            if max_pages and max_pages > 0 and index > max_pages:
                break
            pixmap = page.get_pixmap(dpi=DEFAULT_DPI)
            pages.append(
                RasterPage(
                    page=index,
                    image=pixmap.tobytes("png"),
                    width=pixmap.width,
                    height=pixmap.height,
                )
            )
    finally:
        document.close()
    if not pages:
        raise DocumentExtractionError(f"pymupdf rendered no pages for {path!r}")
    return pages
