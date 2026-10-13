"""``mini_ork.ide_pages.spec`` — the ``document`` section builder.

``S.document`` turns a :func:`mini_ork.documents.extract` preview model into a
JSON-safe ``document`` section. The builder docstring is the IDE renderer's
contract, so these tests pin every documented key per kind, the grid's
showing-N-of-M facts, the text blocks, the absolute-PNG rule (and that no raw
bytes ever reach the payload) and the whole-page JSON round-trip. Fixtures are
built directly in ``tmp_path`` — no host rasteriser, no ``extract()`` — so the
suite stays offline and host-independent.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import pytest

from mini_ork.documents.models import GridPreview, RasterisedPreview, RasterPage, TextPreview
from mini_ork.ide_pages import spec as S

# Every section produced by :func:`_section` carries this fixed layout.
_LAYOUT_KEYS = ("type", "title", "note", "actions", "full")

# A stand-in for a rendered page: a tiny (fake) PNG, enough to be non-empty bytes.
_PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


def _assert_layout(section: dict) -> None:
    for key in _LAYOUT_KEYS:
        assert key in section, (key, section)
    assert isinstance(section["type"], str) and section["type"]
    assert isinstance(section["actions"], list)


def _leaves(value):
    """Every non-container value, so a recursive "no bytes" walk can see them."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _leaves(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _leaves(child)
    else:
        yield value


def _grid(path: str = "/tmp/att/table.csv") -> GridPreview:
    return GridPreview(path=path, columns=["name", "qty"],
                       rows=[["widget", 3], ["gadget", 7]],
                       total_rows=9, total_columns=2, truncated=True)


def _text(path: str = "/tmp/att/brief.docx") -> TextPreview:
    return TextPreview(path=path, text="Intro\n\n• first\n• second",
                       blocks=["Intro", "• first", "• second"])


# ── grid ──────────────────────────────────────────────────────────────────


def test_document_grid_keys() -> None:
    sec = S.document("Attached table", _grid())
    _assert_layout(sec)
    assert sec["type"] == "document"
    assert sec["kind"] == "grid"
    assert sec["path"] == "/tmp/att/table.csv"
    assert sec["columns"] == ["name", "qty"]
    assert sec["rows"] == [["widget", 3], ["gadget", 7]]
    assert sec["total_rows"] == 9 and sec["total_columns"] == 2
    assert sec["truncated"] is True
    assert "sheet" not in sec  # no sheet named -> the key is absent


def test_document_grid_sheet_only_when_set() -> None:
    assert "sheet" not in S.document("t", _grid())
    sheeted = GridPreview(path="/tmp/att/book.xlsx", columns=["a"], rows=[[1]],
                          total_rows=1, total_columns=1, sheet="Sheet1")
    assert S.document("t", sheeted)["sheet"] == "Sheet1"


def test_document_grid_keeps_truncation_facts() -> None:
    """A truncated grid is emitted whole; the N-of-M facts are never re-trimmed."""
    sec = S.document("t", _grid())
    assert len(sec["rows"]) < sec["total_rows"]
    assert sec["truncated"] is True and sec["total_rows"] == 9


def test_document_grid_coerces_cells_to_json_primitives() -> None:
    preview = GridPreview(path="/tmp/att/mixed.csv", columns=["a", "b", "c", "d"],
                          rows=[[date(2026, 10, 13), Decimal("1.50"), None, True]],
                          total_rows=1, total_columns=4)
    sec = S.document("t", preview)
    assert sec["rows"] == [[str(date(2026, 10, 13)), "1.50", None, True]]
    # A strict parser (the Rust renderer, json.dumps(allow_nan=False)) must accept it.
    assert json.dumps(sec, allow_nan=False)


@pytest.mark.parametrize("cell,expected", [
    (float("inf"), "inf"), (float("-inf"), "-inf"), (float("nan"), "nan")])
def test_document_grid_stringifies_non_finite_floats(cell: float, expected: str) -> None:
    preview = GridPreview(path="/tmp/att/nf.csv", columns=["x"], rows=[[cell]],
                          total_rows=1, total_columns=1)
    sec = S.document("t", preview)
    assert sec["rows"] == [[expected]]
    assert json.dumps(sec, allow_nan=False)  # would raise on a bare Infinity/NaN


# ── text ──────────────────────────────────────────────────────────────────


def test_document_text_keys() -> None:
    sec = S.document("Attached brief", _text())
    _assert_layout(sec)
    assert sec["type"] == "document"
    assert sec["kind"] == "text"
    assert sec["path"] == "/tmp/att/brief.docx"
    assert sec["text"] == "Intro\n\n• first\n• second"
    assert sec["blocks"] == ["Intro", "• first", "• second"]  # order + boundaries kept


def test_document_text_defaults_empty_blocks() -> None:
    sec = S.document("t", TextPreview(path="/tmp/att/empty.docx", text=""))
    assert sec["text"] == "" and sec["blocks"] == []


# ── pages ─────────────────────────────────────────────────────────────────


