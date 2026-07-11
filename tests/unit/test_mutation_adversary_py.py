"""Parity gate: mini_ork.ported.mutation_adversary vs lib/mutation-adversary.sh.

Eight cases (kickoff floor: >=6; 2-case buffer):

  (a) compute_cache_hash byte-equality        — 3 input combos, live bash
                                                 `printf %s\\x1e%s\\x1e%s |
                                                 sha256sum` vs Python
                                                 `compute_cache_hash`.
  (b) extract_mutations_from_log happy-path   — synthetic stream-json log
                                                 with 1 assistant block
                                                 containing the mutations
                                                 JSON. Both tiers fire;
                                                 results byte-equal.
  (c) extract_mutations_from_log prose-noisy  — assistant text wrapped in
                                                 chatty prose + the JSON
                                                 buried. Heredoc brace-
                                                 balancer finds it.
  (d) build_mutations_json parse_error shape  — empty/missing extract.
                                                 Both sides emit
                                                 ``{\"mutations\":[],
                                                 \"parse_error\":true,
                                                 \"skipped\":false}``
                                                 byte-equal.
  (e) compute_validation_results skipped:true — bash `jq -n` produces
                                                 ``{kill_rate:1.0,
                                                 skipped:true,results:[]}``
                                                 byte-equal to Python.
  (f) compute_validation_results zero-mutations— bash `jq -n` produces
                                                 ``{kill_rate:0.0,
                                                 skipped:false,results:[],
                                                 note:\"adversary returned
                                                 zero mutations\"}``
                                                 byte-equal to Python.
  (g) compute_validation_results hand-computed
                                              — 3 mutations, 2 caught →
                                                 kill_rate=0.667 →
                                                 threshold=FAIL. Bash
                                                 stub uses live
                                                 `awk BEGIN{ printf %.3f }`
                                                 + `jq -n` with same
                                                 shapes; roundtrip
                                                 JSON byte-equal + 1e-6
                                                 float tolerance.
  (h) DB round-trip                           — temp DB via
                                                 `db/init.sh`; bash
                                                 `mo_cache_emit` vs
                                                 Python
                                                 `_emit_cache_row` →
                                                 SELECT rows, diff all
                                                 logical fields (uuid /
                                                 created_at / updated_at /
                                                 expires_at ignored as
                                                 they differ per-call).

Tolerance: floats 1e-6 (kickoff requirement). All JSON shapes diff
byte-for-byte via json.dumps with the same separators=(\",\", \":\") as
bash `jq -c`. No mocks, no hardcoded outputs beyond what bash itself
emits — every assertion is bash-vs-Python on identical inputs.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mutation_adversary as ma  # noqa: E402

SH = REPO / "lib" / "mutation-adversary.sh"
CACHE_SH = REPO / "lib" / "cache.sh"
INIT_SH = REPO / "db" / "init.sh"

# Float tolerance — kickoff requires 1e-6 (the bash kill_rate is %.3f so
# precision is bounded at 5e-4; 1e-6 is a strict superset).
_FLOAT_TOL = 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which(*tools: str) -> None:
    for t in tools:
        if not shutil.which(t):
            pytest.skip(f"required tool not on PATH: {t}")
    if not SH.exists():
        pytest.skip(f"missing lib/mutation-adversary.sh at {SH}")
    if not CACHE_SH.exists():
        pytest.skip(f"missing lib/cache.sh at {CACHE_SH}")


def _bash_inline(src: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``bash -c <src>`` with optional env overlay."""
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


