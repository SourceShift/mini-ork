"""Panel family-diversity + rho coalition gate — Python port of lib/coalition_gate.sh.

Faithful port of the family-distribution query + the verdict logic. `rho` is taken
as an input parameter: it is measured by lib/topology_metrics.sh::measure_rho, which
is not yet ported — check_panel_coalition accepts the measured value (default 0.0,
matching the fail-open path). Verdict semantics match the bash exactly:
  lens_count < 2                         -> panel_diverse (single_agent_run)
  rho < threshold AND family_count==lens -> panel_diverse (ok)
  else strict                            -> COALITION_ABORT (rc=1)
  else advisory                          -> panel_diverse (advisory_*)
"""
from __future__ import annotations

import os
import re
import sqlite3

LANE_TO_FAMILY = {
    "sonnet": "anthropic", "opus": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}


def _load_lane_map(agents_yaml: str | None) -> dict[str, str]:
    m = dict(LANE_TO_FAMILY)
    if not agents_yaml or not os.path.exists(agents_yaml):
        return m
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(open(agents_yaml, encoding="utf-8")) or {}
        for lane, fam in (cfg.get("lanes") or {}).items():
            m.setdefault(lane.lower(), str(fam).lower())
    except ImportError:
        in_lanes = False
        for line in open(agents_yaml, encoding="utf-8"):
            line = line.rstrip()
            if re.match(r"^lanes:\s*$", line):
                in_lanes = True
                continue
            if in_lanes:
                if line and not line.startswith((" ", "\t")):
                    in_lanes = False
                    continue
                mm = re.match(r"^\s+([\w_-]+):\s*([\w_-]+)\s*$", line)
                if mm:
                    m.setdefault(mm.group(1).lower(), mm.group(2).lower())
    return m


def family_of(version_id: str, lane_map: dict[str, str] | None = None) -> str:
    if not version_id:
        return "unknown"
    lm = lane_map or LANE_TO_FAMILY
    base = version_id.split("-")[0].lower()
    return lm.get(base, base)


def family_distribution(db: str, panel_run_id: str, agents_yaml: str | None = None) -> dict:
    lm = _load_lane_map(agents_yaml)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT trace_id, agent_version_id FROM execution_traces "
        "WHERE trace_id LIKE ? OR trace_id LIKE ?",
        (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%"),
    ).fetchall()
    con.close()
    lenses = [r["agent_version_id"] for r in rows if r["agent_version_id"]]
    distrib: dict[str, int] = {}
    for v in lenses:
        f = family_of(v, lm)
        distrib[f] = distrib.get(f, 0) + 1
    return {"lens_count": len(lenses), "family_count": len(distrib),
            "family_distribution": distrib}


def coalition_verdict(rho: float, rho_threshold: float, lens_count: int,
                      family_count: int, distrib: dict, mode: str = "strict") -> dict:
    base = {"rho": rho, "rho_threshold": rho_threshold, "lens_count": lens_count,
            "family_count": family_count, "family_distribution": distrib}
    if lens_count < 2:
        return {**base, "verdict": "panel_diverse", "reason": "single_agent_run",
                "rationale": ("fewer than 2 lens traces present — coalition check "
                              "not applicable")}
    high_rho = rho >= rho_threshold
    collision = family_count < lens_count
    if not high_rho and not collision:
        return {**base, "verdict": "panel_diverse", "reason": "ok",
                "rationale": (f"ρ={rho:.3f} < {rho_threshold:.3f}, "
                              f"family_count={family_count} == lens_count="
                              f"{lens_count} — panel is family-diverse")}
    reason = "both" if (high_rho and collision) else (
        "high_rho" if high_rho else "family_collision")
    parts = []
    if high_rho:
        parts.append(f"ρ={rho:.3f} >= {rho_threshold:.3f} "
                     "(Rajan 2025 submodularity ceiling)")
    if collision:
        parts.append(f"family_count={family_count} < lens_count={lens_count} "
                     "(Bertalanič 2026 family-diversity precondition violated)")
    if mode.lower() == "advisory":
        return {**base, "verdict": "panel_diverse", "reason": f"advisory_{reason}",
                "advisory_note": "; ".join(parts),
                "rationale": ("MO_FAMILY_DIVERSITY_GATE=advisory — emitting warning "
                              "but proceeding with synthesis")}
    return {**base, "verdict": "COALITION_ABORT", "reason": reason,
            "rationale": "; ".join(parts),
            "remediation": ("widen family diversity in config/agents.yaml (one "
                            "lane per family), OR accept the coalition-flagged "
                            "lens reports without synthesis, OR set "
                            "MO_FAMILY_DIVERSITY_GATE=advisory to bypass")}


def check_panel_coalition(panel_run_id: str, recipe: str, rho: float = 0.0,
                          db: str | None = None, agents_yaml: str | None = None,
                          rho_threshold: float | None = None,
                          mode: str | None = None) -> tuple[dict, int]:
    """Compose the gate. `rho` comes from measure_rho (passed in). Returns
    (verdict_dict, rc) where rc=1 on COALITION_ABORT (matches bash exit code)."""
    db = db or os.environ.get("MINI_ORK_DB")
    if rho_threshold is None:
        rho_threshold = float(os.environ.get("MO_RHO_THRESHOLD", "0.25"))
    mode = mode or os.environ.get("MO_FAMILY_DIVERSITY_GATE", "strict")
    dist = family_distribution(db, panel_run_id, agents_yaml)
    v = coalition_verdict(rho, rho_threshold, dist["lens_count"],
                          dist["family_count"], dist["family_distribution"], mode)
    return v, (1 if v["verdict"] == "COALITION_ABORT" else 0)
