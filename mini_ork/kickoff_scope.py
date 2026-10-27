"""One parser for a kickoff's declared file scope and its "do not" rules.

Three readers used to disagree about the same kickoff text (LF-R8):

- ``gen_profile`` built ``scope_allow`` from every heading whose title merely
  CONTAINED "scope", so ``## Out of scope`` and ``## Explicitly out of scope —
  do NOT touch`` fed the ALLOW list (they also fed ``scope_deny``).
- ``paths_in_text`` accepted only backticked tokens, so a "Files in scope"
  section of bare bullets (``- mini_ork/foo.py``) parsed to ``[]`` — 38 of 257
  headed kickoffs in this repo parsed empty.
- ``parse_contract``'s Do-NOT scan took any line that STARTED with
  "never"/"do not", so a wrapped continuation (``never raises into the run.``)
  became a hard constraint while a real rule mid-line (``Edit files directly;
  do not emit unified diffs. Do not touch …``) was missed.

Polarity is clause-aware inside an allow section: a negative clause (one that
starts with a ``do not`` trigger or ``except``, or that reads
``leave|keep … untouched|unchanged|alone|as-is``) denies the paths it names, so
``Leave `.mini-ork/` (run state) untouched`` no longer lands in ``allow``. The
clauses are read from logical lines (``_logical_lines``), so a sentence wrapped
across physical lines keeps its polarity on every line: a hard-wrapped
``Do NOT modify `a`, `b`,`` / ``` `c`. ``` denies all three.

This module is pure — no I/O, no environment, no imports from the rest of
``mini_ork`` — so every caller can share the parse without a cycle. Callers:
``cli.main.gen_profile`` (``heading_kind``), ``memory.preferences``
(``paths_in_line``), ``context_v2.parse_contract`` (``kickoff_scope`` and
``do_not_sentences``).

Decision record: ``docs/plans/2026-10-15-learning-loop-fixes.md`` (R8).
"""
from __future__ import annotations

import re

__all__ = [
    "heading_kind",
    "paths_in_line",
    "kickoff_scope",
    "do_not_sentences",
]


# ─── headings ────────────────────────────────────────────────────────────────

# Deny phrases are checked first: "Out of scope", "Not in scope" and
# "Explicitly out of scope — do NOT touch" all also contain an allow trigger
# (the word "scope", the phrase "in scope"), and a scope the kickoff rules OUT
# must never be read as one it allows.
_DENY_PHRASES = (
    "out of scope",
    "out-of-scope",
    "not in scope",
    "outside scope",
    "non-goal",
    "scope deny",
    "forbidden",
    "do not touch",
)
_ALLOW_PHRASES = ("files in scope", "scope allow", "in scope")


def heading_kind(title: str | None) -> str | None:
    """``"allow"``, ``"deny"`` or ``None`` for a section title. Deny wins.

    A title is ``deny`` when it contains a deny phrase, else ``allow`` when it
    contains an allow phrase or the bare word ``scope``, else ``None``.
    """
    t = (title or "").strip().lower()
    if any(p in t for p in _DENY_PHRASES):
        return "deny"
    if any(p in t for p in _ALLOW_PHRASES) or "scope" in t:
        return "allow"
    return None


# ─── paths on a line ─────────────────────────────────────────────────────────

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# A markdown list item: a bullet or an ordered marker, then its text.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
# Where a bullet's head token ends. The kickoff names ``whitespace``, `` —``,
# `` (`` and ``:``; the two space-prefixed delimiters sit at the same index as
# the bare space and are listed to keep the contract readable. A BARE ``(`` or
# ``—`` is not a delimiter, so a path that contains one is not cut in half (a
# trailing ``— commentary`` is dropped later, by ``_strip_commentary``).
_HEAD_CUTS = ("\t", " ", ":", " —", " (")
# Punctuation that clings to the END of a head token and is never part of the
# path. Trailing-only: a leading ``.`` is the ``./`` prefix ``_normalize_path``
# strips, and it must survive to be stripped there.
_HEAD_TRAILING = ".,;:"


def _strip_commentary(token: str) -> str:
    """Drop a trailing ``— commentary`` from a scope token."""
    return token.split("—")[0].strip()