def test_document_pages_writes_absolute_pngs_and_no_raw_bytes(tmp_path) -> None:
    src = tmp_path / "paper.pdf"
    preview = RasterisedPreview(path=str(src), rasteriser="pdftoppm",
                                pages=[RasterPage(page=1, image=_PNG)])
    before = set(os.listdir(tmp_path))

    sec = S.document("Attached PDF", preview)

    _assert_layout(sec)
    assert sec["type"] == "document"
    assert sec["kind"] == "pages"  # RasterisedPreview, but the section is "pages"
    assert sec["path"] == str(src)
    assert sec["rasteriser"] == "pdftoppm"
    assert len(sec["pages"]) == 1

    page = sec["pages"][0]
    assert page["page"] == 1
    assert os.path.isabs(page["png"])
    assert os.path.dirname(page["png"]) == str(tmp_path)
    assert os.path.basename(page["png"]) == "paper.pdf.page-1.png"
    assert page["width"] is None and page["height"] is None  # host tools report no size
    with open(page["png"], "rb") as fh:
        assert fh.read() == _PNG  # the fixture bytes, on disk

    assert set(os.listdir(tmp_path)) - before == {"paper.pdf.page-1.png"}

    # Nothing anywhere in the section is bytes/bytearray/memoryview — paths only.
    assert not any(isinstance(v, (bytes, bytearray, memoryview)) for v in _leaves(sec))


def test_document_pages_width_height_pass_through(tmp_path) -> None:
    preview = RasterisedPreview(
        path=str(tmp_path / "paper.pdf"), rasteriser="pymupdf",
        pages=[RasterPage(page=2, image=_PNG, width=800, height=1132)])
    page = S.document("t", preview)["pages"][0]
    assert page == {"page": 2, "png": os.path.join(str(tmp_path), "paper.pdf.page-2.png"),
                    "width": 800, "height": 1132}


def test_document_pages_write_is_idempotent(tmp_path) -> None:
    preview = RasterisedPreview(path=str(tmp_path / "paper.pdf"), rasteriser="sips",
                                pages=[RasterPage(page=1, image=_PNG)])
    first = S.document("t", preview)
    png = first["pages"][0]["png"]
    mtime = os.stat(png).st_mtime_ns

    second = S.document("t", preview)  # a page rebuild
    assert second["pages"][0]["png"] == png  # same deterministic name
    assert os.stat(png).st_mtime_ns == mtime  # identical bytes were left untouched


def test_document_pages_out_dir_override(tmp_path) -> None:
    src = tmp_path / "paper.pdf"
    out = tmp_path / "cache"
    preview = RasterisedPreview(path=str(src), rasteriser="pdftoppm",
                                pages=[RasterPage(page=1, image=_PNG)])
    page = S.document("t", preview, out_dir=str(out))["pages"][0]
    assert page["png"] == str(out / "paper.pdf.page-1.png")  # out_dir created + used
    assert os.path.isfile(page["png"])


# ── dispatch + layout ─────────────────────────────────────────────────────


def test_document_unknown_preview_raises_type_error() -> None:
    with pytest.raises(TypeError):
        S.document("t", {"not": "a preview"})
    with pytest.raises(TypeError):
        S.document("t", "brief.docx")


def test_document_threads_layout_options() -> None:
    sec = S.document("t", _grid(), note="n", actions=[S.btn("Open")],
                     full=True, col=2, row_span=3)
    assert sec["note"] == "n" and sec["col"] == 2 and sec["row_span"] == 3
    assert sec["full"] is True and sec["actions"][0]["label"] == "Open"


def test_document_is_not_ide_level_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder builds regardless of ``MINI_ORK_IDE_SPEC`` (the caller decides)."""
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "1")
    assert S.document("t", _grid())["kind"] == "grid"
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    assert S.document("t", _text())["kind"] == "text"


# ── whole-page JSON round-trip ────────────────────────────────────────────


def test_json_round_trip_page_with_every_document_kind(tmp_path) -> None:
    nasty = GridPreview(
        path=str(tmp_path / "mixed.csv"), columns=["when", "amt", "flag"],
        rows=[[date(2026, 10, 13), Decimal("2.25"), None], [float("inf"), float("nan"), True]],
        total_rows=4, total_columns=3, truncated=True, sheet="Q4")
    sections = [
        S.document("grid", nasty),
        S.document("text", _text()),
        S.document("pages", RasterisedPreview(
            path=str(tmp_path / "paper.pdf"), rasteriser="pdftoppm",
            pages=[RasterPage(page=1, image=_PNG, width=800, height=1132)])),
    ]
    page = S.page("thread", "Thread", sections=sections)

    blob = json.dumps(page, allow_nan=False)  # strict: no Infinity/NaN literals
    restored = json.loads(blob)
    assert [s["type"] for s in restored["sections"]] == ["document", "document", "document"]
    assert [s["kind"] for s in restored["sections"]] == ["grid", "text", "pages"]
    assert restored == page
