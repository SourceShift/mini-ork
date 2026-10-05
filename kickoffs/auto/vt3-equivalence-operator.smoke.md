# VT3 live smoke: declared equivalence operator against a LIVE `mini-ork serve`

This tests the MERGED feature (`kickoffs/auto/vt3-equivalence-operator.md`) against a real HTTP surface. It is not a unit test. There are no LLM calls and no edits to the repo.

## Which entrypoint, honestly

No recipe under `recipes/` lists `api_contract` in `success_verifiers` (`grep -rn api_contract recipes/` finds nothing), so no `bin/mini-ork run <recipe>` path reaches the behavioral verifier today.

The production path is `mini-ork verify` (`mini_ork/cli/verify.py:356-373`):
1. It resolves `verifiers/api_contract.py` through `_find_verifier_script` (`:75-90`).
2. It runs that file as a subprocess under the launcher interpreter (`$ENGINE/.venv/bin/python`).
3. The subprocess calls `behavioral.main`, which reads `MO_OBSERVABLE_SPEC`.

The smoke drives that path in two layers:
- **Step 2 (the matrix):** 20 cases, each spawning `verifiers/api_contract.py` with the same interpreter and env the dispatcher uses.
- **Step 3 (end-to-end):** `bin/mini-ork verify --plan`, to prove the dispatcher maps the verdict to `pass: true/false`.

The live surface is the read-only API from `bin/mini-ork serve`. Three of its endpoints differ from a declared expectation only in ways that do not matter:

| Endpoint | How it differs | Operator that fits |
|---|---|---|
| `GET /api/v1/fingerprint/recipes` | a sorted list, while the declared catalogue says nothing about order | `set` |
| `GET /api/v1/health` | carries `db_path`, which is specific to the deployment | `canonical` with `ignore_keys` |
| `GET /health` (agent-server shim) | `uptime` is `round(monotonic, 3)` and changes on every probe, so `idempotent_repeat` under `exact` REFUTES a correct endpoint | `canonical` or `tolerant` |

## Preconditions

```bash
export ENGINE=/Volumes/docker-ssd/ps/mini-ork
# Local main can be stale after a worktree-merge. Fast-forward first, then check the feature is present.
git -C $ENGINE fetch origin && git -C $ENGINE merge --ff-only origin/main
test -f $ENGINE/mini_ork/verify/equivalence.py && grep -q expect_body_set $ENGINE/mini_ork/verify/behavioral.py && echo FEATURE_PRESENT
$ENGINE/.venv/bin/python -c "import httpx, fastapi, uvicorn; print('deps ok')"
lsof -nP -iTCP:${VT3_PORT:-7090} -sTCP:LISTEN || echo PORT_FREE   # if busy: export VT3_PORT=7091
```

## Env

```bash
export ENGINE=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_ROOT=$ENGINE MINI_ORK_ENGINE_ROOT=$ENGINE MINI_ORK_HOME=$ENGINE/.mini-ork
export MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000   # protocol env; nothing here spends
export VT3_PORT=${VT3_PORT:-7090}
unset MINI_ORK_RECIPE MO_OBSERVABLE_SPEC MO_BEHAV_SURFACE MO_BEHAV_ABSTAIN_EXIT MINI_ORK_RUN_DIR
export SMOKE=/tmp/vt3-smoke-$(date +%Y%m%d-%H%M%S); mkdir -p $SMOKE/specs $SMOKE/out $SMOKE/run
```

## Step 1: start the live surface

```bash
cd $ENGINE && bin/mini-ork serve --port $VT3_PORT --home $MINI_ORK_HOME > $SMOKE/serve.log 2>&1 &
echo $! > $SMOKE/serve.pid
for i in $(seq 1 30); do curl -sf http://127.0.0.1:$VT3_PORT/alive >/dev/null && break; sleep 1; done
curl -s http://127.0.0.1:$VT3_PORT/api/v1/health; echo
curl -s http://127.0.0.1:$VT3_PORT/health; echo
curl -s http://127.0.0.1:$VT3_PORT/api/v1/fingerprint/recipes | head -c 200; echo
```

