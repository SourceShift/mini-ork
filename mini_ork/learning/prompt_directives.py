"""Only-verified-learnings guard for recipe prompt files.

A learned directive may sit in a recipe prompt file only if the apply loop
MEASURED it and promoted it. This module is the enforcement half of that rule
(user decision, 2026-10-07): it finds the ``<!-- applied:gradient_records:* -->``
blocks that were written by the retired ``MO_APPLY_UNVETTED`` path (mock
scorer), removes them byte-exactly, records the removal in ``promotion_records``
so the audit trail explains the gap, and quarantines the matching
``version_registry`` rows.

Three primitives, plus a sidecar writer used by ``mini_ork/cli/apply.py``:

    scan(repo_root)                     -> list of applied blocks on disk
    verification(db, source_id)         -> was this source MEASURED and promoted?
    revert_unverified(repo_root, db)    -> remove / record / quarantine

``verification`` is the predicate that has to stay honest: a source is
VERIFIED only when some ``promotion_records`` row reached ``decision='promoted'``
with a rationale that does not admit a fabricated score (``UNVETTED``,
``scorer=mock``, ``scorer=gepa``). test-seam scorers fabricate utility
(``FABRICATING_SCORERS`` in ``mini_ork/cli/apply.py``) and can never promote, so
their rationale is the flag that marks a block as unearned.

Schema note (why revert rows are ``rejected``/``human``, not ``reverted``/
``operator``): ``db/migrations/0011_evolution.sql`` pins
``decision CHECK (... 'promoted','quarantined','rejected','pending_human_approval')``
and ``decided_by CHECK ('gate','human')``. SQLite cannot widen a CHECK in place
and a new migration is out of scope, so a revert is recorded as a human
REJECTION — the rationale text carries the "reverted under the rule" semantics.
``decided_by='human'`` for operator actions follows the existing precedent in
``mini_ork/gates/promotion_gate.py``.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
import uuid

# The marker renderer (_directive_block, mini_ork/cli/apply.py) emits exactly
# ``<!-- applied:<source_ref> -->`` where ``source_ref`` is
# ``<source_kind>:<source_id>``. Today the only source kind wired through the
# apply loop is gradient_records; the regex accepts any ``applied:<kind>:<id>``
# so the scanner keeps working if another kind appears.
MARKER_PREFIX = "gradient_records:"
_MARKER_RE = re.compile(r"^<!--\s*applied:(?P<ref>[^\s>]+)\s*-->\s*$")
_OBS_RE = re.compile(r"^- Observation:")
_DIR_RE = re.compile(r"^- Directive:")

# A promote rationale containing any of these admits a simulated score. Such a
# promote is NOT a measurement, so the directive it produced is unearned.
UNVERIFIED_TOKENS = ("UNVETTED", "scorer=mock", "scorer=gepa")

# Scorers whose utilities come from real held-out runs. A sidecar entry whose
# scorer is not one of these does not license a prompt block.
VERIFIED_SCORERS = ("probe", "code")

SIDECAR_NAME = ".verified-directives.json"

# Verbatim from the kickoff (2026-10-07). Kept as one constant so the row
# written at revert time and the reason stored on quarantined version rows can
# never drift apart.
REVERT_RATIONALE = (
    "unverified: promoted on a simulated (mock) score via MO_APPLY_UNVETTED; "
    "removed under the rule 'only verified learnings in prompts' (2026-10-07)"
)
# Prefix used to recognise an already-recorded revert (idempotency guard).
REVERT_RATIONALE_PREFIX = "unverified:"

_DEFAULT_SYNTHETIC_BASE = "wf-synthetic-baseline"


# ─────────────────────────────────────────────────────────────────────────────
# DB plumbing — same resolution order as the two live writers we touch
# (mini_ork/cli/apply.py: _db_path; mini_ork/registries/version_registry.py).
# ─────────────────────────────────────────────────────────────────────────────
def _db_path(db: str | None = None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return os.path.join(home, "state.db")
    raise RuntimeError("MINI_ORK_DB unset")


def _existing_db(db: str | None) -> str | None:
    """Resolve the DB path, returning None when it is unset or missing.

    A read-only caller must not create a stray empty sqlite file as a side
    effect of asking "is this directive verified?". Missing DB is a coherent
    state (nothing was ever promoted here) and reads as "unverified".
    """
    try:
        path = _db_path(db)
    except RuntimeError:
        return None
    return path if os.path.exists(path) else None


def _now() -> str:
    """Same timestamp shape as mini_ork/cli/apply.py:_now (literal '%f' kept
    for parity with the bash port)."""
    return time.strftime("%Y-%m-%dT%H:%M:%fZ", time.gmtime())


# ─────────────────────────────────────────────────────────────────────────────
# scan — every applied block on disk
# ─────────────────────────────────────────────────────────────────────────────
def scan(repo_root) -> list[dict]:
    """Return every ``applied:`` directive block under ``recipes/*/prompts/*.md``.

    A block is: the marker line, the ``- Observation:`` / ``- Directive:``
    lines that follow it (stopping at the first line that is neither), and
    exactly one preceding blank line when one is present. ``start_line`` /
    ``end_line`` are 1-based and inclusive of the blank line, so removing the
    slice ``[start_line-1:end_line]`` reverses the append byte-for-byte.

    Each dict carries the kickoff's keys — ``source_id`` (the bare
    ``gr-<hex>`` id), ``file`` (repo-relative), ``start_line``, ``end_line``,
    ``text`` — plus ``marker_ref`` (``gradient_records:gr-<hex>``, the exact
    string the marker carries) for callers that need to match the marker.
    """
    root = os.path.abspath(str(repo_root)) if repo_root else os.getcwd()
    blocks: list[dict] = []
    pattern = os.path.join(root, "recipes", "*", "prompts", "*.md")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, root)
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            m = _MARKER_RE.match(lines[i])
            if not m or not m.group("ref").startswith(MARKER_PREFIX):
                i += 1
                continue
            j = i + 1
            while j < len(lines) and (_OBS_RE.match(lines[j]) or _DIR_RE.match(lines[j])):
                j += 1
            start = i - 1 if i > 0 and lines[i - 1] == "" else i
            end = j - 1  # last Observation/Directive line
            ref = m.group("ref")
            blocks.append({
                "source_id": ref[len(MARKER_PREFIX):],
                "marker_ref": ref,
                "file": rel,
                "start_line": start + 1,
                "end_line": end + 1,
                "marker_line": i + 1,
                "text": "\n".join(lines[start:end + 1]),
            })
            i = j
    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# verification — the predicate that has to stay honest
# ─────────────────────────────────────────────────────────────────────────────
def _candidate_ids_for(con, source_id: str) -> list[str]:
    if not source_id:
        return []
    try:
        rows = con.execute(
            "SELECT DISTINCT candidate_id FROM apply_attempts "
            "WHERE source_id=? AND candidate_id IS NOT NULL AND candidate_id != ''",
            (source_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows if r[0]]


def _token_free(rationale: str) -> bool:
    return not any(tok in (rationale or "") for tok in UNVERIFIED_TOKENS)


def verification(db: str | None, source_id: str) -> dict:
    """Was ``source_id`` MEASURED and promoted?

    Returns ``{"verified", "candidate_id", "decision", "rationale",
    "decided_at"}``. ``verified`` is True iff some ``promotion_records`` row for
    a candidate this source minted reached ``decision='promoted'`` with a
    rationale free of the fabrication markers. A source whose only promote
    carries ``UNVETTED`` / ``scorer=mock`` / ``scorer=gepa``, or that only ever
    quarantined, is False.
    """
    result = {
        "verified": False,
        "candidate_id": None,
        "decision": None,
        "rationale": None,
        "decided_at": None,
    }
    path = _existing_db(db)
    if not path or not source_id:
        return result
    try:
        con = sqlite3.connect(path)
    except sqlite3.Error:
        return result
    try:
        cids = _candidate_ids_for(con, source_id)
        result["candidate_id"] = cids[0] if cids else None
        if not cids:
            return result
        qmarks = ",".join("?" * len(cids))
        try:
            rows = con.execute(
                "SELECT decision, rationale, decided_at FROM promotion_records "
                f"WHERE candidate_id IN ({qmarks}) ORDER BY decided_at DESC",
                cids,
            ).fetchall()
        except sqlite3.OperationalError:
            return result
        clean = None
        first = None
        for decision, rationale, decided_at in rows:
            if first is None:
                first = (decision, rationale, decided_at)
            if decision == "promoted" and _token_free(rationale):
                clean = (decision, rationale, decided_at)
                break
        chosen = clean or first
        if chosen:
            result["decision"] = chosen[0]
            result["rationale"] = chosen[1]
            result["decided_at"] = chosen[2]
        result["verified"] = clean is not None
    finally:
        con.close()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Sidecar — the per-recipe record of which directive blocks were EARNED.
# ─────────────────────────────────────────────────────────────────────────────
def sidecar_path_for(target_file: str) -> str:
    """``recipes/<recipe>/prompts/.verified-directives.json`` for a target file.

    Derived from the target's own directory, never from the recipe root: tests
    point ``apply_mutation`` at ``tmp_path`` targets, and a recipe-root-derived
    path would write sidecars into the live repo tree during those runs.
    """
    return os.path.join(os.path.dirname(os.path.abspath(target_file)), SIDECAR_NAME)


def read_sidecar(target_file: str) -> list[dict]:
    path = sidecar_path_for(target_file)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def append_sidecar_entry(target_file: str, *, source_id: str, candidate_id: str,
                         scorer: str, n=None, before=None, after=None,
                         decided_at: str | None = None,
                         source_ref: str = "") -> str:
    """Append one entry to the recipe's ``.verified-directives.json``.

    Entry shape (kickoff step 5): ``{source_id, candidate_id, scorer, n,
    before, after, decided_at}``. ``source_ref`` (the verbatim marker payload,
    e.g. ``gradient_records:gr-…``) is carried alongside so the guard test can
    match a marker to its entry without re-deriving it. Failures belong to the
    caller: the sidecar is an audit convenience and must never unwind a promote.
    """
    entry = {
        "source_id": source_id,
        "candidate_id": candidate_id,
        "scorer": scorer,
        "n": n,
        "before": before,
        "after": after,
        "decided_at": decided_at or _now(),
    }
    if source_ref:
        entry["source_ref"] = source_ref

    path = sidecar_path_for(target_file)
    entries = read_sidecar(target_file)
    entries = [
        e for e in entries
        if not (isinstance(e, dict)
                and e.get("source_id") == source_id
                and e.get("source_ref", "") == entry.get("source_ref", ""))
    ]
    entries.append(entry)
    entries.sort(key=lambda e: (str(e.get("source_id")), str(e.get("source_ref") or "")))
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def _sidecar_entry_for(repo_root: str, block: dict) -> dict | None:
    target = os.path.join(repo_root, block["file"])
    for entry in read_sidecar(target):
        if not isinstance(entry, dict):
            continue
        if entry.get("source_ref") == block["marker_ref"]:
            return entry
        if entry.get("source_id") == block["source_id"] and not entry.get("source_ref"):
            return entry
    return None


def unverified_markers(repo_root) -> list[str]:
    """Applied markers that lack a sidecar entry with a real scorer.

    The static guard: any ``applied:`` block on disk must be explained by a
    sidecar entry whose ``scorer`` is in ``VERIFIED_SCORERS``. Returns the
    offending repo-relative file paths (sorted, deduped).
    """
    root = os.path.abspath(str(repo_root)) if repo_root else os.getcwd()
    offenders = []
    for block in scan(root):
        entry = _sidecar_entry_for(root, block)
        if entry is None or entry.get("scorer") not in VERIFIED_SCORERS:
            offenders.append(block["file"])
    return sorted(set(offenders))


# ─────────────────────────────────────────────────────────────────────────────
# revert_unverified — remove the unearned blocks, record why, quarantine rows
# ─────────────────────────────────────────────────────────────────────────────
def _strip_blocks(path: str, blocks: list[dict], *, dry_run: bool) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return False
    lines = content.split("\n")
    # Delete from the bottom up so earlier indices stay valid.
    for block in sorted(blocks, key=lambda b: b["start_line"], reverse=True):
        del lines[block["start_line"] - 1: block["end_line"]]
    new = "\n".join(lines)
    if new == content:
        return False
    if not dry_run:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    return True


def _discover_unverified_source_ids(db: str | None) -> list[str]:
    """DB-derived discovery for the post-merge ``--db-only`` run.

    By then the markers are gone from disk, so ``scan`` cannot find them; the
    surviving evidence is the ``promotion_records`` rationale. Select the
    source_ids whose promote admits a simulated score and whose verification is
    still False.
    """
    path = _existing_db(db)
    if not path:
        return []
    try:
        con = sqlite3.connect(path)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = con.execute(
                "SELECT DISTINCT aa.source_id FROM apply_attempts aa "
                "JOIN promotion_records pr ON pr.candidate_id = aa.candidate_id "
                "WHERE aa.source_id IS NOT NULL AND aa.source_id != '' "
                "AND pr.decision='promoted' AND ("
                "pr.rationale LIKE '%UNVETTED%' OR pr.rationale LIKE '%scorer=mock%' "
                "OR pr.rationale LIKE '%scorer=gepa%')"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        candidates = [r[0] for r in rows if r[0]]
    finally:
        con.close()
    return sorted(sid for sid in candidates if not verification(db, sid)["verified"])


def _already_reverted(con, cids: list[str]) -> bool:
    qmarks = ",".join("?" * len(cids))
    try:
        row = con.execute(
            "SELECT 1 FROM promotion_records WHERE decision='rejected' "
            "AND decided_by='human' AND rationale LIKE ? "
            f"AND candidate_id IN ({qmarks}) LIMIT 1",
            [REVERT_RATIONALE_PREFIX + "%", *cids],
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _base_version(con, candidate_id: str) -> str:
    try:
        row = con.execute(
            "SELECT base_workflow_version_id FROM workflow_candidates "
            "WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return (row[0] if row and row[0] else _DEFAULT_SYNTHETIC_BASE)


def _quarantine(db: str | None, cid_by_sid: dict[str, list[str]],
                suffixes: set[str], *, dry_run: bool) -> list[str]:
    """Quarantine the version_registry rows that belong to unverified sources.

    Two keys, in order of trust:

    1. ``candidate_id`` from the row's payload — exact, and the reason a later
       VERIFIED promote of the same file (a second row with the same ``name``)
       is not swept up by the suffix branch.
    2. path suffix — legacy rows promoted before the payload carried a
       ``candidate_id``. Applied only when the row has no candidate_id, so a
       verified row can never be quarantined by mistake.

    Idempotent by status: a row already ``quarantined`` is skipped, so a second
    ``--db-only`` run does not rewrite ``quarantined_at``. A row a human cleared
    (``clear_quarantine`` sets ``status='candidate'`` and records
    ``quarantine_cleared_by``) is skipped too — re-quarantining it would
    silently undo an explicit human override.
    """
    unverified_cids = {cid for cids in cid_by_sid.values() for cid in cids}
    path = _existing_db(db)
    if not path:
        return []
    try:
        con = sqlite3.connect(path)
    except sqlite3.Error:
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT version_id, name, payload, status, quarantine_cleared_by "
            "FROM version_registry WHERE kind='agent'"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    targets = []
    for row in rows:
        # Skip rows already in the target state (a replay) and rows a human
        # deliberately un-quarantined. Both must be left untouched.
        if row["status"] == "quarantined" or row["quarantine_cleared_by"]:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        cid = payload.get("candidate_id")
        name = row["name"] or ""
        if cid and cid in unverified_cids:
            targets.append(row["version_id"])
        elif not cid and any(name.endswith("/" + s) or name == s for s in suffixes):
            targets.append(row["version_id"])

    if dry_run:
        return sorted(set(targets))
    try:
        from mini_ork.registries import version_registry as vr
    except Exception:
        return []
    for version_id in sorted(set(targets)):
        try:
            vr.quarantine("agent", version_id, REVERT_RATIONALE, db=db)
        except Exception:
            pass
    return sorted(set(targets))


def _record_reverts(db: str | None, source_ids, suffixes: set[str],
                    *, dry_run: bool) -> tuple[list[str], int]:
    """Write one REJECTED/HUMAN row per unverified source; quarantine rows."""
    source_ids = sorted(set(source_ids))
    if not source_ids:
        return [], 0
    path = _existing_db(db)
    if not path:
        return [], 0

    recorded = 0
    cid_by_sid: dict[str, list[str]] = {}
    try:
        con = sqlite3.connect(path)
        try:
            for sid in source_ids:
                cid_by_sid[sid] = _candidate_ids_for(con, sid)
            todo = []
            for sid in source_ids:
                cids = cid_by_sid.get(sid) or []
                if not cids or _already_reverted(con, cids):
                    continue
                todo.append(cids[0])
            if not dry_run:
                for cid in todo:
                    base = _base_version(con, cid)
                    con.execute(
                        "INSERT INTO promotion_records "
                        "(promotion_id, candidate_id, from_version_id, to_version_id, "
                        " utility_before, utility_after, benchmark_run_id, rationale, "
                        " decision, decided_at, decided_by) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f"pr-{uuid.uuid4().hex[:16]}", cid, base, base,
                         0.0, 0.0, None, REVERT_RATIONALE, "rejected", _now(), "human"),
                    )
                con.commit()
            recorded = len(todo)
        finally:
            con.close()
    except sqlite3.Error:
        return [], 0

    quarantined = _quarantine(db, cid_by_sid, suffixes, dry_run=dry_run)
    return quarantined, recorded


def revert_unverified(repo_root, db: str | None = None, *, dry_run: bool = False,
                      files: bool = True, record: bool = True) -> dict:
    """Revert every unverified applied directive under ``repo_root``.

    ``files`` scans+edits the prompt files; ``record`` writes the audit rows and
    quarantines the version_registry rows. ``--files-only`` is (files=True,
    record=False) — the worktree run; the operator's post-merge ``--db-only`` is
    (files=False, record=True), whose source_ids come from the DB because the
    markers are gone by then. Idempotent: a second run removes nothing and
    records nothing. Returns ``{removed, kept_verified, files_changed,
    quarantined, recorded, dry_run}``.
    """
    root = os.path.abspath(str(repo_root)) if repo_root else os.getcwd()

    removed: list[str] = []
    kept_verified: list[str] = []
    blocks_by_file: dict[str, list[dict]] = {}
    if files:
        for block in scan(root):
            if verification(db, block["source_id"])["verified"]:
                kept_verified.append(block["source_id"])
                continue
            removed.append(block["source_id"])
            blocks_by_file.setdefault(block["file"], []).append(block)
    else:
        removed = _discover_unverified_source_ids(db)

    files_changed = []
    if files:
        for rel in sorted(blocks_by_file):
            if _strip_blocks(os.path.join(root, rel), blocks_by_file[rel],
                             dry_run=dry_run):
                files_changed.append(rel)

    quarantined: list[str] = []
    recorded = 0
    if record:
        quarantined, recorded = _record_reverts(
            db, removed, set(blocks_by_file) if files else set(), dry_run=dry_run)

    return {
        "removed": sorted(set(removed)),
        "kept_verified": sorted(set(kept_verified)),
        "files_changed": files_changed,
        "quarantined": quarantined,
        "recorded": recorded,
        "dry_run": bool(dry_run),
    }
