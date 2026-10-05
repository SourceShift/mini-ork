# Live smoke: vt2-hackability-audit (G09-T05)

Run this AFTER `make worktree-merge SLUG=vt2-hackability-audit` has landed and the engine checkout has been fast-forwarded. Check that `git -C /Volumes/docker-ssd/ps/mini-ork log --oneline -3` shows the merge and that `/Volumes/docker-ssd/ps/mini-ork/mini_ork/gates/hackability.py` exists.

Every step runs the real `bin/mini-ork` against the real `gate_registry` rows in the live home. Steps 1 and 2 only read the live DB; they write record files under `$MINI_ORK_HOME/gate-hackability/`.

Step 3 runs on a scratch COPY of the live DB. `mini-ork promote --dry-run` still INSERTs a `promotion_records` row, because `promotion_evaluate` writes, so never point step 3 at the live DB.

## Env

```bash
unset MINI_ORK_PROJECT_HOME            # the launcher prefers it over MINI_ORK_HOME
export MINI_ORK_ROOT=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_ENGINE_ROOT=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork
export MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000
export MO_GATE_HACKABILITY_N=4 MO_GATE_HACKABILITY_BUDGET_USD=0.50 MO_GATE_HACKABILITY_MAX=0.25
cd /Volumes/docker-ssd/ps/mini-ork
LIVE_DB=$MINI_ORK_HOME/state.db
REC=$MINI_ORK_HOME/gate-hackability
pd() { python3.11 -c 'import json,sys; t=sys.stdin.read(); print(json.dumps(json.loads(t.split("PromotionDecision:\n",1)[1].split("\n\n",1)[0])))'; }
```

## Gates audited, and why

| Gate | Expectation | Why |
|---|---|---|
| `oracle-coalition` | HACKABLE, 0.8 | A safety=1 gate. A fresh `panel_run_id` with zero voters makes `coalition_gate` answer `panel_diverse`, which counts as pass. Pre-merge, on a scratch DB with the live row, `dangling-evidence`, `empty-document`, `hollow-object` and `zero-leaf-skeleton` all passed (4/5); only `empty-context` deferred. |
| `oracle-synthesis-promote` | SOUND, 0.0 | Fails closed since the CW-POR fix. A hollow verdict gives rc 2, which is defer. The zero-leaf skeleton has `panel_score` 0, giving rc 1 `low_panel_score`, which is fail. |
| `oracle-panel-health` | LLM leg; the operator floor is 1/5 | `cw_por` `indeterminate` maps to pass. `{"voters":[{}]}`-shaped hollow documents pass, so a real proposer has something to find. |

If `oracle-coalition` has been fixed upstream before this smoke runs, use `oracle-liveness` or `oracle-stability` instead; both measured 0.8 pre-merge.

## Step 0: pre-state

```bash
sqlite3 "$LIVE_DB" "select gate_id, gate_type, condition, active from gate_registry where gate_id in ('oracle-coalition','oracle-synthesis-promote','oracle-panel-health')"
rm -f "$REC"/oracle-coalition.json "$REC"/oracle-synthesis-promote.json "$REC"/oracle-panel-health.json   # record files only
```

Expect 3 rows, each with `active=1`.

## Step 1: deterministic audit ($0, under 10 s)

```bash
bin/mini-ork gate-fuzz --hackability --gate oracle-coalition --gate oracle-synthesis-promote --db "$LIVE_DB" --json > /tmp/vt2-smoke-1.json; echo rc=$?
jq '.records[] | {gate_id, hackability, passed_bad, trials, unverified, exploits, proposer_status}' /tmp/vt2-smoke-1.json
jq -e '.schema=="gate-hackability/v1" and .gate_id=="oracle-coalition" and .hackability > 0.25 and .trials==5' "$REC/oracle-coalition.json"
jq -e '.hackability==0 and .trials==5 and (.exploits|length)==0 and .unverified.crashed==0' "$REC/oracle-synthesis-promote.json"
bin/mini-ork gate-fuzz --json | jq -e 'has("blind_spot_rate") and (has("records")|not)'   # legacy path unchanged
```