Expect three responses:
- `/api/v1/health`: `{"ok":true,"db_path":...,"has_task_runs":...}`
- `/health`: a body containing a float `"uptime"`
- `/api/v1/fingerprint/recipes`: a JSON list of recipe names

If any is missing, stop: FAIL (surface not live).

## Step 2: the operator matrix (production verifier subprocess, 20 cases)

The specs are `.json` files. `behavioral._load_spec_file` loads any path that is not yaml with `json.loads`, which avoids yaml quoting.

The expected values are declared independently of the API:
- **recipes:** from `recipes/*/workflow.yaml` on disk.
- **health flags:** from the `state.db` schema, opened read-only.

```bash
cat > $SMOKE/matrix.py <<'PY'
import json, os, sqlite3, subprocess, sys
from pathlib import Path
ENGINE, SMOKE, HOME = Path(os.environ["MINI_ORK_ROOT"]), Path(os.environ["SMOKE"]), os.environ["MINI_ORK_HOME"]
PY, VERIFIER = str(ENGINE / ".venv/bin/python"), str(ENGINE / "verifiers/api_contract.py")
BASE = {"surface": "api", "staging_url": f"http://127.0.0.1:{os.environ['VT3_PORT']}"}
RECIPES = sorted(p.name for p in (ENGINE / "recipes").iterdir() if (p / "workflow.yaml").is_file())
WRONG = ["zz-no-such-recipe"] + RECIPES[1:]                      # genuinely wrong catalogue
con = sqlite3.connect(f"file:{HOME}/state.db?mode=ro", uri=True)
tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
HEALTH = {"ok": True, **{f"has_{t}": t in tables for t in ("task_runs", "mo_events", "self_improve_runs")}}
IGN_DB = {"operator": "canonical", "rules": {"ignore_keys": ["db_path"]}}
IGN_UP = {"operator": "canonical", "rules": {"ignore_keys": ["uptime"]}}
TOL_UP = {"operator": "tolerant", "rules": {"abs_tol": 5.0}}
WS = {"operator": "canonical", "rules": {"strip_whitespace": True}}
TOL = {"operator": "tolerant", "rules": {"abs_tol": 1.0}}
rec = lambda body, eq: {**BASE, "target": "/api/v1/fingerprint/recipes", "expect_body": body, "equivalence": eq}
hlth = lambda body, eq: {**BASE, "target": "/api/v1/health", "expect_body": body, "equivalence": eq}
idem = lambda eq: {**BASE, "target": "/health", "metamorphic": ["idempotent_repeat"], "equivalence": eq}
CASES = {  # F = rejected-correct flip pairs; C/M = control/mutant pairs; U/D = guards
  "F1-exact": rec(RECIPES[::-1], "exact"), "F1-set": rec(RECIPES[::-1], "set"),
  "F2-exact": hlth(HEALTH, "exact"), "F2-canonical": hlth(HEALTH, IGN_DB),
  "F3-exact": idem("exact"), "F3-canonical": idem(IGN_UP),
  "F4-exact": idem("exact"), "F4-tolerant": idem(TOL_UP),
  "C-exact": rec(RECIPES, "exact"), "M-exact": rec(WRONG, "exact"),
  "C-set": rec(RECIPES[::-1], "set"), "M-set": rec(WRONG[::-1], "set"),
  "C-canonical": rec(RECIPES, WS), "M-canonical": rec(WRONG, WS),
  "C-tolerant": rec(RECIPES, TOL), "M-tolerant": rec(WRONG, TOL),
  "C-health": hlth(HEALTH, IGN_DB), "M-health": hlth({**HEALTH, "has_task_runs": not HEALTH["has_task_runs"]}, IGN_DB),
  "U-fuzzy": rec(RECIPES, "fuzzy"),
  "D-default": {**BASE, "target": "/api/v1/fingerprint/recipes", "metamorphic": ["idempotent_repeat"]},
}
def run(name, spec):
    path = SMOKE / "specs" / f"{name}.json"; path.write_text(json.dumps(spec, indent=2))
    p = subprocess.run([PY, VERIFIER], env={**os.environ, "MO_OBSERVABLE_SPEC": str(path)},
                       capture_output=True, text=True, timeout=120)
    (SMOKE / "out" / f"{name}.json").write_text(p.stdout + ("\n--stderr--\n" + p.stderr if p.stderr else ""))
    try: return p.returncode, json.loads(p.stdout)
    except Exception: return p.returncode, {"status": "NO_JSON", "checks": [], "stderr": p.stderr[-300:]}
R = {n: run(n, s) for n, s in CASES.items()}
st = lambda n: R[n][1].get("status")
op_of = lambda n: (lambda e: e if isinstance(e, str) else e["operator"])(CASES[n].get("equivalence") or "exact")
fails = []
need = lambda ok, msg: ok or fails.append(msg)
pairs = [("F1-exact", "F1-set"), ("F2-exact", "F2-canonical"), ("F3-exact", "F3-canonical"), ("F4-exact", "F4-tolerant")]
flips = sum(st(a) == "REFUTED" and st(b) == "PROVEN" for a, b in pairs)
muts = ["exact", "set", "canonical", "tolerant", "health"]
false_approvals = sum(st(f"M-{m}") != "REFUTED" for m in muts)
need(flips == 4, f"flips {flips}/4")
need(false_approvals == 0, f"false approvals {false_approvals}/5")
need(all(st(f"C-{m}") == "PROVEN" for m in muts), "a mutant's paired control was not PROVEN")
for n, (rc, v) in R.items():
    if v.get("status") == "REFUTED":
        op = op_of(n)
        need(rc == 1 and v.get("operator") == op, f"{n}: rc={rc} operator={v.get('operator')} want {op}")
        need(any(c["ok"] is False and c.get("operator") == op and c["detail"].endswith(f"[operator={op}]")
                 for c in v["checks"]), f"{n}: no failing check carries [operator={op}]")
for n, path in [("F2-exact", "$.db_path"), ("F3-exact", "$.uptime"), ("M-health", "$.has_task_runs")]:
    need(path in json.dumps(R[n][1]), f"{n}: refuted, but not at {path}")
u = R["U-fuzzy"]
need(u[1].get("status") == "UNVERIFIED" and u[0] != 0 and any(
    c["name"] == "equivalence" and c["ok"] is None and "fuzzy" in c["detail"] for c in u[1]["checks"]), "U-fuzzy did not abstain")
d = R["D-default"][1]
need(d.get("status") == "PROVEN" and d.get("operator") == "exact" and any(
    c["name"] == "idempotent_repeat" and c["detail"] == "3 probes identical" for c in d["checks"]), "D-default drifted")
for n, (rc, v) in R.items(): print(f"{n:14} rc={rc} status={v.get('status'):10} operator={v.get('operator')}")
print(f"flips={flips}/4 false_approvals={false_approvals}/5")
print("MATRIX PASS" if not fails else "MATRIX FAIL\n  " + "\n  ".join(fails)); sys.exit(1 if fails else 0)
PY
cd $ENGINE && $ENGINE/.venv/bin/python $SMOKE/matrix.py | tee $SMOKE/matrix.txt
```