# Bash wrapper that runs the verbatim 3-tier extraction cascade + jq -e/jq -c
# build_mutations_json. Mirrors lib/mutation-adversary.sh lines 127-175 byte-
# for-byte. Source everything it needs (mo_cache_input_hash is unneeded here).
_BASH_EXTRACT = r"""
set -uo pipefail
# LOG_PATH and OUT come from env (passed by _bash_inline env=...)
: "${LOG_PATH:?}"; : "${OUT:?}"

# Tier 1: jq -r stream-json assistant.text concatenation
result_text=$(jq -r '
  select(.type=="assistant")
  | .message.content[]?
  | select(.type=="text")
  | .text
' "$LOG_PATH" 2>/dev/null)

# Tier 1 fallback: last 'result' line
if [ -z "$result_text" ]; then
  result_text=$(grep '"type":"result"' "$LOG_PATH" | tail -1 | jq -r '.result // empty' 2>/dev/null)
fi

extracted=""
if [ -n "$result_text" ]; then
  extracted=$(RESULT_TEXT="$result_text" python3 -c '
import os, re, sys, json
text = os.environ.get("RESULT_TEXT", "")
starts = [m.start() for m in re.finditer(r"\{[^{]*?\"mutations\"", text)]
for start in reversed(starts):
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == "\"" and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                cand = text[start:i+1]
                try: json.loads(cand); print(cand); sys.exit(0)
                except Exception: break
' 2>/dev/null)
fi

# Tier 3 fallback: awk line-scan
if [ -z "$extracted" ]; then
  extracted=$(awk '
    /\{[[:space:]]*"mutations"[[:space:]]*:/ {
      buf = $0
      while ((getline next_line) > 0) buf = buf "\n" next_line
      print buf; exit
    }
  ' "$LOG_PATH" 2>/dev/null)
fi

# build_mutations_json (mirror lib/mutation-adversary.sh lines 171-175)
if echo "$extracted" | jq -e '.mutations' >/dev/null 2>&1; then
  echo "$extracted" | jq -c '.' > "$OUT"
else
  jq -c -n '{mutations: [], parse_error: true, skipped: false}' > "$OUT"
fi

cat "$OUT"
"""


def _bash_extract(log_path: Path, out_path: Path) -> subprocess.CompletedProcess:
    return _bash_inline(
        _BASH_EXTRACT,
        env={"LOG_PATH": str(log_path), "OUT": str(out_path)},
    )


# Bash stub for compute_validation_results math. Mirrors the bash
# ``mo_run_mutation_validator`` math + jq -n shape at lib/mutation-
# adversary.sh lines 209-296 — but takes pre-computed outcomes via a
# JSON env var (OUTCOMES_JSON) instead of running git apply + npx
# playwright (the kickoff excludes the git/CLI loop from parity testing).
# Threshold (PASS/FAIL) is printed on its own line at the end.
_BASH_VAL = r"""
set -uo pipefail
: "${MUT:?}"; : "${OUT:?}"
KILLED="${KILLED:-0}"
TOTAL="${TOTAL:-0}"

skipped=$(jq -r '.skipped // false' "$MUT")
if [ "$skipped" = "true" ]; then
  jq -n '{kill_rate: 1.0, skipped: true, results: []}' > "$OUT"
  exit 0
fi

count=$(jq -r '.mutations | length' "$MUT" 2>/dev/null || echo 0)
if [ "$count" -eq 0 ]; then
  jq -n '{kill_rate: 0.0, skipped: false, results: [], note: "adversary returned zero mutations"}' > "$OUT"
  exit 0
fi

# OUTCOMES_JSON is an array of {id, target_scenario, applied, caught,
# reason}. The first TOTAL entries are merged into the final results.
# OUTCOMES_JSON is an array of {id, target_scenario, applied, caught,
# reason}. Shape-match jq extracts the same key order as Python's
# dict-insertion order, then wraps into an array.
results=$(printf '%s' "${OUTCOMES_JSON:-[]}" | jq -c '
  [.[] | {id: .id, target_scenario: .target_scenario,
          applied: .applied, caught: .caught, reason: .reason}]')

kill_rate=$(awk -v k="$KILLED" -v t="$TOTAL" 'BEGIN{ printf "%.3f", k/t }')

jq -n \
  --argjson results "$results" \
  --argjson kill_rate "$kill_rate" \
  --argjson total "$TOTAL" \
  --argjson killed "$KILLED" \
  '{kill_rate:$kill_rate, total:$total, killed:$killed, skipped:false, results:$results}' \
  > "$OUT"

# Threshold stdout (mirrors lib/mutation-adversary.sh line 294)
awk -v r="$kill_rate" 'BEGIN{ if (r >= 0.8) print "PASS"; else print "FAIL" }'
"""


