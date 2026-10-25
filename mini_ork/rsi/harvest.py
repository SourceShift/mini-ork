"""HARVEST — turn the repo and its state DB into ranked evidence, not prose.

Each rule is a *live predicate*: it fires while the defect exists and stops
firing once the code or the data changes. That is what makes the queue drain
instead of spinning, and it is why the audit doc (``docs/plans/2026-10-24-…``)
is only the starting point — every anchor in it has drifted at least once, so
detection here is structural (schema, data, AST), never line numbers.

Three outcomes per rule, and the difference matters:

* **no candidate** — the predicate is false. The domain is healthy on that axis.
* **``_SkipRule``** — the table is absent. An old home has not fixed anything;
  it simply cannot answer. Never a candidate, or every fresh install would open
  a dozen fake bugs.
* **``_SchemaDrift``** — the table exists but a column the rule needs is gone.
  That *is* a bug (something dropped or renamed it), so it becomes a
  ``probe_broken`` candidate rather than a silent skip.

A probe error is likewise never "no gaps": a required probe that errors sets
``harvest_ok=False`` so the cycle halts before any paid model call.

No I/O beyond reading: ``harvest`` takes an open connection and returns a
:class:`HarvestReport`. ``now`` is injected so severity and ages are testable.
"""
from __future__ import annotations

import ast
import datetime as _dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from mini_ork.learning.collapse_detector import MIN_STEPS
from mini_ork.rsi.candidates import Candidate, dedupe
from mini_ork.rsi.probes import SYNTHETIC_PREFIX, Finding, ProbeResult, normalize_path

#: Rule ids are the ``source`` suffix recorded in the ledger (``db:L4``).
#: ``db:`` probes are synthetic: re-running the rule *is* the success check.

DEFAULT_CFG: Mapping[str, Any] = {
    "per_source_max": 3,     # garden/validate findings admitted per cycle
    "stale_days": 30,        # "signal stopped refreshing" threshold
    "min_region_rows": 20,   # advantage data must be non-trivial before R2 fires
    "unlabelled_days": 7,    # L11 diagnosis backlog
    "inbox_pending_max": 10,  # V6
    "inbox_oldest_days": 14,  # V6
    "null_ratio_high": 0.9,
    "null_ratio_near_total": 0.99,
}


class _SkipRule(Exception):
    """The rule cannot answer on this database. Not a finding.

    Harvest and a *success check* mean opposite things by this, so the two are
    separate classes: for harvest a skip is "nothing to report", but a check that
    cannot read its evidence must fail closed rather than pass. ``run_rule``
    turns this into a probe ``error`` and :class:`_GapClosed` into "no
    candidates".
    """


class _GapClosed(_SkipRule):
    """The rule answered, and the defect it looks for is not present.

    Raised only after the rule has read its own tables/columns — the healthy
    branch of the same predicate that would have produced a candidate. A success
    check reads it as "fixed"; harvest, which subclasses this, still records a
    plain skip.
    """


class _SchemaDrift(Exception):
    """A present table lost a column the rule depends on. A finding."""


@dataclass(frozen=True, slots=True)
class HarvestReport:
    candidates: tuple[Candidate, ...]
    probes: tuple[ProbeResult, ...]
    harvest_ok: bool
    skipped: tuple[str, ...]
    errors: tuple[str, ...]
    #: Rules that answered "the defect is not here" (``_GapClosed``). Distinct
    #: from ``skipped`` — a closed rule *measured* the table and found it healthy,
    #: so it is evidence for reconciliation, while a skipped rule could not read
    #: its evidence at all.
    closed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "probes": [p.to_dict() for p in self.probes],
            "harvest_ok": self.harvest_ok,
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "closed": list(self.closed),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HarvestReport":
        return cls(
            candidates=tuple(Candidate.from_dict(c) for c in raw.get("candidates") or ()),
            probes=tuple(ProbeResult.from_dict(p) for p in raw.get("probes") or ()),
            harvest_ok=bool(raw.get("harvest_ok", False)),
            skipped=tuple(raw.get("skipped") or ()),
            errors=tuple(raw.get("errors") or ()),
            closed=tuple(raw.get("closed") or ()),
        )


@dataclass(frozen=True, slots=True)
class _Ctx:
    root: Path
    con: sqlite3.Connection
    now: int
    cfg: Mapping[str, Any]

    def int(self, key: str) -> int:
        return int(self.cfg.get(key, DEFAULT_CFG[key]))

    def num(self, key: str) -> float:
        return float(self.cfg.get(key, DEFAULT_CFG[key]))


# --------------------------------------------------------------------------
# Small SQL / filesystem helpers
# --------------------------------------------------------------------------

def _table_exists(ctx: _Ctx, table: str) -> bool:
    row = ctx.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _require_table(ctx: _Ctx, table: str) -> None:
    if not _table_exists(ctx, table):
        raise _SkipRule(f"table {table} is absent")


def _columns(ctx: _Ctx, table: str) -> set[str]:
    _require_table(ctx, table)
    return {str(r[1]) for r in ctx.con.execute(f'PRAGMA table_info("{table}")')}


def _require_column(ctx: _Ctx, table: str, column: str) -> None:
    if column not in _columns(ctx, table):
        raise _SchemaDrift(f"{table} has no column {column!r}")


