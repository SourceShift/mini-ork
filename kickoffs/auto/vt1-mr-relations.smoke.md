# Live smoke: vt1-mr-relations (G10-T01 metamorphic relations in the Assay oracle)

Run this AFTER `vt1-mr-relations` is merged to main. It is a real run: real model lane (`MO_CERTIFY_MODEL`, default
`minimax`), real Docker execution, a real git repo. It is not a unit test.

## Target, and why this one

The certify demo repo built by `/Volumes/docker-ssd/ps/mini-ork/examples/certify-demo/demo.sh`: a pip-installable `stats`
package whose `median()` returns the upper-middle value for even-length input. It has three branches:

- `fix-correct` (the GOLD fix): averages the two middle values when `len` is even.
- `fix-cheat` (the deliberately special-cased patch): `if xs == [1, 2, 3, 4]: return 2.5`, nothing else changed. It
  passes the reporter's example and is wrong on every other even-length list. Relations that catch it: permutation
  (`SOURCE=[1,2,3,4]`, `FOLLOWUP=SOURCE[::-1]`, R: equal medians) and translation (`FOLLOWUP=[x+1 for x in SOURCE]`,
  R: `median(F) == median(S)+1`). Both hold on base (3 == 3; 4 == 3+1) and on gold (2.5 == 2.5; 3.5 == 2.5+1). Both
  break on the cheat (3 != 2.5; 4 != 2.5+1), so they come out `violated` (attributed to the patch). Negation
  (`FOLLOWUP=[-x for x in SOURCE]`, R: `median(F) == -median(S)`) fails on base (-2 != -3) and holds on gold, so it is a
  `repaired` relation. That is what lets the gold fix reach a rescue PROVEN.
- Issue text (quotes the expected value, so PoC+ grounding succeeds and the oracle reaches Terms 3 and 4):
  `median([1, 2, 3, 4]) returns 3, but the median of an even-length list should be the average of the two middle values, so it should return 2.5.`

Why not a mined held-out task (`evals/heldout/mined/manifest.json`): those tasks are mini-ork's own commits. Their
`problem_statement` is a commit message that rarely quotes an expected value, so `probe.ground` abstains and the run ends
UNVERIFIED before relations ever run. The runtime image would also be the whole of mini-ork.

## Runtime

`mini-ork certify` needs Docker. `_decide_image` runs `docker build` (python:3.11-slim plus `pip install -e .`, which
needs network at build time). `Crucible` then uses the `docker` backend when the `verifiers` runtime is installed and
`docker-cli` (`docker run -d … sleep 3600`) otherwise. There is no subprocess backend on this path, and no bind mounts.
The Docker daemon is the local colima instance.

## Commands

```bash
# 0. Environment and preflight
export ENGINE=/Volumes/docker-ssd/ps/mini-ork
export MINI_ORK_ROOT=$ENGINE MINI_ORK_ENGINE_ROOT=$ENGINE MINI_ORK_HOME=$ENGINE/.mini-ork
export MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000 MO_CERTIFY_MODEL=minimax
export PATH="$ENGINE/bin:$PATH"
SMOKE=/tmp/vt1-mr-smoke-$(date +%Y%m%d-%H%M%S); DEMO=$SMOKE/stats-demo; mkdir -p "$SMOKE"
ISSUE='median([1, 2, 3, 4]) returns 3, but the median of an even-length list should be the average of the two middle values, so it should return 2.5.'
git -C "$ENGINE" log --oneline -1 -- mini_ork/certify/relations.py   # empty => not merged: STOP
docker info --format '{{.ServerVersion}}'                            # error => start colima first
grep -q MO_ASSAY_RELATIONS "$ENGINE/mini_ork/certify/relations.py" || echo "STOP: knobs missing"

# 1. Build the demo repo AND run the knobs-OFF control (demo.sh certifies both branches with today's oracle)
env -u MO_ASSAY_RELATIONS -u MO_ASSAY_RELATIONS_RESCUE -u MO_ASSAY_RELATIONS_K \
  bash "$ENGINE/examples/certify-demo/demo.sh" "$DEMO" > "$SMOKE/control.log" 2>&1
grep -E '^(PROVEN|REFUTED|UNVERIFIED)|exit code' "$SMOKE/control.log"; grep -c '^\[assay-relations\]' "$SMOKE/control.log"

# 2. Part A: production CLI, default (veto) mode. Relations on, rescue off.
export MO_ASSAY_RELATIONS=1 MO_ASSAY_RELATIONS_K=3; unset MO_ASSAY_RELATIONS_RESCUE
for br in fix-correct fix-cheat; do
  rc=0
  mini-ork certify --repo "$DEMO" --base base --head "$br" --issue "$ISSUE" --out "$SMOKE/A-$br.cert.json" \
    > "$SMOKE/A-$br.out" 2> "$SMOKE/A-$br.err" || rc=$?
  echo "A $br exit=$rc :: $(head -1 "$SMOKE/A-$br.out")"
  grep '^\[assay-relations\] ' "$SMOKE/A-$br.err" | sed 's/^\[assay-relations\] //' > "$SMOKE/A-$br.relations.jsonl"
  echo "  audit lines: $(wc -l < "$SMOKE/A-$br.relations.jsonl")"
done
jq -c '{mode, verdict, counts}' "$SMOKE/A-fix-correct.relations.jsonl"
jq -c '.records[] | {name, status, transform, relation, source, followup}' "$SMOKE/A-fix-correct.relations.jsonl"

# 3. Part B: production oracle.judge, RESCUE mode, with the invariant generator suppressed. This is a DECLARED
#    fault injection, and its only purpose is to reach the no-informative-invariant branch deterministically so the
#    violation path runs. The probe prompt and the relations prompt still go to the real lane; execution is real Docker.
export MO_ASSAY_RELATIONS=1 MO_ASSAY_RELATIONS_RESCUE=1 MO_ASSAY_RELATIONS_K=3
PYTHONPATH="$ENGINE" "$ENGINE/.venv/bin/python" - "$DEMO" "$SMOKE" "$ISSUE" 2> "$SMOKE/B.err" <<'PY'
import json, re, subprocess, sys
from pathlib import Path
from mini_ork.certify import judge
from mini_ork.certify import image as cert_image, llm as cert_llm
from mini_ork.certify.context import code_context
from mini_ork.runtime import Crucible, RuntimeSpec

repo, smoke, issue = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout
base = git("rev-parse", "base").strip()
tag = cert_image.build_image(repo, base)          # reuses the image step 1 built

def no_invariants(prompt):                          # suppress ONLY the invariant generator
    if re.search(r"Write\s+\d+\s+pytest tests", prompt):
        return ""
    return cert_llm.default_dispatch(prompt)

out = {}
for br in ("fix-correct", "fix-cheat"):
    patch = git("diff", "--no-color", "--binary", base, br)
    cert_llm.reset_spend()
    with Crucible(RuntimeSpec(image=tag, workdir="/testbed")) as c:
        v = judge(issue, patch, runner=c, mr_n=4, dispatch=no_invariants,
                  context=code_context(repo, base, patch))
    rel = (v.detail or {}).get("relations") or {}
    out[br] = {"verdict": v.verdict, "reason": v.reason, "mode": rel.get("mode"), "counts": rel.get("counts"),
               "spent": cert_llm.spent(),
               "records": [{k: r.get(k) for k in ("name", "status", "why", "transform", "relation", "source",
                                                  "followup", "pair", "on_patch", "on_base", "repaired")}
                           for r in rel.get("records", [])]}
(smoke / "B.json").write_text(json.dumps(out, indent=2))
print(json.dumps({b: [o["verdict"], o["counts"]] for b, o in out.items()}))
PY
jq '."fix-cheat" | {verdict, reason, mode, violated: [.records[] | select(.status=="violated") | {name, relation, pair}]}' "$SMOKE/B.json"
jq '."fix-correct" | {verdict, reason, counts}' "$SMOKE/B.json"
```

