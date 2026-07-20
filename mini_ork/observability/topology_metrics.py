"""Pure-logic + DB-access port of ``lib/topology_metrics.sh``.

Faithful port of the deterministic panel-topology measurement logic in
``lib/topology_metrics.sh``. The bash function (a wrapper around several
inline ``python3 - <<'PY'`` heredocs) reads from ``execution_traces``,
computes three axes of realised panel topology — ρ (output correlation),
C (context formation distance), I (inductive prior distance) — and
classifies them into one of eight quadrants from the framework doc:

    docs/_meta/research/20260602-2030-context-formation-diversity-framework-multi-agent-panels.md

This module lifts the heredoc bodies into proper Python functions and
provides both:

  * **Pure helpers** (no I/O):  family_of, classify_quadrant, _quadrant_thresholds,
                                 _compute_rho, _compute_C, _compute_I
  * **DB wrappers** (sqlite3):  ensure_table, measure_rho, measure_C,
                                 measure_I, measure_topology

The bash function ``measure_topology`` is the canonical post-cycle hook.
This module mirrors it 1:1 and writes the row to
``panel_topology_telemetry`` using the SAME column list and the SAME
``pt-<panel_run_id[:16]>-<uuid6>`` ``telemetry_id`` format.

Public API (mirrors bash function names exactly)::

    from mini_ork.observability.topology_metrics import (
        ensure_table,                  # (db_path) -> None
        family_of,                     # (version_id, lane_to_family=None) -> str
        measure_rho,                   # (db_path, panel_run_id) -> float
        measure_C,                     # (db_path, panel_run_id) -> float
        measure_I,                     # (db_path, panel_run_id, root) -> float
        classify_quadrant,             # (rho, C, I) -> str
        measure_topology,              # (db_path, panel_run_id, recipe, root) -> telemetry_id
    )

Co-existence model (strangler-fig): ``lib/topology_metrics.sh`` stays
byte-identical. The Python port mirrors its public API semantically.
Parity is enforced by ``tests/unit/test_topology_metrics_py.py`` (six
live-subprocess parity cases, float tolerance 1e-6, no mocks).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Mapping, Sequence

__all__ = [
    "ensure_table",
    "family_of",
    "classify_quadrant",
    "measure_rho",
    "measure_C",
    "measure_I",
    "measure_topology",
    "_compute_rho",
    "_compute_C",
    "_compute_I",
    "_quadrant_thresholds",
    "FAMILY_CANON",
]

# ─────────────────────────────────────────────────────────────────────────────
# Family canonicalisation map.
#
# Verbatim mirror of lib/topology_metrics.sh lines 192-206. These are
# the distinct training lineages (one per model family). The bash heredoc
# uses this map as a SECOND-STEP lookup (after lane_to_family), so the
# port must do the same. Frozen at import time to avoid config
# non-determinism vs the bash subprocess.
# ─────────────────────────────────────────────────────────────────────────────

FAMILY_CANON: dict[str, str] = {
    "opus": "anthropic", "sonnet": "anthropic", "haiku": "anthropic",
    "opus_lens": "anthropic", "spec_reviewer": "anthropic", "reviewer": "anthropic",
    "brain": "anthropic", "spec_author": "anthropic", "planner": "anthropic",
    "researcher": "anthropic", "implementer": "anthropic", "worker": "anthropic",
    "verifier": "anthropic", "reflector": "anthropic", "publisher": "anthropic",
    "rollback": "anthropic", "bdd_runner": "anthropic", "healer": "anthropic",
    "worker_default": "anthropic", "reviewer_default": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}

# ─────────────────────────────────────────────────────────────────────────────
# Internal constants — verbatim mirror of bash.
# ─────────────────────────────────────────────────────────────────────────────

# Quadrant classification thresholds (line 247-253 of bash):
#   rho >= 0.5 → HIGH
#   C   >= 0.3 → HIGH
#   I   >= 0.5 → HIGH
_QUADRANT_THRESHOLDS = {"rho": 0.5, "C": 0.3, "I": 0.5}

# 8-way mapping (lines 256-265). Tuple is (rho_band, C_band, I_band).
_QUADRANT_TABLE: dict[tuple[str, str, str], str] = {
    ("high", "low",  "low"):  "coalition",
    ("low",  "low",  "low"):  "noise",
    ("high", "high", "low"):  "convergent_corroboration",
    ("low",  "high", "low"):  "genuine_perspective_split",
    ("high", "low",  "high"): "forced_consensus_shared_evidence",
    ("low",  "low",  "high"): "prior_driven_disagreement",
    ("high", "high", "high"): "submodular_gain_target",
    ("low",  "high", "high"): "high_variance_discovery",
}

# Verbatim mirror of the bash heredoc WHERE clause. The bash uses two
# LIKE patterns: substring match anywhere + the role-encoded prefix form.
_TRACES_SELECT_SQL = """
SELECT trace_id, agent_version_id, reviewer_verdict, verifier_output
  FROM execution_traces
 WHERE trace_id LIKE ? OR trace_id LIKE ?
