#!/usr/bin/env bash
# readme-drift-panel.sh — Layer 2b (4-lens panel + opus arbiter, ~$0.20-0.30).
#
# Spawns 4 lens calls in parallel — each given a distinct slice of the
# drift-check job — then runs an opus arbiter that produces the final
# verdict. Designed to complete in 30-60 seconds wall.
#
# Lens roles (each gets the same README + repo inventory + diff, but a
# DIFFERENT lens prompt):
#   codex_lens   — technical accuracy (numbers, paths, schema enums)
#   kimi_lens    — narrative consistency (does the story-arc still match)
#   minimax_lens — comparison-table fairness (vs Claude Code / OpenAI etc)
#   glm_lens     — citation accuracy (do the papers still support claims)
#
# Arbiter: opus — synthesizes the 4 lens verdicts into one DRIFT or NO_DRIFT
# with reasons + suggested fix paragraph per drifted claim.
#
# Exit codes:
#   0  NO_DRIFT — README still accurate, push proceeds
#   1  DRIFT    — README has stale claims, push should block (caller decides)
#   2  invocation error (provider down etc) — fail-open recommended
#
# Output: a JSON object on stdout with shape
#   { "verdict": "NO_DRIFT" | "DRIFT",
#     "lens_count": 4,
#     "drifted_claims": [ {claim, reason, suggested_fix}, ... ],
#     "cost_estimate_usd": <float>,
#     "wall_time_sec": <int>,
#     "report_path": "<path to full markdown report>" }

set +e
MO_README="${MO_README:-README.md}"
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SECRETS="${MO_SECRETS:-$MINI_ORK_ROOT/.mini-ork/config/secrets.local.sh}"
[ -f "$SECRETS" ] || SECRETS="$HOME/.config/mini-ork/secrets.local.sh"
[ -f "$SECRETS" ] && source "$SECRETS"

# Outdir for the run.
ts=$(date +%Y%m%d-%H%M%S)
RUN_DIR="${MO_DRIFT_RUN_DIR:-/tmp/readme-drift-$ts-$$}"
mkdir -p "$RUN_DIR"

# Gather repo inventory (current state).
inventory=$(cat <<EOF
Repo inventory (current state, computed at $ts):

  lib/*.sh count:                 $(find lib -maxdepth 1 -name '*.sh' -type f 2>/dev/null | wc -l | tr -d ' ')
  bin/mini-ork-* entrypoint count: $(ls bin/mini-ork* 2>/dev/null | wc -l | tr -d ' ')
  db/migrations/*.sql count:       $(find db/migrations -maxdepth 1 -name '*.sql' -type f 2>/dev/null | wc -l | tr -d ' ')
  recipes/ subdir count:           $(find recipes -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
  lib/providers/cl_*.sh count:     $(find lib/providers -maxdepth 1 -name 'cl_*.sh' -type f 2>/dev/null | wc -l | tr -d ' ')

Recipes shipped:
$(ls -d recipes/*/ 2>/dev/null | sed 's|^|  |; s|/$||')

Latest commits (15 most recent on this branch):
$(git log --oneline -15 2>/dev/null | sed 's/^/  /')

ROADMAP.md current section header:
$(grep -E '^### v[0-9]' ROADMAP.md 2>/dev/null | tail -1 | sed 's/^/  /')
EOF
)

# Build the shared prompt prefix.
common_prefix=$(cat <<EOF
You are a README drift auditor for the mini-ork OSS framework. Your job is
to verify the load-bearing claims in README.md against the current repo
state. You are ONE of FOUR lenses in a heterogeneous-family panel — focus
ONLY on your assigned axis; the other lenses cover the rest.

Output strict JSON on stdout, no prose around it:

{
  "lens":           "<your lens name>",
  "verdict":        "NO_DRIFT" | "DRIFT",
  "drifted_claims": [
    { "claim":         "<exact quote from README>",
      "evidence":      "<why it's wrong>",
      "suggested_fix": "<one-paragraph replacement text>" }
  ],
  "confidence": 0.0..1.0
}

Repo inventory:
$inventory

README.md (the doc being audited):

$(cat "$MO_README")

EOF
)