def _looks_like_path(token: str) -> bool:
    """True when ``token`` names a file path: a ``/``, or a trailing ``.<ext>``.

    Two guards, both mirroring ``orchestration.concord_admission`` — the repo's
    other scope-token reader — so a token that is plainly not a file is not
    read as one:

    - a token ending in a glob ``**`` is a rule *target*
      (``mini_ork/ide_pages/**``), never a file a run edits;
    - no internal whitespace: a backticked shell command
      (``python -m pytest tests/x.py``) has a ``/`` and would otherwise leak a
      whole command, or a stray bullet marker (``- fake.py``), into the scope.
    """
    t = (token or "").strip()
    if not t or t.endswith("**") or any(c.isspace() for c in t):
        return False
    return "/" in t or bool(_EXT_RE.search(t))


def _normalize_path(token: str) -> str:
    t = token.strip()
    while t.startswith("./"):
        t = t[2:]
    return t


def _list_head(line: str) -> str:
    """The first token of a list item, or ``""`` when the line is not one."""
    m = _LIST_ITEM_RE.match(line or "")
    if not m:
        return ""
    body = m.group(1)
    cut = len(body)
    for d in _HEAD_CUTS:
        i = body.find(d)
        if i != -1 and i < cut:
            cut = i
    head = body[:cut].strip().strip("`\"'").strip()
    head = head.rstrip(_HEAD_TRAILING).strip()
    return head.strip("`\"'").strip()