"""

_TRACES_SELECT_FILES_TOOLS_SQL = """
SELECT trace_id, files_read, tool_calls
  FROM execution_traces
 WHERE trace_id LIKE ? OR trace_id LIKE ?
"""

_TRACES_SELECT_AGENT_VERSION_SQL = """
SELECT trace_id, agent_version_id
  FROM execution_traces
 WHERE trace_id LIKE ? OR trace_id LIKE ?
"""

_TRACES_COUNT_SQL = """
SELECT COUNT(*) FROM execution_traces
 WHERE trace_id LIKE ? OR trace_id LIKE ?
"""

_TRACES_COUNT_DISTINCT_AGENT_SQL = """
SELECT COUNT(DISTINCT agent_version_id) FROM execution_traces
 WHERE (trace_id LIKE ? OR trace_id LIKE ?) AND agent_version_id != ''
"""


def _trace_filter_params(panel_run_id: str) -> tuple[str, str]:
    """Return the two LIKE-pattern params the bash heredoc binds for
    ``execution_traces`` lookups by panel_run_id (bash lines 67, 116,
    225, 297, 301). Both ports must use the SAME two patterns."""
    return f"%{panel_run_id}%", f"tr-%-{panel_run_id}%"


# ─────────────────────────────────────────────────────────────────────────────
# Family-of. Pure function over a single version_id string + optional
# lane→family override map. Mirrors the heredoc's family_of (lines 207-217).
# ─────────────────────────────────────────────────────────────────────────────

def family_of(version_id: str | None, lane_to_family: Mapping[str, str] | None = None) -> str:
    """Map ``agent_version_id`` (e.g. ``glm_lens-v3`` or ``sonnet``) to its
    canonical training lineage.

    Mirrors the bash heredoc's two-step lookup:

        base = version_id.split("-")[0].lower()
        if base in lane_to_family:
            return FAMILY_CANON.get(lane_to_family[base], lane_to_family[base])
        return FAMILY_CANON.get(base, base)

    An empty / missing ``version_id`` returns ``"unknown"`` (matches bash).
    The optional ``lane_to_family`` map overrides via canonicalisation —
    the lane value is itself fed through FAMILY_CANON as a second step.
    """
    if not version_id:
        return "unknown"
    base = version_id.split("-")[0].lower()
    if lane_to_family and base in lane_to_family:
        target = lane_to_family[base]
        return FAMILY_CANON.get(target, target)
    return FAMILY_CANON.get(base, base)


# ─────────────────────────────────────────────────────────────────────────────
# Pure topology helpers. These accept trace lists / raw floats and do
# NOT touch sqlite — same convention as mini_ork/orchestration/topology.py.
# ─────────────────────────────────────────────────────────────────────────────

def _compute_rho(verdicts: Sequence[str | None]) -> float:
    """Mirror of the bash heredoc in ``measure_rho`` (lines 53-99).

    For each trace, prefer ``reviewer_verdict``; fall back to
    ``verifier_output`` if verdict is falsy. Skip rows where both are
    falsy. Agreement proxy:
      * If first 50 chars (stripped + lowercased) match exactly → agree.
      * Else compute token-Jaccard over first 200 chars; ≥ 0.5 → agree.
    Returns ``agreeing / pairs`` rounded to 4 decimals. Fewer than 2
    qualifying verdicts → 0.0 (single agent has no pairwise distance).
    """
    cleaned: list[str] = []
    for v in verdicts:
        s = (v or "").strip().lower()
        if s:
            cleaned.append(s)
    if len(cleaned) < 2:
        return 0.0

    def head(s: str, n: int = 50) -> str:
        return s[:n]

    pairs = 0
    agreeing = 0
    n = len(cleaned)
    for i in range(n):
        for j in range(i + 1, n):
            hi = head(cleaned[i])
            hj = head(cleaned[j])
            if not hi or not hj:
                continue
            pairs += 1
            if hi == hj:
                agreeing += 1
                continue
            ti = set(head(cleaned[i], 200).split())
            tj = set(head(cleaned[j], 200).split())
            if ti and tj:
                jacc = len(ti & tj) / len(ti | tj)
                if jacc >= 0.5:
                    agreeing += 1
    rho = (agreeing / pairs) if pairs > 0 else 0.0
    return round(rho, 4)


def _compute_C(context_rows: Sequence[tuple[list[str], list[dict]]]) -> float:
    """Mirror of the bash heredoc in ``measure_C`` (lines 107-162).

    Each input row is ``(files_read_list, tool_calls_list)``. Tool-call
    signatures are ``f"{tool}:{first_key}={first_val[:80]}"``. Per-row
    context = frozenset(files_read ∪ tool_sigs). Empty contexts are
    dropped. Mean pairwise Jaccard distance over the surviving set;
    n < 2 → 0.0. Rounded to 4 decimals.
    """
    contexts: list[frozenset] = []
    for files, tools in context_rows:
        tool_sigs: list[str] = []
        for tc in tools:
            if isinstance(tc, dict):
                name = tc.get("tool", "?")
                inp = tc.get("input", {}) or {}
                first_key = next(iter(inp.keys()), "")
                first_val = str(inp.get(first_key, ""))[:80]
                tool_sigs.append(f"{name}:{first_key}={first_val}")
        ctx = frozenset(list(files) + tool_sigs)
        if ctx:
            contexts.append(ctx)

    if len(contexts) < 2:
        return 0.0

    def jaccard_dist(a: frozenset, b: frozenset) -> float:
        union = a | b
        if not union:
            return 0.0
        return 1.0 - (len(a & b) / len(union))

    total = 0.0
    pairs = 0
    n = len(contexts)
    for i in range(n):
        for j in range(i + 1, n):
            total += jaccard_dist(contexts[i], contexts[j])
            pairs += 1
    mean_C = total / pairs if pairs > 0 else 0.0
    return round(mean_C, 4)


def _compute_I(version_ids: Sequence[str | None],
                lane_to_family: Mapping[str, str] | None = None) -> float:
    """Mirror of the bash heredoc in ``measure_I`` (lines 168-242).

    Pairwise distance over families: 0.0 if same, 1.0 if different.
    ``version_ids`` may contain falsy values (skipped). n < 2 → 0.0.
    Rounded to 4 decimals.
    """
    families = [family_of(v, lane_to_family) for v in version_ids if v]
    if len(families) < 2:
        return 0.0

    total = 0.0
    pairs = 0
    n = len(families)
    for i in range(n):
        for j in range(i + 1, n):
            total += 0.0 if families[i] == families[j] else 1.0
            pairs += 1
    mean_I = total / pairs if pairs > 0 else 0.0
    return round(mean_I, 4)


def classify_quadrant(rho: float, C: float, I: float) -> str:
    """8-way classification of (ρ, C, I) per the framework doc.

    Verbatim mirror of bash ``_topology_quadrant`` lines 247-267:
      * rho >= 0.5 → HIGH
      * C   >= 0.3 → HIGH
      * I   >= 0.5 → HIGH
    Unknown keys → ``"unclassified"``.
    """
    rh = "high" if rho >= _QUADRANT_THRESHOLDS["rho"] else "low"
    ch = "high" if C   >= _QUADRANT_THRESHOLDS["C"]   else "low"
    ih = "high" if I   >= _QUADRANT_THRESHOLDS["I"]   else "low"
    return _QUADRANT_TABLE.get((rh, ch, ih), "unclassified")


def _quadrant_thresholds() -> dict[str, float]:
    """Expose the thresholds dict for inspection by parity tests."""
    return dict(_QUADRANT_THRESHOLDS)


# ─────────────────────────────────────────────────────────────────────────────
# DB-access wrappers. These open the DB, fetch traces, delegate to the
# pure helpers, and (for ``measure_topology``) persist a row to
# panel_topology_telemetry. The SQL bodies match the bash heredocs verbatim
# apart from the substitution of the two LIKE params (which we materialise
# via ``_trace_filter_params``).
# ─────────────────────────────────────────────────────────────────────────────

def ensure_table(db_path: str) -> None:
    """Idempotently create ``panel_topology_telemetry`` from migration 0015.

    Mirrors bash ``_topology_ensure_table`` (lines 28-46), but the port
    reads the migration SQL out of a sibling file at import-time. If the
    migration file is absent the no-op silently passes (matches bash).

    Uses ``CREATE TABLE IF NOT EXISTS`` semantics so repeated calls are
    safe within a single Python process — the bash function uses a
    module-scoped guard (``_MO_TOPOLOGY_SCHEMA_INIT``) for the same reason.
    """
    mig_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "db", "migrations", "0015_panel_topology_telemetry.sql",
    )
    if not os.path.isfile(mig_path):
        return
    with open(mig_path) as fh:
        sql = fh.read()
    con = sqlite3.connect(db_path)
    con.executescript(sql)
    con.commit()
    con.close()


def _load_lane_to_family(root: str | None) -> dict[str, str]:
    """Build the optional ``lane_to_family`` override map.

    Mirrors bash heredoc lines 180-189: read ``config/agents.yaml``
    under ``root`` (best-effort). Missing file or missing yaml lib
    → empty dict; yaml parse errors → empty dict.
    """
    lane_to_family: dict[str, str] = {}
    if not root:
        return lane_to_family
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return lane_to_family
    agents_yaml = os.path.join(root, "config", "agents.yaml")
    if not os.path.isfile(agents_yaml):
        return lane_to_family
    try:
        with open(agents_yaml) as fh:
            data = yaml.safe_load(fh) or {}
        for lane, family in (data.get("lanes") or {}).items():
            lane_to_family[lane] = str(family).strip()
    except Exception:
        return lane_to_family
    return lane_to_family


def measure_rho(db_path: str, panel_run_id: str) -> float:
    """Mirror bash ``measure_rho`` (lines 51-100).

    Opens ``db_path``, fetches ``reviewer_verdict`` + ``verifier_output``
    for every execution_trace whose ``trace_id`` matches the two bash
    LIKE patterns, then delegates to ``_compute_rho``.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        _TRACES_SELECT_SQL, _trace_filter_params(panel_run_id)
    ).fetchall()
    con.close()
    verdicts = [
        (r["reviewer_verdict"] or r["verifier_output"] or "")
        for r in rows
        if (r["reviewer_verdict"] or r["verifier_output"])
    ]
    return _compute_rho(verdicts)