## Step 3: through the real dispatcher (`bin/mini-ork verify`)

`MINI_ORK_DB` points at a scratch file, so the dispatcher's trace rows and gate run never touch the live `state.db`. `MINI_ORK_RUN_DIR` keeps the evidence logs under `$SMOKE/run/evidence`. `MO_MUTATION_ADVERSARY=0` skips the mutation campaign.

```bash
printf '{"task_class":"vt3-smoke","artifact_contract":{"success_verifiers":["api_contract"]}}\n' > $SMOKE/plan.json
for c in F1-set F1-exact; do
  ( cd $ENGINE && MINI_ORK_RUN_DIR=$SMOKE/run MINI_ORK_DB=$SMOKE/verify-state.db MO_MUTATION_ADVERSARY=0 \
    MO_OBSERVABLE_SPEC=$SMOKE/specs/$c.json bin/mini-ork verify --plan $SMOKE/plan.json \
    > $SMOKE/out/dispatch-$c.json 2> $SMOKE/out/dispatch-$c.err )
done
$ENGINE/.venv/bin/python - <<'PY'
import json, os, sys
S = os.environ["SMOKE"]; bad = []
for case, want_pass, needles in [("F1-set", True, ['"status": "PROVEN"', '"operator": "set"']),
                                 ("F1-exact", False, ['"status": "REFUTED"', '"operator": "exact"', "[operator=exact]"])]:
    out = json.load(open(f"{S}/out/dispatch-{case}.json"))
    row = next((r for r in out["results"] if r.get("verifier") == "api_contract"), None)
    ev = open(row["evidence_path"]).read() if row and os.path.isfile(row.get("evidence_path", "")) else ""
    if not row or row.get("pass") is not want_pass: bad.append(f"{case}: api_contract pass={row and row.get('pass')} want {want_pass}")
    bad += [f"{case}: evidence log lacks {n}" for n in needles if n not in ev]
print("DISPATCH PASS" if not bad else "DISPATCH FAIL\n  " + "\n  ".join(bad)); sys.exit(1 if bad else 0)
PY
```

