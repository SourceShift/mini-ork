# Oracle gates — recipe wiring guide

mini-ork ships 6 oracle-hardening primitives under `lib/` (v0.3-rc1):

| Primitive | Library | Function | Closes failure mode |
|---|---|---|---|
| Coalition gate (W1-B) | `lib/coalition_gate.sh` | `mo_check_panel_coalition` | Same-family lens panel (Bertalanič 2026) + ρ ≥ 0.25 (Rajan 2025) |
| CW-POR (W1-C) | `lib/cw_por.sh` | `mo_compute_cw_por` | Authority-capture (Agarwal & Khanna 2025) — orthogonal to Krippendorff α |
| Synthesis-promote (W1-D) | `lib/promotion_gate.sh` | `mo_promote_synthesis_gate` | Single-signal promotion of LLM-judged candidates (Adapala 2025) |
| Adaptive stability (W2-B) | `lib/adaptive_stability.sh` | `mo_check_panel_stability` | Wasted compute on stabilized debate panels (Hu et al 2025) |
| Circuit breaker (W2-C) | `lib/circuit_breaker.sh` | `mo_check_liveness_breaker` | Spend-under-cap-but-zero-progress runs |
| Gradient reframe (D-048) | `mini_ork/learning/gradient_extractor.py` | recipe-shaped prompt | Audit recipes returning `[]` (coordination vs algorithmic shape) |

The native gradient primitive has standalone pytest coverage; the remaining
Bash gate primitives retain inline self-tests plus unit coverage.

## Recipe-level opt-in pattern

Each primitive has a thin executable shim under `gates/<name>.sh` that
adapts the lib's function signature to the gate-registry's
`condition` contract (executable path + context-JSON-on-argv[1]).
Recipes opt in by registering the shim as a `custom` gate type
pointing at the shim path.

### Wiring example: refactor-audit recipe

