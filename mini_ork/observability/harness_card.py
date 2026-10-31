"""The harness card: which harness, at which release, served each node of a run.

A run's result depends on the harness as much as on the model — a harness swap
alone moves cost and tokens by multiples — and a fallback can quietly serve a
node from a different family than the recipe asked for: a four-family review
panel ran as three when two lenses fell back to the same lane, and nothing in
the run said so. The card discloses both, from records the run already wrote:

* the run's ``llm_calls`` rows — lane, ``metadata_json.harness``, status,
  tokens and cost;
* the run dir's ``.lane-<node>.json`` records — the lane each node requested
  and the one that served it;
* the audit chain's latest ``conformance_probe`` per served lane — whether that
  harness release has passed a conformance probe.

``build`` returns the card as a dict; ``write`` stores ``harness-card.json`` and
``harness-card.md`` in the run dir. Nothing here dispatches or changes a run.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

CARD_SCHEMA = "mini-ork.harness-card/v1"
_UNRECORDED = "unrecorded"


def _family(lane: str) -> str:
    from mini_ork.verify.panel_heterogeneity import lane_family

    try:
        return lane_family(lane) or lane
    except Exception:  # noqa: BLE001 — an unresolvable lane is still a lane
        return lane


def _calls(db: str | None, run_id: str) -> list[dict]:
    if not db or not os.path.isfile(db):
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT model_id, actor, feature_name, status, cost_usd, input_tokens, output_tokens, "
            "metadata_json FROM llm_calls WHERE run_id=? AND COALESCE(feature_name, '') != ? ORDER BY rowid",
            # A conformance probe checks a lane; it is not the run's work.
            (run_id, "mini-ork:conformance"),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for model_id, actor, feature, status, cost, tok_in, tok_out, metadata in rows:
        try:
            harness = (json.loads(metadata or "{}") or {}).get("harness") or {}
        except (TypeError, ValueError):
            harness = {}
        out.append({
            "lane": model_id or "", "actor": actor or "", "feature": feature or "",
            "status": status or "", "cost_usd": float(cost or 0.0),
            "tokens": int(tok_in or 0) + int(tok_out or 0),
            "harness": str(harness.get("harness") or _UNRECORDED),
            "version": str(harness.get("version") or ""),
        })
    return out


def _nodes(run_dir: str) -> list[dict]:
    nodes = []
    for path in sorted(Path(run_dir).glob(".lane-*.json")):
        node = path.name[len(".lane-"):-len(".json")]
        if node in {"affinity", "post-run"}:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not record.get("served"):
            continue
        requested = str(record.get("requested") or record["served"])
        served = str(record["served"])
        nodes.append({
            "node": node, "requested": requested, "served": served,
            "fallback": served != requested,
            "requested_family": _family(requested), "served_family": _family(served),
        })
    return nodes


def _conformance(db: str | None, lane: str, version: str) -> str:
    """How the latest probe on record for ``lane`` relates to the release that served."""
    if not db or not os.path.isfile(db):
        return "unknown"
    from mini_ork.dispatch import conformance

    record = conformance.latest(db, lane)
    if record is None or not isinstance(record.get("event"), dict):
        return "never probed"
    event = record["event"]
    probed = str((event.get("harness") or {}).get("version") or "")
    if version and probed != version:
        return f"probed under {probed or '?'}, not the release that served"
    return "conformant" if event.get("ok") else "probe failed: " + ", ".join(event.get("failures") or [])


def build(run_dir: str, run_id: str, *, db: str | None) -> dict:
    """The card for one run."""
    calls = _calls(db, run_id)
    lanes: dict[tuple[str, str, str], dict] = {}
    for call in calls:
        key = (call["lane"], call["harness"], call["version"])
        row = lanes.setdefault(key, {
            "lane": call["lane"], "family": _family(call["lane"]), "harness": call["harness"],
            "version": call["version"], "calls": 0, "failed": 0, "tokens": 0, "cost_usd": 0.0,
            "actors": [],
        })
        row["calls"] += 1
        row["failed"] += call["status"] != "success"
        row["tokens"] += call["tokens"]
        row["cost_usd"] = round(row["cost_usd"] + call["cost_usd"], 6)
        if call["actor"] and call["actor"] not in row["actors"]:
            row["actors"].append(call["actor"])
    for row in lanes.values():
        row["conformance"] = _conformance(db, row["lane"], row["version"])

    nodes = _nodes(run_dir)
    warnings: list[str] = []
    for node in nodes:
        if node["fallback"]:
            warnings.append(
                f"{node['node']}: requested {node['requested']} ({node['requested_family']}), "
                f"served by {node['served']} ({node['served_family']})")
    # Heterogeneity lost: nodes that asked for different families but were
    # served by one. A same-family panel reads as consensus it is not.
    by_served: dict[str, set[str]] = {}
    for node in nodes:
        by_served.setdefault(node["served_family"], set()).add(node["requested_family"])
    for family, requested in sorted(by_served.items()):
        if len(requested) > 1:
            names = sorted(n["node"] for n in nodes if n["served_family"] == family)
            warnings.append(
                f"family collapse: {', '.join(names)} asked for {len(requested)} families "
                f"({', '.join(sorted(requested))}) and were all served by {family}")
    unrecorded = sum(row["calls"] for row in lanes.values() if row["harness"] == _UNRECORDED)
    if unrecorded:
        warnings.append(f"{unrecorded} call(s) carry no harness identity (written before harness stamping)")
    return {
        "schema": CARD_SCHEMA,
        "run_id": run_id,
        "lanes": sorted(lanes.values(), key=lambda r: (r["lane"], r["harness"], r["version"])),
        "nodes": nodes,
        "warnings": warnings,
        "totals": {
            "calls": len(calls),
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
            "families_served": sorted({n["served_family"] for n in nodes}),
        },
    }


def render_markdown(card: dict) -> str:
    lines = [f"# Harness card — {card['run_id']}", ""]
    lines += ["| lane | family | harness | version | calls | failed | tokens | cost (USD) | conformance |",
              "|---|---|---|---|---:|---:|---:|---:|---|"]
    for row in card["lanes"]:
        lines.append(
            f"| {row['lane']} | {row['family']} | {row['harness']} | {row['version'] or '-'} | {row['calls']} | "
            f"{row['failed']} | {row['tokens']} | {row['cost_usd']:.4f} | {row['conformance']} |")
    if card["nodes"]:
        lines += ["", "| node | requested | served | fallback |", "|---|---|---|---|"]
        for node in card["nodes"]:
            lines.append(
                f"| {node['node']} | {node['requested']} ({node['requested_family']}) | "
                f"{node['served']} ({node['served_family']}) | {'yes' if node['fallback'] else 'no'} |")
    totals = card["totals"]
    lines += ["", f"{totals['calls']} call(s), ${totals['cost_usd']:.4f}; "
                  f"families served: {', '.join(totals['families_served']) or '-'}"]
    if card["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in card["warnings"]]
    return "\n".join(lines) + "\n"


def write(run_dir: str, run_id: str, *, db: str | None) -> dict:
    """Build the card and store it as ``harness-card.json`` + ``harness-card.md``."""
    card = build(run_dir, run_id, db=db)
    base = Path(run_dir)
    (base / "harness-card.json").write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (base / "harness-card.md").write_text(render_markdown(card), encoding="utf-8")
    return card


__all__ = ["CARD_SCHEMA", "build", "render_markdown", "write"]