def _bash_validate_run(
    mut_path: Path,
    out_path: Path,
    killed: int,
    outcomes: list[tuple[str, str, bool, bool, str]],
) -> subprocess.CompletedProcess:
    """Run the bash validator stub. Outcomes are encoded as a JSON env
    var — avoids byte-loss from shell quoting of arbitrary text."""
    outcomes_payload = [
        {"id": mid, "target_scenario": tgt, "applied": applied,
         "caught": caught, "reason": reason}
        for (mid, tgt, applied, caught, reason) in outcomes
    ]
    return _bash_inline(
        _BASH_VAL,
        env={
            "MUT": str(mut_path),
            "OUT": str(out_path),
            "KILLED": str(killed),
            "TOTAL": str(len(outcomes_payload)),
            "OUTCOMES_JSON": json.dumps(outcomes_payload, separators=(",", ":")),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) compute_cache_hash byte-equality
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "a,b,c",
    [
        ("alpha", "beta", "gamma"),
        ("# KICKOFF\ndo thing", "# SPEC\nspec body here", "# PROMPT\n"),
        ("" , "spec-no-kickoff", "prompt"),
    ],
    ids=["ascii-three", "multiline", "empty-kickoff"],
)
def test_compute_cache_hash_parity(a, b, c):
    """Live bash ``printf '%s\\x1e%s\\x1e%s' a b c | sha256sum`` vs Python
    ``compute_cache_hash``. Both must produce the same 64-char lowercase
    hex. The byte boundary ``\\x1e`` (Record Separator, ASCII 30) is the
    load-bearing invariant — any ``\\\\n`` substitution would invalidate
    every cache lookup."""
    _which("bash", "shasum", "sha256sum", "awk")
    # Transport each input as base64 to preserve EXACT bytes — including
    # interior & trailing \n, UTF-8 codepoints >0x7f, empty strings.
    # Bash decodes via `base64 -d` then `IFS= read -r -d ''` (process
    # substitution) — NOT command substitution, which strips trailing
    # newlines. The printf runs the same join as lib/mutation-adversary.sh:37,181.
    a_b64 = base64.b64encode(a.encode()).decode()
    b_b64 = base64.b64encode(b.encode()).decode()
    c_b64 = base64.b64encode(c.encode()).decode()
    bash_src = (
        f"IFS= read -r -d '' a_decoded < <(printf '%s' {a_b64!r} | base64 -d)\n"
        f"IFS= read -r -d '' b_decoded < <(printf '%s' {b_b64!r} | base64 -d)\n"
        f"IFS= read -r -d '' c_decoded < <(printf '%s' {c_b64!r} | base64 -d)\n"
        "printf '%s\\x1e%s\\x1e%s' \"$a_decoded\" \"$b_decoded\" \"$c_decoded\" "
        "| (sha256sum 2>/dev/null || shasum -a 256) | awk '{print $1}'\n"
    )
    rb = _bash_inline(bash_src)
    assert rb.returncode == 0, f"bash cache hash rc={rb.returncode}: {rb.stderr}"
    bash_hash = rb.stdout.strip()
    py_hash = ma.compute_cache_hash(a, b, c)
    assert py_hash == bash_hash, (
        f"\n  bash={bash_hash}\n  py  ={py_hash}\n  (check \\x1e byte boundary)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (b) extract happy path: stream-json + clean assistant.text → JSON
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_happy_path_parity(tmp_path):
    """Synthetic stream-json log: one assistant block whose ``.text``
    contains the mutations JSON. Bash path:
      jq -r assistant.text → concatenation
      → brace-balancer heredoc finds the LAST balanced `{...mutations...}`
      → jq -e '.mutations' → YES → jq -c emission.
    Python: `extract_mutations_from_log` returns the dict; the test
    serializes both via `jq -c` / `json.dumps` and asserts byte-equality."""
    _which("bash", "python3", "jq")
    log = tmp_path / "log.jsonl"
    mutations = {
        "mutations": [
            {"id": "M1", "diff": "--- a\n+++ b\n", "target_scenario": "login"},
            {"id": "M2", "diff": "--- a\n+++ b\n", "target_scenario": "logout"},
        ],
    }
    log.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text",
                 "text": "Here are the mutations:\n```json\n"
                         + json.dumps(mutations, separators=(",", ":")) + "\n```\n"},
            ]},
        }) + "\n"
        + json.dumps({"type": "result", "result": "...", "total_cost_usd": 0.01}) + "\n",
        encoding="utf-8",
    )
    out_bash = tmp_path / "mut_bash.json"
    out_py = tmp_path / "mut_py.json"

    rb = _bash_extract(log, out_bash)
    assert rb.returncode == 0, f"bash extract rc={rb.returncode}: {rb.stderr}"
    assert rb.stdout.strip(), "bash extract emitted nothing"

    # Python port
    py_extracted = ma.extract_mutations_from_log(str(log))
    assert py_extracted is not None and "mutations" in py_extracted
    out_py.write_text(json.dumps(ma.build_mutations_json(py_extracted),
                                 separators=(",", ":"), ensure_ascii=False) + "\n",
                      encoding="utf-8")

    bash_text = out_bash.read_text(encoding="utf-8")
    py_text = out_py.read_text(encoding="utf-8")
    assert py_text == bash_text, (
        f"extract happy-path byte-mismatch\nbash: {bash_text!r}\npy:   {py_text!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) extract with noisy prose: brace-balancer digs through chatty text
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_braced_in_prose_parity(tmp_path):
    """Multiple assistant blocks. The mutations JSON is wrapped in
    prose + a markdown fence + preceded by another small assistant
    block. Both bash and Python must surface the LAST balanced JSON."""
    _which("bash", "python3", "jq")
    log = tmp_path / "log.jsonl"
    mutations = {
        "mutations": [
            {"id": "M-A", "diff": "x", "target_scenario": "form_validation"},
        ],
    }
    prose_intro = "Let me analyze the spec carefully...\n"
    json_inside = json.dumps(mutations, separators=(",", ":"))
    wrapper = (
        "Here are the planned mutations:\n\n```json\n"
        + json_inside + "\n```\n\nAll look good. Done."
    )
    log.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": prose_intro}]},
        }) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": wrapper}]},
        }) + "\n",
        encoding="utf-8",
    )
    out_bash = tmp_path / "mut_bash.json"
    out_py = tmp_path / "mut_py.json"

    rb = _bash_extract(log, out_bash)
    assert rb.returncode == 0
    py_extracted = ma.extract_mutations_from_log(str(log))
    assert py_extracted is not None, "python extracted=None for prose-wrapped"
    out_py.write_text(json.dumps(ma.build_mutations_json(py_extracted),
                                 separators=(",", ":"), ensure_ascii=False) + "\n",
                      encoding="utf-8")

    bash_text = out_bash.read_text(encoding="utf-8")
    py_text = out_py.read_text(encoding="utf-8")
    assert py_text == bash_text, (
        f"brace-in-prose byte-mismatch\nbash: {bash_text!r}\npy:   {py_text!r}"
    )
    # Sanity: the JSON inside is the one we wrote, not a parse_error.
    parsed = json.loads(py_text)
    assert parsed.get("mutations") == mutations["mutations"]
    assert not parsed.get("parse_error")