**PASS when all of these hold:**
- rc=0.
- The coalition record has `hackability` 0.8, `passed_bad` 4/5, and `exploits` equal to `["dangling-evidence","empty-document","hollow-object","zero-leaf-skeleton"]`.
- The synthesis-promote record has `hackability` 0.0 over 5 trials.
- `proposer_status` is `"off"` for both gates.
- Both files exist under `$REC`.
- The legacy `gate-fuzz --json` still emits the blind-spot report.

## Step 2: LLM proposer leg (real lane, one call, ≤ $0.50 hard cap, about 1–3 min)

```bash
bin/mini-ork gate-fuzz --hackability --gate oracle-panel-health --proposer-lane glm --db "$LIVE_DB" --json > /tmp/vt2-smoke-2.json; echo rc=$?
jq '.records[0] | {hackability, passed_bad, trials, unverified, proposer, proposer_status, proposer_cost_usd, exploits}' /tmp/vt2-smoke-2.json
jq -e '.records[0] | ([.results[]|select(.source=="proposer")]|length) as $k | $k>=1 and $k<=4 and ([.results[]|select(.reason=="not_known_bad")|.verdict]|all(.==null)) and ([.results[]|select(.source=="operator")|{(.id):.outcome}]|add)=={"empty-context":"REFUTED","dangling-evidence":"REFUTED","empty-document":"REFUTED","hollow-object":"REFUTED","zero-leaf-skeleton":"PROVEN"}' /tmp/vt2-smoke-2.json
jq -c '[.records[0].results[]|select(.source=="proposer" and .outcome=="PROVEN")|.document]' /tmp/vt2-smoke-2.json | python3.11 -c 'import sys,json; from mini_ork.gates.hackability import is_hollow_document as h; ds=json.load(sys.stdin); assert all(h(d) for d in ds), ds; print("proven proposer docs hollow:", len(ds))'
sqlite3 "$LIVE_DB" "select feature_name, model_id, status, cost_usd from llm_calls where feature_name='mini-ork:gate-hackability-propose' order by id desc limit 1"
```

**PASS when all of these hold:**
- rc=0.
- `proposer=="glm"` and `proposer_status=="ok"`.
- There are 1–4 proposer results.
- Every `not_known_bad` result has `verdict: null`, meaning the gate was never run on non-hollow model output.
- Operator outcomes are exactly as listed in the jq check.
- `hackability == passed_bad/trials`.
- Every PROVEN proposer document re-checks as hollow.
- An `llm_calls` row exists with `status=success`, and `proposer_cost_usd` ≤ 0.50.

The LLM does not have to find a new exploit; the run is valid without one. If `proposer_status` is `dispatch_failed`, re-run with `--proposer-lane minimax`, then `deepseek`. FAIL if all three fail.

## Step 3: promotion consumes the record (scratch copy, about 10 s)

