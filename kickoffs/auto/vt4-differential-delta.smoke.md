# Live smoke: vt4-differential-delta (G10-T02 differential behavioural-equivalence term in the Assay oracle)

Run this AFTER `vt4-differential-delta` is merged to main and the smoke engine has been moved to that commit. It is a real
run: real model lane (`MO_CERTIFY_MODEL`, default `minimax`), real Docker execution, a real git repo. It is not a unit test.

## Target, and why this one

The certify demo repo built by `$ENGINE/examples/certify-demo/demo.sh`: a pip-installable `stats` package whose `median()`
returns the upper-middle value for even-length input (`s[len(s) // 2]`). demo.sh creates `base`, `fix-correct` (GOLD: averages
the two middle values only when `len` is even) and `fix-cheat` (special-cases `[1, 2, 3, 4]`). This smoke adds ONE branch:

- `fix-collateral`: `mid = len(s) // 2; return (s[mid - 1] + s[mid]) / 2` with no parity check. It fixes the reported bug
  (`[1, 2, 3, 4]` → 2.5, every even-length list correct) and breaks odd-length input in the same function: `[1, 2, 3]` → 1.5
  (base 2), `[5]` → 5.0 (base 5), `[1, 2, 3, 4, 5]` → 2.5 (base 3). Every invariant/relation the oracle builds lives in the
  even-length bug domain (invariants must FAIL ON BASE), so today's oracle is expected to PROVE it: the fault this term exists
  to catch. Expected differential evidence: bug_domain case `[1, 2, 3, 4]` diverges (anchor: base `3`, head `2.5`); every
  odd-length preserve case diverges (e.g. base `2`, head `1.5`) and is confirmed by re-run → REFUTED.
- `fix-correct` on the same suite: bug_domain diverges (3 → 2.5), odd-length preserve cases agree exactly (same int) →
  `equivalent`, PROVEN unchanged.

Issue text (quotes the expected value, so PoC+ grounding succeeds and the oracle reaches the vetoes):
`median([1, 2, 3, 4]) returns 3, but the median of an even-length list should be the average of the two middle values, so it should return 2.5.`

## Runtime

`mini-ork certify` needs Docker (local colima): `_decide_image` runs `docker build` (python:3.11-slim + `pip install -e .`,
network at build time), then `Crucible` uses the `docker` backend (verifiers runtime) or `docker-cli`. No subprocess backend.

## Commands