Register at recipe-init time (the recipe's `init.sh` or `setup.sh`):

```bash
source "$MINI_ORK_ROOT/lib/gate_registry.sh"

# Coalition gate — fires before synthesizer node; refuses to synthesize
# a panel whose lens families overlap or whose ρ ≥ 0.25.
gate_register "custom" \
  "$MINI_ORK_ROOT/gates/coalition.sh" \
  "refactor_audit" \
  --safety

# Synthesis-promote gate — fires before publisher node; refuses to
# promote a panel verdict that fails the panel_score + CW-POR +
# structural-signal conjunction.
gate_register "custom" \
  "$MINI_ORK_ROOT/gates/synthesis-promote.sh" \
  "refactor_audit"
```

Then list `custom` in the relevant node's `gates: [...]` field in
`workflow.yaml`:

```yaml
- name: synthesizer
  type: reviewer
  gates: [budget_gate, custom]   # ← custom matches the gate_register call
- name: publisher
  type: publisher
  gates: [scope_gate, custom]    # ← second custom for synthesis-promote
```

At dispatch time, `gate_run_all <task_class> <context_json>` invokes
the shim with the context. The shim sources the lib, runs the function,
and exits with the gate-registry contract:

| rc | Meaning |
|---:|---|
| 0  | pass — node may proceed |
| 1  | fail — node should be skipped or run abort |
| 2  | defer — gate cannot decide (fail-open advisory) |

## Context-JSON contract per shim

Each shim consumes a specific context shape on argv[1]. Recipes are
responsible for assembling this JSON before invoking the gate.

### gates/coalition.sh

```json
{
  "panel_run_id": "run-<unix-ts>-<pid>",
  "recipe":       "<recipe-name>"
}
```

Reads `execution_traces` for the panel run, computes ρ via
`measure_rho`, computes family distribution via `config/agents.yaml`
lane-to-family lookup, returns `verdict: panel_diverse | COALITION_ABORT |
indeterminate`.

Env knobs:
- `MO_RHO_THRESHOLD` (default 0.25 — Rajan 2025 submodularity ceiling)
- `MO_FAMILY_DIVERSITY_GATE` (default `strict`; set `advisory` to
  warn-only without blocking)

### gates/panel-health.sh

```json
{
  "verdict_file": "<path-to-panel-verdict-with-voters-array.json>"
}
```

The verdict file must contain a `voters[]` array per the CW-POR
contract. Each voter has `voter_id`, `vote` (approve|reject),
`confidence` (0..1), and `ground_truth_match` (bool|null). Without
ground truth on at least one voter, CW-POR returns `indeterminate`
(treated as pass by the shim, since absence-of-evidence is not
evidence-of-absence).

Env knob:
- `MO_CW_POR_THRESHOLD` (default 0.3)

### gates/stability.sh

```json
{
  "panel_run_id":  "run-<unix-ts>-<pid>",
  "current_round": <int>
}
```

Reads execution_traces for the panel run, buckets by `-r<N>-` segment
in `trace_id`, computes round-over-round verdict drift.

Note: stability is a DECISION AID, not a hard gate. rc=0 (pass = CONTINUE)
and rc=1 (fail = HALT) reflect the recommendation; recipes consume the
JSON output's `.recommendation` field directly for fine-grained logic.

Env knobs:
- `MO_PANEL_STABILITY_THRESHOLD` (default 0.10)
- `MO_PANEL_MIN_ROUNDS` (default 2)
- `MO_PANEL_MAX_ROUNDS` (default 5)

### gates/synthesis-promote.sh

```json
{
  "verdict_file": "<path-to-panel-verdict-with-structural.json>",
  "task_class":   "research_synthesis"
}
```

The verdict file must include:
- `panel_score` (float 0..100)
- `voters` (passed to CW-POR check)
- `structural` (with `citation_density_per_lens`, `file_coverage_delta`,
  `finding_cardinality`)

Deterministic-oracle task classes (`code_fix`, `db_migration`) bypass
with reason=`deterministic_class`.

Env knobs:
- `MO_PROMOTE_SCORE_THRESHOLD` (default 80)
- `MO_CW_POR_THRESHOLD` (default 0.3)
- `MO_MIN_CITATION_DENSITY` (default 3)
- `MO_MIN_FINDING_CARDINALITY` (default 5)
- `MO_DETERMINISTIC_TASK_CLASSES` (default `"code_fix db_migration"`)

## Why shims instead of inline-case in gate_run_all

`gate_run_all` in `lib/gate_registry.sh` has an inline python
case-statement per gate_type (`budget_gate`, `human_gate`, etc).
Adding 4 new gate types would mean 4 new inline cases — each tightly
coupled to the registry, harder to test in isolation, and rebuilding
the registry on every change.

The `custom` gate path already supports executable conditions: the
registry shells out to the path, passes context JSON on argv[1], and
reads exit code + stdout. The shims under `gates/` use this path,
keeping the lib primitives single-responsibility and the registry
unchanged.

## Composition with the existing 7 gate types

The 6 oracle primitives compose with the existing built-in gates:
- `budget_gate` — refuse to fire if cost-per-run exceeds threshold
- `scope_gate` — refuse to touch files outside the scope clause
- `coalition` — refuse to synthesize when panel is family-collisioned
- `panel-health` — refuse when authority-capture detected
- `stability` — recommend HALT when debate has stabilized
- `synthesis-promote` — refuse promotion when conjunction fails

Recipes can stack any combination. Today's canonical pattern for the
refactor-audit recipe: `[budget_gate, coalition]` on synthesizer +
`[scope_gate, synthesis-promote]` on publisher.

## Smoke test

```bash
# Coalition gate self-smoke (4 same-family lenses → expect rc=1):
( export MINI_ORK_ROOT=~/ps/mini-ork
  TEST_DB=$(mktemp); export MINI_ORK_DB="$TEST_DB"
  source tests/lib/setup_state_db.sh && test_apply_migrations >/dev/null
  python3 -c "
import sqlite3
con = sqlite3.connect('$TEST_DB')
con.execute(\"INSERT OR IGNORE INTO runs (id, agent, final_verdict) VALUES (1, 'test', 'APPROVE')\")
for v in [('tr-1-r', 'sonnet'), ('tr-2-r', 'opus'), ('tr-3-r', 'sonnet'), ('tr-4-r', 'opus')]:
    con.execute(\"INSERT INTO execution_traces (trace_id, agent_version_id, run_id, task_class, status) VALUES (?,?,1,'test','success')\", v)
con.commit()
"
  gates/coalition.sh '{"panel_run_id":"r","recipe":"refactor-audit"}'
  echo \"rc=$?\"
  rm -f \"$TEST_DB\"
)
# Expected: rc=1 + JSON {verdict: COALITION_ABORT, reason: both, ...}
```

## Phase tracker — N + O

This wire-up closes Phase N + O at primitive-level:

- **Phase N — Promotion-class taxonomy enforced.** `mo_promote_synthesis_gate`
  is the executable form of the deterministic-vs-LLM-judged split.
  Recipes opt in via `gates/synthesis-promote.sh`.

- **Phase O — Panel-failure detection.** Three orthogonal diagnostics
  available as opt-in gates:
  - ρ + family-diversity (`gates/coalition.sh`)
  - CW-POR authority-capture (`gates/panel-health.sh`)
  - Round-stability drift (`gates/stability.sh`)
  Each fail-opens when it cannot measure — no silent blocking.

Future work: wire these into mini_ork/cli/execute.py's central dispatch
loop so they fire automatically for any recipe without per-recipe
opt-in. That's a 3-subagent-consensus-pass-first change per the
project skill rules.
