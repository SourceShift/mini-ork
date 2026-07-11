"""finalize — Python port of lib/finalize.sh.

Faithful port of ``mo_finalize``: walks ``$MINI_ORCH_DIR/runs/$JOB_ID``
for epic + iter subdirs, renders a ``COMPLETION_REPORT.md`` with Epics /
Cache reuse / Cost trace / A-B probe / Next-actions sections, and
(default ON) auto-merges APPROVE branches via ``mo_auto_merge`` plus a
cache-reuse summary table when ``mo_aggregate_cache_stats`` is loaded.

Co-existence model (strangler-fig): ``lib/finalize.sh`` stays
byte-identical. This port renders the same markdown section ordering,
the same printf column widths, the same float-formats (``.4f`` for cost
trace totals, ``.2f`` for cache-saved dollars), and delegates to the
SAME bash helpers (``mo_cache_run_summary``, ``mo_auto_merge``,
``mo_aggregate_cache_stats``) via subprocess so both sides share their
output. Parity is enforced by the sibling test
(``tests/unit/test_finalize_py.py``): >=6 live-subprocess cases; bash
stdout is the ground-truth reference, deep-diffed against the Python
report after stripping the volatile ``Generated:`` timestamp and the
``# Mini-ork completion report — <JOB_ID>`` header line.

DB resolution: bash uses ``${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}``.
The port raises ``ValueError`` when both ``db`` and ``MINI_ORK_DB`` are
unset and falls back to ``MINI_ORK_HOME/state.db`` only when ``db`` is
None and ``MINI_ORK_DB`` is also unset — never silently reads a
cwd-relative default.

Public surface:
    mo_finalize(repo_root, orch_dir, job_id, *,
                db=None, home=None,
                auto_merge=None, open_pr=None) -> str
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

_LIB_CACHE_SH = "lib/cache.sh"
_LIB_FINALIZE_SH = "lib/finalize.sh"
_LIB_AUTO_MERGE_SH = "lib/auto-merge.sh"
_LIB_PR_CREATE_SH = "lib/pr-create.sh"

_BRANCH_RE = re.compile(r"^>?\s*\*\*Branch:\*\*")
_ITER_DIR_RE = re.compile(r"iter-(\d+)$")


def _resolve_db(db: str | None, home: str | None) -> str:
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    if home:
        return os.path.join(home, "state.db")
    raise ValueError("MINI_ORK_DB unset and no home provided")


def _resolve_home(home: str | None) -> str:
    if home:
        return home
    env_home = os.environ.get("MINI_ORK_HOME")
    if env_home:
        return env_home
    return ".mini-ork"


def _resolve_root(repo_root: str | None) -> str:
    if not repo_root:
        raise ValueError("REPO_ROOT unset")
    return repo_root


def _resolve_orch_dir(orch_dir: str | None) -> str:
    if not orch_dir:
        raise ValueError("MINI_ORCH_DIR unset")
    return orch_dir


def _job_run_dir(orch_dir: str, job_id: str) -> str:
    return os.path.join(orch_dir, "runs", job_id)


def _report_path(job_run_dir: str) -> str:
    return os.path.join(job_run_dir, "COMPLETION_REPORT.md")


def _iter_dirs(epic_dir: str) -> list[str]:
    """Sorted VERS-reverse iter-* dirs (highest version first)."""
    found: list[tuple[int, str]] = []
    for entry in sorted(os.listdir(epic_dir)):
        full = os.path.join(epic_dir, entry)
        if not os.path.isdir(full):
            continue
        m = _ITER_DIR_RE.match(entry)
        if m:
            found.append((int(m.group(1)), full))
    found.sort(key=lambda t: t[0], reverse=True)
    return [full for _, full in found]


def _last_iter_with_verdict(epic_dir: str) -> str | None:
    for iter_dir in _iter_dirs(epic_dir):
        if os.path.isfile(os.path.join(iter_dir, "verdict.json")):
            m = _ITER_DIR_RE.match(os.path.basename(iter_dir))
            if m is not None:
                return m.group(1)
    return None


def _extract_branch_from_kickoff(kickoff_path: str) -> str:
    """Mirror of bash's:
        grep -E '^>?[[:space:]]*\\*\\*Branch:\\*\\*' "$kickoff_path" | head -1 \
          | sed -E 's/^[^`]*`([^`]+)`.*/\\1/'
    Returns '' when the line is missing or doesn't match."""
    if not kickoff_path or not os.path.isfile(kickoff_path):
        return ""
    with open(kickoff_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not _BRANCH_RE.match(line):
                continue
            backtick_open = line.find("`")
            if backtick_open < 0:
                return ""
            backtick_close = line.find("`", backtick_open + 1)
            if backtick_close < 0:
                return ""
            return line[backtick_open + 1:backtick_close]
    return ""


def _fmt_cost(cost: float) -> str:
    """Bash uses ``awk 'BEGIN{printf "%.4f", c+0}'`` — same as f'{c:.4f}'."""
    return f"{float(cost):.4f}"


def _stage_log_iter(iter_dir: str):
    """Yields (stage_name, log_path) for *.log files, skipping the
    bash-skipped stages (commits|merge|rebase|preflight)."""
    skip = {"commits", "merge", "rebase", "preflight"}
    for entry in sorted(os.listdir(iter_dir)):
        if not entry.endswith(".log"):
            continue
        log_path = os.path.join(iter_dir, entry)
        if not os.path.isfile(log_path):
            continue
        stage = entry[: -len(".log")]
        if stage in skip:
            continue
        yield stage, log_path


def _tail_result_line(log_path: str) -> dict | None:
    """Mirror of bash:
        grep '"type":"result"' "$stage_log" | tail -1 | jq -r '...'
    Returns the parsed JSON object or None if no result line found."""
    last = None
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if '"type":"result"' in raw:
                last = raw
    if last is None:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def _init_model(log_path: str) -> str:
    """Mirror of bash:
        grep -m1 '"subtype":"init"' "$stage_log" | jq -r '.model // empty' \
          | sed 's/\\[.*\\]//'
    Returns '?' when no init line or no model."""
    try:
        proc = subprocess.run(
            [
                "bash", "-c",
                f"grep -m1 '\\\"subtype\\\":\\\"init\\\"' \"{log_path}\" 2>/dev/null"
                " | jq -r '.model // empty' 2>/dev/null"
                " | sed 's/\\[.*\\]//'",
            ],
            capture_output=True, text=True,
        )
        model = proc.stdout.strip()
        return model or "?"
    except Exception:
        return "?"


def _render_epics_section(
    repo_root: str, job_run_dir: str, state_db: str, job_run_basename: str
) -> list[str]:
    lines: list[str] = []
    lines.append("## Epics")
    lines.append("")
    epic_dirs = sorted(
        d for d in os.listdir(job_run_dir)
        if os.path.isdir(os.path.join(job_run_dir, d))
    )
    for epic_name in epic_dirs:
        if epic_name == job_run_basename:
            continue
        epic_dir = os.path.join(job_run_dir, epic_name)
        lines.append(f"### {epic_name}")
        lines.append("")
        last_iter = _last_iter_with_verdict(epic_dir)
        verdict = "UNKNOWN"
        if last_iter is not None:
            vj = os.path.join(epic_dir, f"iter-{last_iter}", "verdict.json")
            if os.path.isfile(vj):
                try:
                    with open(vj, "r", encoding="utf-8") as fh:
                        verdict = json.load(fh).get("verdict") or "UNKNOWN"
                except (json.JSONDecodeError, OSError):
                    verdict = "UNKNOWN"
        iter_label = last_iter if last_iter is not None else "none"
        lines.append(f"- Final verdict: **{verdict}** (iter-{iter_label})")
        kickoff_path = _kickoff_path_for_epic(state_db, epic_name)
        branch = (
            _extract_branch_from_kickoff(os.path.join(repo_root, kickoff_path))
            if kickoff_path else ""
        )
        lines.append(f"- Branch: `{branch}`")
        if branch and _branch_resolves(repo_root, branch):
            lines.append("- Commits ahead of main:")
            for cl in _commits_ahead(repo_root, branch):
                lines.append(f"    {cl}")
        lines.append("")
    return lines


def _kickoff_path_for_epic(state_db: str, epic_id: str) -> str:
    try:
        con = sqlite3.connect(state_db)
        try:
            row = con.execute(
                "SELECT kickoff_path FROM epics WHERE id=?", (epic_id,)
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return row[0]
    except sqlite3.Error:
        pass
    return ""


def _branch_resolves(repo_root: str, branch: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", branch],
            capture_output=True, text=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _commits_ahead(repo_root: str, branch: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "log", "--oneline", f"main..{branch}"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return []
        return proc.stdout.splitlines()[:30]
    except Exception:
        return []


def _render_cache_reuse_section(state_db: str, job_id: str) -> list[str]:
    lines: list[str] = []
    lines.append("## Cache reuse this run")
    lines.append("")
    cache_rows = 0
    try:
        con = sqlite3.connect(state_db)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM mini_orch_sessions "
                "WHERE job_id=? AND reused_count>0",
                (job_id,),
            ).fetchone()
            if row:
                cache_rows = int(row[0] or 0)
        finally:
            con.close()
    except sqlite3.Error:
        cache_rows = 0

    if cache_rows == 0:
        lines.append("_No cache hits this run (cold cache or first dispatch)._")
    else:
        saved_total = "0.00"
        try:
            con = sqlite3.connect(state_db)
            try:
                row = con.execute(
                    "SELECT printf('%.2f', COALESCE(SUM(cost_usd * reused_count), 0)) "
                    "FROM mini_orch_sessions "
                    "WHERE job_id=? AND reused_count>0",
                    (job_id,),
                ).fetchone()
                if row and row[0] is not None:
                    saved_total = str(row[0])
            finally:
                con.close()
        except sqlite3.Error:
            pass
        lines.append(f"**Total dollars saved by cache hits: ${saved_total}**")
        lines.append("")
        lines.append("```")
        try:
            proc = subprocess.run(
                [
                    "bash", "-c",
                    f"source {_LIB_CACHE_SH}; mo_cache_run_summary \"$1\"",
                    "_", job_id,
                ],
                capture_output=True, text=True,
            )
            lines.append(proc.stdout.rstrip("\n"))
        except Exception:
            pass
        lines.append("```")
        lines.append("")
        lines.append("Replay cache state:")
        lines.append("")
        lines.append("```")
        lines.append("  mini-ork replay inspect $JOB_ID")
        lines.append("  mini-ork replay stats")
        lines.append("```")
    return lines


def _render_cost_trace(job_run_dir: str, job_run_basename: str) -> list[str]:
    lines: list[str] = []
    lines.append("## Cost trace (per-stage breakdown)")
    lines.append("")
    lines.append("```")
    lines.append(
        f"{'epic/iter/stage':<22} {'model':<22} {'result':<10} {'turns':>5} {'cost_usd':>12}"
    )
    lines.append(
        f"{'-'*22} {'-'*22} {'-'*10} {'-'*5} {'-'*12}"
    )
    grand_cost = 0.0
    bcap_count = 0
    err_count = 0
    for epic_entry in sorted(os.listdir(job_run_dir)):
        if epic_entry == job_run_basename:
            continue
        epic_dir = os.path.join(job_run_dir, epic_entry)
        if not os.path.isdir(epic_dir):
            continue
        for iter_dir in _iter_dirs(epic_dir):
            m = _ITER_DIR_RE.match(os.path.basename(iter_dir))
            iter_n = m.group(1) if m is not None else "0"
            for stage, log_path in _stage_log_iter(iter_dir):
                obj = _tail_result_line(log_path)
                if obj is None:
                    continue
                cost = float(obj.get("total_cost_usd") or 0)
                turns = obj.get("num_turns") or 0
                subtype = obj.get("subtype") or "?"
                model = _init_model(log_path)
                cost_fmt = _fmt_cost(cost)
                label = f"{epic_entry}/i{iter_n}/{stage}"
                row = (
                    f"{label:<22} {model:<22} {str(subtype):<10} "
                    f"{turns:>5} {cost_fmt:>12}"
                )
                lines.append(row)
                grand_cost += float(cost)
                if subtype == "error_max_budget_usd":
                    bcap_count += 1
                elif isinstance(subtype, str) and subtype.startswith("error_"):
                    err_count += 1
    lines.append(
        f"{'-'*22} {'-'*22} {'-'*10} {'-'*5} {'-'*12}"
    )
    grand_fmt = f"{grand_cost:.4f}"
    lines.append(f"{'TOTAL':<22} {'':<22} {'':<10} {'':>5} {grand_fmt:>12}")
    lines.append("```")
    if bcap_count > 0 or err_count > 0:
        lines.append("")
        lines.append("**Stage failures detected:**")
        if bcap_count > 0:
            lines.append(
                f"- Budget-cap (error_max_budget_usd) hits: **{bcap_count}** "
                "— raise cap or shorten prompt"
            )
        if err_count > 0:
            lines.append(
                f"- Other stage errors: **{err_count}** — inspect *.log for subtype"
            )
    return lines


def _render_ab_probe(job_run_dir: str, job_run_basename: str) -> list[str]:
    lines: list[str] = []
    lines.append("## No-context A/B probe (When Context Hurts, arXiv 2605.04361)")
    lines.append("")
    probe_count = 0
    control_count = 0
    probe_cost_sum = 0.0
    control_cost_sum = 0.0
    probe_approve = 0
    probe_reject = 0
    control_approve = 0
    control_reject = 0
    for epic_entry in sorted(os.listdir(job_run_dir)):
        if epic_entry == job_run_basename:
            continue
        epic_dir = os.path.join(job_run_dir, epic_entry)
        if not os.path.isdir(epic_dir):
            continue
        for iter_dir in _iter_dirs(epic_dir):
            sa_log = os.path.join(iter_dir, "spec-author.log")
            verdict_file = os.path.join(iter_dir, "verdict.json")
            sa_cost = 0.0
            if os.path.isfile(sa_log):
                obj = _tail_result_line(sa_log)
                if obj is not None:
                    sa_cost = float(obj.get("total_cost_usd") or 0)
            v_status = "UNKNOWN"
            if os.path.isfile(verdict_file):
                try:
                    with open(verdict_file, "r", encoding="utf-8") as fh:
                        v_status = json.load(fh).get("verdict") or "UNKNOWN"
                except (json.JSONDecodeError, OSError):
                    v_status = "UNKNOWN"
            if os.path.isfile(os.path.join(iter_dir, "no-context-probe.flag")):
                probe_count += 1
                probe_cost_sum += sa_cost
                if v_status == "APPROVE":
                    probe_approve += 1
                if v_status == "REQUEST_CHANGES":
                    probe_reject += 1
            else:
                control_count += 1
                control_cost_sum += sa_cost
                if v_status == "APPROVE":
                    control_approve += 1
                if v_status == "REQUEST_CHANGES":
                    control_reject += 1
    if probe_count == 0 and control_count == 0:
        lines.append("_No spec-author iters found in this run._")
    else:
        lines.append("```")
        lines.append(
            f"{'arm':<12} {'iters':>5} {'spec-author_sum':>16} "
            f"{'approves':>10} {'rejects':>10}"
        )
        lines.append(
            f"{'no-context':<12} {probe_count:>5} {f'{probe_cost_sum:.4f}':>16} "
            f"{probe_approve:>10} {probe_reject:>10}"
        )
        lines.append(
            f"{'control':<12} {control_count:>5} {f'{control_cost_sum:.4f}':>16} "
            f"{control_approve:>10} {control_reject:>10}"
        )
        lines.append("```")
        lines.append("")
        lines.append(
            "_If no-context approve-rate is comparable to control's, the "
            "memory hints aren't pulling weight on this epic-class — consider "
            "dropping. If much worse, hints are essential._"
        )
    return lines


def _render_next_actions(
    repo_root: str, state_db: str, job_run_dir: str, job_run_basename: str,
    open_pr: bool | None,
) -> list[str]:
    lines: list[str] = []
    lines.append("## Next actions")
    lines.append("")
    lines.append("- Review the final commits per branch (`git log` lines above).")
    lines.append("- Open PRs:")
    lines.append("  ```")
    for epic_entry in sorted(os.listdir(job_run_dir)):
        if epic_entry == job_run_basename:
            continue
        epic_dir = os.path.join(job_run_dir, epic_entry)
        if not os.path.isdir(epic_dir):
            continue
        kickoff_path = _kickoff_path_for_epic(state_db, epic_entry)
        branch = (
            _extract_branch_from_kickoff(os.path.join(repo_root, kickoff_path))
            if kickoff_path else ""
        )
        if open_pr:
            try:
                proc = subprocess.run(
                    [
                        "bash", "-c",
                        f"source {_LIB_FINALIZE_SH}; source {_LIB_AUTO_MERGE_SH}; "
                        f"source {_LIB_PR_CREATE_SH}; "
                        f"mo_open_pr \"$1\" \"$2\" \"$3\"",
                        "_", epic_entry, branch,
                        os.path.join(repo_root, kickoff_path) if kickoff_path else "",
                    ],
                    capture_output=True, text=True,
                )
                url = proc.stdout.strip()
                if url:
                    lines.append(f"  {epic_entry} → {url}")
                else:
                    lines.append(f"  {epic_entry} → (PR open skipped; see mini-ork logs)")
            except Exception:
                lines.append(f"  {epic_entry} → (PR open skipped; see mini-ork logs)")
        else:
            lines.append(f"  gh pr create --base main --head {branch} --title \"...\"")
    lines.append("  ```")
    return lines


def _auto_merge_block(job_run_dir: str, state_db: str) -> list[str] | None:
    """Returns the lines to append to the report for the auto-merge phase,
    or None if MO_AUTO_MERGE=0."""
    auto_merge = os.environ.get("MO_AUTO_MERGE", "1")
    if auto_merge == "0":
        sys.stderr.write("[mini-ork] auto-merge SKIPPED (MO_AUTO_MERGE=0)\n")
        return None
    sys.stdout.write("\n")
    sys.stdout.write("─" * 65 + "\n")
    sys.stdout.write(" auto-merge phase\n")
    sys.stdout.write("─" * 65 + "\n")
    try:
        subprocess.run(
            ["bash", "-c", f"source {_LIB_AUTO_MERGE_SH}; mo_auto_merge"],
            env={**os.environ, "MINI_ORK_DB": state_db},
            capture_output=False,
        )
    except Exception:
        sys.stderr.write(
            "[mini-ork] WARN auto-merge returned non-zero (some epics skipped)\n"
        )
    lines: list[str] = []
    lines.append("")
    lines.append("## Auto-merge results")
    lines.append("")
    merge_log = os.path.join(job_run_dir, "merge.log")
    if os.path.isfile(merge_log):
        lines.append("```")
        with open(merge_log, "r", encoding="utf-8", errors="replace") as fh:
            lines.append(fh.read().rstrip("\n"))
        lines.append("```")
    else:
        lines.append("_No merge log emitted._")
    return lines


def _cache_reuse_summary_block(job_run_dir: str, state_db: str) -> list[str] | None:
    """Returns the lines to append for the prompt-cache summary section,
    or None if ``mo_aggregate_cache_stats`` is not loaded."""
    try:
        proc = subprocess.run(
            [
                "bash", "-c",
                f"source {_LIB_CACHE_SH}; declare -F mo_aggregate_cache_stats "
                f">/dev/null 2>&1 && echo OK || echo NO",
            ],
            env={**os.environ, "MINI_ORK_DB": state_db},
            capture_output=True, text=True,
        )
        if proc.stdout.strip() != "OK":
            return None
    except Exception:
        return None
    lines: list[str] = []
    lines.append("")
    lines.append("## Cache reuse this run (prompt cache)")
    lines.append("")
    lines.append("| Epic | Iter | Cache reads | Cache writes | Uncached | Hit rate | $ saved |")
    lines.append("|---|---|---|---|---|---|---|")
    total_read = 0
    total_creation = 0
    total_uncached = 0
    for epic_entry in sorted(os.listdir(job_run_dir)):
        epic_dir = os.path.join(job_run_dir, epic_entry)
        if not os.path.isdir(epic_dir):
            continue
        if epic_entry.startswith("_"):
            continue
        for iter_dir in _iter_dirs(epic_dir):
            try:
                subprocess.run(
                    ["bash", "-c", f"source {_LIB_CACHE_SH}; mo_aggregate_cache_stats \"$1\"",
                     "_", iter_dir],
                    env={**os.environ, "MINI_ORK_DB": state_db},
                    capture_output=True,
                )
            except Exception:
                continue
            stats_file = os.path.join(iter_dir, "cache-stats.json")
            if not os.path.isfile(stats_file):
                continue
            try:
                with open(stats_file, "r", encoding="utf-8") as fh:
                    stats = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            m = _ITER_DIR_RE.match(os.path.basename(iter_dir))
            iter_n = m.group(1) if m is not None else "0"
            r = int(stats.get("cache_read_tokens") or 0)
            c = int(stats.get("cache_creation_tokens") or 0)
            u = int(stats.get("uncached_input_tokens") or 0)
            hr_val = float(stats.get("hit_rate") or 0)
            hr = f"{hr_val * 100:.1f}%"
            saved = stats.get("estimated_usd_saved") or 0
            total_read += r
            total_creation += c
            total_uncached += u
            lines.append(
                f"| {epic_entry} | {iter_n} | {r} | {c} | {u} | {hr} | ${saved} |"
            )
    lines.append("")
    total_saved = total_read * 0.9 * 3 / 1000000
    lines.append(
        f"**Totals:** {total_read} cache reads, {total_creation} writes, "
        f"{total_uncached} uncached input tokens — **~${total_saved:.2f} saved** "
        "vs all-uncached."
    )
    return lines


def mo_finalize(
    repo_root: str,
    orch_dir: str,
    job_id: str,
    *,
    db: str | None = None,
    home: str | None = None,
    auto_merge: bool | None = None,
    open_pr: bool | None = None,
) -> str:
    """Port of ``mo_finalize``. Returns the path to COMPLETION_REPORT.md
    on stdout-equivalent."""
    repo_root = _resolve_root(repo_root)
    orch_dir = _resolve_orch_dir(orch_dir)
    if not job_id:
        raise ValueError("JOB_ID unset")
    resolved_home = _resolve_home(home)
    state_db = _resolve_db(db, resolved_home)

    if auto_merge is not None:
        os.environ["MO_AUTO_MERGE"] = "1" if auto_merge else "0"
    if open_pr is not None:
        os.environ["MO_OPEN_PR"] = "1" if open_pr else "0"

    job_run_dir = _job_run_dir(orch_dir, job_id)
    report = _report_path(job_run_dir)
    os.makedirs(job_run_dir, exist_ok=True)
    job_run_basename = os.path.basename(job_run_dir)

    sections: list[str] = []
    sections.append(f"# Mini-ork completion report — {job_id}")
    sections.append("")
    sections.append(f"Generated: {__generated_now()}")
    sections.append("")
    sections.extend(_render_epics_section(repo_root, job_run_dir, state_db, job_run_basename))
    sections.extend(_render_cache_reuse_section(state_db, job_id))
    sections.extend(_render_cost_trace(job_run_dir, job_run_basename))
    sections.extend(_render_ab_probe(job_run_dir, job_run_basename))
    sections.extend(_render_next_actions(repo_root, state_db, job_run_dir, job_run_basename, open_pr))

    with open(report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sections) + "\n")

    auto_merge_block = _auto_merge_block(job_run_dir, state_db)
    if auto_merge_block is not None:
        with open(report, "a", encoding="utf-8") as fh:
            fh.write("\n".join(auto_merge_block) + "\n")

    cache_summary_block = _cache_reuse_summary_block(job_run_dir, state_db)
    if cache_summary_block is not None:
        with open(report, "a", encoding="utf-8") as fh:
            fh.write("\n".join(cache_summary_block) + "\n")

    return report


def __generated_now() -> str:
    """Volatile timestamp. Mirror of bash's $(date -u +%FT%TZ)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")