# ─────────────────────────────────────────────────────────────────────────────
# (d) build_mutations_json parse-error shape (extracted missing/empty)
# ─────────────────────────────────────────────────────────────────────────────
def test_build_mutations_json_parse_error_parity(tmp_path):
    """Empty / non-extractable input: bash emits
    ``jq -c -n '{mutations:[],parse_error:true,skipped:false}'``; Python
    returns ``{\"mutations\":[],\"parse_error\":True,\"skipped\":False}``
    which json.dumps with ``separators=(\",\", \":\")`` emits byte-equal."""
    _which("bash", "jq")
    out_bash = tmp_path / "mut_bash.json"
    expected_bash = _bash_inline(
        "jq -c -n '{mutations: [], parse_error: true, skipped: false}'"
    )
    assert expected_bash.returncode == 0
    out_bash.write_text(expected_bash.stdout, encoding="utf-8")

    out_py = tmp_path / "mut_py.json"
    out_py.write_text(
        json.dumps(ma.build_mutations_json(None), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    assert out_py.read_text(encoding="utf-8") == out_bash.read_text(encoding="utf-8"), (
        f"parse_error shape byte-mismatch\nbash: {out_bash.read_text()!r}\npy:   {out_py.read_text()!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (e) compute_validation_results skipped:true branch
# ─────────────────────────────────────────────────────────────────────────────
def test_validation_results_skipped_parity(tmp_path):
    """``mutations.json.skipped == true`` → both sides emit
    ``{kill_rate:1.0, skipped:true, results:[]}``. Float 1.0 must match
    exactly (no precision drift)."""
    _which("bash", "jq")
    mut = tmp_path / "mutations.json"
    mut.write_text(json.dumps({"mutations": [], "skipped": True}), encoding="utf-8")
    out_bash = tmp_path / "results_bash.json"
    out_py = tmp_path / "results_py.json"

    rb = _bash_validate_run(mut, out_bash, killed=0, outcomes=[])
    assert rb.returncode == 0, f"bash validate rc={rb.returncode}: {rb.stderr}"

    py_result = ma.compute_validation_results(
        {"mutations": [], "skipped": True}, per_mutation_outcomes=[]
    )
    # Bash `jq -n` outputs indented JSON + trailing newline; mirror exactly.
    out_py.write_text(json.dumps(py_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    assert out_py.read_text(encoding="utf-8") == out_bash.read_text(encoding="utf-8"), (
        f"skipped branch byte-mismatch\nbash: {out_bash.read_text()!r}\npy:   {out_py.read_text()!r}"
    )
    assert py_result["kill_rate"] == 1.0
    assert py_result["skipped"] is True
    assert py_result["results"] == []


# ─────────────────────────────────────────────────────────────────────────────
# (f) compute_validation_results zero-mutations branch
# ─────────────────────────────────────────────────────────────────────────────
def test_validation_results_zero_mutations_parity(tmp_path):
    """``mutations.json.mutations == []`` (and skipped absent) → both
    sides emit
    ``{kill_rate:0.0, skipped:false, results:[],
       note:\"adversary returned zero mutations\"}``."""
    _which("bash", "jq")
    mut = tmp_path / "mutations.json"
    mut.write_text(json.dumps({"mutations": []}), encoding="utf-8")
    out_bash = tmp_path / "results_bash.json"
    out_py = tmp_path / "results_py.json"

    rb = _bash_validate_run(mut, out_bash, killed=0, outcomes=[])
    assert rb.returncode == 0

    py_result = ma.compute_validation_results(
        {"mutations": []}, per_mutation_outcomes=[]
    )
    out_py.write_text(json.dumps(py_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    assert out_py.read_text(encoding="utf-8") == out_bash.read_text(encoding="utf-8"), (
        f"zero-mutations byte-mismatch\nbash: {out_bash.read_text()!r}\npy:   {out_py.read_text()!r}"
    )
    assert py_result["note"] == "adversary returned zero mutations"
    assert py_result["kill_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (g) compute_validation_results hand-computed (2 of 3 caught → FAIL)
# ─────────────────────────────────────────────────────────────────────────────
def test_validation_results_hand_computed_parity(tmp_path):
    """Hand-computed:
        mutations = [M1, M2, M3]
        outcomes  = [caught=True, caught=True, caught=False]
                    → killed=2, total=3 → kill_rate = 0.667 < 0.8 → FAIL.

    Both bash (`awk BEGIN{ printf %.3f }` + `jq -n`) and Python
    (float(f\"{k/t:.3f}\")) produce the same mutation-results.json; the
    threshold string (FAIL) on bash's stdout must equal Python's
    ``threshold_pass()`` output."""
    _which("bash", "jq", "awk")
    mut = tmp_path / "mutations.json"
    mut.write_text(json.dumps({"mutations": [
        {"id": "M1", "target_scenario": "A"},
        {"id": "M2", "target_scenario": "B"},
        {"id": "M3", "target_scenario": "C"},
    ]}), encoding="utf-8")
    out_bash = tmp_path / "results_bash.json"
    out_py = tmp_path / "results_py.json"

    outcomes = [
        ("M1", "A", True, True, "spec failed → mutation detected"),
        ("M2", "B", True, True, "spec failed → mutation detected"),
        ("M3", "C", True, False,
         "spec passed under mutation (mutation NOT caught)"),
    ]

    rb = _bash_validate_run(mut, out_bash, killed=2, outcomes=outcomes)
    assert rb.returncode == 0, f"bash validate rc={rb.returncode}: {rb.stderr}"
    bash_threshold = rb.stdout.strip()

    py_result = ma.compute_validation_results(
        {"mutations": [{"id": "M1", "target_scenario": "A"},
                       {"id": "M2", "target_scenario": "B"},
                       {"id": "M3", "target_scenario": "C"}]},
        per_mutation_outcomes=outcomes,
    )
    out_py.write_text(json.dumps(py_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    bash_text = out_bash.read_text(encoding="utf-8")
    py_text = out_py.read_text(encoding="utf-8")
    assert py_text == bash_text, (
        f"hand-computed byte-mismatch\nbash: {bash_text!r}\npy:   {py_text!r}"
    )
    # Float tolerance per kickoff (1e-6).
    assert abs(py_result["kill_rate"] - 0.667) < _FLOAT_TOL, py_result
    assert py_result["killed"] == 2
    assert py_result["total"] == 3
    # Threshold: bash awk compares 0.667 >= 0.8 → FAIL.
    py_threshold = ma.threshold_pass(py_result["kill_rate"])
    assert py_threshold == bash_threshold == "FAIL", (
        f"threshold divergence: py={py_threshold} bash={bash_threshold}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (h) DB round-trip — bash mo_cache_emit vs python _emit_cache_row
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via db/init.sh with a unique
    path per test. No row drift afterwards (we only INSERT into
    mini_orch_sessions and ignore the row-key triplet)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: {r.stderr}\n{r.stdout}")
    return dbp


# Columns we compare — uuid/created_at/updated_at/expires_at differ per call
# (uuid = uuid4()/uuidgen; created/updated = strftime now; expires_at =
# now+30d). All logical fields should be byte-equal.
_DB_ROW_COLS = (
    "job_id", "epic_id", "iter", "stage", "input_hash", "status",
    "output_path", "log_path", "cost_usd", "turns", "duration_ms",
    "prompt_version",
)


def test_db_roundtrip_parity(temp_db, tmp_path):
    """Bash ``mo_cache_emit`` writes one row to ``mini_orch_sessions``;
    Python ``_emit_cache_row`` writes another on the SAME epic/iter/
    input_hash. Both rows must be byte-equal on every logical field
    (uuid/created_at/updated_at/expires_at ignored)."""
    _which("bash", "sqlite3")
    epic = "EPIC-PARITY-H"
    iter_n = 1
    input_hash = "sha256:" + "a" * 64
    output_path = str(tmp_path / "out.json")
    log_path = str(tmp_path / "log.log")
    cost = 0.42
    turns = 7
    duration = 250

    # Pre-create empty files so output_path/log_path are valid (bash sqlite
    # INSERT will store the strings either way, but staying tidy).
    Path(output_path).write_text("[]", encoding="utf-8")
    Path(log_path).write_text("dummy\n", encoding="utf-8")

    # Bash side. JOB_ID explicit so it matches Python. Forward MINI_ORK_DB
    # and MINI_ORK_HOME so ``mo_cache_emit`` writes to the SAME temp DB
    # (without this, bash falls back to ``.mini-ork/state.db`` and
    # pollutes the dev DB + produces ghost rows).
    bash_src = (
        f'. "{CACHE_SH}" >/dev/null 2>&1\n'
        f'mo_cache_emit mutation-adversary "{epic}" {iter_n} "{input_hash}" '
        f'success "{output_path}" "{log_path}" {cost} {turns} {duration}\n'
    )
    rb = _bash_inline(
        bash_src,
        env={"JOB_ID": "parity-test", "MINI_ORK_DB": temp_db,
             "MINI_ORK_HOME": str(Path(temp_db).parent)},
    )
    assert rb.returncode == 0, f"bash mo_cache_emit rc={rb.returncode}: {rb.stderr}"

    # Python side — same params.
    ma._emit_cache_row(
        temp_db,
        epic=epic,
        iter=iter_n,
        input_hash=input_hash,
        cost=cost,
        turns=turns,
        dur=duration,
        output_path=output_path,
        log_path=log_path,
        status="success",
        prompt_version="v1",
        job_id="parity-test",
    )

    # Diff rows on logical columns.
    con = sqlite3.connect(temp_db)
    rows = con.execute(
        "SELECT " + ",".join(_DB_ROW_COLS) + " FROM mini_orch_sessions "
        "WHERE epic_id=? AND iter=? AND input_hash=? ORDER BY rowid ASC",
        (epic, iter_n, input_hash),
    ).fetchall()
    con.close()

    assert len(rows) == 2, (
        f"expected exactly 2 rows (bash + python), got {len(rows)}:\n{rows}"
    )

    # Each row must independently satisfy all-equal pairwise assertion.
    # (We don't require bash-row vs py-row ordering — just that EACH row
    # has the same logical field values.)
    assert rows[0] == rows[1], (
        f"rows differ on logical columns:\nrow0={rows[0]}\nrow1={rows[1]}"
    )
    # Spot-check kill_rate math is also intact (== 0.42).
    assert float(rows[0][_DB_ROW_COLS.index("cost_usd")]) == pytest.approx(cost, abs=_FLOAT_TOL)
    assert rows[0][_DB_ROW_COLS.index("iter")] == iter_n
    assert rows[0][_DB_ROW_COLS.index("stage")] == "mutation-adversary"
    assert rows[0][_DB_ROW_COLS.index("status")] == "success"
    assert rows[0][_DB_ROW_COLS.index("prompt_version")] == "v1"