def _scalar(ctx: _Ctx, sql: str, params: Sequence[Any] = ()) -> int:
    row = ctx.con.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _rows(ctx: _Ctx, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(ctx.con.execute(sql, tuple(params)))


def _ratio(ctx: _Ctx, numerator_sql: str, denominator_sql: str) -> tuple[float, int]:
    total = _scalar(ctx, denominator_sql)
    if total <= 0:
        return 0.0, 0
    return _scalar(ctx, numerator_sql) / total, total


def parse_ts(value: Any) -> int | None:
    """Parse epoch-int, epoch-string, or ISO text truncated to seconds.

    The ``:fZ`` corruption means ``MAX(created_at)`` can sort wrong, so every
    age check parses here instead of trusting the column's collation.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    try:
        parsed = _dt.datetime.fromisoformat(text[:19])
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp())


def _age_days(now: int, ts: int | None) -> int:
    if ts is None:
        return 0
    return max(0, (now - ts) // 86400)


# --------------------------------------------------------------------------
# AST / source helpers (structural, never line-based)
# --------------------------------------------------------------------------

def _py_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.glob("mini_ork/**/*.py")):
        if path.is_file():
            yield path


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return None


def _call_sites(root: Path, name: str) -> list[tuple[str, int]]:
    """Every call to ``name(...)`` in ``mini_ork/``, as (relpath, lineno)."""
    out: list[tuple[str, int]] = []
    for path in _py_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        rel = normalize_path(str(path.relative_to(root)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called == name:
                out.append((rel, int(getattr(node, "lineno", 0))))
    return out


def _decision_decide_calls(root: Path) -> list[tuple[str, int, bool]]:
    """``decide(...)`` calls in the dispatch layer, flagging missing ``segment=``."""
    path = root / "mini_ork" / "dispatch" / "routing.py"
    tree = _parse(path)
    if tree is None:
        return []
    out: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") != "decide":
            continue
        has_segment = any(kw.arg == "segment" for kw in node.keywords)
        out.append(("mini_ork/dispatch/routing.py", int(getattr(node, "lineno", 0)), has_segment))
    return out


def _decide_return_keys(root: Path) -> set[str]:
    path = root / "mini_ork" / "steering" / "decision_service.py"
    tree = _parse(path)
    if tree is None:
        return set()
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "decide":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or not isinstance(inner.value, ast.Dict):
                continue
            for key in inner.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _value_produced(root: Path, relpath: str, name: str) -> bool:
    """Is ``name`` bound (or passed by keyword) anywhere in ``relpath``?

    Matches on ``Name(ctx=Store)`` rather than ``ast.Assign`` because the real
    call site uses an annotated assignment (``screened_out: list[dict] = []``)
    and passes the value positionally — checking only ``Assign`` silently
    missed the defect this rule exists to find.
    """
    tree = _parse(root / relpath)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            return True
        if isinstance(node, ast.Call) and any(kw.arg == name for kw in node.keywords):
            return True
    return False


def _code_mentions(
    root: Path, needle: str, *, exclude_prefixes: Sequence[str] = ("mini_ork/rsi/", "tests/")
) -> list[Path]:
    """Files outside ``exclude_prefixes`` whose source mentions ``needle``.

    The exclusion is not cosmetic: a rule whose evidence is "this string appears
    in the repo" otherwise matches its own implementation and fires forever.
    """
    out: list[Path] = []
    for path in _py_files(root):
        rel = normalize_path(str(path.relative_to(root)))
        if any(rel.startswith(prefix) for prefix in exclude_prefixes):
            continue
        if needle in path.read_text(encoding="utf-8", errors="replace"):
            out.append(path)
    return out


def _inserts_table(root: Path, table: str) -> bool:
    needle = f"INSERT INTO {table}"
    for path in _py_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            return True
    return False


# --------------------------------------------------------------------------
# Candidate constructor
# --------------------------------------------------------------------------

def _cand(
    ctx: _Ctx,
    domain: str,
    rule_id: str,
    *,
    kind: str,
    title: str,
    evidence_key: str,
    files: Sequence[str] = (),
    detail: str = "",
    evidence: Mapping[str, Any] | None = None,
    reach: int = 1,
    age_days: int = 0,
    fix_hint: str = "",
    success_metric: str = "",
    source: str | None = None,
    success_command: str | None = None,
) -> Candidate:
    return Candidate.build(
        domain=domain,
        source=source or f"{SYNTHETIC_PREFIX}{rule_id}",
        kind=kind,
        title=title,
        evidence_key=evidence_key,
        now=ctx.now,
        detail=detail,
        files=files,
        evidence=evidence,
        fix_hint=fix_hint,
        success_metric=success_metric or f"rule {rule_id} stops producing a candidate",
        # A DB rule's success check *is* the rule (synthetic ``db:`` probe); a
        # probe-derived candidate points at the real probe id.
        success_command=success_command or f"{SYNTHETIC_PREFIX}{rule_id}",
        reach=reach,
        age_days=age_days,
    )


def _broken(ctx: _Ctx, rule_id: str, domain: str, reason: str) -> Candidate:
    return _cand(
        ctx, domain, rule_id,
        kind="probe_broken",
        title=f"harvest rule {rule_id} cannot evaluate its evidence",
        evidence_key=f"rule:{rule_id}:broken",
        detail=reason,
        evidence={"rule": rule_id, "reason": reason},
        fix_hint="Restore the table/column the rule reads, or retire the rule with a note.",
    )


# --------------------------------------------------------------------------
# LEARNING
# --------------------------------------------------------------------------

def _l1_pass_stats_never_written(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """The table exists, is empty, and nothing calls its writer."""
    _require_table(ctx, "learning_pass_stats")
    if _scalar(ctx, "SELECT count(*) FROM learning_pass_stats"):
        raise _GapClosed("learning_pass_stats has rows")
    callers = [(f, ln) for f, ln in _call_sites(ctx.root, "record_pass_stat")
               if f != "mini_ork/learning/ledger.py"]
    if callers:
        raise _GapClosed(f"record_pass_stat is called at {callers[:3]}")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="record_pass_stat has no caller — learning pass stats are never written",
        evidence_key="table:learning_pass_stats:empty",
        files=("mini_ork/learning/ledger.py",),
        detail="The writer exists but every call site is inside its own module, so the "
               "table is empty on every home. Anything reading pass stages sees nothing.",
        evidence={"table": "learning_pass_stats", "rows": 0,
                  "writer": "mini_ork/learning/ledger.py:record_pass_stat",
                  "external_call_sites": 0},
        fix_hint="Wire record_pass_stat into the reflect stage where a pass completes.",
        success_metric="learning_pass_stats gains rows from a normal run",
    )]


def _l2_reflection_log_never_written(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Reflection queue table exists, is empty, and no code inserts into it."""
    _require_table(ctx, "reflection_log")
    if _scalar(ctx, "SELECT count(*) FROM reflection_log"):
        raise _GapClosed("reflection_log has rows")
    if _inserts_table(ctx.root, "reflection_log"):
        raise _GapClosed("a writer for reflection_log exists")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="reflection_log is never written — no reflection work is queued",
        evidence_key="table:reflection_log:empty",
        files=("mini_ork/memory/store.py",),
        detail="The table is created and named in the store's table list, but no module "
               "inserts into it. Whatever drains it has nothing to drain.",
        evidence={"table": "reflection_log", "rows": 0, "insert_sites": 0},
        fix_hint="Insert one row when a run's reflection stage finds drift.",
        success_metric="reflection_log gains rows from a run with a finding",
    )]


