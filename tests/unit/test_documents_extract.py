"""Unit tests for :mod:`mini_ork.documents` — the read-only extraction layer.

Acceptance cases from the kickoff (``kickoffs/auto/wit-2-documents.md``):

* a CSV and an XLSX both yield a grid;
* a minimal DOCX and PPTX yield their text;
* a PDF with no rasteriser available raises the documented error rather than
  returning empty;
* a missing optional dependency degrades with a clear message.

The fixtures are built here, in ``tmp_path`` — the package itself writes
nothing, which ``test_extraction_never_writes_to_the_input`` pins. The
"no rasteriser" path is forced deterministically (masked ``PATH`` + no
pymupdf), never inferred from whatever the host happens to have installed.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import zipfile

import pytest

from mini_ork.documents import (
    CorruptDocumentError,
    DocumentExtractionError,
    GridPreview,
    RasterisedPreview,
    RasteriserUnavailableError,
    TextPreview,
    UnsupportedFormatError,
    available_rasteriser,
    extract,
    host_rasteriser,
)
from mini_ork.documents import pdf as pdf_extract

# ── fixtures ────────────────────────────────────────────────────────────────

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml"
          ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml"
          ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml"
          ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
              Target="xl/workbook.xml"/>
</Relationships>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
              Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2"
              Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
              Target="sharedStrings.xml"/>
</Relationships>"""

_WB = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>"""

_SHARED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
<si><t>name</t></si><si><t>qty</t></si><si><t>alpha</t></si></sst>"""

_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1</v></c></row>
<row r="3"><c r="A3" t="inlineStr"><is><t>beta</t></is></c><c r="B3"><v>2</v></c></row>
</sheetData></worksheet>"""

_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}</w:body></w:document>"""

_SLIDE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody>{body}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"""


def _write_minimal_xlsx(path) -> None:
    """A hand-built OOXML workbook — no openpyxl needed to *create* it either."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", _WB)
        zf.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        zf.writestr("xl/sharedStrings.xml", _SHARED)
        zf.writestr("xl/worksheets/sheet1.xml", _SHEET)


def _write_docx(path, paragraphs) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", _DOCX.format(body=body))


def _write_pptx(path, slides) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for index, lines in enumerate(slides, start=1):
            body = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
            zf.writestr(f"ppt/slides/slide{index}.xml", _SLIDE.format(body=body))


