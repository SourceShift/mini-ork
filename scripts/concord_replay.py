#!/usr/bin/env python3
"""Concord P0.5 — offline overlap replay over Claude Code transcripts.

Before any per-turn overlap check goes live (Concord P1), measure what it
*would have* reported on real history. This script reads Claude Code session
transcripts, reconstructs every file read and write by every session, detects
cross-session overlaps with the detector rules below, and writes a report plus
a labelling sample. The operator measures precision on that sample with
``--score``.

Standard library only. Never writes inside ``~/.claude`` and never touches the
network. Transcripts are streamed line-by-line: the raw JSON content of a
record is discarded once its compact ``Event`` is extracted, so the whole
corpus is not held in memory at once.

Detector rules (window ``--window-min``, default 30 minutes) compare two
*different* sessions A and B:

  1. ``concurrent_write`` — both write the same ``abs_path`` within the window.
  2. ``stale_read`` — A reads ``abs_path`` at t1, B writes it at t2 > t1, and A
     then writes *any* file in the same checkout within the window after t2
     (A acted on a stale premise). The window bounds A's follow-up write
     relative to B's write, matching the spec literally.
  3. ``logical_overlap`` — both write the same ``(repo_root, rel_path)`` from
     *different* checkouts within the window (a merge risk, not a live clobber).
  4. ``hot_write`` — any ``concurrent_write`` or ``stale_read`` whose
     ``rel_path`` matches the hot set. Reported in its own category and also
     counted in its base category.

Incidents are deduplicated by
``(category, sorted(session pair), path, window bucket)``.

Usage:
    scripts/concord_replay.py --projects-dir ~/.claude/projects --out /tmp/replay
    scripts/concord_replay.py --since 7d --window-min 15 --sample 40 --seed 7
    scripts/concord_replay.py --score /tmp/replay/label-sample.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import functools
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

READ_TOOLS = frozenset({"Read"})
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

CATEGORIES = ("concurrent_write", "stale_read", "logical_overlap", "hot_write")

# Per-run scratch space: sessions that collide there are not coordinating on
# shared work. Excluded by default; `--exclude` replaces the list, and
# `--no-default-excludes` turns it off.
DEFAULT_EXCLUDES = ("/tmp/*", "/private/tmp/*", "/private/var/folders/*", "/var/folders/*")

HOT_PATTERNS = (
    ".mini-ork/config/**",
    "**/secrets*.sh",
    "**/providers.yaml",
    "**/agents.yaml",
    "**/.claude/settings*.json",
    "db/migrations/**",
    "**/.env*",
)

CSV_FIELDS = ("incident_id", "category", "path", "session_a", "session_b",
              "ts", "label", "notes")


@dataclass
class Event:
    ts: dt.datetime
    session_id: str
    cwd: str
    op: str
    abs_path: str
    checkout: str | None
    repo_root: str | None
    rel_path: str


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(iso: object) -> dt.datetime | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _duration(text: str) -> int:
    """Parse a ``14d`` / ``2h`` / ``30m`` / ``90s`` duration into seconds."""
    s = (text or "").strip().lower()
    if len(s) < 2:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(s[-1])
    if mult is None:
        raise argparse.ArgumentTypeError(f"invalid duration unit in {text!r}")
    try:
        num = float(s[:-1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}") from None
    return int(num * mult)


def _default_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


@lru_cache(maxsize=None)
@functools.lru_cache(maxsize=None)
def _checkout_of(directory: str) -> tuple[str | None, str | None]:
    """Resolve ``(checkout, repo_root)`` for a directory, walking up to ``.git``.

    ``checkout`` is the nearest ancestor containing ``.git``. For a plain repo
    the two are equal. For a worktree the ``.git`` file's ``gitdir:`` points at
    ``<main>/.git/worktrees/<name>``, whose parent two levels up is the main
    repository.
    """
    d = Path(directory)
    while True:
        git = d / ".git"
        if git.exists():
            checkout = str(d)
            if git.is_dir():
                return checkout, checkout
            main: str | None = None
            try:
                for line in git.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("gitdir:"):
                        gd = Path(line[len("gitdir:"):].strip())
                        if not gd.is_absolute():
                            gd = d / gd
                        main = str(gd.parents[2])
                        break
            except OSError:
                main = None
            return checkout, main
        if d.parent == d:
            return None, None
        d = d.parent


def _resolve(abs_path: str) -> tuple[str | None, str | None, str]:
    parent = str(Path(abs_path).parent)
    checkout, repo_root = _checkout_of(parent)
    rel = os.path.relpath(abs_path, checkout) if checkout else abs_path
    return checkout, repo_root, rel


def _events_from_record(rec: object) -> list[Event]:
    if not isinstance(rec, dict):
        return []
    ts = _parse_ts(rec.get("timestamp"))
    sid = rec.get("sessionId")
    if ts is None or not isinstance(sid, str) or not sid:
        return []
    cwd = rec.get("cwd") or ""
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    out: list[Event] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        name = item.get("name")
        if name in READ_TOOLS:
            op = "read"
        elif name in WRITE_TOOLS:
            op = "write"
        else:
            continue
        inp = item.get("input")
        if not isinstance(inp, dict):
            continue
        key = "notebook_path" if name == "NotebookEdit" else "file_path"
        fp = inp.get(key)
        if not isinstance(fp, str) or not fp:
            continue
        abs_path = os.path.abspath(fp if os.path.isabs(fp) else os.path.join(cwd, fp))
        checkout, repo_root, rel = _resolve(abs_path)
        out.append(Event(ts=ts, session_id=sid, cwd=cwd, op=op, abs_path=abs_path,
                         checkout=checkout, repo_root=repo_root, rel_path=rel))
    return out


def _collect(projects_dir: Path, since_seconds: int) -> tuple[list[Event], int]:
    events: list[Event] = []
    malformed = 0
    cutoff = time.time() - since_seconds
    if not projects_dir.is_dir():
        return events, malformed
    for f in sorted(projects_dir.glob("*/*.jsonl")):
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                # Only assistant tool_use records can yield events. Skipping
                # every other line before json.loads cuts parse CPU several
                # times over — large tool_result payloads dominate transcripts,
                # and a sustained-CPU guard on the host kills long parses.
                if '"tool_use"' not in line:
                    continue
                line = line.strip()
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                events.extend(e for e in _events_from_record(rec)
                              if e.ts.timestamp() >= cutoff)
    return events, malformed


def _matches_hot(rel_path: str | None) -> bool:
    if not rel_path:
        return False
    p = rel_path.replace("\\", "/")
    return any(fnmatch.fnmatch(p, pat) for pat in HOT_PATTERNS)


def _event_dict(e: Event) -> dict:
    return {"ts": _iso(e.ts), "session_id": e.session_id, "cwd": e.cwd,
            "op": e.op, "abs_path": e.abs_path}


def _make_incident(category: str, path: str, rel_path: str | None,
                   repo_root: str | None, events: list[Event],
                   window_seconds: int, base_category: str | None = None) -> dict:
    ev_sorted = sorted(events, key=lambda e: (e.ts, e.session_id, e.abs_path))
    sids = sorted({e.session_id for e in ev_sorted})
    sa, sb = sids[0], sids[1]
    cwd_a = next(e.cwd for e in ev_sorted if e.session_id == sa)
    cwd_b = next(e.cwd for e in ev_sorted if e.session_id == sb)
    ts_all = [e.ts for e in ev_sorted]
    inc = {
        "category": category,
        "path": path,
        "rel_path": rel_path,
        "repo_root": repo_root,
        "session_a": sa,
        "session_b": sb,
        "cwd_a": cwd_a,
        "cwd_b": cwd_b,
        "ts_start": _iso(ts_all[0]),
        "ts_end": _iso(ts_all[-1]),
        "events": [_event_dict(e) for e in ev_sorted],
        "_bucket": int(ts_all[0].timestamp() // window_seconds),
    }
    if base_category:
        inc["base_category"] = base_category
    return inc


def _dedup_add(dedup: dict, inc: dict) -> None:
    key = (inc["category"], (inc["session_a"], inc["session_b"]),
           inc["path"], inc["_bucket"])
    prev = dedup.get(key)
    if prev is None or inc["ts_start"] < prev["ts_start"]:
        dedup[key] = inc


def _detect(events: list[Event], window_seconds: int) -> list[dict]:
    by_path_writes: dict[str, list[Event]] = {}
    by_path_reads: dict[str, list[Event]] = {}
    session_writes: dict[str, list[Event]] = {}
    logical: dict[tuple[str, str], list[Event]] = {}
    for e in events:
        if e.op == "write":
            by_path_writes.setdefault(e.abs_path, []).append(e)
            session_writes.setdefault(e.session_id, []).append(e)
            if e.repo_root:
                logical.setdefault((e.repo_root, e.rel_path), []).append(e)
        else:
            by_path_reads.setdefault(e.abs_path, []).append(e)

    dedup: dict[tuple, dict] = {}

    # 1. concurrent_write
    for writes in by_path_writes.values():
        writes.sort(key=lambda e: (e.ts, e.session_id))
        for i in range(len(writes)):
            a = writes[i]
            for j in range(i + 1, len(writes)):
                b = writes[j]
                if (b.ts - a.ts).total_seconds() > window_seconds:
                    break
                if a.session_id == b.session_id:
                    continue
                inc = _make_incident("concurrent_write", a.abs_path, a.rel_path,
                                     a.repo_root, [a, b], window_seconds)
                _dedup_add(dedup, inc)

    # 2. stale_read
    for reads in by_path_reads.values():
        reads.sort(key=lambda e: (e.ts, e.session_id))
        writes = sorted(by_path_writes.get(reads[0].abs_path, []),
                        key=lambda e: (e.ts, e.session_id))
        for r in reads:
            a_writes = sorted(session_writes.get(r.session_id, []), key=lambda e: e.ts)
            for w in writes:
                if w.session_id == r.session_id or w.ts <= r.ts:
                    continue
                for a_w in a_writes:
                    if a_w.checkout != r.checkout:
                        continue
                    if a_w.ts <= w.ts:
                        continue
                    if (a_w.ts - w.ts).total_seconds() > window_seconds:
                        continue
                    if any(rr.session_id == r.session_id and w.ts < rr.ts <= a_w.ts
                           for rr in reads):
                        break  # A re-read the file after B's write: premise refreshed
                    inc = _make_incident("stale_read", r.abs_path, r.rel_path,
                                         r.repo_root, [r, w, a_w], window_seconds)
                    _dedup_add(dedup, inc)
                    break

    # 3. logical_overlap
    for (repo_root, rel_path), writes in logical.items():
        writes.sort(key=lambda e: (e.ts, e.session_id))
        for i in range(len(writes)):
            a = writes[i]
            for j in range(i + 1, len(writes)):
                b = writes[j]
                if (b.ts - a.ts).total_seconds() > window_seconds:
                    break
                if a.session_id == b.session_id or a.checkout == b.checkout:
                    continue
                inc = _make_incident("logical_overlap", rel_path, rel_path,
                                     repo_root, [a, b], window_seconds)
                _dedup_add(dedup, inc)

    # 4. hot_write (derived; also counted in its base category)
    base = [inc for inc in dedup.values()
            if inc["category"] in ("concurrent_write", "stale_read")]
    for b in base:
        if _matches_hot(b["rel_path"]):
            inc = dict(b)
            inc["category"] = "hot_write"
            inc["path"] = b["rel_path"]
            inc["base_category"] = b["category"]
            _dedup_add(dedup, inc)

    return list(dedup.values())


def _sort_key(inc: dict) -> tuple:
    return (inc["category"], inc["session_a"], inc["session_b"],
            inc["path"], inc["ts_start"])


def _public(inc: dict) -> dict:
    return {k: v for k, v in inc.items() if not k.startswith("_")}


def _finalize(incidents: list[dict]) -> list[dict]:
    out = []
    for idx, inc in enumerate(incidents, 1):
        pub = _public(inc)
        pub["incident_id"] = f"inc-{idx:04d}"
        out.append(pub)
    return out


def _write_incidents(path: Path, incidents: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for inc in incidents:
            fh.write(json.dumps(inc, sort_keys=True) + "\n")


def _write_sample(path: Path, incidents: list[dict], sample_size: int, seed: int) -> None:
    rng = random.Random(seed)
    counts: dict[str, int] = {}
    for inc in incidents:
        counts[inc["category"]] = counts.get(inc["category"], 0) + 1
    total = len(incidents)
    chosen: list[dict] = []
    if total and sample_size:
        for cat in sorted(counts):
            cat_list = [inc for inc in incidents if inc["category"] == cat]
            k = max(1, round(sample_size * len(cat_list) / total))
            k = min(k, len(cat_list))
            chosen.extend(rng.sample(cat_list, k))
    chosen.sort(key=_sort_key)
    chosen = chosen[:sample_size]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for inc in chosen:
            writer.writerow([inc["incident_id"], inc["category"], inc["path"],
                             inc["session_a"], inc["session_b"], inc["ts_start"],
                             "", ""])


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    frac = k - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def _noise_estimate(events: list[Event], incidents: list[dict]) -> tuple[float | None, float | None]:
    bounds: dict[str, list[dt.datetime]] = {}
    for e in events:
        b = bounds.get(e.session_id)
        if b is None:
            bounds[e.session_id] = [e.ts, e.ts]
        else:
            if e.ts < b[0]:
                b[0] = e.ts
            if e.ts > b[1]:
                b[1] = e.ts
    notices: dict[str, int] = {}
    for inc in incidents:
        for sid in (inc["session_a"], inc["session_b"]):
            notices[sid] = notices.get(sid, 0) + 1
    rates: list[float] = []
    for sid, (lo, hi) in bounds.items():
        hours = (hi - lo).total_seconds() / 3600.0
        if hours > 0:
            rates.append(notices.get(sid, 0) / hours)
    if not rates:
        return None, None
    return _percentile(rates, 0.5), _percentile(rates, 0.95)


def _write_report(path: Path, incidents: list[dict], events: list[Event]) -> None:
    counts = {c: 0 for c in CATEGORIES}
    for inc in incidents:
        counts[inc["category"]] = counts.get(inc["category"], 0) + 1

    per_day: dict[str, int] = {}
    for inc in incidents:
        d = inc["ts_start"][:10]
        per_day[d] = per_day.get(d, 0) + 1

    path_counts: dict[str, int] = {}
    for inc in incidents:
        path_counts[inc["path"]] = path_counts.get(inc["path"], 0) + 1

    pair_counts: dict[tuple[str, str], int] = {}
    for inc in incidents:
        k = (inc["session_a"], inc["session_b"])
        pair_counts[k] = pair_counts.get(k, 0) + 1

    inside = sum(1 for inc in incidents if inc.get("repo_root"))
    outside = len(incidents) - inside
    med, p95 = _noise_estimate(events, incidents)
    med_s = f"{med:.3f}" if med is not None else "n/a"
    p95_s = f"{p95:.3f}" if p95 is not None else "n/a"

    lines = [
        "# Concord P0.5 replay report",
        "",
        f"Generated at: {_iso(dt.datetime.now(dt.timezone.utc))}",
        "",
        "## Totals by category",
        "",
        "| category | incidents |",
        "|---:|---:|",
    ]
    for c in CATEGORIES:
        lines.append(f"| {c} | {counts.get(c, 0)} |")

    lines += [
        "",
        "## Incidents per day",
        "",
        "| day | incidents |",
        "|---:|---:|",
    ]
    for d in sorted(per_day):
        lines.append(f"| {d} | {per_day[d]} |")

    lines += [
        "",
        "## Top paths",
        "",
        "| path | incidents |",
        "|---:|---:|",
    ]
    for p, n in sorted(path_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        lines.append(f"| {p} | {n} |")

    lines += [
        "",
        "## Top session pairs",
        "",
        "| session_a | session_b | incidents |",
        "|---|---:|---:|",
    ]
    for (sa, sb), n in sorted(pair_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        lines.append(f"| {sa} | {sb} | {n} |")

    lines += [
        "",
        "## Repo split",
        "",
        f"- inside a git repo: {inside}",
        f"- outside any repo: {outside}",
        "",
        "## Noise estimate (notices per session per hour)",
        "",
        f"- median: {med_s}",
        f"- p95: {p95_s}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _score(csv_path: str) -> int:
    by_cat: dict[str, dict[str, int]] = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            label = (row.get("label") or "").strip()
            cat = (row.get("category") or "").strip()
            if label in ("real", "noise") and cat:
                b = by_cat.setdefault(cat, {"real": 0, "noise": 0})
                b[label] += 1
    for cat in sorted(by_cat):
        real = by_cat[cat]["real"]
        noise = by_cat[cat]["noise"]
        print(f"{cat}: {real / (real + noise):.6f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="concord_replay",
                                description="Concord P0.5 offline overlap replay over Claude Code transcripts.")
    p.add_argument("--projects-dir", type=Path, default=None,
                   help="transcripts root (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    p.add_argument("--since", type=_duration, default=_duration("14d"),
                   help="only read files modified within this (default: 14d)")
    p.add_argument("--window-min", type=int, default=30,
                   help="overlap window in minutes (default: 30)")
    p.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                   help="absolute-path glob to ignore (repeatable; replaces the defaults)")
    p.add_argument("--no-default-excludes", action="store_true",
                   help="do not ignore per-run scratch paths (/tmp, /private/var/folders)")
    p.add_argument("--out", type=Path, default=None,
                   help="output dir (default: ./concord-replay-<date>)")
    p.add_argument("--sample", type=int, default=40, help="label-sample size (default: 40)")
    p.add_argument("--seed", type=int, default=7, help="sampling seed (default: 7)")
    p.add_argument("--score", metavar="label-sample.csv", default=None,
                   help="score a labelled sample instead of replaying")
    args = p.parse_args(argv)

    if args.score:
        return _score(args.score)

    projects_dir = args.projects_dir or _default_projects_dir()
    out = args.out or Path(f"./concord-replay-{dt.date.today().isoformat()}")
    events, malformed = _collect(projects_dir, args.since)
    excludes = args.exclude if args.exclude is not None else (
        [] if args.no_default_excludes else list(DEFAULT_EXCLUDES))
    events = [e for e in events if not any(fnmatch.fnmatch(e.abs_path, g) for g in excludes)]
    incidents = _detect(events, args.window_min * 60)
    incidents_sorted = sorted(incidents, key=_sort_key)
    final = _finalize(incidents_sorted)
    out.mkdir(parents=True, exist_ok=True)
    _write_incidents(out / "incidents.jsonl", final)
    _write_report(out / "report.md", final, events)
    _write_sample(out / "label-sample.csv", final, args.sample, args.seed)
    print(f"[concord-replay] {len(events)} events, {malformed} malformed line(s) skipped, "
          f"{len(final)} incident(s) -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
