#!/usr/bin/env python3
"""verifiers/evidence-citations.py — the citation gate for repo-practices-benchmark.

A benchmark is only worth reading if its claims can be checked. This gate
checks the one thing a text document can be checked for mechanically: each
lens report cites at least N *distinct paths that exist*, and the synthesis
carries a ranked list rather than a book report.

Unresolved tokens are logged and counted, not fatal. A glob
(``.agents/skills/*``), a directory, or a bare filename (``ci.yml``) all read
as an unresolved citation while being legitimate — the real Warp run logged 80
in a sound synthesis. What the floor catches is the report that is *mostly*
invented: eight fake paths and nothing real drops the count to zero.

What it does NOT prove: that the cited line says what the claim says. That
is the synthesizer's job and a reviewer's; pretending a path-existence check
settles it would be worse than no gate.

Roots
-----
Citations are resolved against, in order:
  * ``BENCH_REFERENCE_ROOT`` — the reference repository checkout
  * ``BENCH_SUBJECT_ROOT``  — our own repo (mini-ork)
  * any absolute path that exists as written
If neither env var is set, ``reference root:`` / ``subject root:`` lines in
the kickoff (``MINI_ORK_KICKOFF_PATH``) are parsed as the fallback, so an
operator who sets nothing still gets a gate rather than a silent pass.

Inputs (env): MINI_ORK_RUN_DIR, BENCH_REFERENCE_ROOT?, BENCH_SUBJECT_ROOT?,
              MINI_ORK_KICKOFF_PATH?
Output: JSON to stdout; evidence log at <run>/verifier-evidence-citations.log.
Exit code: always 0 (the caller reads ``pass``).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

RUN_DIR = Path(os.environ["MINI_ORK_RUN_DIR"])
EVIDENCE = RUN_DIR / "verifier-evidence-citations.log"

#: (report stem, minimum distinct resolving citations)
LENSES = {
    "conventions": 8,
    "pipeline": 8,
    "architecture": 8,
    "agent-ux": 8,
}
SYNTHESIS_MIN_ITEMS = 6

#: A backticked path, optionally with a line suffix. Deliberately permissive
#: about the path body and strict about the line number — a claim of
#: ``file:12`` is checkable, a claim of ``file`` is a reference.
_CITE_RE = re.compile(r"`([^`\s]+?)(?::(\d+))?`")
_ITEM_RE = re.compile(r"^###\s+\d+\.\s+\S", re.MULTILINE)
#: A citation that points at code has a separator or a recognized extension;
#: this filters out backticked flag names (`--no-resume`) and env vars.
_PATHY_RE = re.compile(r"[/.]")


def _roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("BENCH_REFERENCE_ROOT", "BENCH_SUBJECT_ROOT"):
        value = os.environ.get(key, "").strip()
        if value:
            roots.append(Path(value))
    if roots:
        return roots
    kickoff = os.environ.get("MINI_ORK_KICKOFF_PATH", "")
    if kickoff and Path(kickoff).is_file():
        for line in Path(kickoff).read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*[-*]?\s*(reference|subject) root:\s*`?([^`\s]+)`?", line, re.I)
            if m:
                roots.append(Path(m.group(2)))
    return roots


def _resolves(token: str, roots: list[Path]) -> bool:
    candidate = Path(token)
    if candidate.is_absolute():
        return candidate.is_file()
    return any((root / token).is_file() for root in roots)


def _citations(text: str, roots: list[Path]) -> tuple[set[str], set[str]]:
    """(resolved, unresolved) distinct citation tokens."""
    resolved: set[str] = set()
    unresolved: set[str] = set()
    for match in _CITE_RE.finditer(text):
        token = match.group(1).strip()
        if not _PATHY_RE.search(token) or token.startswith("-"):
            continue
        if token.endswith("/") or token.startswith("["):
            continue
        (resolved if _resolves(token, roots) else unresolved).add(token)
    return resolved, unresolved


def main() -> int:
    roots = _roots()
    ev = EVIDENCE.open("w", encoding="utf-8")
    missing: list[str] = []
    total_resolved = 0

    ev.write(f"roots: {[str(r) for r in roots] or 'NONE — no citation can resolve'}\n")
    if not roots:
        missing.append("no roots: set BENCH_REFERENCE_ROOT/BENCH_SUBJECT_ROOT")

    for stem, floor in LENSES.items():
        # The runtime derives a report filename from the NODE name
        # (`agent_ux_lens` → `lens-agent_ux.md`), while the prompt names the
        # hyphenated file. Either satisfies the gate; the prompt's spelling is
        # preferred when both exist.
        path = RUN_DIR / f"lens-{stem}.md"
        if not path.is_file():
            path = RUN_DIR / f"lens-{stem.replace('-', '_')}.md"
        if not path.is_file():
            missing.append(f"lens-{stem}.md (missing)")
            ev.write(f"MISSING lens-{stem}.md\n")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n") + 1
        resolved, unresolved = _citations(text, roots)
        ev.write(
            f"lens-{stem}: {lines} lines, {len(resolved)} resolving citations, "
            f"{len(unresolved)} unresolved\n")
        if lines < 20:
            missing.append(f"lens-{stem}.md (too short: {lines} lines)")
        if len(resolved) < floor:
            missing.append(
                f"lens-{stem}.md ({len(resolved)} resolving citations, need >={floor})")
        for token in sorted(unresolved)[:10]:
            ev.write(f"  unresolved: {token}\n")
        total_resolved += len(resolved)

    synth = RUN_DIR / "synthesis.md"
    if not synth.is_file():
        missing.append("synthesis.md (missing)")
        ev.write("MISSING synthesis.md\n")
    else:
        text = synth.read_text(encoding="utf-8", errors="replace")
        for stem in LENSES:
            if f"lens-{stem}.md" not in text:
                missing.append(f"synthesis.md (does not reference lens-{stem}.md)")
        items = len(_ITEM_RE.findall(text))
        ev.write(f"synthesis.md: {items} numbered improvement items\n")
        if items < SYNTHESIS_MIN_ITEMS:
            missing.append(
                f"synthesis.md ({items} numbered items, need >={SYNTHESIS_MIN_ITEMS})")
        if not re.search(r"already does|better than|keeps? what works", text, re.I):
            missing.append("synthesis.md (no 'what mini-ork already does well' section)")
        resolved, unresolved = _citations(text, roots)
        ev.write(
            f"synthesis.md: {len(resolved)} resolving citations, "
            f"{len(unresolved)} unresolved\n")
        total_resolved += len(resolved)

    ev.close()

    print(json.dumps({
        "verifier": "evidence-citations",
        "pass": not missing,
        "evidence_path": str(EVIDENCE),
        "citations_resolved": total_resolved,
        "missing": missing,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
