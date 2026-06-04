# Mini-Orch Hardening — Preflight Gate + Secrets/Syntax Robustness

**Date:** 2026-06-02
**Origin:** Downstream observation in `SourceShift/libwit` (`.agentflow/mini-orch/`)
**Author:** claude-autopilot
**Status:** Spec — to be applied by mini-ork's own agents

> This doc is a FIX-SPEC, not a patch. It names the failure modes observed
> in a single intensive day of mini-orch dispatching downstream, the
> structural fix for each, and the file:line references the upstream's
> agents should target. Apply via your own mini-orch dispatch — dogfood the
> tool while hardening it.

## Why this exists

A 2026-06-02 downstream session (libwit-autopilot) lost ~3-4 hours and
~$15-20 of LLM budget to 11 distinct dispatch failures — none catastrophic
individually, but cumulatively eating most of a productive day. Bucket
analysis: **9 design / 2 spec**. The pattern is design fragility, not
bad specs. The 5-layer dispatch chain has too many silent-failure
windows; each layer has its own implicit contract and a failure at any
layer aborts the whole pipeline late.

## The 11 failure modes observed

| # | Failure | Hit when |
|---|---|---|
| 1 | `_worker-launcher.sh` case-statement missing `codex|gemini` resolver | cca-a-impl iter-1, 2026-06-02 09:16 |
| 2 | cl_codex.sh sourced (not exec'd) by launcher → "no prompt provided" rc=1 | cca-a-impl iter-1 (rc=99) |
| 3 | cl_codex.sh default `CODEX_MODEL=gpt-5.2` rejected by ChatGPT-account API | cca-a-impl iter-2 (400 invalid_request) |
| 4 | `lib/decomposer.sh` subshell sources `cl_glm.sh` without first sourcing `secrets.local.sh` → `GLM_API_KEY required` | SELF-EVOLVING-P0-P1-WAVE decompose step (rc=4) |
| 5 | `lib/rubric-prescreen.sh` has the same twin bug as #4 → `KIMI_API_KEY required` | every dispatch's rubric pre-screen this session |
| 6 | `run.sh:743` bash syntax error (`syntax error near unexpected token 'fi'`) crashed orch post-APPROVE | cca-a-impl + self-evolving-audit dispatches, blocked auto-merge |
| 7 | Auto-merge race: state.db status='in review' when orch loop closes → auto-merge skips with "no approved epics to merge" | cca-a-impl APPROVE + SELF-EVOLVING-AUDIT APPROVE both required manual squash-merge |
| 8 | `scaffold-from-decompose.sh` appends a duplicate `<epic_id>:` entry to agents.yaml even when a manual entry exists → YAML last-key-wins shadows the manual entry (default `worker: glm` overrides explicit `worker: codex`) | SELF-EVOLVING-AUDIT, cca-impl re-dispatch |
| 9 | Kickoff missing `**Branch:** \`feat/<slug>\`` line → `run.sh` exits with `ERROR: no branch in kickoff` at Step 4 | SELF-EVOLVING-AUDIT first dispatch attempt |
| 10 | `kickoff-path-lint.sh` requires inline `(new file)` marker on the SAME line as each new-file path; code-fence "NEW files:" listing fails | P2-19, SELF-EVOLVING-AUDIT (twice) |
| 11 | `dod-probe-lint.sh` slices DoD section from `## DoD` heading to first `###` — probes inside `### DoD-N` subsections are EXCLUDED from the slice | P2-19 second dispatch (zero probes detected despite 8 DoD subsections with grep commands) |

## The structural fix — `preflight-gate.sh`

Add a single dispatch-readiness probe that runs ALL known failure-mode
checks BEFORE the orch loop spawns any worker. Fail fast with a SPECIFIC
fix suggestion when a probe fails. Total budget: ~10s + ~$0.05.

### Wire-in shape

```bash
# In deliver.sh, immediately AFTER arg parsing and BEFORE "Step 0":
if [ "${MINIORCH_PREFLIGHT_DISABLED:-0}" != "1" ] \
   && [ -x "$SCRIPT_DIR/lib/preflight-gate.sh" ]; then
  echo
  echo "▶ Step 0a: preflight-gate ($SCRIPT_DIR/lib/preflight-gate.sh)..."
  if ! "$SCRIPT_DIR/lib/preflight-gate.sh" "$KICKOFF" >&2; then
    echo "[deliver] PREFLIGHT BLOCKED — fix the named probe above + re-run" >&2
    exit 1
  fi
fi
```

### Probe set (P1-P10)

| Probe | What it checks | Fix on failure |
|---|---|---|
| P1 | kickoff exists + readable | retry with correct path |
| P2 | `**Branch:** \`feat/<slug>\`` line present | add Branch line under Epic ID |
| P3 | `kickoff-path-lint.sh` clean (all new-file paths have inline `(new file)` markers) | add markers; see lib/kickoff-path-lint.sh:50 |
| P4 | `dod-probe-lint.sh` clean (≥1 semantic probe in DoD slice) | put probe under `## DoD` heading BEFORE any `###` subsection |
| P5 | agents.yaml has NO duplicate-ID entry for this epic | delete one of the duplicates |
| P6 | scope-patterns.yaml has an entry for this epic | add scope entry or accept scaffold-from auto-append |
| P7 | secrets.local.sh loadable + GLM/KIMI/MINIMAX/CODEX_MODEL keys non-empty | source secrets.local.sh + set missing keys |
| P8 | `bash -n` syntax-clean on all `.agentflow/mini-orch/**/*.sh` | run `bash -n <file>` to see the specific line |
| P9 | state.db row, if exists, has status in `{'not started','done'}` | reset via `sqlite3 state.db "UPDATE epics SET status='not started' WHERE id='$EPIC_ID'"` |
| P10 | 1-token LLM lane probe (opt-in via `--strict`) | fix env/model config (e.g. `CODEX_MODEL`, API key) |

### Reference implementation

A complete bash implementation lives at `.agentflow/mini-orch/lib/preflight-gate.sh` in the downstream repo (commit pending). ~200 lines, no
external dependencies beyond `sqlite3` + `jq` + standard coreutils. Adapt
to mini-ork's directory layout (paths derived from `BASH_SOURCE` work
across repos).