def _digest(path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.fixture()
def no_rasteriser(monkeypatch):
    """Deterministically remove *every* rasteriser, host and Python alike."""
    monkeypatch.setattr(pdf_extract, "_host_binary", lambda name: None)
    monkeypatch.setattr(pdf_extract, "_load_pymupdf", lambda: None)


# ── import hygiene (success criterion c:0) ──────────────────────────────────


def test_import_pulls_no_heavy_reader():
    """`import mini_ork.documents` must stay cheap — heavy readers are lazy."""
    probe = (
        "import sys, mini_ork.documents; "
        "heavy=[m for m in ('pandas','pyarrow','numpy','fitz') if m in sys.modules]; "
        "print(','.join(heavy))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"heavy modules imported eagerly: {proc.stdout!r}"


# ── CSV / XLSX → grid ───────────────────────────────────────────────────────


def test_csv_yields_a_grid(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("name,qty\nalpha,1\nbeta,2\n")
    preview = extract(path)
    assert isinstance(preview, GridPreview)
    assert preview.kind == "grid"
    assert preview.columns == ["name", "qty"]
    assert preview.rows == [["alpha", 1], ["beta", 2]]
    assert preview.total_rows == 2
    assert preview.total_columns == 2
    assert preview.truncated is False


def test_tsv_yields_a_grid_split_on_tabs(tmp_path):
    """`.tsv` must split on tabs — a comma-default read one-columns the file."""
    path = tmp_path / "data.tsv"
    path.write_text("name\tqty\nalpha\t1\nbeta\t2\n")
    preview = extract(path)
    assert isinstance(preview, GridPreview)
    assert preview.columns == ["name", "qty"]
    assert preview.rows == [["alpha", 1], ["beta", 2]]
    assert preview.total_columns == 2


def test_tab_separated_file_named_csv_is_still_split(tmp_path):
    """A tab-separated file misnamed `.csv` is rescued, not flattened."""
    path = tmp_path / "data.csv"
    path.write_text("name\tqty\nalpha\t1\n")
    preview = extract(path)
    assert preview.columns == ["name", "qty"]
    assert preview.rows == [["alpha", 1]]


def test_tab_separated_csv_with_a_quoted_comma_stays_tab(tmp_path):
    """A comma *inside a field* must not flip a tab-separated `.csv` to comma.

    The delimiter is decided from the header's own counts, so a quoted comma in
    a data field cannot flatten the grid to one column.
    """
    path = tmp_path / "data.csv"
    path.write_text('name\tnote\nalpha\t"hello, world"\n')
    preview = extract(path)
    assert preview.columns == ["name", "note"]
    assert preview.rows == [["alpha", "hello, world"]]


def test_comma_csv_with_a_quoted_tab_stays_comma(tmp_path):
    """The rescue never fires for a real comma CSV — even one quoting a tab."""
    path = tmp_path / "data.csv"
    path.write_text('name,note\nalpha,"a\tb"\n')
    preview = extract(path)
    assert preview.columns == ["name", "note"]
    assert preview.rows == [["alpha", "a\tb"]]


def test_csv_grid_truncates_without_hiding_the_total(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("n\n" + "\n".join(str(i) for i in range(10)) + "\n")
    preview = extract(path, max_rows=3)
    assert len(preview.rows) == 3
    assert preview.total_rows == 10
    assert preview.truncated is True


def test_xlsx_yields_a_grid(tmp_path):
    path = tmp_path / "book.xlsx"
    _write_minimal_xlsx(path)
    preview = extract(path)
    assert isinstance(preview, GridPreview)
    assert preview.columns == ["name", "qty"]
    assert preview.rows == [["alpha", 1], ["beta", 2]]
    assert preview.sheet


def test_xlsx_falls_back_to_stdlib_when_no_pandas_engine(tmp_path, monkeypatch):
    """With no pandas Excel engine the stdlib OOXML reader must still yield a grid."""
    from mini_ork.documents import tabular

    path = tmp_path / "book.xlsx"
    _write_minimal_xlsx(path)
    monkeypatch.setattr(tabular, "_read_xlsx_via_pandas", lambda _path: None)
    preview = extract(path)
    assert isinstance(preview, GridPreview)
    assert preview.columns == ["name", "qty"]
    assert preview.rows == [["alpha", 1], ["beta", 2]]


# ── DOCX / PPTX → text ──────────────────────────────────────────────────────


def test_docx_yields_its_text(tmp_path):
    path = tmp_path / "brief.docx"
    _write_docx(path, ["Hello world", "Second paragraph"])
    preview = extract(path)
    assert isinstance(preview, TextPreview)
    assert preview.kind == "text"
    assert preview.blocks == ["Hello world", "Second paragraph"]
    assert preview.text == "Hello world\nSecond paragraph"


def test_pptx_yields_its_text(tmp_path):
    path = tmp_path / "deck.pptx"
    _write_pptx(path, [["Title slide"], ["Bullet one", "Bullet two"]])
    preview = extract(path)
    assert isinstance(preview, TextPreview)
    assert preview.blocks == ["Title slide", "Bullet one", "Bullet two"]


# ── PDF: fail closed, never blank ───────────────────────────────────────────


def test_pdf_without_rasteriser_fails_closed(tmp_path, monkeypatch, no_rasteriser):
    monkeypatch.setenv("PATH", str(tmp_path))  # belt: nothing on PATH either way
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n% not rasterised by this test\n")
    assert available_rasteriser() == ""
    with pytest.raises(RasteriserUnavailableError) as excinfo:
        extract(path)
    # The documented error names what to install — never a silent empty result.
    message = str(excinfo.value)
    assert "pdftoppm" in message or "poppler" in message
    assert "mini-ork[documents]" in message


def test_pdf_missing_optional_extra_message_names_the_extra(tmp_path, no_rasteriser):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RasteriserUnavailableError) as excinfo:
        extract(path)
    assert "pymupdf" in str(excinfo.value)


def test_resolve_prefers_a_host_binary_over_pymupdf(tmp_path, monkeypatch):
    """The host tier wins; only its absence falls through to the extra."""
    monkeypatch.setattr(
        pdf_extract, "_host_binary", lambda name: f"/usr/bin/{name}" if name == "pdftoppm" else None
    )
    monkeypatch.setattr(pdf_extract, "_load_pymupdf", lambda: object())
    assert available_rasteriser() == "pdftoppm"


def test_host_probe_order_is_qlmanage_then_sips_then_pdftoppm(monkeypatch):
    present = {"qlmanage", "sips", "pdftoppm"}

    def which(name):
        return f"/usr/bin/{name}" if name in present else None

    monkeypatch.setattr(pdf_extract, "_host_binary", which)
    assert host_rasteriser() == "qlmanage"
    present.discard("qlmanage")
    assert host_rasteriser() == "sips"
    present.discard("sips")
    assert host_rasteriser() == "pdftoppm"
    present.discard("pdftoppm")
    assert host_rasteriser() == ""


def test_pdf_rasterises_when_a_rasteriser_is_available(tmp_path, monkeypatch):
    """End-to-end rasterisation via the pymupdf tier (skipped when absent)."""
    fitz = pytest.importorskip("fitz")
    monkeypatch.setattr(pdf_extract, "_host_binary", lambda name: None)  # force the extra tier
    path = tmp_path / "paper.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(str(path))
    document.close()

    preview = extract(path)
    assert isinstance(preview, RasterisedPreview)
    assert preview.rasteriser == "pymupdf"
    assert [page.page for page in preview.pages] == [1, 2]
    assert preview.pages[0].image.startswith(b"\x89PNG")

    bounded = extract(path, max_pages=1)
    assert len(bounded.pages) == 1


# ── error surfaces & read-only guarantee ────────────────────────────────────


def test_unsupported_format_names_what_is_supported(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("plain")
    with pytest.raises(UnsupportedFormatError) as excinfo:
        extract(path)
    assert ".csv" in str(excinfo.value)
    assert ".pdf" in str(excinfo.value)


def test_missing_file_raises_rather_than_returning_empty(tmp_path):
    with pytest.raises(DocumentExtractionError):
        extract(tmp_path / "absent.csv")


def test_corrupt_docx_raises_rather_than_returning_empty(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(CorruptDocumentError):
        extract(path)


def test_extraction_never_writes_to_the_input(tmp_path):
    """Extraction is read-only: bytes and mtime of the input are untouched."""
    csv = tmp_path / "data.csv"
    csv.write_text("name,qty\nalpha,1\n")
    docx = tmp_path / "brief.docx"
    _write_docx(docx, ["Hello"])

    for path in (csv, docx):
        before = (_digest(path), os.stat(path).st_mtime_ns)
        extract(path)
        after = (_digest(path), os.stat(path).st_mtime_ns)
        assert before == after, f"{path.name} was modified by extraction"


def test_supported_formats_are_advertised():
    from mini_ork.documents import SUPPORTED_FORMATS

    assert set(SUPPORTED_FORMATS) == {".csv", ".tsv", ".xlsx", ".xlsm", ".docx", ".pptx", ".pdf"}
    # Guard against a docstring drifting away from the code it documents.
    assert textwrap.dedent(extract.__doc__ or "").strip()