def _l3_held_out_never_populated(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Injections exist, but none are held out — the contamination check is hollow."""
    _require_table(ctx, "lesson_injections")
    total = _scalar(ctx, "SELECT count(*) FROM lesson_injections")
    if total == 0:
        raise _SkipRule("lesson_injections is empty; nothing to hold out")
    _require_column(ctx, "lesson_injections", "held_out")
    held = _scalar(ctx, "SELECT count(*) FROM lesson_injections WHERE held_out=1")
    if held:
        raise _GapClosed(f"{held} held-out injections exist")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="every lesson injection is in-sample — held-out evaluation is hollow",
        evidence_key="table:lesson_injections:held_out:never-1",
        files=("mini_ork/learning/ledger.py",),
        detail=f"{total} injections, 0 held out. Any measured gain is on the same "
               "lessons the prompt was built from.",
        evidence={"table": "lesson_injections", "rows": total, "held_out": 0},
        reach=total,
        fix_hint="Reserve a deterministic slice at selection time and record held_out=1.",
        success_metric="at least one held-out injection exists",
    )]


def _l4_malformed_timestamps(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """``:fZ`` suffixes make these text timestamps unsortable."""
    targets = (("promotion_records", "decided_at"), ("apply_attempts", "created_at"))
    counts: dict[str, int] = {}
    totals: dict[str, int] = {}
    for table, column in targets:
        if not _table_exists(ctx, table):
            continue
        if column not in _columns(ctx, table):
            raise _SchemaDrift(f"{table} has no column {column!r}")
        totals[table] = _scalar(ctx, f'SELECT count(*) FROM "{table}"')
        counts[table] = _scalar(
            ctx, f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE \'%:fZ%\''
        )
    if not counts:
        raise _SkipRule("none of the timestamp tables exist")
    bad = sum(counts.values())
    if not bad:
        raise _GapClosed("timestamps parse")
    return [_cand(
        ctx, domain, rule_id,
        kind="temporal_corruption",
        title="malformed ':fZ' timestamps — ORDER BY/MAX on these columns lies",
        evidence_key="timestamp:promotion_records.decided_at:malformed-fZ",
        files=("mini_ork/gates/promotion_gate.py", "mini_ork/cli/apply.py"),
        detail="; ".join(f"{t}: {counts[t]}/{totals[t]} malformed" for t in counts),
        evidence={"malformed": counts, "totals": totals},
        reach=bad,
        fix_hint="Normalise with a guarded rewrite, then make the writers emit ISO+Z.",
        success_metric="no row matches '%:fZ%'",
    )]


def _l5_promoted_to_never_set(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Patterns are recorded but never marked promoted."""
    _require_table(ctx, "pattern_records")
    _require_column(ctx, "pattern_records", "promoted_to")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM pattern_records WHERE promoted_to IS NULL",
        "SELECT count(*) FROM pattern_records",
    )
    if total == 0 or ratio <= ctx.num("null_ratio_high"):
        raise _GapClosed(f"promoted_to null ratio {ratio:.2f}")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="pattern_records.promoted_to is never set — promotion is unobservable",
        evidence_key="table:pattern_records:promoted_to:null",
        files=("mini_ork/learning/pattern_induction.py",),
        detail=f"{ratio:.1%} of {total} patterns have no promotion target.",
        evidence={"table": "pattern_records", "rows": total, "null_ratio": round(ratio, 4)},
        reach=total,
        fix_hint="Set promoted_to when a pattern is turned into a directive or lesson.",
        success_metric="promoted_to null ratio falls below 0.9",
    )]


def _l6_lesson_text_missing(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Themes are clustered but most carry no text a prompt could use."""
    _require_table(ctx, "lesson_themes")
    _require_column(ctx, "lesson_themes", "lesson_text")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM lesson_themes WHERE lesson_text IS NULL OR lesson_text=''",
        "SELECT count(*) FROM lesson_themes",
    )
    if total == 0 or ratio <= 0.8:
        raise _GapClosed(f"lesson_text missing ratio {ratio:.2f}")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="lesson_themes rows carry no lesson text — clustering without a lesson",
        evidence_key="table:lesson_themes:lesson_text:null",
        files=("mini_ork/learning/pattern_induction.py",),
        detail=f"{ratio:.1%} of {total} themes have NULL/empty lesson_text, so any "
               "injection built from them injects an identifier, not an instruction.",
        evidence={"table": "lesson_themes", "rows": total, "missing_ratio": round(ratio, 4)},
        reach=total,
        fix_hint="Author lesson_text at clustering time; drop themes that cannot get one.",
        success_metric="lesson_text missing ratio falls below 0.8",
    )]


def _l7_collapse_history_under_min(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """The collapse detector cannot fire until it has MIN_STEPS rows."""
    _require_table(ctx, "collapse_history")
    rows = _scalar(ctx, "SELECT count(*) FROM collapse_history")
    if rows >= MIN_STEPS:
        raise _GapClosed(f"collapse_history has {rows} >= {MIN_STEPS} rows")
    return [_cand(
        ctx, domain, rule_id,
        kind="coverage_gap",
        title="collapse_history has too few rows for the collapse detector to fire",
        evidence_key="table:collapse_history:under-min-steps",
        files=("mini_ork/cli/apply.py",),
        detail=f"{rows} rows, detector needs {MIN_STEPS}. The learning-stall halt is "
               "unreachable by construction, not by health.",
        evidence={"table": "collapse_history", "rows": rows, "min_steps": MIN_STEPS},
        reach=MIN_STEPS,
        fix_hint="Write one collapse_history row per learning iteration.",
        success_metric=f"collapse_history row count >= {MIN_STEPS}",
    )]


def _l9_benchmark_delta_never_computed(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Learning records never gain a benchmark delta, and the table is stale."""
    _require_table(ctx, "learning_record")
    _require_column(ctx, "learning_record", "benchmark_delta")
    _require_column(ctx, "learning_record", "created_at")
    total = _scalar(ctx, "SELECT count(*) FROM learning_record")
    if total == 0:
        raise _SkipRule("learning_record is empty")
    with_delta = _scalar(ctx, "SELECT count(*) FROM learning_record WHERE benchmark_delta IS NOT NULL")
    if with_delta:
        raise _GapClosed(f"{with_delta} records carry a benchmark delta")
    # Parse every timestamp rather than MAX(): the ':fZ' corruption (L4) means
    # the text column cannot be trusted to sort.
    stamps = [ts for ts in (parse_ts(r[0]) for r in _rows(
        ctx, "SELECT created_at FROM learning_record")) if ts is not None]
    stale = _age_days(ctx.now, max(stamps) if stamps else None)
    if stale <= ctx.int("stale_days"):
        raise _GapClosed(f"newest record is {stale}d old")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="learning_record.benchmark_delta is never computed",
        evidence_key="table:learning_record:benchmark_delta:null",
        files=("mini_ork/learning/ledger.py",),
        detail=f"{total} learning records, 0 with a benchmark delta; newest is {stale}d old. "
               "Nothing measures whether the learning moved a benchmark.",
        evidence={"table": "learning_record", "rows": total, "with_delta": 0, "stale_days": stale},
        reach=total,
        age_days=stale,
        fix_hint="Attach benchmark_delta when a learning iteration has benchmark results.",
        success_metric="at least one learning_record carries benchmark_delta",
    )]