def paths_in_line(line: str) -> list[str]:
    """Path-like tokens on one line: backticked spans plus a bare bullet head.

    Backticked spans use ``_looks_like_path`` (glob targets ending ``**`` and
    tokens with internal whitespace excluded, trailing ``— commentary``
    stripped). In addition, when the line is a markdown list item whose first
    token — up to whitespace, ``:``, ``—`` or ``(`` — contains a ``/`` or ends
    in a ``.<ext>``, that head is a path too, so a "Files in scope" section of
    BARE bullets (``- mini_ork/foo.py``) parses. Prose words are never paths.
    Normalized (``./`` stripped), de-duplicated, order kept.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        t = _strip_commentary(token)
        if not _looks_like_path(t):
            return
        p = _normalize_path(t)
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    for m in _BACKTICK_RE.finditer(line or ""):
        _add(m.group(1))
    _add(_list_head(line or ""))
    return out


# ─── the whole kickoff ───────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
# ``##`` through ``######``: an H1 is a document title, not a scope section.
_HEADING_RE = re.compile(r"^\s{0,3}(#{2,6})\s+(.+?)\s*$")


def kickoff_scope(text: str) -> dict:
    """``{"allow": [...], "deny": [...]}`` — the paths the kickoff scopes.

    Paths come from ``allow``/``deny`` sections (``heading_kind``) outside
    fenced code, normalized, de-duplicated and in document order. A path that
    appears in both lists stays only in ``deny``: a scope the kickoff rules out
    is never also allowed.

    In a ``deny`` section every path on the logical line is denied, as before.
    In an ``allow`` section the logical line (a paragraph or list item with its
    hard-wrapped continuations joined — ``_logical_lines(keep_marker=True)``,
    so a bare bullet head still parses) is split into clauses
    (``_SENTENCE_SPLIT_RE``) and each clause's paths go to ``deny`` when
    ``_negative_clause`` reads the clause as negative, else to ``allow``.
    """
    allow: list[str] = []
    deny: list[str] = []
    seen_allow: set[str] = set()
    seen_deny: set[str] = set()
    current: str | None = None
    for line in _logical_lines(text, keep_marker=True):
        m = _HEADING_RE.match(line)
        if m:
            current = heading_kind(m.group(2))
            continue
        if current is None:
            continue
        if current == "deny":
            for p in paths_in_line(line):
                if p not in seen_deny:
                    seen_deny.add(p)
                    deny.append(p)
            continue
        for clause in _SENTENCE_SPLIT_RE.split(line):
            bucket, seen = ((deny, seen_deny) if _negative_clause(clause)
                            else (allow, seen_allow))
            for p in paths_in_line(clause):
                if p not in seen:
                    seen.add(p)
                    bucket.append(p)
    deny_set = set(deny)
    return {"allow": [p for p in allow if p not in deny_set], "deny": deny}


# ─── "do not" sentences ──────────────────────────────────────────────────────

_MAX_SENTENCE_CHARS = 200
_TRIGGERS = ("do not", "don't", "never", "must not")
# Sentence boundaries. The kickoff's `(?<=[.!?])\s+` alone loses the rule in
# "Edit files directly; do not emit unified diffs." — that sentence STARTS with
# "Edit", so a start-with rule drops it, yet the kickoff's own Tests section
# requires "do not emit unified diffs." to survive. Splitting the clause too
# keeps the start-with rule honest and recovers the rule.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|;\s*")
# Markup a sentence may open with before its trigger word (``**Never** …``).
_START_MARKUP_RE = re.compile(r"^[\s`*_]+")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
# The ``leave|keep … untouched/unchanged/alone/as-is`` polarity pattern. Kept
# separate from ``_TRIGGERS`` so a do-not scan does not also promote every
# ``except …`` clause to a rule: only this pattern is shared with
# ``do_not_sentences``.
_LEAVE_KEEP_RE = re.compile(
    r"^(leave|keep)\b.*\b(untouched|unchanged|alone|as[- ]is)\b")


def _strip_markup(s: str) -> str:
    return s.replace("**", "").replace("__", "").replace("`", "").strip()


def _normalized_clause(clause: str) -> str:
    """A clause with its list marker and markup stripped, then lowercased."""
    t = _LIST_MARKER_RE.sub("", clause or "")
    t = _START_MARKUP_RE.sub("", t)
    return _strip_markup(t).lower()


def _negative_clause(clause: str) -> bool:
    """True when ``clause`` negates the paths it names (scope polarity).

    The clause is stripped of a leading list marker, leading markup and
    ``**``/``__``/backticks. It is negative when it then starts with a
    ``_TRIGGERS`` phrase or ``except``, or opens with the
    ``leave|keep … untouched/unchanged/alone/as-is`` pattern.
    """
    t = _normalized_clause(clause)
    return (
        any(t.startswith(w) for w in _TRIGGERS)
        or t.startswith("except")
        or bool(_LEAVE_KEEP_RE.match(t))
    )


def _is_leave_keep(clause: str) -> bool:
    """True when ``clause`` alone matches the leave/keep polarity pattern."""
    return bool(_LEAVE_KEEP_RE.match(_normalized_clause(clause)))


def _starts_with_trigger(sentence: str) -> bool:
    t = _START_MARKUP_RE.sub("", sentence).lower()
    return any(t.startswith(w) for w in _TRIGGERS)


def _logical_lines(text: str, *, keep_marker: bool = False) -> list[str]:
    """Paragraphs and list items, each one line, heading and fences aside.

    A list item swallows its continuation lines — indented, or flush-left
    without a blank line between (the kickoff writer hard-wrapped at ~78
    columns) — so a rule split across a wrap is one sentence. A heading line is
    its own logical line and never joins the text under it. Fenced blocks
    contribute nothing.

    With ``keep_marker=True`` a list item's logical line starts with the item's
    raw line stripped (marker included) instead of the text after the marker,
    so ``paths_in_line`` can still read a bare bullet head. Continuation
    joining, headings, blank lines and fences are unchanged.
    """
    out: list[str] = []
    buf = ""
    in_fence = False

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            out.append(buf.strip())
        buf = ""

    for raw in (text or "").splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            flush()
            continue
        if in_fence:
            continue
        if not raw.strip():
            flush()
            continue
        if _HEADING_LINE_RE.match(raw):
            flush()
            out.append(raw.strip())
            continue
        m = _LIST_MARKER_RE.match(raw)
        if m:
            flush()
            buf = raw.strip() if keep_marker else raw[m.end():].strip()
            continue
        buf = f"{buf} {raw.strip()}" if buf else raw.strip()
    flush()
    return out


def do_not_sentences(text: str) -> list[str]:
    """The kickoff's "do not" rules, as sentences.

    Outside fences, each list item is joined with its continuation lines and
    each paragraph into one logical line; sentences are split on
    ``(?<=[.!?])\\s+`` (and on ``;``, see ``_SENTENCE_SPLIT_RE``); a sentence is
    kept when it STARTS — after ``**``/``__``/backticks — with ``do not``,
    ``don't``, ``never`` or ``must not`` (case-insensitive), or matches the
    ``leave|keep … untouched/unchanged/alone/as-is`` polarity pattern. Markup is
    stripped, a sentence is capped at 200 chars, and the result is
    de-duplicated in order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for logical in _logical_lines(text):
        for sentence in _SENTENCE_SPLIT_RE.split(logical):
            if not (_starts_with_trigger(sentence) or _is_leave_keep(sentence)):
                continue
            clean = _strip_markup(sentence)[:_MAX_SENTENCE_CHARS]
            if clean and clean not in seen:
                seen.add(clean)
                out.append(clean)
    return out
