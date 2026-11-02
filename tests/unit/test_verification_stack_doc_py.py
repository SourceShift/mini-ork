"""Pin the verification-stack doc's claims to the code they describe.

``docs/architecture/verification-stack.md`` is the document a reviewer reads to
decide whether a gate is real, and every claim in it used to be checked by
nobody: the table was dated "checked 2026-10-27" and the date was the only
evidence. A renamed module, a removed knob, or a row claiming a state the code
no longer has would leave the doc confidently wrong.

Four assertions, each tied to a claim the doc actually makes:

* every module path it names exists (and a *removed* section's module is gone);
* every ``MO_*`` knob it names is read somewhere in the runtime;
* every table row has a numbered section, and every ``section ⑮``-style
  cross-reference resolves to one;
* the state column equals :data:`PINNED_STATES` — changing the doc's claim is
  a test edit, which is the point.

What this does NOT prove: that a "live" row runs where the doc says it runs.
That is the runtime's job (the verify catalog and the publisher's level gate),
and pretending a text parser settles it would be worse than the date.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "architecture" / "verification-stack.md"

NUMBERS = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭")

#: The table's state column, verbatim. Editing a row's claim means editing this
#: map — a reviewer then has to look at the section too.
PINNED_STATES = {
    "①": "on inside certify; shadow in code-fix",
    "②": "on inside certify; shadow in code-fix",
    "③": "live",
    "④": "live",
    "⑤": "consumer on, producer manual",
    "⑥": "opt-in per recipe",
    "⑦": "consumer on, producer manual",
    "⑧": "off by default; `shadow` measures without enforcing",
    "⑨": "live",
    "⑩": "consumer on, producer manual",
    "⑪": "removed (669c181c)",
    "⑫": "certify only",
    "⑬": "opt-in per recipe",
    "⑭": "certify only",
}

_PATH_RE = re.compile(r"`((?:mini_ork|recipes|docs|db|config)/[^`]+?\.(?:py|md|sql|yaml|sh)|recipes/[^`]+/)`")
_KNOB_RE = re.compile(r"`(MO_[A-Z0-9_]+)`")
_TABLE_ROW_RE = re.compile(r"^\|\s*([①-⑭])\s*\|(.*)\|\s*$")


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    """``{number: body}`` for every ``## ① …`` heading."""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^## ([①-⑳])", line)
        if m:
            current = m.group(1)
            out[current] = line + "\n"
        elif current:
            out[current] += line + "\n"
    return out


def _rows(text: str) -> dict[str, list[str]]:
    """``{number: [instrument, runs_in, state]}`` from the "Where each …" table."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) != 3:
            continue
        out[m.group(1)] = cells
    return out


def _removed(text: str) -> set[str]:
    return {num for num, body in _sections(text).items()
            if "removed" in body.splitlines()[0].lower()}


#: Paths the doc names that are *supposed* to be absent from the checkout:
#: `secrets.local.sh` is gitignored and created per machine, so its absence is
#: the correct state, not a stale reference.
_NOT_IN_REPO = {"config/secrets.local.sh"}


def _code_paths(body: str) -> list[str]:
    return [rel for rel in _PATH_RE.findall(body)
            if rel not in _NOT_IN_REPO and not rel.startswith("docs/")]


def test_the_doc_still_has_the_table_it_is_pinning():
    rows = _rows(_doc())
    assert tuple(rows) == NUMBERS, f"table rows changed: {list(rows)}"


def test_every_state_column_matches_the_pinned_claim():
    rows = _rows(_doc())
    actual = {}
    for num, cells in rows.items():
        actual[num] = cells[2].replace("**", "")
    assert actual == PINNED_STATES


def test_every_named_module_exists():
    text = _doc()
    removed = _removed(text)
    missing: list[str] = []
    for num, body in _sections(text).items():
        for rel in _code_paths(body):
            path = REPO / rel
            if num in removed:
                if path.exists():
                    missing.append(f"section {num} says removed, but {rel} exists")
            elif not path.exists():
                missing.append(f"{num}: {rel}")
    for rel in _code_paths(text):
        # A path discussed outside a numbered section (the diagram, the prose).
        if not (REPO / rel).exists():
            missing.append(f"prose: {rel}")
    assert not missing, missing


def test_every_knob_is_read_by_the_runtime():
    text = _doc()
    removed = _removed(text)
    knob_sections = {}
    for num, body in _sections(text).items():
        if num in removed:
            continue
        for knob in _KNOB_RE.findall(body):
            knob_sections.setdefault(knob, set()).add(num)

    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for root in ("mini_ork", "recipes", "bin", "config")
        for p in (REPO / root).rglob("*")
        if p.is_file()
    )
    missing = sorted(knob for knob in knob_sections if knob not in blob)
    assert not missing, f"documented but never read: {missing}"


def test_every_table_row_has_its_section():
    sections = _sections(_doc())
    assert [num for num in NUMBERS if num not in sections] == []


def test_every_section_reference_resolves():
    text = _doc()
    sections = _sections(text)
    referenced = set(re.findall(r"section ([①-⑳])", text))
    assert referenced <= set(sections), f"dangling: {sorted(referenced - set(sections))}"
