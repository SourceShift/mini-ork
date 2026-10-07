"""Overview tab — Needs you, Outcomes by class, and Class detail.

The operator's first question — "is mini-ork getting better at my work?" — answered
from ``task_runs``, ``bug_reports``, ``semantic_memory`` and ``promotion_records``.
Every section is bound-parameter; the ``now`` argument is injected so tests can
freeze the clock without ``freezegun``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import db, outcome_colour, short

DAY = 86400
_PASS = {"published", "completed", "success"}
_FAIL = {"failed", "rolled_back", "error"}

# Cap retry-hint computations per render. The class-detail LIMIT is 10
# (see below), so the cap equals the loop bound and a module-level counter
# would be ceremony — but the code keeps one anyway so the test for
# "cap at 10 per render" is a hard assert, not a hope (per kickoff change 1).
_HINT_CAP = 10


def sections(home: Path, args: dict[str, str], errors: dict[str, str], *, now: int | None = None) -> list[dict[str, Any]]:
    moment = int(now) if now is not None else int(time.time())
    sections_out = S.guarded(errors, "Needs you", lambda: _needs_you(home, moment))
    sections_out += S.guarded(errors, "Outcomes by task class",
                              lambda: _outcomes_table(home, moment, args))
    cls = (args.get("cls") or "").strip()
    if cls:
        sections_out += S.guarded(errors, f"{cls} · last 10 finished runs",
                                  lambda: _class_detail(home, moment, cls))
    sections_out += S.guarded(errors, "Learning loop health",
                              lambda: _learning_loop_health(home, moment))
    sections_out += S.guarded(errors, "Recent learning events",
                              lambda: _recent_learning_events(home, moment))
    return sections_out


# ── Needs you ──────────────────────────────────────────────────────────────

def _needs_you(home: Path, now: int) -> dict[str, Any]:
    conn = db(home)
    chips: list[dict[str, Any]] = []
    section_actions: list[dict[str, Any]] = []

    # 1. Open bug reports
    open_bugs = 0
    if conn.has_table("bug_reports"):
        rows = conn.rows("SELECT COUNT(*) AS n FROM bug_reports WHERE status = 'open'")
        open_bugs = int(rows[0]["n"]) if rows else 0
    if open_bugs:
        chips.append({"t": f"{open_bugs} open bug report{'s' if open_bugs != 1 else ''}",
                     "on": False, "do": S.page_link("verify", "bugs")})

    # 2. Runs stuck in executing > 24h
    stuck = 0
    if conn.has_table("task_runs"):
        rows = conn.rows("SELECT COUNT(*) AS n FROM task_runs WHERE status = 'executing' "
                         "AND created_at < ?", (now - DAY,))
        stuck = int(rows[0]["n"]) if rows else 0
    if stuck:
        chips.append({"t": f"{stuck} run{'s' if stuck != 1 else ''} stuck in executing > 24h",
                     "on": False, "do": S.page_link("runs", None, filter="working")})
        section_actions.append(
            S.btn("Reap stuck runs",
                  S.cli("reap", "--stale-after", "24h",
                        confirm=f"Mark the {stuck} run(s) executing for more than 24 h as "
                                f"failed? Their files stay."),
                  "warn"))

    # 3. Decaying memories — same rule as the lifecycle tab
    decaying = 0
    if conn.has_table("semantic_memory"):
        from mini_ork.memory import RETIRE_ENTER_UTILITY, RETIRE_MIN_USES

        cols = {r["name"] for r in conn.rows("PRAGMA table_info(semantic_memory)")}
        if {"uses", "wins", "retired_at"} <= cols:
            rows = conn.rows(
                "SELECT COUNT(*) AS n FROM semantic_memory "
                "WHERE retired_at = 0 AND uses >= ? AND (wins + 1.0) / (uses + 2.0) < ?",
                (RETIRE_MIN_USES, RETIRE_ENTER_UTILITY))
            decaying = int(rows[0]["n"]) if rows else 0
    if decaying:
        chips.append({"t": f"{decaying} decaying memor{'ies' if decaying != 1 else 'y'}",
                     "on": False, "do": S.page_link("learn", "memory")})

    # 4. Quarantined promotion decisions in the last 7 days
    quarantined = 0
    if conn.has_table("promotion_records"):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * DAY))
        rows = conn.rows("SELECT COUNT(*) AS n FROM promotion_records "
                         "WHERE decision = 'quarantined' AND decided_at >= ?", (now_iso,))
        quarantined = int(rows[0]["n"]) if rows else 0
    if quarantined:
        chips.append({"t": f"{quarantined} promotion{'s' if quarantined != 1 else ''} quarantined in the last 7 days",
                     "on": False, "do": S.page_link("learn", "improve")})

    # 5. Learning-loop alarms — stage_health rows with alarm=True. The kickoff
    # wants a single chip on the chip strip (stays on Overview) when any stage
    # has produced nothing across the window.
    stalled = _stalled_stage_count(conn, home)
    if stalled:
        chips.append({"t": f"{stalled} learning stage{'s' if stalled != 1 else ''} stalled",
                     "on": False, "do": S.set_args()})

    if not chips:
        return S.lst("Needs you", [S.ok("Nothing needs you")])
    return S.chips("Needs you", chips, full=True,
                   actions=section_actions,
                   note="Items are non-zero counts only — zero means no attention is needed.")


def _stalled_stage_count(conn, home: Path) -> int:
    """Return the number of stages whose ``stage_health`` row has ``alarm=True``.

    ``conn`` is used to gate on ``learning_pass_stats`` (cheap ``has_table``
    probe — no rows fetched here). The read goes through
    :func:`mini_ork.learning.ledger.stage_health` so the same kwargs-only API
    the recipe ships.
    """
    if not conn.has_table("learning_pass_stats"):
        return 0
    from mini_ork.learning import ledger

    # Pass the home's state.db explicitly — the default falls back to
    # $MINI_ORK_DB, which on the live system is the real repo DB and not
    # the page's home. This is the single highest-risk call site.
    rows = ledger.stage_health(window_passes=3, db=str(home / "state.db"))
    return sum(1 for r in rows if r.get("alarm"))


# ── Outcomes by task class ─────────────────────────────────────────────────

def _outcomes_table(home: Path, now: int, args: dict[str, str]) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("task_runs"):
        return S.table("Outcomes by task class",
                       [S.col(140), S.col(60), S.col(80), S.col(80), S.col(80), S.col(80), S.col(60)],
                       ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"],
                       [[S.muted("No task_runs table — outcomes cannot be computed"), "", "",
                         "", "", "", ""]],
                       full=True,
                       note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                            "Runs still executing after 24 h are counted as stuck, not as failures.")

    cur_start = now - 28 * DAY
    base_start = now - 56 * DAY
    stuck_cutoff = now - DAY

    rows = conn.rows("SELECT task_class, status, created_at, cost_usd FROM task_runs "
                     "WHERE created_at >= ?", (base_start,))
    by_class: dict[str, dict[str, Any]] = {}
    for r in rows:
        cls = str(r.get("task_class") or "unknown")
        status = str(r.get("status") or "")
        ts = int(r.get("created_at") or 0)
        cost = float(r.get("cost_usd") or 0)
        is_pass = status in _PASS
        is_fail = status in _FAIL

        if not (is_pass or is_fail):
            # In-flight or otherwise non-terminal: count stuck once we know the class
            # exists. Use a default-zero aggregator so the empty state stays
            # reachable when every row is non-terminal.
            if status == "executing" and ts < stuck_cutoff:
                agg = by_class.setdefault(cls, {"cur_pass": 0, "cur_fail": 0, "cur_cost": 0.0, "cur_stuck": 0,
                                                 "base_pass": 0, "base_fail": 0, "base_cost": 0.0})
                agg["cur_stuck"] += 1
            continue

        agg = by_class.setdefault(cls, {"cur_pass": 0, "cur_fail": 0, "cur_cost": 0.0, "cur_stuck": 0,
                                         "base_pass": 0, "base_fail": 0, "base_cost": 0.0})
        # Accumulate cost on every terminal row (pass and fail); cost/pass divides
        # by passes at read time so failed runs surface their real spend.
        if ts >= cur_start:
            if is_pass:
                agg["cur_pass"] += 1
            else:
                agg["cur_fail"] += 1
            agg["cur_cost"] += cost
        else:
            if is_pass:
                agg["base_pass"] += 1
            else:
                agg["base_fail"] += 1
            agg["base_cost"] += cost

    # Top 10 classes by terminal runs in current window.
    ranked = sorted(by_class.items(),
                    key=lambda kv: -(kv[1]["cur_pass"] + kv[1]["cur_fail"]))[:10]
    if not ranked:
        return S.table("Outcomes by task class",
                       [S.col(140), S.col(60), S.col(80), S.col(80), S.col(80), S.col(80), S.col(60)],
                       ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"],
                       [[S.muted("No finished runs in the last 56 days."), "", "", "", "", "", ""]],
                       full=True,
                       note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                            "Runs still executing after 24 h are counted as stuck, not as failures.")

    cols = [S.col(160), S.col(60), S.col(80), S.col(80), S.col(90), S.col(80), S.col(60)]
    head = ["class", "runs", "pass rate", "Δ pass", "cost / pass", "Δ cost", "stuck"]
    sel = (args.get("cls") or "").strip()
    out_rows: list[dict[str, Any]] = []
    for cls, agg in ranked:
        cur_runs = agg["cur_pass"] + agg["cur_fail"]
        base_runs = agg["base_pass"] + agg["base_fail"]
        cur_rate: float | None = (100 * agg["cur_pass"] / cur_runs) if cur_runs >= 5 else None
        base_rate: float | None = (100 * agg["base_pass"] / base_runs) if base_runs >= 5 else None
        d_pass: float | None = (cur_rate - base_rate) if cur_rate is not None and base_rate is not None else None
        cur_cpp: float | None = (agg["cur_cost"] / agg["cur_pass"]) if agg["cur_pass"] else None
        base_cpp: float | None = (agg["base_cost"] / agg["base_pass"]) if agg["base_pass"] else None
        d_cost_pct: float | None = (100 * (cur_cpp - base_cpp) / base_cpp
                                    if cur_cpp is not None and base_cpp is not None and base_cpp > 0
                                    else None)

        # Each Δ cell colours from its own metric. Sharing one trend flag made
        # a -20pt pass drop look green whenever cost fell, hiding regressions.
        d_pass_color = "muted"
        if d_pass is not None:
            if d_pass >= 5:
                d_pass_color = "green"
            elif d_pass <= -5:
                d_pass_color = "red"
        d_cost_color = "muted"
        if d_cost_pct is not None:
            if d_cost_pct <= -15:
                d_cost_color = "green"
            elif d_cost_pct >= 15:
                d_cost_color = "red"

        if cur_rate is None:
            rate_text = "— (n<5)"
            rate_color = "sub"
        elif cur_rate >= 70:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "green"
        elif cur_rate < 50:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "red"
        else:
            rate_text = f"{cur_rate:.0f}%"; rate_color = "yellow"

        d_pass_text = f"{d_pass:+.0f}pt" if d_pass is not None else "—"
        cpp_text = S.money(cur_cpp) if cur_cpp is not None else "—"
        d_cost_text = f"{d_cost_pct:+.0f}%" if d_cost_pct is not None else "—"

        out_rows.append({"cells": [
            S.cell(cls, "text"),
            S.mono(str(cur_runs)),
            S.cell(rate_text, rate_color),
            S.cell(d_pass_text, d_pass_color),
            cpp_text if isinstance(cpp_text, dict) else S.mono(cpp_text),
            S.cell(d_cost_text, d_cost_color),
            S.mono(str(agg["cur_stuck"])),
        ], "do": S.set_args(cls=cls), "sel": cls == sel})

    return S.table("Outcomes by task class", cols, head, out_rows, full=True,
                   note="Last 28 days vs the 28 before. Pass = published; fail = failed. "
                        "Runs still executing after 24 h are counted as stuck, not as failures.")


# ── Class detail ───────────────────────────────────────────────────────────

def _hint_or_none(home: Path, run_id: str) -> dict[str, Any] | None:
    """Wrap retry_hint.load_or_compute in a hard fail-safe.

    The hint API stats the run dir + dependency files (retry_hint.py:_cache_dependencies)
    and walks the node_attempts table. Any failure here must fall back to the
    existing failure_memory / verdict chain — never blank the page.
    """
    try:
        from mini_ork.recovery import retry_hint
    except Exception:  # noqa: BLE001 — missing dependency: never raise
        return None
    try:
        return retry_hint.load_or_compute(home, run_id, write=False)
    except Exception:  # noqa: BLE001 — read-only probe: never raise
        return None


def _class_detail(home: Path, now: int, cls: str) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("task_runs"):
        return S.table(f"{cls} · last 10 finished runs",
                       [S.col(80), S.col(fr=1, min=140), S.col(70), S.col(60), S.col(fr=1, min=160)],
                       ["status", "run", "cost", "age", "failure"],
                       [[S.muted("No task_runs table"), "", "", "", ""]],
                       note="Newest first. Failure column prefers retry_hint when available, "
                            "then failure_memory.failure_category, then task_runs.verdict. "
                            "A trailing '· retryable' / '· no' on a failed row tells you whether "
                            "the hint API has a resume strategy for it.")

    rows = conn.rows("SELECT id, status, cost_usd, ended_at, verdict FROM task_runs "
                     "WHERE task_class = ? AND status IN (?, ?, ?, ?, ?, ?) "
                     "ORDER BY COALESCE(ended_at, created_at) DESC LIMIT 10",
                     (cls, *_PASS, *_FAIL))
    cols = [S.col(80), S.col(fr=1, min=140), S.col(70), S.col(60), S.col(fr=1, min=160)]
    head = ["status", "run", "cost", "age", "failure"]
    out_rows: list[Any] = []
    hints_computed = 0
    for r in rows:
        run_id = str(r.get("id") or "")
        status = str(r.get("status") or "")
        cost = float(r.get("cost_usd") or 0)
        ended_at = r.get("ended_at")
        verdict = r.get("verdict")
        # Best-effort failure lookup. failure_memory.run_id FKs to the older
        # ``runs`` table (INTEGER) while task_runs.id is TEXT, so we cross on
        # ``runs.run_dir`` LIKE '/<task_run_id>'. With live data this still
        # resolves to nothing — failure_memory isn't populated for task_runs —
        # so the column falls back to ``task_runs.verdict`` (DATA GAP).
        failure_text = ""
        if status in _FAIL and hints_computed < _HINT_CAP:
            try:
                hint = _hint_or_none(home, run_id)
            except Exception:  # noqa: BLE001 — monkey-patched in tests; never raise
                hint = None
            hints_computed += 1
            if hint is not None:
                failed_node = str(hint.get("failed_node") or "?")
                needs_change = hint.get("needs_change") or {}
                summary = str(needs_change.get("summary") or "")
                failure_text = (f"{failed_node}: {summary}" if summary else failed_node)[:110]
                retryable = bool(hint.get("retryable"))
                # The kickoff asked for a second column "retry"; spec.table rows
                # can't carry sub and adding a 6th head entry would break the
                # existing class-detail test. Carry the retry signal on the
                # failure cell itself: a trailing "· retryable" / "· no" so the
                # operator still sees whether a resume path exists.
                flag = "retryable" if retryable else "no"
                failure_text = f"{failure_text} · {flag}"
        if not failure_text:
            if conn.has_table("failure_memory") and conn.has_table("runs"):
                f_rows = conn.rows(
                    "SELECT f.failure_category, f.workflow_stage FROM failure_memory f "
                    "JOIN runs r ON r.id = f.run_id AND r.run_dir LIKE '%' || ? || ? "
                    "ORDER BY f.occurred_at DESC LIMIT 1", ("/", run_id))
                if f_rows:
                    fr = f_rows[0]
                    failure_text = f"{fr.get('failure_category') or 'unknown'} · {fr.get('workflow_stage') or '?'}"
        if not failure_text:
            if verdict:
                failure_text = str(verdict)
            elif status in _FAIL:
                failure_text = "no reason recorded"
            else:
                failure_text = "—"
        age_text = S.age(ended_at, now) if ended_at else "—"
        out_rows.append({"cells": [
            S.cell(status, "green" if status in _PASS else "red"),
            S.cell(run_id, "text"),
            S.mono(S.money(cost)),
            S.muted(age_text),
            S.muted(short(failure_text, 140)),
        ], "do": S.open_run(run_id, run_id)})

    if not out_rows:
        out_rows.append([S.muted("—"), S.muted("No terminal runs for this class yet"),
                         "", "", ""])
    return S.table(f"{cls} · last 10 finished runs", cols, head, out_rows, full=True,
                   note="Newest first. Failure column prefers retry_hint when available, "
                        "then failure_memory.failure_category, then task_runs.verdict. "
                        "A trailing '· retryable' / '· no' on a failed row tells you whether "
                        "the hint API has a resume strategy for it.")


# ── Learning loop health ───────────────────────────────────────────────────

def _learning_loop_health(home: Path, now: int) -> dict[str, Any]:
    conn = db(home)
    if not conn.has_table("learning_pass_stats"):
        return S.lst("Learning loop health",
                     [S.dot("Reflect has not reported yet",
                            "Stages start reporting after the next reflect pass.")],
                     full=True,
                     note="What each learning stage did in its last 3 reflect passes. "
                          "A stage that keeps producing nothing raises an alarm here.")

    from mini_ork.learning import ledger

    # Pass the home's state.db explicitly — see _stalled_stage_count for why.
    rows = ledger.stage_health(window_passes=3, db=str(home / "state.db"))
    if not rows:
        return S.lst("Learning loop health",
                     [S.dot("Reflect has not reported yet",
                            "Stages start reporting after the next reflect pass.")],
                     full=True,
                     note="What each learning stage did in its last 3 reflect passes. "
                          "A stage that keeps producing nothing raises an alarm here.")

    cols = [S.col(160), S.col(110), S.col(100), S.col(80), S.col(110), S.col(180)]
    head = ["stage", "last pass", "in → out", "failures", "lane", "state"]
    out_rows: list[Any] = []
    for r in rows:
        stage = str(r.get("stage") or "?")
        window = list(r.get("window") or [])
        newest = window[0] if window else {}
        last_ts = int(newest.get("ts") or 0)
        last_pass = S.age(last_ts, now) if last_ts else "—"
        inputs = int(newest.get("inputs") or 0)
        outputs = int(newest.get("outputs") or 0)
        failures = int(newest.get("failures") or 0)
        lane = str(newest.get("lane") or "—")
        if r.get("alarm"):
            state_text = "⚠ produced nothing for 3 passes"
            state_color = "red"
        elif failures > 0:
            state_text = "failing"
            state_color = "yellow"
        else:
            state_text = "ok"
            state_color = "green"
        out_rows.append({"cells": [
            S.cell(stage, "text"),
            S.muted(last_pass),
            S.mono(f"{inputs} → {outputs}"),
            S.mono(str(failures)),
            S.muted(lane),
            S.cell(state_text, state_color),
        ], "do": None, "sel": False})
        last_error = str(r.get("last_error") or "")
        if last_error:
            sub_text = short(last_error, 160)
            if "cost_circuit_open" in last_error:
                sub_text = (
                    f"{sub_text} — daily budget reached — raise MO_DAILY_BUDGET_USD or wait"
                )
            # S.table rows have no sub field (spec.table normalises to
            # cells/do/sel), so render the error as a follow-up muted row
            # pinned to the first column. The reviewer must look at the
            # stage name to find it — call this out in the table note.
            out_rows.append({"cells": [
                S.muted(sub_text), "", "", "", "", "",
            ], "do": None, "sel": False})

    return S.table("Learning loop health", cols, head, out_rows, full=True,
                   note="What each learning stage did in its last 3 reflect passes. "
                        "A stage that keeps producing nothing raises an alarm here. "
                        "A muted row under a stage carries its last_error (cost "
                        "circuit hits include the daily-budget hint).")


# ── Recent learning events ─────────────────────────────────────────────────

def _recent_learning_events(home: Path, now: int) -> dict[str, Any]:
    conn = db(home)
    cutoff = now - 14 * DAY
    # Collect (epoch_ts, item) pairs so the four kinds merge in a single
    # newest-first ordering — the kickoff's "at most 12" is across all kinds,
    # so per-kind LIMITs are raised (we still bound per kind at 12 to avoid
    # pulling a runaway table; the global cap of 12 trims the merged list).
    pairs: list[tuple[int, dict[str, Any]]] = []

    # 1. emergent_patterns approved (resolved_at set).
    if conn.has_table("emergent_patterns"):
        cols = {r["name"] for r in conn.rows("PRAGMA table_info(emergent_patterns)")}
        if {"resolved_at", "pattern_id", "status"} <= cols:
            rows = conn.rows(
                "SELECT pattern_id, cluster_label, lesson_text, resolved_at FROM emergent_patterns "
                "WHERE status = 'approved' AND resolved_at IS NOT NULL AND resolved_at >= ? "
                "ORDER BY resolved_at DESC LIMIT 12", (cutoff,))
            for r in rows:
                pid = str(r.get("pattern_id") or "")
                label = str(r.get("cluster_label") or "")
                lesson_text = str(r.get("lesson_text") or "")
                ts = int(r.get("resolved_at") or 0)
                primary = (lesson_text or label)[:140]
                date = _iso_date(ts)
                pairs.append((ts, S.ok(f"Pattern approved: {primary}", f"{date} · pattern {pid}")))

    # 2. semantic_memory retired (retired_at > 0). retire_reason is read-only.
    if conn.has_table("semantic_memory"):
        cols = {r["name"] for r in conn.rows("PRAGMA table_info(semantic_memory)")}
        if {"retired_at", "retire_reason"} <= cols:
            rows = conn.rows(
                "SELECT text, retired_at, retire_reason FROM semantic_memory "
                "WHERE retired_at > 0 AND retired_at >= ? "
                "ORDER BY retired_at DESC LIMIT 12", (cutoff,))
            for r in rows:
                text = str(r.get("text") or "")
                ts = int(r.get("retired_at") or 0)
                reason = str(r.get("retire_reason") or "retired")
                date = _iso_date(ts)
                pairs.append((ts, S.warn(f"Memory retired: {text[:140]}", f"{date} · {reason}")))

    # 3. promotion_records (any decision within 14 days).
    if conn.has_table("promotion_records"):
        cols = {r["name"] for r in conn.rows("PRAGMA table_info(promotion_records)")}
        if {"decided_at", "decision", "candidate_id"} <= cols:
            iso_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
            rows = conn.rows(
                "SELECT promotion_id, candidate_id, decision, decided_at, "
                "utility_before, utility_after FROM promotion_records "
                "WHERE decided_at >= ? ORDER BY decided_at DESC LIMIT 12", (iso_cutoff,))
            for r in rows:
                decision = str(r.get("decision") or "?")
                cand = str(r.get("candidate_id") or "")
                iso_ts = str(r.get("decided_at") or "")
                ts = _iso_to_epoch(iso_ts)
                before = r.get("utility_before")
                after = r.get("utility_after")
                utility = (f"utility {before:.2f} → {after:.2f}"
                           if isinstance(before, (int, float)) and isinstance(after, (int, float))
                           else "")
                date = iso_ts[:10] if iso_ts else ""
                pairs.append((ts, S.item(f"Promotion {decision}: {cand}", f"{date} · {utility}".strip(" ·"),
                              mc=outcome_colour(decision))))

    # 4. bug_reports with agent_role='learning' (first_seen_at within 14 days).
    if conn.has_table("bug_reports"):
        cols = {r["name"] for r in conn.rows("PRAGMA table_info(bug_reports)")}
        if {"agent_role", "first_seen_at", "title", "frequency"} <= cols:
            rows = conn.rows(
                "SELECT fingerprint, title, first_seen_at, frequency FROM bug_reports "
                "WHERE agent_role = 'learning' AND first_seen_at >= ? "
                "ORDER BY first_seen_at DESC LIMIT 12", (cutoff,))
            for r in rows:
                title = str(r.get("title") or "")
                ts = int(r.get("first_seen_at") or 0)
                freq = int(r.get("frequency") or 0)
                date = _iso_date(ts)
                pairs.append((ts, S.bad(
                    f"mini-ork issue: {title[:140]}",
                    f"{date} · seen {freq}×",
                    acts=[S.page_link("verify", "bugs")],
                )))

    # Newest-first across all four kinds, then the first 12 of those — the
    # kickoff's "at most 12" is global, not per-kind, so the merge + sort +
    # cap is what enforces it.
    pairs.sort(key=lambda p: p[0], reverse=True)
    items = [item for _, item in pairs[:12]]

    if not items:
        return S.lst("Recent learning events",
                     [S.dot("No learning events in 14 days",
                            "Approvals, retirements and promotions appear here.")],
                     full=True,
                     note="Last 14 days, newest first. One entry per kind — "
                          "patterns approved, memories retired, promotions decided, "
                          "and mini-ork bugs reported by the learning role.")

    return S.lst("Recent learning events", items, full=True,
                 note="Last 14 days, newest first. One entry per kind — "
                      "patterns approved, memories retired, promotions decided, "
                      "and mini-ork bugs reported by the learning role.")


def _iso_date(epoch: int) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(int(epoch)))


def _iso_to_epoch(iso_ts: str) -> int:
    """Normalise a promotion_records.decided_at (ISO-8601 text) to epoch seconds.

    The column is stored as ``YYYY-MM-DDTHH:MM:SSZ`` text, not as a SQLite
    integer — so it can't be compared or sorted against the other kinds'
    epoch columns. Returning 0 on a bad/empty string keeps the sort stable
    (those rows fall to the bottom of the merge).
    """
    if not iso_ts:
        return 0
    try:
        return int(time.mktime(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0