```bash
# 0. Environment and preflight
export ENGINE=/Volumes/docker-ssd/ps/mini-ork-worktrees/vt-smoke-engine
export MINI_ORK_ROOT=$ENGINE MINI_ORK_ENGINE_ROOT=$ENGINE MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork
export MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000 MO_CERTIFY_MODEL=minimax
export PATH="$ENGINE/bin:$PATH"
SMOKE=/tmp/vt4-diff-smoke-$(date +%Y%m%d-%H%M%S); DEMO=$SMOKE/stats-demo; mkdir -p "$SMOKE"
ISSUE='median([1, 2, 3, 4]) returns 3, but the median of an even-length list should be the average of the two middle values, so it should return 2.5.'
KNOBS_OFF="-u MO_ASSAY_DIFFERENTIAL -u MO_ASSAY_DIFFERENTIAL_N -u MO_ASSAY_DIFFERENTIAL_VETO_MIN -u MO_ASSAY_RELATIONS -u MO_ASSAY_RELATIONS_RESCUE -u MO_ASSAY_RELATIONS_K"
git -C "$ENGINE" log --oneline -1 -- mini_ork/certify/differential.py   # empty => engine not at merged main: STOP
grep -q MO_ASSAY_DIFFERENTIAL "$ENGINE/mini_ork/certify/differential.py" || echo "STOP: knobs missing"
PYTHONPATH="$ENGINE" "$ENGINE/.venv/bin/python" -c 'import mini_ork.certify.differential as d; print(d.__file__)'  # must be under $ENGINE
docker info --format '{{.ServerVersion}}'                                 # error => start colima first

# 1. Knobs-OFF control: demo.sh builds the repo and certifies fix-correct + fix-cheat with today's oracle
env $KNOBS_OFF bash "$ENGINE/examples/certify-demo/demo.sh" "$DEMO" > "$SMOKE/control.log" 2>&1
# 1b. Add fix-collateral (fixes even-length, breaks odd-length in the same function), then ground-truth all branches
git -C "$DEMO" checkout -q -b fix-collateral base
python3 - "$DEMO/stats/__init__.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read(); old = "    s = sorted(xs)\n    return s[len(s) // 2]"
assert old in s, "demo.sh changed: base median body not found"
open(p, "w").write(s.replace(old, "    s = sorted(xs)\n    mid = len(s) // 2\n    return (s[mid - 1] + s[mid]) / 2"))
PY
git -C "$DEMO" commit -q -am "fix median for even-length input" && git -C "$DEMO" checkout -q base
for br in base fix-correct fix-collateral; do
  git -C "$DEMO" show "$br:stats/__init__.py" > "$SMOKE/gt_$(echo $br | tr - _).py"
  (cd "$SMOKE" && python3 -c "import gt_$(echo $br | tr - _) as m; print('$br', m.median([1,2,3,4]), m.median([1,2,3]), m.median([5]))")
done   # expect: base 3 2 5 | fix-correct 2.5 2 5 | fix-collateral 2.5 1.5 5.0
rc=0; env $KNOBS_OFF mini-ork certify --repo "$DEMO" --base base --head fix-collateral --issue "$ISSUE" \
  --out "$SMOKE/control-fix-collateral.cert.json" > "$SMOKE/control-collateral.out" 2> "$SMOKE/control-collateral.err" || rc=$?
echo "control fix-collateral exit=$rc :: $(head -1 "$SMOKE/control-collateral.out")"
grep -E '^(PROVEN|REFUTED|UNVERIFIED)|exit code' "$SMOKE/control.log"
cat "$SMOKE/control.log" "$SMOKE/control-collateral.err" | grep -c '^\[assay-differential\]'   # must be 0

# 2. Part A: production CLI, differential on (relations off, to isolate the term)
export MO_ASSAY_DIFFERENTIAL=1 MO_ASSAY_DIFFERENTIAL_N=4; unset MO_ASSAY_DIFFERENTIAL_VETO_MIN MO_ASSAY_RELATIONS MO_ASSAY_RELATIONS_RESCUE
for br in fix-correct fix-collateral; do
  rc=0
  mini-ork certify --repo "$DEMO" --base base --head "$br" --issue "$ISSUE" --out "$SMOKE/A-$br.cert.json" \
    > "$SMOKE/A-$br.out" 2> "$SMOKE/A-$br.err" || rc=$?
  echo "A $br exit=$rc :: $(head -1 "$SMOKE/A-$br.out")"
  grep '^\[assay-differential\] ' "$SMOKE/A-$br.err" | sed 's/^\[assay-differential\] //' > "$SMOKE/A-$br.diff.jsonl"
  echo "  audit lines: $(wc -l < "$SMOKE/A-$br.diff.jsonl")"
  jq -c '{state, verdict, anchored, counts}' "$SMOKE/A-$br.diff.jsonl"
  jq -c '.records[] | {name, partition, input, status, why, base_obs, head_obs}' "$SMOKE/A-$br.diff.jsonl"
done
jq -r '.verdict + " :: " + .reason' "$SMOKE/A-fix-collateral.cert.json"

# 3. Part B: the term itself, driven directly with the real lane + real Docker on BOTH patches (independent of whether the
#    stochastic invariants already decided Part A). PoC from the production probe.build; no fault injection.
PYTHONPATH="$ENGINE" "$ENGINE/.venv/bin/python" - "$DEMO" "$SMOKE" "$ISSUE" 2> "$SMOKE/B.err" <<'PY'
import json, subprocess, sys
from pathlib import Path
from mini_ork.certify import differential, probe
from mini_ork.certify import image as cert_image, llm as cert_llm
from mini_ork.certify.context import code_context
from mini_ork.runtime import Crucible, RuntimeSpec

repo, smoke, issue = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout
base = git("rev-parse", "base").strip()
tag = cert_image.build_image(repo, base)                       # reuses the image step 1 built
out = {}
for br in ("fix-correct", "fix-collateral"):
    patch = git("diff", "--no-color", "--binary", base, br)
    ctx = code_context(repo, base, patch)
    cert_llm.reset_spend()
    with Crucible(RuntimeSpec(image=tag, workdir="/testbed")) as c:
        poc, why = probe.build(issue, lambda s: c.run_test(s), context=ctx)
        rec = differential.check(poc, issue, patch, runner=c, context=ctx, n=4) if poc else {"state": "no-poc", "reason": why}
    out[br] = {k: rec.get(k) for k in ("state", "verdict", "reason", "anchored", "counts", "records", "suite")}
    out[br]["spent"] = cert_llm.spent()
(smoke / "B.json").write_text(json.dumps(out, indent=2))
print(json.dumps({b: [o["state"], o["verdict"], o["counts"]] for b, o in out.items()}))
PY
jq '."fix-collateral" | {state, verdict, reason, diverged: [.records[] | select(.partition=="preserve" and .status=="diverged") | {name, input, base_obs, head_obs}]}' "$SMOKE/B.json"
jq '."fix-correct" | {state, verdict, anchored, counts, preserve: [.records[] | select(.partition=="preserve") | {input, status, base_obs, head_obs}]}' "$SMOKE/B.json"
```