def _l10_lesson_conflicts_never_found(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Verified themes exist but the conflict detector has never recorded one."""
    _require_table(ctx, "lesson_conflicts")
    _require_table(ctx, "lesson_themes")
    conflicts = _scalar(ctx, "SELECT count(*) FROM lesson_conflicts")
    if conflicts:
        raise _GapClosed(f"{conflicts} lesson conflicts recorded")
    themes = _scalar(ctx, "SELECT count(*) FROM lesson_themes")
    if themes == 0:
        raise _SkipRule("no themes to conflict")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="lesson_conflicts is empty while themes exist — contradictions are unchecked",
        evidence_key="table:lesson_conflicts:empty",
        files=("mini_ork/learning/lesson_conflict.py",),
        detail=f"{themes} themes, 0 conflicts. Either no two lessons ever contradict "
               "(implausible at this size) or the detector is not on the write path.",
        evidence={"table": "lesson_conflicts", "rows": 0, "themes": themes},
        reach=themes,
        fix_hint="Run lesson_conflict detection when a theme is promoted or injected.",
        success_metric="lesson_conflicts gains a row, or the check runs and finds none",
    )]


def _l11_diagnosis_unlabelled(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Diagnoses older than the labelling window with no label."""
    _require_table(ctx, "diagnosis_ledger")
    _require_column(ctx, "diagnosis_ledger", "label")
    _require_column(ctx, "diagnosis_ledger", "created_at")
    rows = _rows(ctx, "SELECT id, created_at FROM diagnosis_ledger WHERE label IS NULL")
    old = [(int(r[0]), parse_ts(r[1])) for r in rows]
    old = [(rid, ts) for rid, ts in old if ts is not None]
    window = ctx.int("unlabelled_days")
    stale = [(rid, _age_days(ctx.now, ts)) for rid, ts in old if _age_days(ctx.now, ts) > window]
    if not stale:
        raise _GapClosed(f"{len(rows)} unlabelled diagnoses, none older than {window}d")
    return [_cand(
        ctx, domain, rule_id,
        kind="stale_signal",
        title="diagnoses sit unlabelled past the labelling window",
        evidence_key="table:diagnosis_ledger:unlabelled",
        files=("mini_ork/stores/diagnosis_ledger.py",),
        detail=f"{len(stale)} of {len(rows)} unlabelled diagnoses are older than {window}d.",
        evidence={"unlabelled": len(rows), "stale": len(stale), "window_days": window},
        reach=len(stale),
        age_days=max(age for _, age in stale),
        fix_hint="Label or expire old diagnoses; an unlabelled diagnosis teaches nothing.",
        success_metric="no unlabelled diagnosis older than the window",
    )]


def _l12_verifier_pass_rate_regression(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """A verifier's pass rate dropped between its older and newer half."""
    _require_table(ctx, "verifier_results")
    for column in ("verifier_name", "verdict", "created_at"):
        _require_column(ctx, "verifier_results", column)
    rows = _rows(
        ctx,
        "SELECT verifier_name, verdict, created_at FROM verifier_results "
        "ORDER BY verifier_name, created_at",
    )
    by_name: dict[str, list[str]] = {}
    for name, verdict, _ in rows:
        by_name.setdefault(str(name), []).append(str(verdict).lower())
    fired: list[Candidate] = []
    for name, verdicts in sorted(by_name.items()):
        if len(verdicts) < 10:
            continue
        half = len(verdicts) // 2
        older = sum(1 for v in verdicts[:half] if v in ("pass", "passed", "ok")) / half
        newer_set = verdicts[half:]
        newer = sum(1 for v in newer_set if v in ("pass", "passed", "ok")) / len(newer_set)
        if older - newer < 0.2:
            continue
        fired.append(_cand(
            ctx, domain, rule_id,
            kind="regression",
            title=f"verifier {name} pass rate fell {older:.0%} -> {newer:.0%}",
            evidence_key=f"verifier:{name}:pass-rate-drop",
            files=("mini_ork/gates/verifier_rubric.py",),
            detail=f"{len(verdicts)} results; older half {older:.0%}, newer half {newer:.0%}.",
            evidence={"verifier_name": name, "n": len(verdicts),
                      "older_rate": round(older, 3), "newer_rate": round(newer, 3)},
            reach=len(verdicts),
            fix_hint="Compare the failing cases: a rubric change or a real regression.",
            success_metric="pass rate recovers to the older half's level",
        ))
    if not fired:
        raise _GapClosed("no verifier shows a pass-rate drop")
    return fired


# --------------------------------------------------------------------------
# ROUTING
# --------------------------------------------------------------------------

def _r1_decide_without_segment(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """``decide()`` takes ``segment`` and the dispatch layer never passes it."""
    calls = _decision_decide_calls(ctx.root)
    if not calls:
        raise _SkipRule("no decide() call sites found")
    missing = [(f, ln) for f, ln, has in calls if not has]
    if not missing:
        raise _GapClosed("every decide() call passes segment")
    return [_cand(
        ctx, domain, rule_id,
        kind="signal_not_propagated",
        title="decision_service.decide() is called without segment=",
        evidence_key="file:mini_ork/dispatch/routing.py:decide:segment-not-passed",
        files=("mini_ork/dispatch/routing.py", "mini_ork/steering/decision_service.py"),
        detail=f"{len(missing)} of {len(calls)} call sites omit segment=; decide() therefore "
               "always derives code_region='' and region-conditioned advantage is invisible.",
        evidence={"call_sites": [{"file": f, "line": ln} for f, ln in missing]},
        reach=len(missing),
        fix_hint="Thread the segment through the dispatch call and persist it on the trace.",
        success_metric="every decide() call site passes segment=",
    )]


def _r2_learned_route_underused(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Learned routing is a sliver of all routes while advantage data is fresh."""
    _require_table(ctx, "execution_traces")
    _require_column(ctx, "execution_traces", "route_source")
    _require_table(ctx, "lane_region_advantage")
    rows = _scalar(ctx, "SELECT count(*) FROM lane_region_advantage")
    if rows < ctx.int("min_region_rows"):
        raise _SkipRule(f"only {rows} advantage rows")
    total = _scalar(ctx, "SELECT count(*) FROM execution_traces WHERE route_source IS NOT NULL")
    if total == 0:
        raise _SkipRule("no routed traces")
    learned = _scalar(ctx, "SELECT count(*) FROM execution_traces WHERE route_source='learned'")
    share = learned / total
    if share >= 0.05:
        raise _GapClosed(f"learned share {share:.1%}")
    return [_cand(
        ctx, domain, rule_id,
        kind="unused_capability",
        title="learned routing decides under 5% of traced routes",
        evidence_key="table:execution_traces:route_source:learned-under-5pct",
        files=("mini_ork/dispatch/routing.py",),
        detail=f"{learned}/{total} routes ({share:.2%}) used the learned policy while "
               f"{rows} advantage rows exist.",
        evidence={"learned": learned, "routed": total, "advantage_rows": rows,
                  "share": round(share, 4)},
        reach=total,
        fix_hint="Find which policy short-circuits before the learned arm and why.",
        success_metric="learned share of routed traces rises above 5%",
    )]


def _r3_code_region_null(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Region is mostly empty on traces.

    If ``decide()`` is also called without ``segment=`` (R1), this shares R1's
    identity on purpose: it is the *same* defect observed downstream, and the
    queue must not fix it twice. If the AST is clean yet the column is still
    empty, the segment is not being persisted — a different bug, so it gets its
    own signature.
    """
    _require_table(ctx, "execution_traces")
    _require_column(ctx, "execution_traces", "code_region")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM execution_traces WHERE code_region IS NULL OR code_region=''",
        "SELECT count(*) FROM execution_traces",
    )
    if total == 0 or ratio <= 0.5:
        raise _GapClosed(f"code_region null ratio {ratio:.2f}")
    ast_clean = all(entry[2] for entry in _decision_decide_calls(ctx.root))
    nulls = int(ratio * total)
    if not ast_clean:
        return [_cand(
            ctx, domain, rule_id,
            kind="signal_not_propagated",
            title="decision_service.decide() is called without segment=",
            evidence_key="file:mini_ork/dispatch/routing.py:decide:segment-not-passed",
            files=("mini_ork/dispatch/routing.py", "mini_ork/steering/decision_service.py"),
            detail=f"Also visible as data: {nulls}/{total} traces ({ratio:.1%}) carry no "
                   "code_region. Same defect as R1; merged to avoid a second fix.",
            evidence={"table": "execution_traces", "rows": total, "null_ratio": round(ratio, 4)},
            reach=total,
            fix_hint="Thread the segment through the dispatch call and persist it on the trace.",
            success_metric="code_region null ratio falls below 0.5",
        )]
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="execution_traces.code_region is empty although decide() receives a segment",
        evidence_key="table:execution_traces:code_region:null",
        files=("mini_ork/steering/decision_service.py",),
        detail=f"{nulls}/{total} traces ({ratio:.1%}) carry no code_region; the AST shows "
               "segment= is passed, so the value is dropped between decide() and the trace.",
        evidence={"table": "execution_traces", "rows": total, "null_ratio": round(ratio, 4)},
        reach=nulls,
        fix_hint="Persist the derived code_region onto the execution trace.",
        success_metric="code_region null ratio falls below 0.5",
    )]


def _r4_screened_out_dropped(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """``screened_out`` is computed and passed to a detail helper, never returned."""
    produced = _value_produced(ctx.root, "mini_ork/lane_router.py", "screened_out")
    if not produced:
        raise _SkipRule("lane_router does not compute screened_out")
    keys = _decide_return_keys(ctx.root)
    if not keys:
        raise _SkipRule("could not read decide()'s return shape")
    if "screened_out" in keys:
        raise _GapClosed("decide() already returns screened_out")
    return [_cand(
        ctx, domain, rule_id,
        kind="silent_swallow",
        title="screened_out is computed by lane routing but never returned or persisted",
        evidence_key="file:mini_ork/steering/decision_service.py:decide:screened-out-not-returned",
        files=("mini_ork/lane_router.py", "mini_ork/steering/decision_service.py"),
        detail="lane_router passes screened_out into its detail payload; decide()'s return "
               "dict has no such key, so the screening decision is unobservable after the fact.",
        evidence={"produced_in": "mini_ork/lane_router.py", "decide_return_keys": sorted(keys)},
        fix_hint="Return screened_out from decide() and store it with the routing decision.",
        success_metric="decide() returns screened_out",
    )]


def _r5_brain_and_policy_decisions_empty(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Both decision journals exist and are empty."""
    present = [t for t in ("brain_decisions", "policy_decisions") if _table_exists(ctx, t)]
    if len(present) < 2:
        raise _SkipRule(f"only {present} present")
    counts = {t: _scalar(ctx, f'SELECT count(*) FROM "{t}"') for t in present}
    if any(counts.values()):
        raise _GapClosed(f"decision journals have rows: {counts}")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="brain_decisions and policy_decisions are both empty",
        evidence_key="table:brain_decisions+policy_decisions:empty",
        files=("mini_ork/dispatch/routing.py",),
        detail="Both journals are created and neither is written; the router's reasoning "
               "and policy evaluations leave no record.",
        evidence={"tables": counts},
        fix_hint="Write the decision journal entry where the router commits to a lane.",
        success_metric="either journal gains a row",
    )]


def _r6_stale_routing_signals(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Win-rate and conductor tables stopped refreshing."""
    targets = (
        ("prompt_win_rates", "last_updated", "prompt win rates",
         "mini_ork/learning/rho_aggregator.py"),
        ("topology_win_rates", "last_updated", "topology win rates",
         "mini_ork/orchestration/topology.py"),
        ("conductor_decisions", "decided_at", "conductor decisions",
         "mini_ork/learning/writeback.py"),
    )
    stale: list[Candidate] = []
    seen_any = False
    for table, column, label, writer in targets:
        if not _table_exists(ctx, table):
            continue
        seen_any = True
        if column not in _columns(ctx, table):
            raise _SchemaDrift(f"{table} has no column {column!r}")
        rows = _rows(ctx, f'SELECT "{column}" FROM "{table}"')
        stamps = [ts for ts in (parse_ts(r[0]) for r in rows) if ts is not None]
        if not stamps:
            raise _SkipRule(f"{table} has no parseable timestamps")
        age = _age_days(ctx.now, max(stamps))
        if age <= ctx.int("stale_days"):
            continue
        stale.append(_cand(
            ctx, domain, rule_id,
            kind="stale_signal",
            title=f"{label} stopped refreshing {age} days ago",
            evidence_key=f"table:{table}:stale",
            files=(writer,),
            detail=f"newest row is {age}d old (threshold {ctx.int('stale_days')}d).",
            evidence={"table": table, "rows": len(rows), "age_days": age},
            reach=len(rows),
            age_days=age,
            fix_hint="Find the writer and why it stopped; the table feeds routing choices.",
            success_metric=f"{table} gains a row inside the window",
        ))
    if not seen_any:
        raise _SkipRule("no routing signal tables present")
    if not stale:
        raise _GapClosed("routing signals are fresh")
    return stale


def _r7_workflow_candidates_flat(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Mutation candidates are recorded with zero measured utility change."""
    _require_table(ctx, "workflow_candidates")
    _require_column(ctx, "workflow_candidates", "utility_delta")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM workflow_candidates WHERE utility_delta = 0",
        "SELECT count(*) FROM workflow_candidates",
    )
    if total == 0 or ratio <= 0.9:
        raise _GapClosed(f"zero-delta ratio {ratio:.2f}")
    return [_cand(
        ctx, domain, rule_id,
        kind="unused_capability",
        title="workflow_candidates always measure utility_delta=0",
        evidence_key="table:workflow_candidates:utility_delta:zero",
        files=("mini_ork/orchestration/workflow_lifecycle.py",),
        detail=f"{ratio:.1%} of {total} candidates show zero measured delta — the mutation "
               "search cannot tell a good mutation from a no-op.",
        evidence={"table": "workflow_candidates", "rows": total, "zero_ratio": round(ratio, 4)},
        reach=total,
        fix_hint="Benchmark mutations against a baseline before recording a zero.",
        success_metric="zero-delta ratio falls below 0.9",
    )]


def _r8_llm_calls_no_lane(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Cost rows cannot be attributed to a lane.

    The column's *absence* is this rule's finding, so it must not go through
    ``_require_column``: that raises ``_SchemaDrift``, which the dispatcher turns
    into a ``probe_broken`` candidate about the rule itself. The two readings
    differ operationally — ``doctor`` treats schema drift as "the harvester
    cannot look" and reports the checkout unready, while a real gap is an
    ordinary queue item that the loop can fix. ``_columns`` still raises
    ``_SkipRule`` when the table itself is absent, so an old home stays a skip.
    """
    if "lane" in _columns(ctx, "llm_calls"):
        raise _GapClosed("llm_calls has a lane column")
    rows = _scalar(ctx, "SELECT count(*) FROM llm_calls")
    return [_cand(
        ctx, domain, rule_id,
        kind="signal_not_propagated",
        title="llm_calls records no lane, so cost cannot be attributed to a route",
        evidence_key="table:llm_calls:lane:absent",
        files=("mini_ork/dispatch/telemetry.py", "mini_ork/dispatch/llm_dispatch.py"),
        detail="The dispatch knows which lane served a call, but the cost row drops it. "
               "Per-lane cost, win-rate and routing credit are then unattributable.",
        evidence={"table": "llm_calls", "column": "lane", "rows": rows},
        reach=rows,
        fix_hint="Add a lane column and write it from the dispatch path that already "
                 "resolves the lane.",
        success_metric="llm_calls has a lane column and new rows populate it",
    )]


# --------------------------------------------------------------------------
# VERIFICATION
# --------------------------------------------------------------------------

def _v1_confidence_never_recorded(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    _require_table(ctx, "verifier_results")
    _require_column(ctx, "verifier_results", "confidence")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM verifier_results WHERE confidence IS NULL",
        "SELECT count(*) FROM verifier_results",
    )
    if total <= 100 or ratio <= ctx.num("null_ratio_near_total"):
        raise _GapClosed(f"confidence null ratio {ratio:.3f} over {total} rows")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="verifier_results.confidence is never recorded",
        evidence_key="table:verifier_results:confidence:null",
        files=("mini_ork/gates/verifier_rubric.py",),
        detail=f"{ratio:.1%} of {total} results have NULL confidence; escalation has no "
               "uncertainty signal to threshold on.",
        evidence={"table": "verifier_results", "rows": total, "null_ratio": round(ratio, 4)},
        reach=total,
        fix_hint="Record the verifier's confidence wherever verdicts are written.",
        success_metric="confidence null ratio falls below 0.99",
    )]


def _v2_rubrics_absent(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    present = [t for t in ("verifier_rubrics", "verifier_criteria") if _table_exists(ctx, t)]
    if not present:
        raise _SkipRule("neither rubric table exists")
    counts = {t: _scalar(ctx, f'SELECT count(*) FROM "{t}"') for t in present}
    if any(counts.values()):
        raise _GapClosed(f"rubrics exist: {counts}")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="no verifier rubrics or criteria exist — scoring is unstructured",
        evidence_key="table:verifier_rubrics+verifier_criteria:empty",
        files=("mini_ork/gates/verifier_rubric.py",),
        detail="Both tables are empty, so any rubric-scored axis has no definition behind it.",
        evidence={"tables": counts},
        fix_hint="Seed the rubric set a verifier actually uses, or delete the dead read path.",
        success_metric="at least one rubric with its criteria exists",
    )]


def _v3_calibration_never_recorded(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """FP/FN labels and repair links are never written on any result."""
    _require_table(ctx, "verifier_results")
    for column in ("is_false_positive", "is_false_negative", "repair_run_id"):
        _require_column(ctx, "verifier_results", column)
    total = _scalar(ctx, "SELECT count(*) FROM verifier_results")
    if total == 0:
        raise _SkipRule("verifier_results is empty")
    fp = _scalar(ctx, "SELECT count(*) FROM verifier_results WHERE is_false_positive=1")
    fn = _scalar(ctx, "SELECT count(*) FROM verifier_results WHERE is_false_negative=1")
    repairs = _scalar(ctx, "SELECT count(*) FROM verifier_results WHERE repair_run_id IS NOT NULL")
    if fp or fn or repairs:
        raise _GapClosed(f"calibration present: fp={fp} fn={fn} repairs={repairs}")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="verifier calibration ground truth is never recorded",
        evidence_key="table:verifier_results:calibration:never-recorded",
        files=("mini_ork/cli/verifier.py", "mini_ork/gates/verifier_rubric.py"),
        detail=f"{total} results, 0 false positives, 0 false negatives, 0 repair links. Any "
               "precision claim is unfalsifiable.",
        evidence={"table": "verifier_results", "rows": total},
        reach=total,
        fix_hint="Wire the annotator and the repair-run linkage into the result writer.",
        success_metric="at least one labelled result or repair link exists",
    )]


