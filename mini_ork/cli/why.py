"""``mini-ork why <run> <node>`` — why this artifact came from this model.

Attribution already exists, but it is split across four stores: the routing
reason is a ``[route]`` line on stderr and five columns of ``execution_traces``
(``route_source``, ``route_score``, ``route_margin``, ``predicted_error``,
``route_explore``); the lane that actually served is ``<run_dir>/.lane-<node>.json``,
written by the dispatcher from ``selected_model``; the lanes walked on the way
there are ``llm_calls`` rows; and the lessons assembled into the node's prompt
are ``<run_dir>/learned/<node>.json``. Answering "which model produced this, and
why" means opening all four.

This command joins them and prints one provenance card. It reads; it never
writes to the DB or the run directory, and it opens ``state.db`` read-only so a
typo in a home path cannot create an empty database. A store that was never
written says so — "not recorded" is the honest answer and is printed, not
omitted, because a silently absent field reads as "there was nothing to know".

Exit codes: 0 the card printed (possibly with gaps), 1 the run exists but has no
record for the node, 2 usage error or unknown run.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

_USAGE = """\
mini-ork why — provenance for one node's artifact.

Usage:
  mini-ork why <run> <node> [--json] [--home <dir>] [--db <path>]

<run> is a run id under $MINI_ORK_HOME/runs/ (or a path to a run directory).
Prints the lane the node asked for and the lane that served it, the routing
reason recorded with the node's trace, every lane call the node made (the
fallback chain actually walked), and the memory ids that were injected into
its prompt. ``not recorded`` means the store exists but has no row for this
node — not that the value was zero.
"""


def _home(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")


def _run_dir(run: str, home: str) -> str | None:
    """The run directory: an explicit path, else ``<home>/runs/<run>``."""
    if os.path.isdir(run):
        return run
    candidate = os.path.join(home, "runs", run)
    return candidate if os.path.isdir(candidate) else None


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _node_type(injection, traces: list[dict]) -> str:
    if isinstance(injection, dict) and injection.get("node_type"):
        return str(injection["node_type"])
    for row in traces:
        payload = row.get("verifier_output")
        if isinstance(payload, dict) and payload.get("node_type"):
            return str(payload["node_type"])
    return ""


def _trace_rows(db: str, run: str, node: str) -> tuple[list[dict], str]:
    """This node's trace rows, newest first, plus why they are missing."""
    if not os.path.isfile(db):
        return [], f"no database at {db}"
    try:
        con = sqlite3.connect(
            Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return [], f"database unreadable: {exc}"
    try:
        rows = con.execute(
            "SELECT trace_id, agent_version_id, route_source, route_explore, "
            "route_score, route_margin, predicted_error, status, "
            "verifier_output, created_at "
            "FROM execution_traces WHERE run_id = ? ORDER BY created_at DESC",
            (run,),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], f"execution_traces unreadable: {exc}"
    finally:
        con.close()

    out: list[dict] = []
    for (trace_id, lane, source, explore, score, margin, predicted,
         status, payload, created) in rows:
        decoded = payload
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except ValueError:
                decoded = {}
        row = {
            "trace_id": trace_id,
            "agent_version_id": lane or "",
            "route_source": source or "",
            "route_explore": bool(explore),
            "route_score": score,
            "route_margin": margin,
            "predicted_error": predicted,
            "status": status or "",
            "created_at": created or "",
        }
        # The node id lives inside the verifier payload for the rich trace path;
        # the legacy trace id encodes it as `tr-<node_type>-<node>-<hex>`. Ask
        # both, and treat a `-<node>-` hit as a match so a payload-less row is
        # still attributable.
        node_id = decoded.get("node_id") if isinstance(decoded, dict) else None
        if node_id == node or f"-{node}-" in str(trace_id):
            out.append(row)
    return out, "" if out else f"no execution_traces row for node {node!r}"


def _calls(db: str, run: str, node: str) -> tuple[list[dict], str]:
    """Every lane call the node made, in wall-clock order, and why if none."""
    if not os.path.isfile(db):
        return [], f"no database at {db}"
    try:
        con = sqlite3.connect(
            Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return [], f"database unreadable: {exc}"
    try:
        rows = con.execute(
            "SELECT actor, model_id, status, input_tokens, output_tokens, "
            "cost_usd, session_id, ts, feature_name "
            "FROM llm_calls WHERE run_id = ? ORDER BY ts, id",
            (run,),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], f"llm_calls unreadable: {exc}"
    finally:
        con.close()

    # A node's calls are keyed by its ACTOR, which is the node id for node-shaped
    # actors (`codex_lens`) but the role for role-shaped ones (`synthesizer` logs
    # as `reviewer`). Exact actor first; then a feature-name substring, which is
    # `mini-ork:<node>` on the same path. Never guess beyond that: a wrong match
    # would attribute another node's cost to this one.
    picked = [r for r in rows if r[0] == node]
    matched = node
    if not picked:
        picked = [r for r in rows if f":{node}" in str(r[8] or "")]
        matched = f"feature~{node}" if picked else ""
    if not picked:
        return [], f"no llm_calls row for node {node!r} (run has {len(rows)} call(s))"
    out = []
    for (actor, model, status, tok_in, tok_out, cost, session, ts, feature) in picked:
        out.append({
            "actor": actor or matched,
            "lane": model or "",
            "status": status or "",
            "input_tokens": int(tok_in or 0),
            "output_tokens": int(tok_out or 0),
            "cost_usd": round(float(cost or 0.0), 6),
            "session_id": session or "",
            "feature": feature or "",
            "ts": ts or "",
        })
    return out, ""


def _family(lane: str) -> str:
    from mini_ork.verify.panel_heterogeneity import lane_family

    try:
        return lane_family(lane)
    except Exception:  # noqa: BLE001 — a hostile registry must not break a read
        return ""


def collect(run: str, node: str, *, home: str, db: str | None = None) -> dict:
    """The provenance card for one node of one run. Never raises on bad files."""
    run_dir = _run_dir(run, home)
    result: dict = {
        "run": run,
        "node": node,
        "run_dir": run_dir or "",
        "node_type": "",
        "lane": {},
        "affinity": None,
        "route": {},
        "memory": None,
        "outcomes": {},
        "walked": [],
        "notes": [],
    }
    if run_dir is None:
        result["notes"].append(f"no run directory for {run!r} under {home}/runs")
        return result

    lane = _read_json(os.path.join(run_dir, f".lane-{node}.json"))
    if isinstance(lane, dict) and lane.get("served"):
        requested = str(lane.get("requested") or lane["served"])
        result["lane"] = {
            "requested": requested,
            "served": str(lane["served"]),
            "fallback": bool(lane.get("fallback", requested != str(lane["served"]))),
            "family": _family(str(lane["served"])),
            "requested_family": _family(requested),
        }
    else:
        result["notes"].append(
            f"lane record not recorded ({os.path.basename(run_dir)}/.lane-{node}.json)")

    affinity = _read_json(os.path.join(run_dir, ".lane-affinity.json"))
    if isinstance(affinity, dict) and isinstance(affinity.get(node), dict):
        result["affinity"] = affinity[node]

    injection = _read_json(os.path.join(run_dir, "learned", f"{node}.json"))

    traces, why_trace = _trace_rows(db or os.path.join(home, "state.db"), run, node)
    calls, why_calls = _calls(db or os.path.join(home, "state.db"), run, node)
    result["node_type"] = _node_type(injection, traces)

    if traces:
        row = traces[0]
        result["route"] = {
            "source": row["route_source"],
            "explore": row["route_explore"],
            "score": row["route_score"],
            "margin": row["route_margin"],
            "predicted_error": row["predicted_error"],
            "agent_version_id": row["agent_version_id"],
            "status": row["status"],
            "trace_id": row["trace_id"],
            "rows": len(traces),
        }
    else:
        result["notes"].append(why_trace)

    if calls:
        result["walked"] = calls
    else:
        result["notes"].append(why_calls)

    if isinstance(injection, dict):
        sources = injection.get("sources")
        result["memory"] = {
            "injected": bool(injection.get("injected")),
            "reason": injection.get("reason") or "",
            "arm": injection.get("arm"),
            "attempt": injection.get("attempt"),
            "sources": sources if isinstance(sources, list) else [],
        }
        result["outcomes"] = _outcomes(
            result["memory"], db or os.path.join(home, "state.db"))
    else:
        result["notes"].append(
            f"prompt memory not recorded (learned/{node}.json)")
    return result


def _outcomes(memory: dict, db_path: str) -> dict:
    """Per-source node outcomes for the lessons this node was given.

    One read for all of them. A missing 0092 table comes back as
    ``{"status": "unavailable"}`` per lesson — the render says so, because a
    zero here would read as "measured, no effect" and the two are not the same
    claim.
    """
    from mini_ork.learning import lesson_outcomes  # noqa: PLC0415

    pairs = [
        (str(source.get("kind")), str(source.get("id")))
        for source in memory.get("sources") or []
        if isinstance(source, dict) and source.get("kind") and source.get("id")
    ]
    if not pairs:
        return {}
    return lesson_outcomes.summaries(pairs, db=db_path)


def _ids(memory) -> list[str]:
    out = []
    for source in memory.get("sources") or []:
        if isinstance(source, dict) and source.get("id"):
            out.append(str(source["id"]))
    return out


def _outcome_text(outcomes: dict, *, limit: int = 6) -> str:
    """``clean 93%(n=778)`` per lesson, or the reason there is no number.

    Every branch names its insufficiency; none of them prints ``0%`` for a
    lesson that was simply never measured.
    """
    parts: list[str] = []
    for key, summary in list(outcomes.items())[:limit]:
        if summary.get("status") == "unavailable":
            parts.append(f"{key}: unavailable")
            continue
        rate = summary.get("clean_rate_primary")
        n = int(summary.get("n_primary") or 0)
        if rate is None:
            rate, n = summary.get("clean_rate"), int(summary.get("n_nodes") or 0)
        if rate is None:
            parts.append(f"{key}: no outcomes yet")
            continue
        parts.append(f"{key}: clean {round(rate * 100)}% (n={n}) · {summary['status']}")
    more = f" (+{len(outcomes) - limit} more)" if len(outcomes) > limit else ""
    return "outcomes: " + "; ".join(parts) + more


def render(card: dict) -> str:
    """The human card. Every absent field is named, never left blank."""
    lines = [f"run:  {card['run']}"]
    if card["node_type"]:
        lines.append(f"node: {card['node']} ({card['node_type']})")
    else:
        lines.append(f"node: {card['node']}")

    lane = card["lane"]
    if lane:
        arrow = " -> " if lane["fallback"] else " == "
        lines.append(
            f"lane: {lane['requested']} ({lane['requested_family']})"
            f"{arrow}{lane['served']} ({lane['family']})"
            + ("   [fell back]" if lane["fallback"] else ""))
    else:
        lines.append("lane: not recorded")

    route = card["route"]
    if route:
        def _num(value):
            return "-" if value is None else f"{float(value):.4f}"
        source = route["source"] or "unrecorded"
        lines.append(
            f"route: source={source}"
            + ("  pinned by the recipe (a pin beats the router)" if source == "pinned" else "")
            + f"  explore={'yes' if route['explore'] else 'no'}"
            + f"  score={_num(route['score'])}  margin={_num(route['margin'])}"
            + f"  predicted_error={_num(route['predicted_error'])}")
        lines.append(
            f"       trace={route['trace_id']}  status={route['status']}"
            f"  agent_version_id={route['agent_version_id'] or '-'}")
    else:
        lines.append("route: not recorded")

    affinity = card["affinity"]
    if isinstance(affinity, dict) and affinity:
        lines.append(
            f"affinity: lead={affinity.get('lead', '-')}"
            f" served={affinity.get('served', '-')} (reused on later rounds)")

    if card["walked"]:
        lines.append("walked (llm_calls, oldest first):")
        total = 0.0
        for call in card["walked"]:
            total += call["cost_usd"]
            session = f"  session={call['session_id']}" if call["session_id"] else ""
            lines.append(
                f"  {call['actor'] or '?'}: {call['lane']} [{_family(call['lane'])}]"
                f" status={call['status']} in={call['input_tokens']}"
                f" out={call['output_tokens']} ${call['cost_usd']:.4f}{session}")
        lines.append(f"  total ${total:.4f} across {len(card['walked'])} call(s)")
    else:
        lines.append("walked: not recorded")

    memory = card["memory"]
    if memory:
        ids = _ids(memory)
        head = (f"memory: injected={memory['injected']} arm={memory['arm'] or '-'}"
                f" attempt={memory['attempt'] if memory['attempt'] is not None else '-'}")
        if not memory["injected"]:
            head += f" reason={memory['reason'] or '-'}"
        lines.append(head)
        if ids:
            shown = ", ".join(ids[:12])
            more = f" (+{len(ids) - 12} more)" if len(ids) > 12 else ""
            lines.append(f"        {len(ids)} source id(s): {shown}{more}")
        else:
            lines.append("        0 source id(s) recorded")
        outcomes = card.get("outcomes") or {}
        if outcomes:
            lines.append("        " + _outcome_text(outcomes))
        elif ids:
            lines.append("        outcomes: unavailable (0092 not applied)")
    else:
        lines.append("memory: not recorded")

    if card["notes"]:
        lines.append("")
        for note in card["notes"]:
            lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"


def main(argv=None, root=None) -> int:
    del root  # provenance is read from the home + DB, never from the engine tree
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("run", nargs="?")
    parser.add_argument("node", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home")
    parser.add_argument("--db")
    parser.add_argument("-h", "--help", action="store_true")
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit:
        return 2
    if args.help or not args.run or not args.node:
        sys.stdout.write(_USAGE)
        return 0 if args.help else 2

    home = _home(args.home)
    card = collect(args.run, args.node, home=home, db=args.db)
    if not card["run_dir"]:
        if args.json:
            print(json.dumps(card, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render(card))
        return 2
    if args.json:
        print(json.dumps(card, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render(card))
    recorded = any((card["lane"], card["route"], card["walked"], card["memory"]))
    return 0 if recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