## Observable evidence

- Ground truth (1b): `base 3 2 5`, `fix-correct 2.5 2 5`, `fix-collateral 2.5 1.5 5.0`. Any other line ⇒ INVALID.
- Control: `fix-correct` → `PROVEN … exit code: 0`, `fix-cheat` → `REFUTED … exit code: 1`, `[assay-differential]` count 0.
  `control fix-collateral` is expected `PROVEN` (exit 0): today's gate passes the fault. Record it either way; REFUTED here
  means an invariant happened to touch odd-length input.
- A, fix-correct: stdout starts `PROVEN`, exit 0, `A-fix-correct.cert.json` `.verdict == "PROVEN"`, exactly one audit line
  with `.state == "equivalent"`, `.anchored == true`, `.counts.preserve_diverged == 0`, `.counts.preserve_agree >= 1`.
- A, fix-collateral: exit 1, `REFUTED`, certificate `.reason` starts `differential: collateral divergence on preserve input`
  and names an odd-length input with `base=` ≠ `head=`; one audit line with `.verdict == "REFUTED"`, `.state == "refuted"`,
  ≥1 record `partition == "preserve"`, `status == "diverged"`. (If control-collateral was REFUTED upstream, A-collateral is
  REFUTED by the same upstream term with 0 audit lines; then Part B carries the term's evidence.)
- B, fix-collateral: `.verdict == "REFUTED"`, `.anchored == true`, ≥1 preserve record `diverged` with non-empty `input`,
  `base_obs` ≠ `head_obs` (e.g. `[1, 2, 3]`: `2` vs `1.5`). B, fix-correct: `.verdict == null`, `.state == "equivalent"`,
  `.counts.preserve_diverged == 0`.

## PASS / FAIL

PASS requires all of: ground truth and control match as above; A-correct PROVEN with one audit line, `equivalent`,
preserve_diverged == 0; A-collateral REFUTED (via `differential:` reason, or upstream as noted); B-collateral REFUTED through a
confirmed preserve divergence carrying input + both observations; B-correct not REFUTED with preserve_diverged == 0.
Headline measurement: control-collateral PROVEN and A-collateral REFUTED via `differential:` = one fault caught that today's
gate passes.

FAIL on any of:
- any `fix-correct` run REFUTED or with preserve_diverged ≥ 1 (a false REFUTED, the precision cost this smoke exists to
  catch; record the diverging input: an even-length list labelled `preserve`, e.g. `[2, 2]` → `2` vs `2.0`, is a prompt defect);
- B-collateral anchored, with ≥1 odd-length preserve input observed on both sides, yet not REFUTED;
- A-correct PROVEN with no audit line (term not wired in the production path);
- the knobs-off control prints an audit line.

INVALID (fix the environment, rerun): preflight STOP fires, ground truth differs, control-correct/cheat are not PROVEN/REFUTED,
Docker or the lane is down. INCONCLUSIVE (rerun once, then report the yield as a measurement finding, not a code defect):
`suite == null` (no admissible suite; read `counts.inadmissible` and B.err), `anchored == false`, B `state == "no-poc"`, or
every preserve record `excluded` (record each `why`).

## Cost and time

Seven oracle-equivalents: 3 control certificates, 2 in Part A, 2 PoC builds + 2 differential checks in Part B. RESULTS.md §1
measured about $0.03 and 1.5–2 min per certificate at MiniMax list price. The term adds 1–2 LLM calls and 2n = 8 sandbox runs
per judged patch (+2 per diverging preserve input for the confirm re-run, so ≤ 14 on fix-collateral). Expected total about
$0.25 (ceiling about $4 on an expensive lane), 20–30 min including one image build (1–3 min); far below `MO_DAILY_BUDGET_USD`.
Artifacts stay in `$SMOKE`; nothing is written to the engine repo.
