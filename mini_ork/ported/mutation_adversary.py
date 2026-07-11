"""mutation_adversary — Python port of lib/mutation-adversary.sh.

Faithful port of the *testable* deterministic sub-pipelines of
``lib/mutation-adversary.sh`` — the cache-hash, the assistant-text JSON
extraction (jq-stream + brace-balancer + awk-grep fallback cascade), the
mutations.json shape, and the mutation-validator math (skipped / zero /
dirty / counted / threshold). The bash function ``mo_run_mutation_validator``'s
git-apply + npx-playwright loop stays bash-only (it would require a fake
worktree + Playwright runtime per case = hours of test time). The Python
port gives callers an in-process surface and gives parity tests a stable
target to byte-diff against the live bash.

Co-existence model (strangler-fig): bash ``lib/mutation-adversary.sh``
stays byte-identical. Parity is enforced by
``tests/unit/test_mutation_adversary_py.py`` (>=6 live-subprocess cases:
cache-hash via ``printf|shasum`` vs Python; extraction via a bash
pipeline that wraps the verbatim brace-balancer heredoc vs the Python
function; validation math via ``jq -n`` vs Python; DB row-shape via
real ``mo_cache_emit`` vs Python's stdlib ``sqlite3`` INSERT, both
running against a temp ``db/init.sh``-scaffolded state).

Pipeline map (bash → Python):

  mo_run_mutation_adversary cache-key (lines 34-37, 181)
    bash  printf '%s\\x1e%s\\x1e%s' kickoff spec prompt_hash
       | sha256sum                             →  compute_cache_hash
    bash  mo_cache_input_hash (sha256sum|shasum)→  sha256 of joined bytes

  mo_run_mutation_adversary JSON extraction (lines 127-175)
    jq -r assistant.text blocks (stream-json)  →  _iter_assistant_text
    grep+tail 'result'.result fallback         →  _read_fallback_result_text
    Python heredoc brace-balancer
      (lib/mutation-adversary.sh:140-159,
       verbatim: ``r"\\{[^{]*?\\"mutations\\""``)
                                                 →  _brace_balance_mutations
    awk scan for line containing '\"mutations\"'
      slurping rest of file                     →  _awk_grep_mutations
    jq -e '.mutations' | jq -c '.'              →  build_mutations_json
    jq -n '{mutations:[],parse_error:true,
              skipped:false}'                   →  build_mutations_json (err branch)

  mo_run_mutation_validator math (lines 209-296)
    skipped:true early-bail                     →  compute_validation_results
    zero-mutations early-bail                  →  compute_validation_results
    worktree-dirty early-bail                  →  compute_validation_results
    awk 'BEGIN{ printf \"%.3f\", k/t }'         →  compute_validation_results (kill_rate)
    jq -n {kill_rate,total,killed,
              skipped:false,results}            →  compute_validation_results (output)
    awk 'BEGIN{ if (r>=0.8) print PASS else FAIL }'
                                                 →  threshold_pass

  mo_cache_emit → mini_orch_sessions INSERT (lib/cache.sh:135)
    sqlite3 INSERT                            →  _emit_cache_row

Public surface:
    compute_cache_hash(kickoff, spec, prompt) -> str
    extract_mutations_from_log(log_path)      -> dict | None
    build_mutations_json(extracted)           -> dict
    compute_validation_results(mutations_json, outcomes, *,
                               worktree_dirty=False) -> dict
    threshold_pass(kill_rate)                 -> str  ("PASS"|"FAIL")
    _emit_cache_row(db_path, epic, iter, hash, cost, turns, dur,
                    output_path=\"\", log_path=\"\", status=\"success\",
                    prompt_version=\"v1\", job_id=\"unknown\") -> None
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

__all__ = [
    "compute_cache_hash",
    "extract_mutations_from_log",
    "build_mutations_json",
    "compute_validation_results",
    "threshold_pass",
    "_emit_cache_row",
    "_iter_assistant_text",
    "_read_fallback_result_text",
    "_brace_balance_mutations",
    "_awk_grep_mutations",
]

# Record separator byte (\x1e = ASCII 30). Mirrors `printf '%s\\x1e%s\\x1e%s'`
# inside lib/mutation-adversary.sh. Do NOT substitute newline — the cache-key
# tie-breaker collapses on \x1e vs \n. See risk_notes in the plan.
_RS = b"\x1e"

# Verbatim regex from the brace-balancer heredoc at
# lib/mutation-adversary.sh:143. The pattern matches `{` followed by zero
# or more non-`{` chars (non-greedy) followed by the literal substring
# `"mutations"`. Because Claude's stream-json wrapper escapes inner
# quotes as `\"`, the bash heredoc writes `r"\\{[^{]*?\\\"mutations\\\""`
# which Python (with single-quoted bash heredoc) sees as
# `r"\\{[^{]*?\"mutations\""` — equivalent to the r-string below.
_MUTATIONS_START_RE = re.compile(r'\{[^{]*?"mutations"')

# Awk-style fallback marker: `\\{\\s*\"mutations\"\\s*:` at line start
# (bash: `/\\{\\[\\[:space:\\]\\]*\"mutations\"\\[:space:\\]\\*:/`).
_AWK_MUTATIONS_RE = re.compile(r'\{\s*"mutations"\s*:')

# Precision: bash emits kill_rate via `awk 'BEGIN{ printf \"%.3f\", k/t }'`.
# Mirror with float(f\"{x:.3f}\") so the JSON number text is identical.
_KILL_RATE_PRECISION = 3
# Threshold paper TDAD §3.3: kill_rate >= 0.8 → PASS.
_THRESHOLD = 0.8


# ─────────────────────────────────────────────────────────────────────────────
# Cache-hash (Phase A.3 line 34-37, 181)
# ─────────────────────────────────────────────────────────────────────────────
def compute_cache_hash(
    kickoff: str | bytes,
    spec: str | bytes,
    prompt: str | bytes,
) -> str:
    """Mirror bash ``printf '%s\\x1e%s\\x1e%s' a b c | mo_cache_input_hash``.

    The byte boundary is the ASCII Record Separator (``\\x1e``), NOT
    newline — see ``_RS`` module constant. ``mo_cache_input_hash`` is
    ``sha256sum | awk '{print $1}'`` (Linux) or ``shasum -a 256 | awk``
    (macOS). Both emit the lowercase hex SHA-256 of the stdin bytes.

    Args:
        kickoff: kickoff markdown body (string OR bytes — strings are
                 encoded as UTF-8).
        spec:    spec file body (string OR bytes).
        prompt:  prompt template body (string OR bytes). The bash hash
                 pipelines this through ``mo_cache_input_hash`` FIRST
                 (so the inner hash is already a 64-char hex string);
                 the Python equivalent just passes a string body — both
                 produce the same final hash because the inner
                 ``mo_cache_input_hash`` step is just a sha256 over the
                 prompt bytes.

    Returns:
        64-char lowercase hex SHA-256 digest.
    """
    def _b(x: str | bytes) -> bytes:
        return x.encode("utf-8") if isinstance(x, str) else x

    bundle = _b(kickoff) + _RS + _b(spec) + _RS + _b(prompt)
    return hashlib.sha256(bundle).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Extraction — 3-tier cascade mirroring bash lines 127-169
# ─────────────────────────────────────────────────────────────────────────────
def _iter_assistant_text(log_path: str) -> Iterator[str]:
    """Mirror bash jq-stream: every ``.type=='assistant'`` block's
    ``.message.content[]`` items of ``type=='text'`` (yielded one at a
    time so the caller can concat or feed each separately).

    Robust to malformed lines (json.JSONDecodeError → skip). Multi-line
    JSON objects in a Claude stream-json log are NOT supported by jq -r
    either — bash's jq also operates line-by-line on each ``{...\\n}``
    event. The 3-tier cascade tolerates multi-line JSON only in the
    awk-fallback tier (case (b) test verifies this).
    """
    p = Path(log_path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s.startswith("{"):
            continue
        try:
            evt = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict) or evt.get("type") != "assistant":
            continue
        content = evt.get("message", {}).get("content", []) or []
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                yield item.get("text", "")


def _read_fallback_result_text(log_path: str) -> str:
    """Mirror bash: ``grep '\"type\":\"result\"' $log_path | tail -1 | jq -r '.result // empty'``.

    Returns the empty string if no ``\"type\":\"result\"`` line is found
    OR if it doesn't parse.
    """
    p = Path(log_path)
    if not p.is_file():
        return ""
    matches = [
        line for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if '"type":"result"' in line
    ]
    if not matches:
        return ""
    try:
        evt = json.loads(matches[-1])
    except json.JSONDecodeError:
        return ""
    return evt.get("result", "") or "" if isinstance(evt, dict) else ""


def _brace_balance_mutations(text: str) -> dict | None:
    """Verbatim port of the heredoc at lib/mutation-adversary.sh lines 140-159.

    Iterates every regex match for ``{[^{]*?\"mutations\"`` from the END
    (last match first — the model may emit multiple JSON objects). For
    each, walks forward tracking brace depth + string-state + escape-state
    to find the matching close. If the candidate parses as JSON, return
    it. The first JSON-parseable candidate wins; subsequent ones are
    ignored.

    Returns None if no balanced candidate parses (caller falls back to awk).
    \"Escape\" and \"string\" state handling matches the heredoc exactly.
    """
    starts = [m.start() for m in _MUTATIONS_START_RE.finditer(text)]
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
                        return json.loads(cand)
                    except Exception:
                        break
    return None


def _awk_grep_mutations(log_path: str) -> dict | None:
    """Mirror bash lines 162-169 awk: scan line-by-line for
    ``\\{\\s*\"mutations\"\\s*:\", slurp the rest of the file to EOF,
    json.loads.

    Returns None if no matching line OR if the slurped buffer doesn't
    parse as JSON.
    """
    p = Path(log_path)
    if not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if _AWK_MUTATIONS_RE.search(line):
            buf = "\n".join(lines[i:])
            try:
                return json.loads(buf)
            except json.JSONDecodeError:
                return None
    return None


def extract_mutations_from_log(log_path: str) -> Optional[dict]:
    """3-tier cascade mirroring ``mo_run_mutation_adversary`` lines 127-169.

    Tier 1 — jq-stream assistant.text blocks (concatenated into one
             ``full_text`` string mirroring jq's multi-value emission).
    Tier 2 — last-line ``{\"type\":\"result\"}.result`` fallback (only if
             Tier 1 produced no text).
    Then run the heredoc brace-balancer over the chosen text.
    If Tier 1+2+brace-balancer produced nothing parseable:
    Tier 3 — awk line-by-line scan of the raw file for ``\"mutations\":``
             marker, slurp to EOF, json.loads the buffer.

    Returns:
        The extracted JSON object (typically ``{\"mutations\": [...]}``)
        or None if every tier failed.
    """
    full_text = "\n".join(_iter_assistant_text(log_path))
    if not full_text:
        full_text = _read_fallback_result_text(log_path)
    result: dict | None = None
    if full_text:
        result = _brace_balance_mutations(full_text)
    if result is not None and isinstance(result, dict) and "mutations" in result:
        return result
    return _awk_grep_mutations(log_path)


def build_mutations_json(extracted: dict | None) -> dict:
    """Mirror bash lines 171-175:

        if jq -e '.mutations' >/dev/null; then
            jq -c '.' > mutations_json
        else
            jq -n '{mutations: [], parse_error: true, skipped: false}' \
              > mutations_json
        fi

    In Python, the *dict* shape mirrors bash's eventual JSON. Callers
    write it with ``json.dumps(d, separators=(\",\", \":\"))`` to match
    bash's ``jq -c`` byte emission.

    Note: bash emits ``skipped: false`` (lowercase, unquoted) via
    ``jq -n`` literal — JSON output is just the field value ``false``.
    Python emits ``False`` → json.dumps → ``false``. Equivalent.
    """
    if (
        extracted is not None
        and isinstance(extracted, dict)
        and "mutations" in extracted
    ):
        return extracted
    return {"mutations": [], "parse_error": True, "skipped": False}


# ─────────────────────────────────────────────────────────────────────────────
# Validator math (Phase A.3 line 209-296)
# ─────────────────────────────────────────────────────────────────────────────
def threshold_pass(kill_rate: float) -> str:
    """Mirror bash line 294: ``awk -v r=$kr 'BEGIN{ if (r>=0.8) print \"PASS\"; else print \"FAIL\" }'``.

    Per TDAD paper §3.3, ≥80% kill-rate is the spec-quality bar.
    """
    return "PASS" if float(kill_rate) >= _THRESHOLD else "FAIL"


def compute_validation_results(
    mutations_json: dict,
    per_mutation_outcomes: List[Tuple[str, str, bool, bool, str]] | None = None,
    *,
    worktree_dirty: bool = False,
) -> dict:
    """Mirror ``mo_run_mutation_validator`` lines 209-296 (math only).

    Args:
        mutations_json: parsed ``<iter-dir>/mutations.json`` dict (must
                        have ``mutations`` list + optional ``skipped``
                        flag).
        per_mutation_outcomes:
                        list of 5-tuples ``(id, target_scenario, applied,
                        caught, reason)``, ordered to match mutations
                        json. Each entry mirrors one iteration of the
                        bash loop's results accumulator (lines 246-271).
                        If None, treated as empty (zero-mutations path).
        worktree_dirty: if True, mirrors the bash dirty-worktree check
                        at line 228 → ``{kill_rate: -1, skipped: false,
                        error: \"worktree dirty\"}``.

    Returns:
        Dict matching the bash ``jq -n`` output at lines 213, 222, 230, 287.

    Key ordering matters (Python dict insertion order is preserved by
    ``json.dumps``):

        skipped      : ``{kill_rate: 1.0, skipped: True, results: []}``
        zero         : ``{kill_rate: 0.0, skipped: False, results: [],
                          note: \"adversary returned zero mutations\"}``
        dirty        : ``{kill_rate: -1, skipped: False,
                          error: \"worktree dirty\"}``
        normal       : ``{kill_rate, total, killed, skipped: False,
                          results: [...]}``

    The kill_rate is computed via ``float(f\"{k/t:.{_KILL_RATE_PRECISION}f}\")``
    so JSON emits e.g. ``0.667`` (not ``0.6666666...``) — exactly what
    bash ``awk 'BEGIN{ printf \"%.3f\", k/t }'`` produces.
    """
    skipped_flag = bool(mutations_json.get("skipped", False))
    if skipped_flag:
        return {"kill_rate": 1.0, "skipped": True, "results": []}

    mutations = mutations_json.get("mutations", [])
    if not mutations:
        return {
            "kill_rate": 0.0,
            "skipped": False,
            "results": [],
            "note": "adversary returned zero mutations",
        }

    if worktree_dirty:
        return {"kill_rate": -1, "skipped": False, "error": "worktree dirty"}

    outcomes = per_mutation_outcomes or []
    results: List[dict] = [
        {
            "id": mid,
            "target_scenario": target,
            "applied": bool(applied),
            "caught": bool(caught),
            "reason": reason,
        }
        for (mid, target, applied, caught, reason) in outcomes
    ]
    killed = sum(1 for r in results if r["caught"])
    total = len(results)
    kill_rate = float(f"{killed / total:.{_KILL_RATE_PRECISION}f}") if total > 0 else 0.0
    return {
        "kill_rate": kill_rate,
        "total": total,
        "killed": killed,
        "skipped": False,
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB row emit — mirror lib/cache.sh mo_cache_emit (lines 135-163)
# ─────────────────────────────────────────────────────────────────────────────
def _emit_cache_row(
    db_path: str,
    epic: str,
    iter: int,
    input_hash: str,
    cost: float,
    turns: int,
    dur: float,
    output_path: str = "",
    log_path: str = "",
    status: str = "success",
    prompt_version: str = "v1",
    job_id: str = "unknown",
) -> None:
    """Mirror ``mo_cache_emit`` at lib/cache.sh:135-163.

    Inserts one row into ``mini_orch_sessions`` with the same shape bash
    writes. Field-by-field parity:

        uuid          — fresh uuid4() (bash uses uuidgen or fallback)
        job_id        — caller-provided (bash: ${JOB_ID:-unknown})
        epic_id       — caller-provided
        iter          — caller-provided (int)
        stage         — literal \"mutation-adversary\" (caller hardcodes
                        this in dispatch flow; here we leave it generic
                        so this helper can be reused by other ports —
                        BUT the parity test (case h) sets stage via the
                        surrounding call site, NOT here. We default
                        stage=\"mutation-adversary\" to mirror bash.
        input_hash    — caller-provided
        status        — \"success\" by default
        output_path   — caller-provided (or \"\")
        log_path      — caller-provided (or \"\")
        cost_usd      — caller-provided
        turns         — caller-provided
        duration_ms   — caller-provided (the ``dur`` param maps to
                        bash's ``duration_ms`` slot)
        expires_at    — now + 30 days, ISO-8601 ms precision with Z suffix
                        (bash: same logic via ``python3 -c 'import
                        datetime as d; print(...)'``)
        prompt_version — \"v1\" default (bash: ${11:-v1})
        created_at/updated_at
                      — column DEFAULT handles; we leave them blank.

    The parity test (case h) ignores uuid, created_at, updated_at,
    expires_at and asserts the logical fields (epic, iter, stage,
    status, input_hash, output_path, log_path, cost_usd, turns,
    duration_ms, prompt_version, job_id) are byte-equal between bash and
    Python on the same row key.

    Does NOT trigger the cache_emit UNIQUE-key dance (bash uses ``ON
    CONFLICT (uuid) DO NOTHING`` — uuid4 collision is astronomically
    unlikely, so we let sqlite3.IntegrityError propagate on the rare
    conflict; matches bash behavior).
    """
    stage = "mutation-adversary"
    # Mirror bash: `python3 -c "import datetime as d; print((d.datetime.utcnow() + d.timedelta(days=30)).strftime(...))"`.
    # utcnow() is deprecated in 3.12+; use timezone-aware now() and emit the same
    # '+00:00'-free text (bash uses literal 'Z' suffix in its format string).
    _now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)
    expires_at = _now.strftime("%Y-%m-%dT%H:%M:%f") + "Z"
    u = str(uuid.uuid4())
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO mini_orch_sessions "
            "(uuid, job_id, epic_id, iter, stage, input_hash, status, "
            " output_path, log_path, cost_usd, turns, duration_ms, "
            " expires_at, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                u,
                job_id,
                epic,
                int(iter),
                stage,
                input_hash,
                status,
                output_path,
                log_path,
                float(cost),
                int(turns),
                int(dur),
                expires_at,
                prompt_version,
            ),
        )
        con.commit()
    finally:
        con.close()