## Companion fixes (apply alongside the preflight gate)

### Fix A — Source secrets once at orch entry, not per-subshell

**Bug class:** Every subshell that does `source "$env_script"` (cl_glm.sh /
cl_kimi.sh / cl_minimax.sh / cl_codex.sh) must FIRST source
`secrets.local.sh`, because those scripts have `${API_KEY:?msg}` validators
that exit the subshell when the secret env var isn't populated.

**Sites observed twin-buggy:**
- `lib/_worker-launcher.sh:274-283` — fixed downstream 2026-06-02 morning (commit `d99702624` equivalent)
- `lib/decomposer.sh:98-100` — fixed afternoon (commit `ca62c4aa8`)
- `lib/rubric-prescreen.sh:121-125` — fixed afternoon (commit `ca62c4aa8`)
- Likely-also-buggy: `scaffold-from-decompose.sh`, any spec-author runner, anything else that does `source cl_<lane>.sh`

**Canonical fix pattern:**

```bash
(
  set -uo pipefail
  # Source secrets BEFORE cl_<lane>.sh validators fire
  local _secrets="$REPO_ROOT/.agentflow/config/secrets.local.sh"
  if [ -f "$_secrets" ]; then
    # shellcheck disable=SC1090
    source "$_secrets" 2>/dev/null || true
  fi
  [ -f "$env_script" ] && source "$env_script"
  ...
)
```

**Better:** refactor so secrets source happens ONCE at orch entry (e.g.
in `deliver.sh` or `run.sh` top-level), and child subshells inherit the
env via the exported variables. Eliminates the duplicate-site bug class
entirely. Estimated effort: 1-2 hours.

### Fix B — EXEC-style vs sourceable-env shim dispatch branch

**Bug class:** Lanes split into two families that the launcher must dispatch
differently:

- **Sourceable env shims** (cl_kimi/cl_glm/cl_minimax/cl_deepseek): export
  `ANTHROPIC_BASE_URL` so the subsequent `claude -p` call hits an alternate
  Anthropic-compatible endpoint. The launcher SOURCES them, then runs `claude -p $PROMPT`.

- **EXEC-style CLI shims** (cl_codex.sh / cl_gemini.sh): parse args, validate
  a prompt, then exec a non-Anthropic CLI (`codex exec` / `gemini`). The
  launcher must INVOKE them as a subprocess with the prompt arg —
  sourcing them with no args trips their own arg validators and exits the
  subshell.

**Fix:** in `_worker-launcher.sh` dispatch section, branch on `$MO_AGENT`:

```bash
case "$MO_AGENT" in
  codex|gemini)
    # EXEC-style shim — invoke as subprocess with prompt arg
    bash "$ENV_SCRIPT" --print --output-format text "$PROMPT_TEXT" \
      > "$ITER_DIR/worker.log" 2> "$ITER_DIR/worker.err"
    ;;
  *)
    # Sourceable env shim — source already done; claude -p uses alt endpoint
    claude -p [...] "$PROMPT_TEXT" > "$ITER_DIR/worker.log" 2> "$ITER_DIR/worker.err"
    ;;
esac
```

Also: in the SOURCE step earlier in the launcher, skip-source for codex+gemini:

```bash
case "$MO_AGENT" in
  codex|gemini)
    # Don't source these — they're EXEC-style, not env shims
    echo "    env-resolved: $ENV_SCRIPT (exec-style CLI shim)"
    ;;
  *)
    source "$ENV_SCRIPT"
    echo "    env-loaded: $ENV_SCRIPT"
    ;;
esac
```

### Fix C — YAML duplicate-ID detection in scaffold-from-decompose

**Bug class:** `scaffold-from-decompose.sh` unconditionally appends a new
`<epic_id>:` block to agents.yaml and scope-patterns.yaml, even when a
manual entry with the same ID already exists. YAML last-key-wins → the
manual entry gets shadowed by a default-worker entry.

**Fix:** before appending, grep for the same field-level ID. If found,
either skip the append (preferring the manual entry) OR emit a
warning + abort with operator decision.

```bash
existing=$(grep -cE "^  ${EPIC_ID}:$" "$AGENTS_YAML" 2>/dev/null || echo 0)
if [ "$existing" -gt 0 ]; then
  echo "[scaffold-from] WARN: agents.yaml already has entry for $EPIC_ID — skipping auto-append (manual entry will be used)" >&2
else
  cat >> "$AGENTS_YAML" <<EOF
  # AUTO-GENERATED via scaffold-from-decompose.sh — $job_id
  $EPIC_ID:
    worker: glm
EOF
fi
```

### Fix D — Atomic state.db status transition

**Bug class:** Auto-merge race — orch loop closes with state.db
`status='in review'`, then auto-merge phase queries for `status='approved'`
or similar, finds nothing, and skips. Manual squash-merge required.

**Fix:** Either (a) emit a single atomic `UPDATE epics SET status='approved'
WHERE id=$1 AND status='in review' RETURNING *` at end-of-review, OR (b)
loosen auto-merge query to accept `status IN ('in review','approved')`
with reviewer-verdict cross-check.

**Reference verdict.json shape:** `{"verdict":"APPROVE", ...}` → the JSON
file is the canonical truth; state.db `status` should derive from it.

### Fix E — Pre-commit `bash -n` lint on orch scripts

**Bug class:** A single bash syntax error in `run.sh` (or any orch script)
crashes the whole pipeline silently — no useful error message at dispatch
time, just `set -uo pipefail` killing the loop.

**Fix:** Add a husky pre-commit hook that runs `bash -n <file>` on every
staged `.agentflow/mini-orch/**/*.sh`. ~20 LOC. Catches the regression
before it lands.

```bash
# .husky/check-orch-bash-syntax.sh
stash=$(git diff --cached --name-only --diff-filter=AM | grep -E '^\.agentflow/mini-orch/.*\.sh$')
failures=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  bash -n "$f" 2>/dev/null || failures+=("$f")
done <<<"$stash"
[ "${#failures[@]}" -gt 0 ] && { echo "✗ bash -n failed on: ${failures[*]}" >&2; exit 1; }
exit 0
```

