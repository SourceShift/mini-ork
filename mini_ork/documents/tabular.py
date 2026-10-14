"""CSV/TSV and XLSX → :class:`~mini_ork.documents.models.GridPreview`.

Heavy readers are imported *inside* the functions so ``import mini_ork.documents``
never pays for pandas/pyarrow. Both formats have a zero-dependency stdlib
fallback, so a missing engine degrades to a *working* preview, never to an empty
grid — and when even that fails the error is explicit. An ``.xlsx`` is a ZIP of
XML, readable with nothing but ``zipfile`` + ``xml.etree``; delimited text has
the stdlib ``csv`` reader, chosen for the same explicit delimiter so the two
tiers agree on the dialect.

Delimited text is read with an explicit delimiter decided from the suffix
(``.tsv`` → tab), never left to pandas' comma default: reading a tab-separated
file as one column is the silent-wrongness this package exists to prevent.

Nothing here writes: both readers open the input read-only.
"""
from __future__ import annotations

import importlib.util
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from mini_ork.documents.errors import CorruptDocumentError
from mini_ork.documents.models import GridPreview

DEFAULT_MAX_ROWS = 50

#: Excel engines pandas can drive, most-preferred first. None of these are
#: mini-ork dependencies; their absence is handled, not assumed.
_EXCEL_ENGINES = ("calamine", "openpyxl")

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_SHEET_XML = re.compile(r"xl/worksheets/sheet(\d+)\.xml\Z")


# ── public extractors ───────────────────────────────────────────────────────