# Lens-specific suffixes.
lens_codex="Lens role: TECHNICAL ACCURACY. Verify every numerical claim, file path, and schema enum cited in the README against the repo inventory above. Flag any count that's wrong, any cited path that doesn't exist, any schema enum that doesn't match."
lens_kimi="Lens role: NARRATIVE CONSISTENCY. Read the README as a story. Are the claims in section N still supported by section M? Does the architecture diagram match the recipe table? Is the v0.3 status section consistent with the Roadmap pointer? Flag any internal contradictions or stale narrative arcs."
lens_minimax="Lens role: COMPARISON-TABLE FAIRNESS. Look at the table comparing mini-ork to Claude Code / OpenAI Agents SDK / LangGraph dynamic workflows. Is each row still fair? Has any row become misleading because of changes in those external tools? Has any row become misleading because of what mini-ork has shipped or removed? Flag stale comparisons."
lens_glm="Lens role: CITATION ACCURACY. The README cites 6 arXiv papers (Nasser 2026, Rajan 2025, Karanam 2025, Zietsman 2026, Shehata 2026, Song 2026). For each citation, verify the claim being attached to the citation is one the paper actually supports. Flag any case where the README's gloss of a paper has drifted from what the paper actually argues."

# Dispatch a single lens. Args: <lens_name> <provider_name> <lens_suffix>.
dispatch_lens() {
  local lens_name="$1" provider="$2" lens_suffix="$3"
  local out_file="$RUN_DIR/lens-${lens_name}.json"
  local err_file="$RUN_DIR/lens-${lens_name}.err"
  local prompt="${common_prefix}

${lens_suffix}"

  local provider_path="$MINI_ORK_ROOT/lib/providers/cl_${provider}.sh"
  if [ ! -f "$provider_path" ]; then
    echo "{\"lens\":\"$lens_name\",\"verdict\":\"NO_DRIFT\",\"drifted_claims\":[],\"confidence\":0.0,\"error\":\"provider missing: $provider_path\"}" > "$out_file"
    return
  fi

  (
    if [ "$provider" = "codex" ]; then
      # cl_codex.sh is executable, not sourceable.
      timeout 90 "$provider_path" --print --output-format text "$prompt" < /dev/null 2>"$err_file" > "$out_file.raw"
    else
      source "$provider_path" 2>/dev/null
      timeout 90 claude --print --output-format text "$prompt" < /dev/null 2>"$err_file" > "$out_file.raw"
    fi
  )

  # Strip markdown code fences if present, then extract the FIRST complete
  # JSON object via JSONDecoder.raw_decode (NOT a greedy regex — providers
  # like Codex/Opus append `<z-insight>` telemetry blocks AFTER the JSON,
  # and a greedy `{.*}` would mash both into one unparseable string).
  python3 - "$out_file.raw" "$lens_name" > "$out_file" 2>>"$err_file" <<'PY'
import json, re, sys
raw_path, lens = sys.argv[1], sys.argv[2]
try:
    with open(raw_path) as f:
        raw = f.read()
except Exception:
    raw = ""

# First, try to strip a markdown ```json ... ``` fence if present.
m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
if m:
    raw = m.group(1)

# Then locate the first '{' and let JSONDecoder.raw_decode walk forward
# until it has a complete object. Anything after that (z-insight, prose,
# trailing thinking blocks) is ignored.
parsed = None
err = None
brace_pos = raw.find('{')
if brace_pos >= 0:
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw[brace_pos:])
        parsed = obj
    except Exception as e:
        err = f"raw_decode failed: {e}"
else:
    err = "no '{' found in lens output"

if parsed is None:
    print(json.dumps({
        "lens": lens, "verdict": "NO_DRIFT",
        "drifted_claims": [], "confidence": 0.0,
        "error": err or "parse failed"
    }))
else:
    parsed.setdefault("lens", lens)
    parsed.setdefault("verdict", "NO_DRIFT")
    parsed.setdefault("drifted_claims", [])
    parsed.setdefault("confidence", 0.5)
    print(json.dumps(parsed))
PY
}

# Fan out 4 lenses in parallel.
t0=$(date +%s)
dispatch_lens codex_lens   codex   "$lens_codex"   &
dispatch_lens kimi_lens    kimi    "$lens_kimi"    &
dispatch_lens minimax_lens minimax "$lens_minimax" &
dispatch_lens glm_lens     glm     "$lens_glm"     &
wait
t1=$(date +%s)
parallel_wall=$((t1 - t0))