def _v4_defect_attribution_absent(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    _require_table(ctx, "defect_attributions")
    rows = _scalar(ctx, "SELECT count(*) FROM defect_attributions")
    if rows:
        raise _GapClosed(f"{rows} attributions recorded")
    return [_cand(
        ctx, domain, rule_id,
        kind="producer_missing",
        title="defect_attributions is never written — blame never reaches a lane",
        evidence_key="table:defect_attributions:empty",
        files=("mini_ork/learning/advantage_store.py",),
        detail="The table is created and empty; credit assignment across lanes has no input.",
        evidence={"table": "defect_attributions", "rows": 0},
        fix_hint="Attribute a defect when a run fails a verification it previously passed.",
        success_metric="defect_attributions gains a row after a blamed failure",
    )]


def _v5_gauntlet_and_safety_absent(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """No gauntlet/validation/safety evidence exists at all."""
    candidates = ("gauntlet_runs", "gauntlet_results", "validations", "safety_checks")
    present = [t for t in candidates if _table_exists(ctx, t)]
    if not present:
        raise _SkipRule("no gauntlet/safety tables exist")
    counts = {t: _scalar(ctx, f'SELECT count(*) FROM "{t}"') for t in present}
    if any(counts.values()):
        raise _GapClosed(f"gauntlet evidence exists: {counts}")
    return [_cand(
        ctx, domain, rule_id,
        kind="coverage_gap",
        title="no gauntlet, validation or safety evidence is stored",
        evidence_key="table:gauntlet+validations+safety:empty",
        files=("mini_ork/memory/store.py",),
        detail=f"Present but empty: {sorted(counts)}.",
        evidence={"tables": counts},
        fix_hint="Persist the gauntlet verdict; it exists at runtime but leaves no trace.",
        success_metric="at least one gauntlet/validation/safety row exists",
    )]


def _v6_oversight_backlog(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    _require_table(ctx, "mo_inbox_gates")
    for column in ("status", "enqueued_at"):
        _require_column(ctx, "mo_inbox_gates", column)
    pending = _scalar(ctx, "SELECT count(*) FROM mo_inbox_gates WHERE status='pending'")
    stamps = [ts for ts in (parse_ts(r[0]) for r in _rows(
        ctx, "SELECT enqueued_at FROM mo_inbox_gates WHERE status='pending'")) if ts is not None]
    oldest = max((_age_days(ctx.now, ts) for ts in stamps), default=0)
    if pending < ctx.int("inbox_pending_max") and oldest <= ctx.int("inbox_oldest_days"):
        raise _GapClosed(f"{pending} pending, oldest {oldest}d")
    return [_cand(
        ctx, domain, rule_id,
        kind="stale_signal",
        title="oversight inbox has an unprocessed backlog",
        evidence_key="table:mo_inbox_gates:backlog",
        files=("mini_ork/cli/oversight.py",),
        detail=f"{pending} pending gates, oldest {oldest}d.",
        evidence={"pending": pending, "oldest_days": oldest},
        reach=pending,
        age_days=oldest,
        fix_hint="Resolve or expire the backlog; pending gates block dispatch for the feature.",
        success_metric="pending count and oldest age are inside the thresholds",
    )]


def _v7_promotions_unbenchmarked(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    _require_table(ctx, "promotion_records")
    _require_column(ctx, "promotion_records", "benchmark_run_id")
    ratio, total = _ratio(
        ctx,
        "SELECT count(*) FROM promotion_records WHERE benchmark_run_id IS NULL",
        "SELECT count(*) FROM promotion_records",
    )
    if total == 0 or ratio <= ctx.num("null_ratio_high"):
        raise _GapClosed(f"benchmark_run_id null ratio {ratio:.2f}")
    return [_cand(
        ctx, domain, rule_id,
        kind="data_path_dead",
        title="promotions carry no benchmark run — they were not measured",
        evidence_key="table:promotion_records:benchmark_run_id:null",
        files=("mini_ork/gates/promotion_gate.py",),
        detail=f"{ratio:.1%} of {total} promotions have no benchmark_run_id.",
        evidence={"table": "promotion_records", "rows": total, "null_ratio": round(ratio, 4)},
        reach=total,
        fix_hint="Require a benchmark run before a promotion record is written.",
        success_metric="benchmark_run_id null ratio falls below 0.9",
    )]


def _v8_level_vector_unstored(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Level vectors are emitted at runtime but no table stores them."""
    mentions = _code_mentions(ctx.root, "level_vector")
    if not mentions:
        raise _SkipRule("level vectors are not produced")
    stored = False
    for row in _rows(ctx, "SELECT name FROM sqlite_master WHERE type='table'"):
        table = str(row[0])
        if "level_vector" in {str(r[1]) for r in ctx.con.execute(f'PRAGMA table_info("{table}")')}:
            stored = True
            break
    if stored:
        raise _GapClosed("a table stores level vectors")
    return [_cand(
        ctx, domain, rule_id,
        kind="coverage_gap",
        title="level vectors are produced but stored in no table",
        evidence_key="table:level_vector:no-storage",
        files=tuple(normalize_path(str(p.relative_to(ctx.root))) for p in mentions[:3]),
        detail="Produced in code, absent from the schema, so the verification level vector "
               "cannot be compared across runs.",
        evidence={"mentions": [normalize_path(str(p.relative_to(ctx.root))) for p in mentions]},
        reach=len(mentions),
        fix_hint="Add a level_vectors table (integer epoch) and write on verify.",
        success_metric="a table with a level_vector column exists",
    )]


def _v9_apply_stale(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    """Apply attempts stopped arriving (parsed temporally, never lexicographically)."""
    _require_table(ctx, "apply_attempts")
    _require_column(ctx, "apply_attempts", "created_at")
    stamps = [ts for ts in (parse_ts(r[0]) for r in _rows(
        ctx, "SELECT created_at FROM apply_attempts")) if ts is not None]
    if not stamps:
        raise _SkipRule("no parseable apply timestamps")
    age = _age_days(ctx.now, max(stamps))
    if age <= ctx.int("stale_days"):
        raise _GapClosed(f"newest apply is {age}d old")
    return [_cand(
        ctx, domain, rule_id,
        kind="stale_signal",
        title=f"apply_attempts stopped {age} days ago",
        evidence_key="table:apply_attempts:stale",
        files=("mini_ork/cli/apply.py",),
        detail=f"newest attempt is {age}d old; the apply loop has not run.",
        evidence={"table": "apply_attempts", "rows": len(stamps), "age_days": age},
        reach=len(stamps),
        age_days=age,
        fix_hint="Find why apply stopped being attempted; it gates the whole improve loop.",
        success_metric="a new apply attempt appears inside the window",
    )]


def _v12_vacuous_verdicts(ctx: _Ctx, domain: str, rule_id: str) -> list[Candidate]:
    _require_table(ctx, "verifier_results")
    _require_column(ctx, "verifier_results", "verdict")
    rows = _scalar(ctx, "SELECT count(*) FROM verifier_results WHERE verdict='vacuous'")
    if rows == 0:
        raise _GapClosed("no vacuous verdicts")
    total = _scalar(ctx, "SELECT count(*) FROM verifier_results")
    return [_cand(
        ctx, domain, rule_id,
        kind="quality_red",
        title="verifiers are returning vacuous verdicts",
        evidence_key="table:verifier_results:verdict:vacuous",
        files=("mini_ork/gates/verifier_rubric.py",),
        detail=f"{rows}/{total} verdicts are 'vacuous' — the gate ran but proved nothing.",
        evidence={"table": "verifier_results", "vacuous": rows, "rows": total},
        reach=rows,
        fix_hint="Find the rubric path that returns vacuous and give it a real check.",
        success_metric="no vacuous verdicts are produced",
    )]


# --------------------------------------------------------------------------
# Rule registry
# --------------------------------------------------------------------------

Rule = Callable[[_Ctx, str, str], list[Candidate]]

LEARNING_RULES: tuple[tuple[str, Rule], ...] = (
    ("L1", _l1_pass_stats_never_written),
    ("L2", _l2_reflection_log_never_written),
    ("L3", _l3_held_out_never_populated),
    ("L4", _l4_malformed_timestamps),
    ("L5", _l5_promoted_to_never_set),
    ("L6", _l6_lesson_text_missing),
    ("L7", _l7_collapse_history_under_min),
    # L8 (verifier audit) is a probe: it has no CLI verb and runs in-process.
    ("L9", _l9_benchmark_delta_never_computed),
    ("L10", _l10_lesson_conflicts_never_found),
    ("L11", _l11_diagnosis_unlabelled),
    ("L12", _l12_verifier_pass_rate_regression),
)

ROUTING_RULES: tuple[tuple[str, Rule], ...] = (
    ("R1", _r1_decide_without_segment),
    ("R2", _r2_learned_route_underused),
    ("R3", _r3_code_region_null),
    ("R4", _r4_screened_out_dropped),
    ("R5", _r5_brain_and_policy_decisions_empty),
    ("R6", _r6_stale_routing_signals),
    ("R7", _r7_workflow_candidates_flat),
    ("R8", _r8_llm_calls_no_lane),
)

VERIFICATION_RULES: tuple[tuple[str, Rule], ...] = (
    ("V1", _v1_confidence_never_recorded),
    ("V2", _v2_rubrics_absent),
    ("V3", _v3_calibration_never_recorded),
    ("V4", _v4_defect_attribution_absent),
    ("V5", _v5_gauntlet_and_safety_absent),
    ("V6", _v6_oversight_backlog),
    ("V7", _v7_promotions_unbenchmarked),
    ("V8", _v8_level_vector_unstored),
    ("V9", _v9_apply_stale),
    # V10 (failing tests) and V11 (garden/validate findings) come from probes.
    ("V12", _v12_vacuous_verdicts),
)

ALL_RULES: tuple[tuple[str, tuple[tuple[str, Rule], ...]], ...] = (
    ("learning", LEARNING_RULES),
    ("routing", ROUTING_RULES),
    ("verification", VERIFICATION_RULES),
)

#: Probe findings that map to candidates, and the kind they carry.
PROBE_KINDS: Mapping[str, str] = {
    "garden": "quality_red",
    "validate": "quality_red",
    "tests": "regression",
    "verifier-audit": "quality_red",
}


# --------------------------------------------------------------------------
# Probe → candidate
# --------------------------------------------------------------------------

def _from_probes(
    ctx: _Ctx, probes: Sequence[ProbeResult]
) -> tuple[list[Candidate], bool, list[str]]:
    """Map probe results to candidates. Returns (candidates, ok, required_failures)."""
    out: list[Candidate] = []
    ok = True
    failures: list[str] = []
    for result in probes:
        if result.failed:
            if result.required:
                ok = False
                failures.append(result.probe_id)
            out.append(_cand(
                ctx, "verification", result.probe_id,
                kind="probe_broken",
                title=f"probe {result.probe_id} {result.status}",
                evidence_key=f"probe:{result.probe_id}:{result.status}",
                detail=f"cmd: {result.cmd}; rc={result.rc}; {result.payload.get('error', '')}".strip(),
                evidence={"probe_id": result.probe_id, "status": result.status,
                          "rc": result.rc, "stderr_digest": result.stderr_digest},
                source=f"probe:{result.probe_id}",
                success_command=result.probe_id,
                fix_hint="Fix the probe before trusting any cycle that reports no gaps.",
            ))
            continue
        kind = PROBE_KINDS.get(result.probe_id, "quality_red")
        findings = _admitted(result, ctx.int("per_source_max"))
        for finding in findings:
            path = finding.path
            out.append(_cand(
                ctx, "verification", result.probe_id,
                kind=kind,
                title=f"{result.probe_id}: {_headline(finding.message)}",
                evidence_key=f"probe:{result.probe_id}:{finding.signature()}",
                files=(path,) if path else (),
                detail=finding.message,
                evidence={"probe_id": result.probe_id, "level": finding.level,
                          "finding": finding.to_dict(), "rc": result.rc},
                source=f"probe:{result.probe_id}",
                success_command=result.probe_id,
                fix_hint="Resolve the finding; the differential gate requires it to disappear.",
            ))
    return out, ok, failures


def _admitted(result: ProbeResult, cap: int) -> tuple[Finding, ...]:
    """Deterministic, capped slice of a probe's findings.

    Sorted by signature before the cap so two cycles on the same tree admit the
    same findings — otherwise the queue would wander for no reason.
    """
    ordered = sorted(result.findings, key=lambda f: f.signature())
    return tuple(ordered[: max(0, int(cap))])


def _headline(message: str, limit: int = 110) -> str:
    text = " ".join(str(message or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

_RULE_INDEX: dict[str, tuple[str, Rule]] = {
    rule_id: (domain, rule)
    for domain, rules in ALL_RULES
    for rule_id, rule in rules
}


def rule_ids() -> tuple[str, ...]:
    """Every registered rule id, in harvest order."""
    return tuple(_RULE_INDEX)


def run_rule(
    rule_id: str,
    root: str | Path,
    con: sqlite3.Connection,
    *,
    now: int,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[Candidate, ...]:
    """Run ONE rule, for the synthetic ``db:<RULE>`` success check.

    The mapping differs from :func:`harvest` on exactly one axis — what an
    absence means:

    * ``_GapClosed`` — the rule read its evidence and the defect is gone. That
      is a fix: return no candidates.
    * ``_SkipRule`` — the rule could not read its evidence (absent table, empty
      input, no call sites). This must **not** be read as "fixed": G8 would then
      merge a fix against a data path nobody checked. Raises, so the probe is an
      ``error`` and the gate fails closed.
    * ``_SchemaDrift`` — a present table lost a column. Also an error, and per
      the plan drift is itself a bug.
    """
    entry = _RULE_INDEX.get(rule_id)
    if entry is None:
        raise LookupError(f"unknown harvest rule {rule_id!r}")
    domain, rule = entry
    ctx = _Ctx(root=Path(root), con=con, now=int(now),
               cfg={**DEFAULT_CFG, **dict(cfg or {})})
    try:
        return tuple(rule(ctx, domain, rule_id))
    except _GapClosed:
        return ()
    except _SkipRule as exc:
        raise RuntimeError(f"rule {rule_id} cannot answer: {exc}") from exc
    except _SchemaDrift as exc:
        raise RuntimeError(f"rule {rule_id} schema drift: {exc}") from exc


def harvest(
    root: str | Path,
    con: sqlite3.Connection,
    *,
    now: int,
    cfg: Mapping[str, Any] | None = None,
    probes: Sequence[ProbeResult] = (),
) -> HarvestReport:
    """Run every rule and every probe mapping. Reads only; returns the evidence."""
    merged = {**DEFAULT_CFG, **dict(cfg or {})}
    ctx = _Ctx(root=Path(root), con=con, now=int(now), cfg=merged)
    candidates: list[Candidate] = []
    skipped: list[str] = []
    closed: list[str] = []
    errors: list[str] = []

    for domain, rules in ALL_RULES:
        for rule_id, rule in rules:
            try:
                candidates.extend(rule(ctx, domain, rule_id))
            except _GapClosed:
                # Checked BEFORE _SkipRule: it subclasses it. A closed rule read
                # its own tables and found them healthy, so reconciliation may
                # trust its absence; a skipped rule could not read them at all.
                closed.append(rule_id)
            except _SkipRule:
                skipped.append(rule_id)
            except _SchemaDrift as exc:
                candidates.append(_broken(ctx, rule_id, domain, str(exc)))
            except Exception as exc:  # noqa: BLE001 - a rule crash is evidence, not a stop
                errors.append(rule_id)
                candidates.append(_broken(
                    ctx, rule_id, domain, f"rule crashed: {type(exc).__name__}: {exc}"
                ))

    probe_candidates, probe_ok, probe_failures = _from_probes(ctx, probes)
    candidates.extend(probe_candidates)
    for probe_id in probe_failures:
        if probe_id not in errors:
            errors.append(probe_id)

    return HarvestReport(
        candidates=tuple(dedupe(candidates)),
        probes=tuple(probes),
        harvest_ok=probe_ok and not errors,
        skipped=tuple(skipped),
        errors=tuple(errors),
        closed=tuple(closed),
    )


__all__ = [
    "ALL_RULES",
    "DEFAULT_CFG",
    "HarvestReport",
    "LEARNING_RULES",
    "PROBE_KINDS",
    "ROUTING_RULES",
    "SYNTHETIC_PREFIX",
    "VERIFICATION_RULES",
    "harvest",
    "parse_ts",
    "rule_ids",
    "run_rule",
]