```bash
SM=/tmp/vt2-smoke-home; rm -rf "$SM"; mkdir -p "$SM"
sqlite3 "$LIVE_DB" ".backup $SM/state.db" && cp -R "$REC" "$SM/gate-hackability"
SM=$SM python3.11 - <<'PY'
import os, sqlite3
con = sqlite3.connect(os.environ["SM"] + "/state.db")
con.execute("INSERT OR IGNORE INTO workflow_memory (workflow_version_id, workflow_name, yaml_hash, yaml_blob) VALUES ('vt2-smoke-wf-v1','vt2-smoke','deadbeef','# vt2 smoke')")
con.execute("INSERT OR IGNORE INTO workflow_candidates (candidate_id, base_workflow_version_id, status, created_by) VALUES ('wc-vt2-smoke','vt2-smoke-wf-v1','shadow','human')")
for i in (1, 2, 3):
    rid = con.execute("INSERT INTO runs (epic_id, run_dir, branch, baseline_sha, agent) VALUES ('vt2-smoke', ?, 'vt2-smoke', '0000000', 'smoke')", (f"vt2-smoke-run-{i}",)).lastrowid
    con.execute("INSERT OR IGNORE INTO benchmark_tasks (benchmark_id, task_class) VALUES (?, 'code_fix')", (f"vt2-smoke-bench-{i}",))
    con.execute("INSERT INTO benchmark_results (result_id, benchmark_id, candidate_id, run_id, pass, utility_score) VALUES (?,?,?,?,1,0.92)", (f"vt2-smoke-res-{i}", f"vt2-smoke-bench-{i}", "wc-vt2-smoke", rid))
con.commit(); print("seeded wc-vt2-smoke: 3 independent passing runs, class code_fix")
PY
P="env MINI_ORK_HOME=$SM MINI_ORK_DB=$SM/state.db MO_PROMOTION_VERIFIER_AUDIT=0 bin/mini-ork promote --candidate wc-vt2-smoke --dry-run"
$P | tee /tmp/vt2-smoke-3a.txt | pd | jq '{decision, rationale, gh: .gate_hackability}'
MO_PROMOTION_GATE_HACKABILITY=0 $P | pd | jq -e '.decision=="promoted" and (has("gate_hackability")|not)'
MO_GATE_HACKABILITY_MAX=0.9 $P | pd | jq -e '.decision=="promoted" and ([.gate_hackability.within_threshold[].gate_id]|index("oracle-coalition")!=null)'
mv "$SM/gate-hackability" "$SM/gate-hackability.off"; $P | pd | jq -e '.decision=="promoted" and ([.gate_hackability.unmeasured[]|select(.reason=="no_record")|.gate_id]|index("oracle-coalition")!=null)'
```

`MO_PROMOTION_VERIFIER_AUDIT=0` isolates the new hook, because live `collapse_history` could quarantine the candidate first. The candidate gets 3 fresh `runs` rows because the live DB has only 1, and `promotion_evaluate` requires at least 3 independent runs.

**PASS for 3a:**
- `/tmp/vt2-smoke-3a.txt` contains `[dry-run] decision=rejected`.
- The JSON `decision=="rejected"`, and the rationale contains `gate-hackability:` and `oracle-coalition=0.800`.
- `gate_hackability.over_threshold` lists `oracle-coalition`, and `within_threshold` lists `oracle-synthesis-promote` at 0.0.
- `unmeasured` lists `oracle-liveness`, `oracle-stability`, `mutation-adversary-gate` and `step-rules-gate`, each with reason `no_record`.

**PASS for the 3 controls:** each `jq -e` exits 0.
- Knob off ⇒ promoted, with no `gate_hackability` key (legacy shape).
- Threshold raised ⇒ promoted, with the gate in `within_threshold`.
- Records removed ⇒ promoted, and the gate is unmeasured. An unmeasured gate never bricks a promote.

## Overall verdict

- **PASS:** steps 1, 2 and 3 plus all controls pass.
- **FAIL:** any of these happens:
  - a non-zero rc
  - a missing record file
  - a proposer document evaluated without passing the hollow witness
  - a `PROVEN` outcome on a non-hollow document
  - a promote that was rejected while the knob was off or no record existed
  - the legacy `gate-fuzz` output changed

Afterwards, delete `/tmp/vt2-smoke-*` and `$SM`. Keep the `$REC` records: they are now the live measurement the promotion gate reads. Note that `oracle-coalition` at 0.8 will reject every would-be promote on the live DB until the gate is fixed and re-audited, `MO_GATE_HACKABILITY_MAX` is raised, or the record is removed.

## Cost and time

- Step 1: $0, under 10 s. Each audit does one scratch `init_db` (about 0.6 s) plus 5 in-process gate evaluations.
- Step 2: one glm call, typically under $0.10 and hard-capped at $0.50 by `MO_GATE_HACKABILITY_BUDGET_USD`, taking 1–3 min.
- Step 3: $0, about 10 s; it includes a 34 MB `.backup`.
- Total: about 5 min, at most $0.50.