Only the `api_contract` entry is judged. The overall `verdict`/rc of `mini-ork verify` also folds in `__gates__` evaluated against the scratch DB, so it is ignored. The dispatcher's inline `detail` is the last stdout line (`}` for the indented verdict JSON), so the evidence log is the source of truth.

## Step 4: teardown

```bash
kill $(cat $SMOKE/serve.pid) 2>/dev/null; lsof -ti tcp:$VT3_PORT | xargs kill 2>/dev/null; true
```

## PASS / FAIL

**PASS** requires both `MATRIX PASS` (Step 2) and `DISPATCH PASS` (Step 3), which together mean all of the following hold:
- **Rejected-correct flips: 4/4.** `exact` REFUTES each correct live surface: the order-only recipes list, `/api/v1/health` with `db_path` undeclared, and `/health` with volatile `uptime` (twice). The declared fitted operator (`set`, `canonical`, `canonical`, `tolerant`) PROVES each one.
- **False approvals: 0/5.** Every mutant is REFUTED: the wrong recipe catalogue under exact, set, canonical and tolerant, plus a flipped `has_task_runs` under canonical. Each paired control is PROVEN under the same operator, so each refutation is caused by the mutation.
- **Refuted for the right reason:**
  - Every REFUTED verdict has `rc=1` and a top-level `"operator"` equal to the declared one.
  - It also has a failing check whose `"operator"` matches and whose detail ends `[operator=<op>]`.
  - The diff paths are `$.db_path` (F2-exact), `$.uptime` (F3-exact) and `$.has_task_runs` (M-health).
- **Guards:**
  - `U-fuzzy` is UNVERIFIED with rc != 0 and an `equivalence` check whose detail names `fuzzy`.
  - `D-default` (nothing declared) is PROVEN with `"operator": "exact"` and the unchanged detail `3 probes identical`.
- **Dispatcher:** `pass: true` for F1-set and `pass: false` for F1-exact. The evidence logs carry the operator-stamped verdict.

**FAIL** is anything else. In particular, any mutant that is PROVEN is a false approval, which blocks the feature. A NO_JSON row means the verifier crashed; read `$SMOKE/out/<case>.json`. Keep `$SMOKE/` as the evidence bundle: specs, raw verdict JSON, `matrix.txt` and the dispatch logs.

## Cost and time

$0.00. No LLM lane is touched, and nothing reads or charges `MO_DAILY_BUDGET_USD`. Expect about 1-2 min wall time: about 10 s for the server to boot, 20 verifier subprocesses (the idempotent cases F3, F4 and D make 4 probes each, the others 1), and 2 dispatcher invocations.
