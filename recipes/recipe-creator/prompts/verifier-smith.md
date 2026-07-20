# Verifier Smith — generate task-specific bash verifiers

The arbiter wrote the chosen recipe draft under
`${MINI_ORK_RUN_DIR}/chosen/<derived_recipe_name>/`, with stub verifier
scripts. Your job: replace each stub with a real, deterministic,
exit-code-meaningful bash verifier that mechanizes the relevant
`verifier_contract.checks[]` entries from `plan.json`.

## Hybrid strategy (per user direction)

- **Template tier (always)** — every recipe's verifier ALWAYS includes
  these mechanical checks. You don't need an LLM to write them; just
  copy from the canonical template at the bottom of this prompt:
  - All declared artifacts exist and are non-empty
  - Each artifact passes a basic shape check (line count, regex anchor,
    JSON-parses for `.json`, YAML-parses for `.yaml`)
  - Evidence log written to `$RUN_DIR/verifier-<name>.log`
  - Outputs JSON to stdout: `{verifier, pass, evidence_path, ...}`
  - Always `exit 0` (caller reads `.pass` from JSON)
- **Task-specific tier (you write)** — assertions that come from the
  epic's domain. Examples:
  - "synthesis.md must cross-reference all 4 lens-*.md files"
  - "plan.json's risk_class must equal task_class.yaml's risk_class"
  - "every drafter rationale must mention ≥ 1 arxiv citation"

## Inputs (in your context)

- `${MINI_ORK_RUN_DIR}/chosen/recipe_name` — kebab-case slug
- `${MINI_ORK_RUN_DIR}/chosen/<derived_recipe_name>/` — the chosen draft
- `${MINI_ORK_RUN_DIR}/chosen-recipe.json` — arbiter's `next_steps_for_smith`
- `${MINI_ORK_RUN_DIR}/plan.json` — `verifier_contract.checks[]` (your spec)

## Output

For EACH stub at `chosen/<name>/verifiers/*.sh`:

1. Overwrite the file with a real verifier. Use the canonical template
   below. Keep file sizes ≤ 250 lines each.
2. `chmod +x` is set by the publisher; you only need to ensure the
   shebang `#!/usr/bin/env bash` is line 1.
3. `bash -n` must pass (the recipe_validator runs this).
4. The script's stdout must be a single JSON object on the last line.

Also write `${MINI_ORK_RUN_DIR}/chosen/verifier-smith.json`:

```json
{
  "smith": "codex",
  "verifiers_written": ["verifiers/<a>.sh", "verifiers/<b>.sh"],
  "template_checks_per_verifier": <int>,
  "task_specific_checks_per_verifier": <int>,
  "uncovered_checks": ["<check_id>"],
  "rationale": "<2-3 sentences>"
}
```

`uncovered_checks` = `verifier_contract.checks[]` entries you couldn't
mechanize because they're inherently behavioral (e.g. "synthesis is
high-quality"). Those become advisory rubric items, not hard gates.

## Hard constraints

- NEVER call out to network. NEVER call out to the LLM dispatcher. Verifiers
  are deterministic, hermetic, offline.
- NEVER `rm -rf` anything outside `$MINI_ORK_RUN_DIR`.
- ALWAYS quote paths in bash. Quote `"$MINI_ORK_RUN_DIR"`.
- ALWAYS set `set -uo pipefail` (NOT `set -e` — let individual failures
  be checked, not whole-script abort).
- `bash -n` must pass.
- No `<z-insight>` blocks.

## Canonical verifier template (paste + customize)

```bash
#!/usr/bin/env bash
# verifiers/<NAME>.sh — <one-line purpose>
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "<NAME>", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-<NAME>.log"
exec 3>"$EVIDENCE"

checks_run=()
failed_checks=()

_check() {
  local id="$1" expr_desc="$2" cond="$3"
  checks_run+=("$id")
  echo "  [$id] $expr_desc" >&3
  if eval "$cond" >&3 2>&1; then
    echo "    ok" >&3
  else
    echo "    FAIL" >&3
    failed_checks+=("$id")
  fi
}

# Template tier (mechanical) — always
_check "artifact-exists"      "<artifact>.md exists" \
       '[ -f "$RUN_DIR/<artifact>.md" ]'
_check "artifact-non-empty"   "<artifact>.md non-empty" \
       '[ -s "$RUN_DIR/<artifact>.md" ]'
_check "evidence-anchors"     "<artifact>.md cites file:line" \
       'grep -qE "[a-zA-Z_./-]+:[0-9]+" "$RUN_DIR/<artifact>.md"'

# Task-specific tier (smith writes per epic)
# _check "<task-id>" "<desc>" '<bash test>'

if [ "${#failed_checks[@]}" -eq 0 ]; then
  pass=true
else
  pass=false
fi

python3 - <<PY
import json
print(json.dumps({
  "verifier": "<NAME>",
  "pass": $pass,
  "evidence_path": "$EVIDENCE",
  "checks_run": "${checks_run[@]}".split(),
  "failed_checks": "${failed_checks[@]}".split(),
}))
PY

exit 0
```