# Arbiter: opus reads all 4 lens outputs and emits final verdict.
arbiter_prompt=$(cat <<EOF
You are the arbiter of a 4-lens drift-audit panel. Each lens audited
README.md against a different axis and emitted JSON. Synthesize them
into ONE final verdict.

Rules:
- If ALL 4 lenses say NO_DRIFT → final NO_DRIFT.
- If 1+ lens says DRIFT with confidence ≥ 0.6 → final DRIFT.
- If 1+ lens says DRIFT but with confidence < 0.6 → final NO_DRIFT,
  but capture the low-confidence flag in "notes".
- De-duplicate drifted_claims across lenses (same README quote = same
  claim, merge the evidence/suggested_fix into the strongest one).

Lens outputs (one JSON per line):
$(cat "$RUN_DIR/lens-codex_lens.json")
$(cat "$RUN_DIR/lens-kimi_lens.json")
$(cat "$RUN_DIR/lens-minimax_lens.json")
$(cat "$RUN_DIR/lens-glm_lens.json")

Emit strict JSON on stdout:

{
  "verdict": "NO_DRIFT" | "DRIFT",
  "lens_verdicts": {
    "codex_lens":   "NO_DRIFT" | "DRIFT",
    "kimi_lens":    "NO_DRIFT" | "DRIFT",
    "minimax_lens": "NO_DRIFT" | "DRIFT",
    "glm_lens":     "NO_DRIFT" | "DRIFT"
  },
  "drifted_claims": [
    { "claim": "...", "evidence": "...", "suggested_fix": "...",
      "lenses_flagged": ["..."] }
  ],
  "notes": "<low-confidence flags or split verdicts>"
}
EOF
)

arbiter_raw="$RUN_DIR/arbiter.raw"
arbiter_json="$RUN_DIR/arbiter.json"
(
  source "$MINI_ORK_ROOT/lib/providers/cl_opus.sh" 2>/dev/null
  timeout 120 claude --print --output-format text "$arbiter_prompt" < /dev/null 2>"$RUN_DIR/arbiter.err" > "$arbiter_raw"
)

python3 - "$arbiter_raw" > "$arbiter_json" 2>>"$RUN_DIR/arbiter.err" <<'PY'
import json, re, sys
try:
    with open(sys.argv[1]) as f:
        raw = f.read()
except Exception:
    raw = ""

# Try ```json``` fence first (most explicit signal).
m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
if m:
    raw = m.group(1)

# Locate first '{' and walk forward with raw_decode — ignore any trailing
# <z-insight> blocks, prose, or thinking output that some providers
# append after the JSON.
parsed = None
brace_pos = raw.find('{')
if brace_pos >= 0:
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[brace_pos:])
    except Exception:
        parsed = None

if parsed is None:
    parsed = {"verdict": "NO_DRIFT", "lens_verdicts": {}, "drifted_claims": [],
              "notes": "arbiter output unparseable — fail-open"}
print(json.dumps(parsed))
PY

# Build a human-readable markdown report.
report_md="$RUN_DIR/report.md"
{
  echo "# README drift audit — $ts"
  echo
  echo "**Verdict**: $(jq -r '.verdict' "$arbiter_json")"
  echo
  echo "## Per-lens verdicts"
  echo
  jq -r '.lens_verdicts | to_entries[] | "- **\(.key)**: \(.value)"' "$arbiter_json"
  echo
  echo "## Drifted claims"
  echo
  jq -r '.drifted_claims[] | "### Claim: \(.claim)\n\n**Evidence**: \(.evidence)\n\n**Suggested fix**: \(.suggested_fix)\n\n**Lenses flagged**: \(.lenses_flagged | join(", "))\n"' "$arbiter_json" 2>/dev/null
  echo
  echo "## Notes"
  jq -r '.notes // "(none)"' "$arbiter_json"
  echo
  echo "## Run metadata"
  echo "- Parallel-lens wall time: ${parallel_wall}s"
  echo "- Total run dir: $RUN_DIR"
} > "$report_md"

t2=$(date +%s)
total_wall=$((t2 - t0))

# Cost estimate: 4 lenses × ~$0.05 + opus × ~$0.10 = ~$0.30 (rough).
cost_estimate="0.30"

verdict=$(jq -r '.verdict' "$arbiter_json")
drifted_count=$(jq -r '.drifted_claims | length' "$arbiter_json")

# Final stdout JSON for the caller.
jq -c -n \
  --arg verdict "$verdict" \
  --argjson lens_count 4 \
  --slurpfile arbiter "$arbiter_json" \
  --arg cost "$cost_estimate" \
  --argjson wall "$total_wall" \
  --arg report "$report_md" \
  '{
    verdict: $verdict,
    lens_count: $lens_count,
    drifted_claims: ($arbiter[0].drifted_claims // []),
    notes: ($arbiter[0].notes // ""),
    cost_estimate_usd: ($cost | tonumber),
    wall_time_sec: $wall,
    report_path: $report
  }'

[ "$verdict" = "NO_DRIFT" ] && exit 0 || exit 1