def measure_C(db_path: str, panel_run_id: str) -> float:
    """Mirror bash ``measure_C`` (lines 105-163)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        _TRACES_SELECT_FILES_TOOLS_SQL, _trace_filter_params(panel_run_id)
    ).fetchall()
    con.close()
    parsed: list[tuple[list[str], list[dict]]] = []
    for r in rows:
        try:
            files = json.loads(r["files_read"] or "[]")
        except Exception:
            files = []
        try:
            tools = json.loads(r["tool_calls"] or "[]")
        except Exception:
            tools = []
        parsed.append((files, tools))
    return _compute_C(parsed)


def measure_I(db_path: str, panel_run_id: str,
              root: str | None = None) -> float:
    """Mirror bash ``measure_I`` (lines 168-243).

    Loads the ``lane_to_family`` override from ``<root>/config/agents.yaml``
    (best-effort) and then resolves each trace's family via ``family_of``.
    """
    lane_to_family = _load_lane_to_family(root)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        _TRACES_SELECT_AGENT_VERSION_SQL, _trace_filter_params(panel_run_id)
    ).fetchall()
    con.close()
    return _compute_I([r["agent_version_id"] for r in rows], lane_to_family)


def measure_topology(db_path: str, panel_run_id: str, recipe: str,
                      root: str | None = None) -> str:
    """Mirror bash ``measure_topology`` (lines 274-314).

    Ensures the table exists, computes ρ / C / I, classifies the
    quadrant, then writes a row to ``panel_topology_telemetry`` keyed by
    ``telemetry_id = f"pt-{panel_run_id[:16]}-{uuid.uuid4().hex[:6]}"``.

    Returns the ``telemetry_id`` (printed by bash on stdout). The
    ``target_topology`` column is left NULL (bash INSERT omits it;
    bash also has no recipe-driven target wiring — that is E-MO-03).
    """
    ensure_table(db_path)
    rho = measure_rho(db_path, panel_run_id)
    C = measure_C(db_path, panel_run_id)
    I = measure_I(db_path, panel_run_id, root)
    quadrant = classify_quadrant(rho, C, I)

    telemetry_id = f"pt-{panel_run_id[:16]}-{uuid.uuid4().hex[:6]}"
    params = _trace_filter_params(panel_run_id)

    con = sqlite3.connect(db_path)
    n_traces = con.execute(_TRACES_COUNT_SQL, params).fetchone()[0]
    agent_count = con.execute(_TRACES_COUNT_DISTINCT_AGENT_SQL, params).fetchone()[0]
    con.execute(
        """
        INSERT INTO panel_topology_telemetry
            (telemetry_id, panel_run_id, recipe, rho, context_distance,
             inductive_distance, agent_count, n_traces, quadrant)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            telemetry_id, panel_run_id, recipe,
            float(rho), float(C), float(I),
            int(agent_count), int(n_traces), quadrant,
        ),
    )
    con.commit()
    con.close()
    return telemetry_id