Wire into `.husky/pre-commit` after the other guards.

## Suggested apply-order (mini-ork agents)

```
Wave 1 (1-2 hours, atomic fixes)
  ├─ Apply Fix B — EXEC-style dispatch branch (closes 3 codex bugs)
  ├─ Apply Fix A — secrets-source pattern in all twin sites
  ├─ Apply Fix E — pre-commit bash -n hook
  └─ Apply Fix C — scaffold YAML dedup

Wave 2 (1 day, structural)
  ├─ Implement preflight-gate.sh with all 10 probes
  ├─ Wire as Step 0a in deliver.sh
  └─ Add `--strict` mode with LLM-lane probe

Wave 3 (1-2 days, refactor)
  ├─ Apply Fix D — atomic state.db transitions
  ├─ Single-point secrets source at orch entry (kills Fix A's twin-bug class)
  └─ Add observability: emit Loki/Tempo span per preflight probe pass/fail
```

## Memory anchors (downstream insforge memory IDs)

- `mini_orch_worker_launcher_secrets_bug_fixed_2026_06_02` (id ~1240)
- `kimi_text_format_silent_exit_no_max_turns_2026_06_01`
- `always_fix_mini_orch_bugs_in_situ_2026_06_02` (id 1244)
- `feedback_mini_orch_fixes_mirror_to_upstream_2026_05_30`

## Verification approach

After applying each fix, dispatch a known-good kickoff via deliver.sh and
verify:

1. **First-time success rate** rises from observed ~30-50% to ≥90%
2. **Time-to-detected-error** drops from ~30-60s (worker spawn) to ~10s (preflight)
3. **Manual recovery rate** (post-APPROVE squash-merge required) drops from 100% to 0%

Track in dispatch-stats over a 1-week window post-rollout.

## Addendum (2026-06-02 evening) — Preflight self-bug class

After implementing `preflight-gate.sh` (downstream commit `535e8e0ca`), dogfooding the gate on a freshly-authored WAVE 3a kickoff surfaced a bug in the gate's own duplicate-ID detector at line 129:

```bash
# BUG: grep -c returns 0 with exit-code 1 when no match → || echo 0 appends a second "0"
# → dup_count = "0\n0" → arithmetic comparison errors with "integer expected"
dup_count=$(grep -cE "^  ${EPIC_ID}:$" "$AGENTS_YAML" 2>/dev/null || echo 0)

# FIX: coalesce multiline to single int via head -1 + ${var:-default}
dup_count=$(grep -cE "^  ${EPIC_ID}:$" "$AGENTS_YAML" 2>/dev/null | head -1)
dup_count=${dup_count:-0}
```

Fixed downstream in commit `38b851486` (same commit that added WAVE 3a kickoff — the dogfood loop made the gate self-improving). Lesson for the upstream apply: every preflight probe that uses `grep -c | ... || echo 0` has this same latent bug; check all 10 probes for the pattern.

## Addendum (2026-06-02 evening) — Subsequent waves queued downstream

The downstream session continued past preflight-gate ship with 4 mini-orch waves queued (not all dispatched yet):

| Wave | Status | Sub-epics | Spec ch covered |
|---|---|---|---|
| WAVE 1 (P0+P1 reliability triad) | dispatched, 5/6 in flight, 1 escalated | 6 | Ch 6 + Ch 8 |
| WAVE 3a (memory cluster) | queued | 4 | Ch 9 + Ch 10 + Ch 14 + Ch 15 |
| WAVE 3b (capsule + evidence) | queued | 2 | Ch 5 + Ch 7 |
| WAVE 3c (5 unaudited features) | queued | 5 | Ch 11 + Ch 12 + Ch 13 + Ch 16 + Ch 26 |

Total: 17 sub-epics across 4 waves. Estimated combined cost ~$80-130 + ~8-12hr wall once dispatched sequentially. mini-ork's own agents can fork this pattern: queue waves with explicit prereq chains, dispatch sequentially after each prior wave's auto-merge.