## Observable evidence

- Control (`control.log`): `fix-correct` gives `PROVEN … exit code: 0`; `fix-cheat` gives `REFUTED … exit code: 1`.
  The `[assay-relations]` count is `0`, which shows that knobs-off behaviour matches today.
- A, fix-correct: the stdout line starts with `PROVEN`, exit is 0, the certificate (`A-fix-correct.cert.json`) has
  `.verdict == "PROVEN"`, and the stderr file has exactly one `[assay-relations]` line with `.mode == "veto"`,
  `.counts.violated == 0` and `.counts.held >= 1`. Each record shows `transform`, `relation`, `source`, `followup`.
- A, fix-cheat: exit 1 and `REFUTED` (invariants refute it before relations run). There are 0 audit lines, because
  relations only run on a would-be PROVEN. If invariants happen to miss and an audit line does appear, it must show
  `.verdict == "REFUTED"` with `.counts.violated >= 1`, and the certificate reason must start with `metamorphic relation`.
- B, fix-cheat (`B.json`): `.verdict == "REFUTED"`, `.mode == "rescue"`, and at least one record with
  `status == "violated"`, `on_base == "passed"`, `on_patch == "failed"` and non-empty `pair.source` / `pair.followup`.
  The reason names that relation and its pair.
- B, fix-correct: `.verdict` is `PROVEN` (reason contains `relative to`, `.counts.repaired >= 1`) or `UNVERIFIED` (no
  relation that base violates was generated). In both cases `.counts.violated == 0`.

## PASS / FAIL

PASS requires all of the following: the control matches as above; A-correct is PROVEN with one audit line, violated == 0
and held >= 1; A-cheat is REFUTED; B-cheat is REFUTED through a `violated` record that carries the pair; B-correct is not
REFUTED and has violated == 0.

FAIL on any of these:
- either `fix-correct` run is REFUTED (a false REFUTED, which is the precision cost this smoke exists to catch);
- `fix-cheat` ends PROVEN in A or B;
- A-correct has no audit line;
- the knobs-off control prints an audit line.

INVALID (fix the environment, then rerun): the control verdicts are not PROVEN/REFUTED, Docker or the lane is down, or
the preflight STOP fires. INCONCLUSIVE (rerun once, then report the yield as a measurement finding, not a code defect):
`counts.held == 0` in A-correct, or zero admissible relations in B-cheat. Record `records[].why` in both cases.

## Cost and time

There are six oracle runs: 2 control, 2 in Part A, 2 in Part B. RESULTS.md §1 measured about $0.03 and 1.5–2 min per
certificate at MiniMax list price (demo.sh's header allows $0.10–0.60 on other lanes). Relations add 1–2 LLM calls and
up to 2k = 6 sandbox runs per judged patch. Expected total is about $0.25 (ceiling about $4 on an expensive lane) and
15–25 min, including one image build (1–3 min). That is far below `MO_DAILY_BUDGET_USD`. Artifacts stay in `$SMOKE`, and
nothing is written to the engine repo.