def extract_csv(path: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> GridPreview:
    """Preview a CSV *or* TSV. Read via pandas, else the stdlib ``csv`` reader.

    The delimiter is chosen from the suffix before the read — ``.tsv`` splits on
    tabs — so a tab-separated file is never silently returned as one column.
    """
    columns, rows = _read_csv(path)
    return _grid(path, columns, rows, max_rows=max_rows)


def extract_xlsx(path: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> GridPreview:
    """Preview the first worksheet of an XLSX.

    Tries the pandas + engine path first; when no Excel engine is importable it
    falls back to the stdlib OOXML reader so the preview still works.
    """
    table = _read_xlsx_via_pandas(path)
    if table is None:
        table = _read_xlsx_via_stdlib(path)
    columns, rows, sheet = table
    return _grid(path, columns, rows, max_rows=max_rows, sheet=sheet)


# ── shared shaping ──────────────────────────────────────────────────────────


def _grid(
    path: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    max_rows: int,
    sheet: str | None = None,
) -> GridPreview:
    total_rows = len(rows)
    shown = rows[:max_rows] if isinstance(max_rows, int) and max_rows > 0 else list(rows)
    return GridPreview(
        path=str(path),
        columns=list(columns),
        rows=[list(row) for row in shown],
        total_rows=total_rows,
        total_columns=len(columns),
        truncated=len(shown) < total_rows,
        sheet=sheet,
    )


def _cell(value: Any) -> Any:
    """Coerce a pandas/numpy scalar to a plain, JSON-renderable Python value.

    NaN/NaT/NA become ``None`` rather than the float NaN, whose ``repr`` would
    leak into a preview as ``nan``.
    """
    if value is None:
        return None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except Exception:
            pass
    try:
        if value != value:  # NaN / NaT (pd.NA raises here, hence the try)
            return None
    except Exception:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


# ── CSV ─────────────────────────────────────────────────────────────────────


def _read_csv(path: str) -> tuple[list[str], list[list[Any]]]:
    """Read delimited text, pandas first, then the stdlib ``csv`` reader.

    Two interchangeable tiers, the same shape as the XLSX readers: a host with
    no pandas still gets a *working* grid from ``_read_csv_via_stdlib``. The
    delimiter is decided once, before either tier, so the tiers can never
    disagree on the dialect.
    """
    separator = _separator(path)
    table = _read_csv_via_pandas(path, separator)
    if table is None:
        table = _read_csv_via_stdlib(path, separator)
    return table


def _read_csv_via_pandas(
    path: str, separator: str
) -> tuple[list[str], list[list[Any]]] | None:
    """Read with pandas, or ``None`` when pandas is not importable.

    Returning ``None`` (rather than raising ``ImportError``) is deliberate: a
    bare interpreter with no pandas must still get a preview, from the stdlib
    reader. A *parse* error from the C engine is not swallowed — it propagates
    exactly as before, since pandas being present means the caller asked for
    the pandas dialect.
    """
    try:
        import pandas as pd
    except Exception:
        return None
    # ``read_csv``'s stubs admit a dict return in the multi-``sep`` overloads;
    # this call site never hits those, so bind to Any and read the frame.
    frame: Any = None
    try:
        frame = pd.read_csv(path, sep=separator, engine="pyarrow")
    except Exception:
        # pyarrow engine missing or it cannot parse this dialect — the default
        # C engine is the compatibility path and re-raises genuine parse errors.
        frame = None
    if frame is None:
        frame = pd.read_csv(path, sep=separator)
    columns = [str(c) for c in frame.columns]
    rows = [[_cell(v) for v in rec] for rec in frame.itertuples(index=False, name=None)]
    return columns, rows


def _read_csv_via_stdlib(path: str, separator: str) -> tuple[list[str], list[list[Any]]]:
    """Read sheet-less delimited text with the stdlib ``csv`` module only.

    The zero-dependency tier. The delimiter is the same one the pandas tier
    uses, so a tab-separated file is never flattened to one column, and a
    quoted comma inside a field is handled by the reader's own quoting rules
    rather than by a count heuristic. The first row is the header, matching the
    pandas tier.
    """
    import csv

    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            records = list(csv.reader(handle, delimiter=separator))
    except OSError as exc:
        raise CorruptDocumentError(f"{path!r} could not be read: {exc}") from exc
    if not records:
        return [], []
    header = [str(c) for c in records[0]]
    rows = [[_coerce_text_cell(v) for v in rec] for rec in records[1:]]
    return header, rows


def _coerce_text_cell(value: str) -> Any:
    """A stdlib-read field coerced the way pandas would have read it.

    Empty fields become ``None`` (pandas' NaN) and numeric-looking fields become
    ``int``/``float``, so the stdlib and pandas tiers are interchangeable — the
    ``qty`` column of ``alpha,3`` reads as ``3`` either way. Genuinely textual
    fields are returned verbatim; only the syntax pandas itself would interpret
    is reinterpreted here.
    """
    if value == "":
        return None
    return _cell(_coerce_number(value))


def _separator(path: str) -> str:
    """Delimiter for a delimited-text file, decided *before* pandas reads it.

    The suffix is authoritative — ``.tsv`` means tab — because both reader
    tiers (pyarrow, then the C engine) must agree on the dialect. Sniffing only
    rescues what the suffix cannot: a tab-separated file that was named
    ``.csv``. It never overrides a header that reads as comma-separated, so a
    real CSV is safe.
    """
    suffix = os.path.splitext(os.fspath(path))[1].lower()
    if suffix == ".tsv":
        return "\t"
    sniffed = _sniff_tab(path)
    return "\t" if sniffed else ","


def _sniff_tab(path: str) -> bool:
    """``True`` when a non-``.tsv`` file's header reads as tab-separated.

    Decided from the first non-empty line — the header — by comparing tab and
    comma counts, and choosing tab **only when tabs strictly outnumber commas**.
    Anchoring on the header's counts (rather than an ``any comma`` test on a
    whole sample) is what keeps a real comma CSV on the comma dialect while
    still rescuing a tab-separated file misnamed ``.csv`` *even when one of its
    fields quotes a comma* — otherwise such a file is flattened to a single
    column, the silent wrongness this module exists to prevent. Richer
    delimiters (``;``, ``|``) are out of contract and left to the declared
    suffix.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return line.count("\t") > line.count(",")
    except OSError:
        return False
    return False


# ── XLSX: pandas path ───────────────────────────────────────────────────────


def _read_xlsx_via_pandas(path: str) -> tuple[list[str], list[list[Any]], str | None] | None:
    """Read sheet 0 with pandas, or ``None`` when no engine is importable.

    Returning ``None`` (rather than raising ``ImportError``) is deliberate: a
    bare interpreter with no Excel engine must still get a preview, from the
    stdlib reader below.
    """
    try:
        import pandas as pd
    except Exception:
        return None
    engine = _excel_engine()
    if engine is None:
        return None
    try:
        with pd.ExcelFile(path, engine=engine) as book:
            sheet = str(book.sheet_names[0])
            frame = book.parse(sheet)
    except ImportError:
        return None
    columns = [str(c) for c in frame.columns]
    rows = [[_cell(v) for v in rec] for rec in frame.itertuples(index=False, name=None)]
    return columns, rows, sheet


def _excel_engine() -> str | None:
    """Name of an importable pandas Excel engine, or ``None``."""
    for module in _EXCEL_ENGINES:
        try:
            if importlib.util.find_spec(module) is not None:
                return module
        except (ImportError, ValueError):
            continue
    return None


# ── XLSX: stdlib path ───────────────────────────────────────────────────────


def _read_xlsx_via_stdlib(path: str) -> tuple[list[str], list[list[Any]], str | None]:
    """Read sheet 0 of an XLSX with ``zipfile`` + ``xml.etree`` only.

    The OOXML spreadsheet format is a ZIP of XML; this mirrors what the pandas
    path returns (first row is the header) so the two tiers are interchangeable.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _shared_strings(zf)
            sheet_name, sheet_path = _first_sheet(zf)
            try:
                payload = zf.read(sheet_path)
            except KeyError as exc:
                raise CorruptDocumentError(
                    f"{path!r} has no worksheet part at {sheet_path!r}"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise CorruptDocumentError(
            f"{path!r} is not a readable XLSX (not a ZIP archive)"
        ) from exc

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CorruptDocumentError(f"{path!r} has unparsable sheet XML: {exc}") from exc

    cells_per_row: list[dict[int, Any]] = []
    for row in root.iter(f"{{{_SHEET_NS}}}row"):
        cells = {
            _col_index(cell.get("r") or ""): _xlsx_value(cell, shared)
            for cell in row.iter(f"{{{_SHEET_NS}}}c")
        }
        if cells:
            cells_per_row.append(cells)

    width = max((max(c) + 1 for c in cells_per_row if c), default=0)
    matrix = [[cells.get(i) for i in range(width)] for cells in cells_per_row]
    if not matrix:
        return [], [], sheet_name
    header = ["" if v is None else str(v) for v in matrix[0]]
    return header, matrix[1:], sheet_name


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        payload = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(payload)
    return [
        "".join(t.text or "" for t in si.iter(f"{{{_SHEET_NS}}}t"))
        for si in root.iter(f"{{{_SHEET_NS}}}si")
    ]


def _first_sheet(zf: zipfile.ZipFile) -> tuple[str, str]:
    candidates: list[tuple[int, str]] = []
    for name in zf.namelist():
        match = _SHEET_XML.match(name)
        if match:
            candidates.append((int(match.group(1)), name))
    if not candidates:
        raise CorruptDocumentError("XLSX workbook contains no worksheet parts")
    candidates.sort()
    sheet_path = candidates[0][1]
    return _workbook_sheet_name(zf) or "Sheet1", sheet_path


def _workbook_sheet_name(zf: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(zf.read("xl/workbook.xml"))
    except (KeyError, ET.ParseError):
        return ""
    for sheet in root.iter(f"{{{_SHEET_NS}}}sheet"):
        name = sheet.get("name")
        if name:
            return name
    return ""


def _xlsx_value(cell: ET.Element, shared: list[str]) -> Any:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(f"{{{_SHEET_NS}}}is")
        if inline is None:
            return None
        return "".join(t.text or "" for t in inline.iter(f"{{{_SHEET_NS}}}t"))
    value = cell.find(f"{{{_SHEET_NS}}}v")
    if value is None or value.text is None:
        return None
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return None
    if kind == "b":
        return value.text == "1"
    if kind == "str":
        return value.text
    return _coerce_number(value.text)


def _coerce_number(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _col_index(ref: str) -> int:
    """``"B3"`` → ``1``. Returns 0 for a missing/garbled ref."""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    if not letters:
        return 0
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1
