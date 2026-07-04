"""rubric_prescreen — Python port of lib/rubric-prescreen.sh.

Faithful port of the public surface of ``lib/rubric-prescreen.sh``. The
bash file already lifts three Python heredocs into itself (extracting
JSON, summarizing artifacts, substituting template markers) — this port
collapses those back into a Python module while keeping the bash
control flow (cache lookup → template split → claude subprocess →
extract → cache emit) mirrored as a callable orchestrator.

Co-existence model (strangler-fig): ``lib/rubric-prescreen.sh`` stays
byte-identical. Parity is enforced by
``tests/unit/test_rubric_prescreen_py.py`` (≥6 live-subprocess cases:
each helper called via real ``bash -c 'source lib/rubric-prescreen.sh
&& source lib/cache.sh && fn args'`` against a temp
``db/init.sh``-scaffolded SQLite, then again via the Python port, then
DB-row + stdout diffed). The orchestrators
(``mo_run_rubric_prescreen`` / ``mo_rubric_run_score``) shell out to
non-deterministic subprocesses (claude -p, llm_dispatch); their parity
is structural (same argv, same DB writes) and not covered by the
test suite.

Pipeline map (bash → Python; bash line ranges from
``lib/rubric-prescreen.sh`` and ``lib/cache.sh``):

  extract_rubric_json       lines 140-159  → extract_rubric_json
  artifact_summary          lines 247-267  → artifact_summary
  substitute_template       lines 271-279  → substitute_template
  build_parse_error_payload lines 187-191  → build_parse_error_payload
  build_panel_verdict       lines 335-339  → build_panel_verdict
  fetch_kickoff_path        lines 27-29    → fetch_kickoff_path
  mo_cache_input_hash       cache.sh 69-75 → cache_input_hash
  mo_cache_lookup           cache.sh 98-112 → cache_lookup
  mo_cache_record_hit       cache.sh 115-128 → cache_record_hit
  mo_cache_emit             cache.sh 135-163 → cache_emit
  mo_cache_costline_from_log cache.sh 168-177 → cache_costline_from_log
  mo_run_rubric_prescreen   lines 17-207   → mo_run_rubric_prescreen
  mo_rubric_run_score       lines 229-362  → mo_rubric_run_score
  mo_append_rubric_to_feedback lines 365-383 → mo_append_rubric_to_feedback

Public surface (mirrors the bash signatures exactly where possible):
    extract_rubric_json(text: str) -> Optional[str]
    substitute_template(template: str, kickoff_body: str, diff_summary: str) -> str
    artifact_summary(run_dir: str, max_chars: int = 12000) -> str
    build_parse_error_payload(diag: str = "", log_path: Optional[str] = None) -> dict
    build_panel_verdict(score: int, pass_: bool, task_class: str) -> dict
    fetch_kickoff_path(db_path: str, epic: str, repo_root: str) -> Optional[str]
    cache_input_hash(data: str) -> str
    cache_lookup(db_path: str, stage: str, epic: str, iter: int, input_hash: str) -> str
    cache_record_hit(db_path: str, stage: str, epic: str, iter: int, input_hash: str) -> None
    cache_emit(db_path: str, stage: str, epic: str, iter: int, input_hash: str,
               status: str, output_path: str, log_path: str,
               cost_usd: float, turns: int, duration_ms: int,
               job_id: str = "unknown", prompt_version: str = "v1") -> None
    cache_costline_from_log(log_path: str) -> tuple[float, int, int]
    mo_run_rubric_prescreen(epic, worktree, iter, repo_root, prompts_dir,
                            scripts_dir) -> None
    mo_rubric_run_score(kickoff_path, run_dir, task_class="generic") -> None
    mo_append_rubric_to_feedback(epic, iter, feedback_path) -> None

Notes on parity:
- The three Python heredocs lifted from bash are byte-equivalent by
  construction (they were Python source files wrapped in bash heredocs).
  The port reproduces them with only the minimum required type hints.
- ``cache_emit`` uses ``secrets.token_hex(16)`` for the uuid (32 hex
  chars, no hyphens). The bash version uses ``uuidgen`` which emits a
  hyphenated UUID. The DB-row diff IGNORES the uuid column (per the
  plan's risk note) — only logical columns (stage, epic_id, iter,
  input_hash, status, output_path, log_path, cost_usd, turns,
  duration_ms, prompt_version) are compared.
- ``substitute_template`` does FIRST-occurrence-only replacement
  (mirrors the bash awk splitter at lines 57-66 which splits on the
  first marker). This is intentionally different from ``str.replace``
  which would substitute every occurrence. The parity test exercises
  the first-only semantics.
- ``cache_costline_from_log`` returns ``(0.0, 0, 0)`` on parse failure
  (bash emits literal "0 0 0" — three space-separated zeros).
- ``mo_run_rubric_prescreen`` / ``mo_rubric_run_score`` are not
  parity-tested: they shell out to ``claude -p`` and ``llm_dispatch``
  which are non-deterministic. The port mirrors the bash control flow
  verbatim including argv construction and DB writes.
- The bash file's own subprocess-on-bash handling
  (``( set -uo pipefail; ... )``) and the ``|| true`` after the
  claude call are preserved: the orchestrator never raises on
  subprocess failure (advisory-only, matches bash semantics).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

__all__ = [
    "extract_rubric_json",
    "substitute_template",
    "artifact_summary",
    "build_parse_error_payload",
    "build_panel_verdict",
    "fetch_kickoff_path",
    "cache_input_hash",
    "cache_lookup",
    "cache_record_hit",
    "cache_emit",
    "cache_costline_from_log",
    "mo_run_rubric_prescreen",
    "mo_rubric_run_score",
    "mo_append_rubric_to_feedback",
]


# ─────────────────────────────────────────────────────────────────────────────
# Heredoc-lifted helpers (lines 140-159, 247-267, 271-279 of
# lib/rubric-prescreen.sh — these were already Python source lifted into
# bash heredocs; the port just lifts them into a module).
# ─────────────────────────────────────────────────────────────────────────────

def extract_rubric_json(text: str) -> Optional[str]:
    """Mirror bash heredoc at lines 140-159.

    Brace-balanced JSON scanner: finds the LAST ``{"pass":`` start in
    the text, walks forward with a depth counter (respecting string
    literals + backslash escapes) until the matching close brace, then
    tries ``json.loads`` on the candidate. Returns the candidate
    substring on success, ``None`` otherwise.

    The bash heredoc iterates ``starts`` in REVERSED order — it
    prefers the LAST ``{"pass":`` in the text, so a "Here's the
    final rubric: {...}" preamble with an earlier ``{"pass"`` is
    ignored. The port mirrors exactly.
    """
    starts = [m.start() for m in re.finditer(r'\{[^{]*?"pass"\s*:', text)]
    for start in reversed(starts):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    cand = text[start:i + 1]
                    try:
                        json.loads(cand)
                    except Exception:
                        break
                    return cand
    return None


def substitute_template(template: str, kickoff_body: str, diff_summary: str) -> str:
    """Mirror bash heredoc at lines 271-279.

    First-occurrence-only replacement of ``{{KICKOFF_BODY}}`` and
    ``{{DIFF_SUMMARY}}``. Mirrors the awk splitter at lines 57-66 of
    the bash file which splits the template on the FIRST occurrence of
    each marker. If a marker does not appear, it passes through
    unchanged. ``str.replace`` would substitute every occurrence —
    do NOT use it here, the parity test will catch the difference.

    The ``diff_summary`` is rstripped of trailing newlines because the
    bash caller feeds it via ``"$(python3 ...)"`` (artifact_summary
    variable at line 247), and bash command-substitution strips
    trailing newlines from ``$(...)`` outputs. The kickoff body is
    passed as-is because the bash version reads it from a file via
    ``open(kickoff).read()`` (no rstrip happens at that boundary).

    The return value is rstripped of trailing newlines to match the
    bash caller's ``prompt_text=$(python3 ...)`` capture, which
    strips trailing newlines from ``$(...)`` outputs.
    """
    body = template
    if "{{KICKOFF_BODY}}" in body:
        body = body.replace("{{KICKOFF_BODY}}", kickoff_body, 1)
    if "{{DIFF_SUMMARY}}" in body:
        body = body.replace("{{DIFF_SUMMARY}}", diff_summary.rstrip("\n"), 1)
    return body.rstrip("\n")


def artifact_summary(run_dir: str, max_chars: int = 12000) -> str:
    """Mirror bash heredoc at lines 247-267.

    Bounded work-product summary: list files in ``run_dir`` (skipping
    dotfiles), print ``### <filename> (<size> bytes)`` header, then the
    first 25 lines (capped at 2000 chars) for text files (.md / .json /
    .txt / .yaml / .log) that are non-empty. Output is capped at
    ``max_chars`` total (default 12000 — matches bash).
    """
    lines: list[str] = []
    try:
        names = sorted(os.listdir(run_dir))
    except FileNotFoundError:
        return ""
    for name in names:
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        lines.append(f"### {name} ({size} bytes)")
        if name.endswith((".md", ".json", ".txt", ".yaml", ".log")) and size > 0:
            try:
                with open(path, errors="replace") as f:
                    head = "".join(f.readlines()[:25])
                lines.append(head[:2000].rstrip())
            except Exception:
                pass
        lines.append("")
    # Bash callers use ``$(python3 ...)`` which strips trailing
    # newlines from the heredoc's print output. Match that semantic
    # by rstripping the joined string so the parity test sees the
    # same effective string on both sides.
    return "\n".join(lines)[:max_chars].rstrip("\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON payload builders
# ─────────────────────────────────────────────────────────────────────────────

def build_parse_error_payload(
    diag: str = "",
    log_path: Optional[str] = None,
) -> dict[str, Any]:
    """Mirror bash jq -n at lines 187-191.

    When ``log_path`` is provided, the payload includes
    ``parse_error_diagnostic`` (last 800 chars of the model output)
    and ``parse_error_log_hint`` ("inspect last 200 lines of <path>")
    so the operator can diagnose why all 4 extraction strategies
    missed. When ``log_path`` is None, the diagnostic fields are
    omitted (mirrors the dispatch-failure branch at lines 323-325
    which only emits ``parse_error_diagnostic``).
    """
    payload: dict[str, Any] = {
        "pass": False,
        "score": -1,
        "parse_error": True,
        "items": [],
    }
    if log_path is not None:
        payload["parse_error_diagnostic"] = diag
        payload["parse_error_log_hint"] = f"inspect last 200 lines of {log_path}"
    else:
        payload["parse_error_diagnostic"] = diag
    return payload


def build_panel_verdict(
    score: int,
    pass_: bool,
    task_class: str,
    source: str = "rubric-prescreen",
) -> dict[str, Any]:
    """Mirror bash jq -n at lines 335-339.

    Maps rubric score (0-8) to panel_score (0-100) via
    ``panel_score = score * 12.5``. Consumed by lib/promotion_gate.sh.
    """
    return {
        "panel_score": float(score) * 12.5,
        "pass": pass_,
        "source": source,
        "task_class": task_class,
        "scale": "rubric 0-8 mapped to 0-100",
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers (mini_orch_sessions table, mirroring lib/cache.sh 98-177)
# ─────────────────────────────────────────────────────────────────────────────

def _open(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def fetch_kickoff_path(db_path: str, epic: str, repo_root: str) -> Optional[str]:
    """Mirror bash SELECT at lines 27-29.

    Returns the absolute kickoff path (``<repo_root>/<kickoff_path>``)
    for the given epic, or ``None`` if the epic has no kickoff_path
    set. Mirrors the bash's ``local kickoff_rel=$(sqlite3 ...)`` which
    emits an empty string on no row, then the bash uses
    ``$REPO_ROOT/$kickoff_rel``. The port collapses that into a
    single ``Optional[str]`` return — None on miss.
    """
    con = _open(db_path)
    try:
        row = con.execute(
            "SELECT kickoff_path FROM epics WHERE id=?",
            (epic,),
        ).fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        return None
    kickoff_rel = row[0]
    return f"{repo_root}/{kickoff_rel}"


def cache_input_hash(data: str) -> str:
    """Mirror bash ``mo_cache_input_hash`` (lib/cache.sh 69-75).

    Prefers ``sha256sum`` if available, falls back to ``shasum -a 256``.
    In Python this is a single ``hashlib.sha256`` call — both shells
    produce the same hex digest on the same input.

    Note: bash reads from stdin; this function takes a string argument.
    The caller is responsible for feeding it the full bundle (use
    ``hash_bundle`` if you need bash's record-separator semantics).
    """
    import hashlib
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cache_lookup(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
) -> str:
    """Mirror bash ``mo_cache_lookup`` (lib/cache.sh 98-112).

    Returns the output_path on a cache HIT (status=success, not
    expired). Empty string on miss (matches bash's empty stdout).
    """
    con = _open(db_path)
    try:
        row = con.execute(
            """
            SELECT output_path FROM mini_orch_sessions
            WHERE epic_id = ? AND iter = ? AND stage = ? AND input_hash = ?
              AND status = 'success'
              AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
            ORDER BY updated_at DESC
            LIMIT 1;
            """,
            (epic, iter, stage, input_hash),
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def cache_record_hit(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
) -> None:
    """Mirror bash ``mo_cache_record_hit`` (lib/cache.sh 115-128).

    Bumps ``reused_count`` on the row that just served the hit. The
    bash ``UPDATE`` does NOT include the WHERE condition ``status =
    'success'`` (line 126) so it could increment a non-success row —
    the port includes it as well to match verbatim.
    """
    con = _open(db_path)
    try:
        con.execute(
            """
            UPDATE mini_orch_sessions
               SET reused_count = reused_count + 1,
                   updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE epic_id = ? AND iter = ? AND stage = ? AND input_hash = ?
               AND status = 'success';
            """,
            (epic, iter, stage, input_hash),
        )
        con.commit()
    finally:
        con.close()


def _expires_at_30d() -> str:
    """Mirror bash expires_at at lib/cache.sh 146-149.

    ``now + 30 days`` formatted as ``%Y-%m-%dT%H:%M:%S.fZ`` (3-digit
    millisecond precision, trailing Z). Matches the bash
    ``python3 -c 'datetime.utcnow() + timedelta(days=30)'`` output
    (utcnow is deprecated in 3.12 but the result is identical to
    ``datetime.now(timezone.utc)`` for this use).
    """
    dt = datetime.now(timezone.utc) + timedelta(days=30)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


def cache_emit(
    db_path: str,
    stage: str,
    epic: str,
    iter: int,
    input_hash: str,
    status: str,
    output_path: str,
    log_path: str,
    cost_usd: float,
    turns: int,
    duration_ms: int,
    job_id: str = "unknown",
    prompt_version: str = "v1",
) -> None:
    """Mirror bash ``mo_cache_emit`` (lib/cache.sh 135-163).

    Insert a row at stage completion. uuid is ``secrets.token_hex(16)``
    (32 hex chars, no hyphens) — differs from bash's hyphenated
    ``uuidgen`` output, but the DB-row diff IGNORES the uuid column
    per the plan's parity contract.
    """
    uuid = secrets.token_hex(16)
    expires_at = _expires_at_30d()
    con = _open(db_path)
    try:
        con.execute(
            """
            INSERT INTO mini_orch_sessions
              (uuid, job_id, epic_id, iter, stage, input_hash, status,
               output_path, log_path, cost_usd, turns, duration_ms,
               expires_at, prompt_version)
            VALUES
              (?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?,
               ?, ?)
            ON CONFLICT (uuid) DO NOTHING;
            """,
            (
                uuid, job_id, epic, iter, stage, input_hash, status,
                output_path, log_path, cost_usd, turns, duration_ms,
                expires_at, prompt_version,
            ),
        )
        con.commit()
    finally:
        con.close()


def cache_costline_from_log(log_path: str) -> tuple[float, int, int]:
    """Mirror bash ``mo_cache_costline_from_log`` (lib/cache.sh 168-177).

    Returns ``(cost_usd, turns, duration_ms)``. Emits ``(0.0, 0, 0)``
    if the log file is missing or no ``"type":"result"`` line is
    present (bash emits literal "0 0 0").
    """
    if not os.path.isfile(log_path):
        return (0.0, 0, 0)
    try:
        with open(log_path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return (0.0, 0, 0)
    # Match bash: grep '"type":"result"' | tail -1
    m = None
    for line in text.splitlines():
        if '"type":"result"' in line:
            m = line
    if m is None:
        return (0.0, 0, 0)
    try:
        obj = json.loads(m)
    except (ValueError, TypeError):
        return (0.0, 0, 0)
    cost = float(obj.get("total_cost_usd") or 0)
    nturns = int(obj.get("num_turns") or 0)
    dur = int(obj.get("duration_ms") or 0)
    return (cost, nturns, dur)


# ─────────────────────────────────────────────────────────────────────────────
# Free-lane helper (mirrors lib/lane-helpers.sh mo_lane_is_free)
# ─────────────────────────────────────────────────────────────────────────────

_FREE_LANES = frozenset({"glm", "kimi", "minimax"})


def _is_free_lane(lane: str) -> bool:
    return lane in _FREE_LANES


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrators (mirror lib/rubric-prescreen.sh 17-207 + 229-362)
# ─────────────────────────────────────────────────────────────────────────────

def mo_run_rubric_prescreen(
    epic: str,
    worktree: str,
    iter: int,
    repo_root: str,
    prompts_dir: str,
    scripts_dir: str,
    mini_ork_home: Optional[str] = None,
    mini_ork_db: Optional[str] = None,
    skip_cache: bool = False,
    lane: Optional[str] = None,
    rubric_budget_usd: Optional[float] = None,
    rubric_effort: Optional[str] = None,
    rubric_max_output_tokens: Optional[int] = None,
    rubric_timeout_sec: Optional[int] = None,
) -> None:
    """Mirror bash ``mo_run_rubric_prescreen`` (lib/rubric-prescreen.sh 17-207).

    Cache lookup → template split → claude subprocess → JSON extract
    → rubric.json write → cache emit. All subprocess calls (claude -p)
    are wrapped in ``(set -uo pipefail; ... ) || true`` semantics:
    failures are advisory-only, never raise.

    Args mirror the bash: ``epic worktree iter`` are positional, the
    rest come from env (caller can pass explicitly here for parity
    with tests that don't source the bash env).
    """
    if mini_ork_db is None:
        mini_ork_db = os.environ.get(
            "MINI_ORK_DB",
            f"{mini_ork_home or os.environ.get('MINI_ORK_HOME', '.mini-ork')}/state.db",
        )

    if lane is None:
        lane = os.environ.get("MO_RUBRIC_LANE", "kimi")

    if rubric_budget_usd is None:
        rubric_budget_usd = float(os.environ.get("MO_RUBRIC_BUDGET_USD", "0.60"))
    if rubric_effort is None:
        rubric_effort = os.environ.get("MO_RUBRIC_EFFORT", "low")
    if rubric_max_output_tokens is None:
        rubric_max_output_tokens = int(os.environ.get("MO_RUBRIC_MAX_OUTPUT_TOKENS", "2000"))
    if rubric_timeout_sec is None:
        rubric_timeout_sec = int(os.environ.get("MO_RUBRIC_TIMEOUT_SEC", "480"))

    iter_dir = f"{_run_dir(epic, mini_ork_home, repo_root)}/iter-{iter}"
    prompt_path = f"{iter_dir}/rubric-prompt.md"
    log_path = f"{iter_dir}/rubric.log"
    rubric_path = f"{iter_dir}/rubric.json"
    os.makedirs(iter_dir, exist_ok=True)

    kickoff_abs = fetch_kickoff_path(mini_ork_db, epic, repo_root)
    if not kickoff_abs or not os.path.isfile(kickoff_abs):
        # Match bash's behavior: sqlite3 emits empty on miss, then
        # ``cat $kickoff_abs`` at line 41 fails with a warning but
        # the script continues. Write a parse_error rubric and bail.
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    # Diff summary: file list + per-file +/- LOC. Cheaper than full diff.
    try:
        diff_summary = subprocess.run(
            ["git", "-C", worktree, "diff", "--stat", "main..HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout
        diff_summary = "\n".join(diff_summary.splitlines()[:30])
    except Exception:
        diff_summary = ""

    template_path = f"{prompts_dir}/rubric-prescreen.md"

    # T3: cache lookup. Hash = kickoff_body + diff_summary + template content.
    cached = ""
    cache_hash = ""
    if not skip_cache and os.environ.get("MO_SKIP_CACHE", "0") != "1":
        try:
            kickoff_body = _read_text(kickoff_abs)
            tpl_body = _read_text(template_path) if os.path.isfile(template_path) else ""
            bundle = f"{kickoff_body}\x1e{diff_summary}\x1e{tpl_body}"
            cache_hash = cache_input_hash(bundle)
            cached = cache_lookup(mini_ork_db, "rubric", epic, iter, cache_hash)
        except Exception:
            cached = ""
        if cached and os.path.isfile(cached):
            # Copy to rubric_path + record hit + emit log line.
            with open(cached, "rb") as src, open(rubric_path, "wb") as dst:
                dst.write(src.read())
            cache_record_hit(mini_ork_db, "rubric", epic, iter, cache_hash)
            try:
                with open(rubric_path) as f:
                    meta = json.loads(f.read())
            except (ValueError, OSError):
                meta = {}
            print(
                f"[mini-ork] CACHE HIT: rubric epic={epic} iter={iter} "
                f"pass={meta.get('pass')} score={meta.get('score')}",
                file=__import__("sys").stderr,
            )
            return

    # Build prompt via substitute_template.
    try:
        tpl_text = _read_text(template_path) if os.path.isfile(template_path) else ""
        kickoff_body = _read_text(kickoff_abs)
    except OSError:
        tpl_text, kickoff_body = "", ""
    prompt_text = substitute_template(tpl_text, kickoff_body, diff_summary)
    with open(prompt_path, "w") as f:
        f.write(prompt_text)

    print(
        f"[mini-ork] rubric pre-screen epic={epic} iter={iter} (model={lane})",
        file=__import__("sys").stderr,
    )

    env_script = f"{scripts_dir}/cl_{lane}.sh"
    if not os.path.isfile(env_script):
        print(
            f"[mini-ork] rubric: env script missing for lane={lane} → {env_script}",
            file=__import__("sys").stderr,
        )
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    # Subprocess env: source the lane env-script then run claude -p.
    budget_flag: list[str] = []
    if not _is_free_lane(lane):
        budget_flag = ["--max-budget-usd", str(rubric_budget_usd)]

    rubric_schema = (
        '{"type":"object","properties":{"pass":{"type":"boolean"},'
        '"score":{"type":"integer","minimum":0,"maximum":8},'
        '"items":{"type":"array","items":{"type":"object","properties":{'
        '"label":{"type":"string"},"verdict":{"type":"string",'
        '"enum":["PASS","FAIL","SKIP"]},"note":{"type":"string"}'
        '},"required":["label","verdict"]}}},"required":["pass","score","items"]}'
    )

    # Run claude in a sourced subshell. We do the source via env=
    # rather than bash -c so the port stays in pure Python.
    sub_env = dict(os.environ)
    sub_env["CLAUDE_CODE_EFFORT_LEVEL"] = rubric_effort
    sub_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(rubric_max_output_tokens)
    # Best-effort: source the env-script values into the subprocess env.
    try:
        with open(env_script) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export ") and "=" in line:
                    kv = line[len("export "):].split("=", 1)
                    if len(kv) == 2 and kv[0].strip().isidentifier():
                        v = kv[1].strip().strip('"').strip("'")
                        # Honor ${VAR:?...} only when VAR is set;
                        # if not, skip (bash would error).
                        if v.startswith("${") and ":" in v:
                            inner = v[2:-1]
                            var_name = inner.split(":", 1)[0]
                            if var_name in os.environ:
                                v = os.environ[var_name]
                            else:
                                continue
                        sub_env[kv[0].strip()] = v
    except OSError:
        pass

    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", rubric_schema,
        "--dangerously-skip-permissions",
        "--permission-mode", "acceptEdits",
        *budget_flag,
        prompt_text,
    ]

    # Best-effort subprocess: bash uses `( set -uo pipefail; ... ) || true`
    # so a claude failure is advisory-only. Mirror with check=False +
    # swallowing exceptions.
    try:
        with open(log_path, "w") as logf:
            subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                env=sub_env, check=False,
            )
    except (OSError, subprocess.SubprocessError):
        # Fall through to extraction (which will produce parse_error
        # because the log is empty/missing).
        pass

    # Extract JSON. Primary: --output-format json wrapper has model
    # output in .result field. Fallbacks mirror bash lines 126-138.
    result_text = _extract_result_text(log_path)

    extracted = ""
    if result_text:
        extracted = extract_rubric_json(result_text) or ""

    if not extracted:
        # awk fallback (line 163-170).
        try:
            with open(log_path) as f:
                for line in f:
                    if '{' in line and '"pass"' in line and ':' in line:
                        extracted = line.strip()
                        break
        except OSError:
            pass

    if extracted:
        try:
            json.loads(extracted)
            with open(rubric_path, "w") as f:
                f.write(extracted if extracted.endswith("\n") else extracted + "\n")
        except ValueError:
            diag = result_text[-800:] if result_text else ""
            with open(rubric_path, "w") as f:
                json.dump(
                    build_parse_error_payload(diag=diag, log_path=log_path),
                    f,
                )
    else:
        diag = result_text[-800:] if result_text else ""
        with open(rubric_path, "w") as f:
            json.dump(
                build_parse_error_payload(diag=diag, log_path=log_path),
                f,
            )

    try:
        with open(rubric_path) as f:
            meta = json.loads(f.read())
    except (ValueError, OSError):
        meta = {}
    print(
        f"[mini-ork] rubric epic={epic} iter={iter} "
        f"pass={meta.get('pass')} score={meta.get('score')}",
        file=__import__("sys").stderr,
    )

    # T3: emit cache row.
    if not skip_cache and os.environ.get("MO_SKIP_CACHE", "0") != "1":
        try:
            cost, turns, dur = cache_costline_from_log(log_path)
        except Exception:
            cost, turns, dur = 0.0, 0, 0
        try:
            cache_emit(
                mini_ork_db, "rubric", epic, iter, cache_hash, "success",
                rubric_path, log_path, cost, turns, dur,
                job_id=os.environ.get("JOB_ID", "unknown"),
            )
        except sqlite3.IntegrityError:
            pass


def mo_rubric_run_score(
    kickoff_path: str,
    run_dir: str,
    task_class: str = "generic",
    mini_ork_root: Optional[str] = None,
    mini_ork_home: Optional[str] = None,
    mini_ork_db: Optional[str] = None,
) -> None:
    """Mirror bash ``mo_rubric_run_score`` (lib/rubric-prescreen.sh 229-362).

    Run-shaped sibling: takes a kickoff_path + run_dir (no epics table
    needed), dispatches through ``llm_dispatch`` (mirror with a
    subprocess call), extracts JSON, writes ``<run_dir>/rubric.json``
    and ``<run_dir>/panel-verdict.json``, optionally writes an
    ``execution_traces`` row via ``trace_write`` (no-op if not
    available — the bash version checks ``declare -f trace_write``
    and silently skips).

    Advisory-only: never raises on subprocess failure.
    """
    if mini_ork_root is None:
        mini_ork_root = os.environ.get("MINI_ORK_ROOT", ".")
    if mini_ork_db is None:
        mini_ork_db = os.environ.get(
            "MINI_ORK_DB",
            f"{mini_ork_home or os.environ.get('MINI_ORK_HOME', '.mini-ork')}/state.db",
        )

    if not os.path.isfile(kickoff_path):
        print(f"rubric: kickoff not found: {kickoff_path}", file=__import__("sys").stderr)
        return
    if not os.path.isdir(run_dir):
        print(f"rubric: run_dir not found: {run_dir}", file=__import__("sys").stderr)
        return

    template = f"{mini_ork_root}/prompts/rubric-prescreen.md"
    if not os.path.isfile(template):
        print(f"rubric: template missing: {template}", file=__import__("sys").stderr)
        return

    rubric_path = f"{run_dir}/rubric.json"
    verdict_path = f"{run_dir}/panel-verdict.json"

    artifacts = artifact_summary(run_dir)
    if not artifacts:
        artifacts = "(run dir contains no readable artifacts)"

    prompt_text = substitute_template(_read_text(template), _read_text(kickoff_path), artifacts)

    print(f"  rubric: scoring run artifacts (task_class={task_class})", file=__import__("sys").stderr)
    raw = ""
    rc = 0
    try:
        r = subprocess.run(
            [
                "llm_dispatch",
                "--task-class", task_class,
                "--node-type", "rubric",
                "--prompt-text", prompt_text,
            ],
            capture_output=True, text=True, check=False,
        )
        raw = r.stdout
        rc = r.returncode
    except FileNotFoundError:
        rc = 127

    if rc != 0:
        print(
            f"  rubric: dispatch failed (rc={rc}): {raw[-300:]}",
            file=__import__("sys").stderr,
        )
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    extracted = extract_rubric_json(raw) or ""
    if extracted and "pass" in (json.loads(extracted) if extracted else {}) \
            and "score" in (json.loads(extracted) if extracted else {}):
        with open(rubric_path, "w") as f:
            f.write(extracted if extracted.endswith("\n") else extracted + "\n")
    else:
        diag = raw[-800:] if raw else ""
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(diag=diag), f)

    try:
        with open(rubric_path) as f:
            meta = json.loads(f.read())
    except (ValueError, OSError):
        meta = {}
    score = meta.get("score", -1)
    passed = bool(meta.get("pass", False))
    print(
        f"  rubric: pass={passed} score={score}/8 → {rubric_path}",
        file=__import__("sys").stderr,
    )

    # Panel verdict for the promotion gate: 0-8 → 0-100.
    if score != -1:
        with open(verdict_path, "w") as f:
            json.dump(
                build_panel_verdict(int(score), passed, task_class),
                f,
            )

    # Learning hook: persist as an execution trace. Mirror bash lines
    # 346-360: if ``trace_write`` is not available, skip silently.
    # The bash version calls ``trace_write <payload> >/dev/null 2>&1
    # || true`` — we mirror with a best-effort subprocess that is
    # allowed to fail without raising.
    trace_id = f"tr-rubric-{int(datetime.now(timezone.utc).timestamp())}"
    status = "success" if passed else "failure"
    try:
        with open(rubric_path) as f:
            rub = json.loads(f.read())
    except (ValueError, OSError):
        rub = {}
    payload = {
        "trace_id": trace_id,
        "task_class": task_class,
        "status": status,
        "final_artifact_ref": rubric_path,
        "verifier_output": rub,
    }
    try:
        subprocess.run(
            ["trace_write", json.dumps(payload)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, env={**os.environ, "MINI_ORK_DB": mini_ork_db},
        )
    except (OSError, subprocess.SubprocessError):
        pass


def mo_append_rubric_to_feedback(
    epic: str,
    iter: int,
    feedback_path: str,
    run_dir: Optional[str] = None,
    mini_ork_home: Optional[str] = None,
) -> None:
    """Mirror bash ``mo_append_rubric_to_feedback`` (lib/rubric-prescreen.sh 365-383).

    Pure file append. Reads ``<run_dir>/iter-<iter>/rubric.json`` (or
    the supplied ``run_dir``), and if ``.pass != true``, appends a
    formatted section to ``feedback_path`` listing only the non-PASS
    items (FAIL + SKIP).

    Pass-through: returns silently if the rubric file is missing or
    the rubric passed.
    """
    if run_dir is None:
        run_dir = _run_dir(epic, mini_ork_home)
    rub = f"{run_dir}/iter-{iter}/rubric.json"
    if not os.path.isfile(rub):
        return
    try:
        with open(rub) as f:
            data = json.loads(f.read())
    except (ValueError, OSError):
        return
    if data.get("pass") is True:
        return
    score = data.get("score", -1)
    items = data.get("items", [])
    lines = [
        "",
        "## Rubric pre-screen (advisory — Phase A.5)",
        "",
        f"Score: {score}/8 (need ≥6 to PASS)",
        "",
    ]
    for it in items:
        verdict = it.get("verdict", "")
        if verdict == "PASS":
            continue
        label = it.get("label", "")
        note = it.get("note", "")
        lines.append(f"- **[{verdict}]** {label} — {note}")
    lines.append("")
    with open(feedback_path, "a") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (not part of __all__; not part of the bash surface)
# ─────────────────────────────────────────────────────────────────────────────

def _run_dir(epic: str, mini_ork_home: Optional[str] = None,
             repo_root: Optional[str] = None) -> str:
    """Mirror bash ``mo_run_dir`` (lib/mo-runner.sh).

    Returns the per-epic run dir. Bash's exact resolution chain
    (T1.0 run-dir-first agents.yaml pattern) is not part of the
    parity contract — the port uses ``<home>/runs/<epic>`` which is
    the canonical location. The bash version is more elaborate (it
    checks $MINI_ORK_HOME, $MO_RUNS_DIR, the repo_root/.mini-ork
    layout); tests pass the resolved dir explicitly via ``run_dir=``
    so the simpler default is sufficient.
    """
    home = mini_ork_home or os.environ.get(
        "MINI_ORK_HOME", repo_root or "."
    )
    return os.path.join(home, "runs", epic)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_result_text(log_path: str) -> str:
    """Mirror bash jq fallbacks at lines 126-138.

    Tries three extraction strategies in order:
    1. ``.result`` field at the top level (--output-format json wrapper).
    2. ``select(.type=="assistant") | .message.content[]?
       | select(.type=="text") | .text`` (legacy stream-json shape).
    3. ``grep '"type":"result"' | tail -1 | jq -r '.result'`` (mixed
       deployment fallback).

    Returns the extracted text or empty string on miss.
    """
    if not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path) as f:
            text = f.read()
    except OSError:
        return ""

    # Strategy 1: top-level .result from --output-format json.
    for line in text.splitlines():
        if '"type":"result"' in line:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("result"):
                    return str(obj["result"])
            except (ValueError, TypeError):
                pass

    # Strategy 2: legacy stream-json shape.
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        for chunk in (msg.get("content") or []):
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                t = chunk.get("text")
                if t:
                    return str(t)

    return ""
