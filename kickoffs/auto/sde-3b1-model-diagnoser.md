# SDE-3b1 model diagnoser for unclassified failures (self-driving epic)

## Goal

After SDE-1, 22% of recent failed runs still diagnose `unclassified` (11 of the
50 most recent; mostly `lifecycle exited without a verdict`), and every one goes
to a human. The operator chose AGGRESSIVE escalation: a model may diagnose them,
and the frozen policy (`mini_ork/recovery/escalation_policy.py`) admits a
`source: "model"` engine diagnosis only above its confidence bar. This step adds
that model diagnoser. A model diagnosis must be evidence-grounded — every claim
quotes the run's own evidence verbatim — or it is discarded.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New `mini_ork/recovery/diagnoser_llm.py`:
   - `build_evidence_pack(home, run_id, rule_diag) -> str`, at most 12 000 chars:
     the last 40 `run_events` rows (type + payload, truncated), `task_runs.notes`,
     `verdict.json`, `publish-abstain.json`, `repair.json`, the retry-hint
     summary, and the last 80 lines of the failing node's `impl-*.log` /
     `agent-*.live.jsonl` text. Redact anything matching
     `(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+` to `<redacted>`.
   - `diagnose_with_model(home, run_id, rule_diag, *, dispatch=None) -> dict | None`:
     only when `rule_diag["layer"] == "unclassified"`. Lane = env
     `MO_DIAGNOSER_LANE` (default `glm`). If that lane resolves to the same
     MODEL as the run's implementer lane (from `run_profile.json`
     `provider_policy.lanes`; compare the provider entry's `model`, or the lane
     name when `model` is unset), return `None` — a model never diagnoses its own
     work. Do NOT compare the provider `family` field: it names the transport
     (`glm`, `deepseek_flash` and `minimax` are all `gateway`), not the model. Dispatch through `mini_ork.dispatch.providers.dispatch_model`
     (inject `dispatch=` for tests) with a prompt asking for ONE JSON object:
     `{layer, stage, node, routes_to, confidence, summary, evidence: [{kind, ref, excerpt}]}`.
   - Validation (any failure → `None`, recorded in the returned note):
     `layer` / `stage` / `routes_to` inside the enums exported by
     `mini_ork/recovery/diagnose.py`; `layer != "unclassified"`;
     `0 <= confidence <= 1`; at least one evidence item; EVERY `excerpt` is at
     least 20 chars and a verbatim substring of the evidence pack.
   - On success return a diagnosis dict in the SDE-1 contract with
     `source: "model"`, `rule_id: "M1"`, plus `model_lane` and `cost_usd`.
2. `mini_ork/recovery/diagnose.py`: `diagnose(home, run_id, *, write=True, allow_model=False)`.
   When `allow_model` and the rule result is `unclassified`, try
   `diagnose_with_model`; keep the rule result when it returns `None`. A model
   diagnosis is computed AT MOST ONCE per run: if `<run_dir>/diagnosis.json`
   already has `source == "model"`, reuse it without dispatching.
3. `mini_ork/recovery/auto_repair.py` `decide` passes
   `allow_model = context_env("MO_MODEL_DIAGNOSER", "1") != "0"` (default on —
   operator decision). The parked-run guard and `MO_AUTO_REPAIR=0` still come
   first, so a parked or disabled run never dispatches.

## Files in scope

- mini_ork/recovery/diagnoser_llm.py (new)
- mini_ork/recovery/diagnose.py
- mini_ork/recovery/auto_repair.py
- tests/unit/test_diagnoser_llm.py (new)

ALLOW_HIGH_BLAST_RADIUS: operator-authored step edits auto_repair.py (frozen only for auto-opened runs).

## Tests (`tests/unit/test_diagnoser_llm.py`, hermetic, injected `dispatch`; no network)

- an unclassified run + a stub reply citing a verbatim excerpt → `source: "model"`,
  `rule_id: "M1"`, the reply's layer;
- a reply whose excerpt is NOT in the pack → `None`, rule result kept;
- out-of-enum layer, `confidence: 1.4`, empty evidence, or non-JSON → `None`;
- a rule-classified run never dispatches (stub asserts not called);
- diagnoser lane resolving to the implementer's model → `None`, no dispatch; a
  different model on the same `gateway` family DOES dispatch;
- second `diagnose(..., allow_model=True)` reuses `diagnosis.json` (dispatch
  called once across two calls);
- secrets in a log line are redacted in the pack;
- `MO_MODEL_DIAGNOSER=0` → `decide` never dispatches.

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_diagnoser_llm.py tests/unit/test_diagnose.py tests/unit/test_auto_repair.py --junitxml="${MINI_ORK_RUN_DIR}/diagnoser-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/diagnoser-junit.xml`

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_diagnoser_llm.py tests/unit/test_diagnose.py tests/unit/test_auto_repair.py tests/unit/test_escalation.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch escalation.py,
escalation_policy.py, triage/, or any recipe.
