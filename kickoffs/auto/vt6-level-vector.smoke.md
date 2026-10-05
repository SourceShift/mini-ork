# VT6 live smoke: level-vector publish gate inside a real `mini-ork run code-fix`

This smoke tests the MERGED feature end to end. It uses real planner, implementer and reviewer lanes, real `pytest` in a real scratch git repo, and the real code-fix test verifier, with `MO_SUITE_ADEQUACY=1` and `MO_LEVEL_VECTOR=1`. The two arms carry the identical `add()` bug and are taken from the vt5 smoke:
- **STRONG**: the suite kills mutants. Expect all required levels PROVEN, `levels_ok: true`, and the publisher commits.
- **WEAK**: the suite only exercises `add`. Expect the vt5 audit to downgrade the green to `adequacy_unverified`, so target is UNVERIFIED and `levels_ok` is false. The publisher must NOT commit, the edit must be kept rather than rolled back, and the status must be `failed`.

Before the WEAK arm, an exit-0 abstention used to count as test-node success and was published. Closing that gap is this smoke's purpose. A $0 deterministic preflight runs first. It measures the publish decision with the knob on and off on the same patched trees.

## 0. Preconditions

```bash
E=/Volumes/docker-ssd/ps/mini-ork-worktrees/vt-smoke-engine-b     # detached engine worktree; .venv -> main checkout venv
H=/Volumes/docker-ssd/ps/mini-ork/.mini-ork                         # LIVE home (secrets, lanes, state.db) — NEVER $E/.mini-ork
PY=$E/.venv/bin/python
git -C $E fetch -q origin && git -C $E checkout -q --detach origin/main
test -f $E/mini_ork/verify/levels.py && grep -q levels_unverified $E/mini_ork/cli/publisher.py \
  && grep -q level_report $E/mini_ork/cli/execute.py && grep -q _green_pass $E/recipes/code-fix/verifiers/test.py && echo ENGINE-OK
test -f $H/config/secrets.local.sh && echo HOME-OK
unset MINI_ORK_RECIPE_ROOT MINI_ORK_SECRETS MO_CODEFIX_REPLAY MO_TEST_BASELINE
```

- Lanes come from `$H/config/agents.yaml`: worker=minimax, planner/reviewer=glm,minimax, judge=minimax.
- If a run dies in dispatch, check `sqlite3 $H/state.db "SELECT provider,model_id,status,error_message FROM llm_calls WHERE run_id='<RID>'"`, point `MO_FALLBACK_CODING` / `MO_FALLBACK_REVIEW` at live lanes, and rerun.
- Do NOT set `MINI_ORK_SECRETS`; the home's `config/secrets.local.sh` resolves by default.

## 1. Scratch targets ($0)

Use `/Volumes/docker-ssd/ps/`, NOT `/tmp` or `/var`: colima only shares `/Volumes/docker-ssd`. The fixture is the vt5 one verbatim.

```bash
S=/Volumes/docker-ssd/ps/vt6-smoke; rm -rf $S && mkdir -p $S/kickoffs
cat > $S/calc.py <<'EOF'
"""Tiny arithmetic helpers: the vt6 level-vector smoke target."""


def add(a, b):
    return a - b


def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def is_even(n):
    return n % 2 == 0


def mean(xs):
    if not xs:
        return 0.0
    return sum(xs) / len(xs)
EOF
cat > $S/test_strong.py <<'EOF'
from calc import add, clamp, is_even, mean


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_clamp():
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
    assert clamp(5, 0, 10) == 5


def test_is_even():
    assert is_even(4) is True
    assert is_even(3) is False


def test_mean():
    assert mean([]) == 0.0
    assert mean([1, 2, 3]) == 2.0
EOF
cat > $S/test_weak.py <<'EOF'
from calc import add, clamp, is_even, mean


def test_add():
    assert add(2, 3) == 5


def test_runs():
    clamp(5, 0, 10)
    is_even(4)
    mean([1, 2])
EOF
for ARM in strong weak strong-ctl weak-ctl-on weak-ctl-off; do
  case $ARM in strong*) SUITE=strong;; *) SUITE=weak;; esac
  R=$S/$ARM; mkdir -p $R/tests; : > $R/conftest.py; cp $S/calc.py $R/calc.py; cp $S/test_$SUITE.py $R/tests/test_calc.py
  git -C $R init -q && git -C $R config user.email smoke@local && git -C $R config user.name smoke
  git -C $R add -A && git -C $R commit -qm "vt6 smoke target ($ARM)"
done
for ARM in strong-ctl weak-ctl-on weak-ctl-off; do perl -pi -e 's/return a - b/return a \+ b/' $S/$ARM/calc.py; done   # hand fix, uncommitted
for ARM in strong weak; do (cd $S/$ARM && $PY -m pytest -q -p no:cacheprovider tests/ >/dev/null; echo "$ARM HEAD rc=$? (expect 1)"); done
cat > $S/kickoffs/template.md <<'EOF'
# Fix add() in calc.py: it subtracts instead of adding

## Problem

`add(a, b)` in `@R@/calc.py` returns `a - b`; it must return the sum `a + b`. `tests/test_calc.py::test_add` fails on the current code.

## Files in scope (touch ONLY these)

- `@R@/calc.py` — the buggy function lives here

Do NOT modify any other file. Do NOT add or edit tests.

## Verification command

- `cd @R@ && @PY@ -m pytest -q -p no:cacheprovider tests/`

## Success criteria

- `add(2, 3) == 5` and `add(-1, 1) == 0`; the verification command exits 0; only calc.py changes.
EOF
for ARM in strong weak; do sed "s#@R@#$S/$ARM#g; s#@PY@#$PY#g" $S/kickoffs/template.md > $S/kickoffs/$ARM.md; done
```

## 2. $0 deterministic preflight: real producers, real level derivation, real publisher decision, no LLM

Each control arm runs the production pieces in production order:
1. `_write_implementer_summary`, which produces the applies evidence;
2. the code-fix verifier, with its stdout+stderr merged the same way the executor captures `verifier_test.json`;
3. `level_report`;
4. `publisher_node`.

`review-verdict.json` stands in for the glm reviewer's APPROVE. It is the only seeded file, and the level vector never reads it.

```bash
export MINI_ORK_TEST_CMD="$PY -m pytest -p no:cacheprovider"    # no -q: the replay parses per-test -v lines
for ARM in strong-ctl weak-ctl-on weak-ctl-off; do
  K=1; [ $ARM = weak-ctl-off ] && K=0
  R=$S/$ARM; RD=$S/ctl-home/runs/ctl-$ARM; mkdir -p $RD
  PYTHONPATH=$E $PY -c 'import sys; from mini_ork.cli.execute import _write_implementer_summary as w; print("files:", w(sys.argv[1], sys.argv[2], "impl.log"))' $RD $R
  (cd $R && MO_SUITE_ADEQUACY=1 MINI_ORK_HOME=$S/ctl-home MINI_ORK_RUN_ID=ctl-$ARM PYTHONPATH=$E PYTHONDONTWRITEBYTECODE=1 \
     $PY $E/recipes/code-fix/verifiers/test.py > $RD/verifier_test.json 2>&1)
  echo '{"verdict": "approve"}' > $RD/review-verdict.json
  PYTHONPATH=$E $PY -c 'import json,sys; from mini_ork.verify import levels; r=levels.level_report(sys.argv[1],"code_fix"); print(sys.argv[2], json.dumps({k:r[k] for k in ("levels","levels_ok","levels_decision")}))' $RD $ARM
  (cd $R && MO_LEVEL_VECTOR=$K MO_TARGET_CWD=$R PYTHONPATH=$E $PY -c 'import sys; from mini_ork.cli.publisher import publisher_node; print(sys.argv[3], "->", publisher_node(sys.argv[1], sys.argv[2], "", "ctl", "code-fix", "code_fix"))' $E $RD $ARM)
  echo "$ARM knob=$K commits=$(git -C $R rev-list --count HEAD) dirty=[$(git -C $R status --porcelain calc.py)]"
done
```

These are the expected rows. Anything else is a FAIL: stop and do not spend on step 3.

| arm | knob | `levels` (applies/executes/target/preserve/contract) | `levels_ok` / decision | publisher returns | commits / dirty |
|---|---|---|---|---|---|
| strong-ctl | 1 | PROVEN/PROVEN/PROVEN/PROVEN/n/a | true / publish | `(0, 'done')` after `[ok] publisher: level gate pass` | 2 / `[]` |
| weak-ctl-on | 1 | PROVEN/PROVEN/UNVERIFIED/UNVERIFIED/n/a | false / abstain | `(0, 'levels_unverified')` after `[ABSTAIN] publisher: level vector not proven` | 1 / `[ M calc.py]` |
| weak-ctl-off | 0 | same as weak-ctl-on | false / abstain | `(0, 'done')`: today's gap, the abstention is committed | 2 / `[]` |

Also check `tail -n 1 $S/ctl-home/runs/ctl-weak-ctl-on/verifier_test.json`. It must contain `"pass":false`, `"adequacy_unverified":true` and a non-empty `replay.overlap`.

Record the delta: with the knob on, 1 abstention publish is removed (weak) and 0 good publishes are withheld (strong).

## 3. Paid real runs (sequential)

```bash
export MINI_ORK_ROOT=$E MINI_ORK_ENGINE_ROOT=$E MINI_ORK_HOME=$H \
  MINI_ORK_PROFILE_GATE=0 MINI_ORK_NONINTERACTIVE=1 \
  MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000 \
  MO_LEVEL_VECTOR=1 MO_SUITE_ADEQUACY=1 MINI_ORK_TEST_CMD="$PY -m pytest -p no:cacheprovider"
TS=$(date +%Y%m%d%H%M%S)
for ARM in strong weak; do
  RID=vt6-$ARM-$TS
  (cd $S/$ARM && MO_TARGET_CWD=$S/$ARM MINI_ORK_RUN_ID=$RID \
     $E/bin/mini-ork run --json code-fix $S/kickoffs/$ARM.md > $S/$ARM.result.json 2> $S/$ARM.log)
  echo "$ARM rc=$? run=$RID"
done
```

## 4. Evidence

Each run dir is `$H/runs/<RID>/`. In it:
- `verdict.json` is the run-level verdict. If a recipe-owned `verdict.json` exists, the executor writes `run-verdict.json` instead, so read that.
- `verifier_test.json` holds stderr lines first, with the JSON payload as the LAST line.
- `execute.log` is execute's stdout, which carries the publisher gate line.

```bash
for ARM in strong weak; do RID=vt6-$ARM-$TS; D=$H/runs/$RID; V=$D/verdict.json; [ -f $D/run-verdict.json ] && V=$D/run-verdict.json
  echo "== $ARM $RID"
  jq -c '{verdict, failed_nodes, levels, levels_required, levels_ok, levels_decision}' $V
  jq -r '.levels_reasons | to_entries[] | "   \(.key): \(.value)"' $V
  tail -n 1 $D/verifier_test.json | jq -c '{pass, adequacy_unverified, replay_unverified, post_rc, overlap:.replay.overlap, adequacy:.suite_adequacy.verdict, score:.suite_adequacy.score}'
  cat $D/review-verdict.json 2>/dev/null; echo
  grep -E '\[(ok|ABSTAIN|BLOCK)\] publisher: level' $D/execute.log || echo "publisher gate line: NONE (publisher did not run)"
  sqlite3 $H/state.db "SELECT status, round(cost_usd,3) FROM task_runs WHERE id='$RID'"
  echo "run commits: $(git -C $S/$ARM log --format=%s | grep -c "\[run $RID\]")"   # publisher commit subject = mini-ork(code-fix): … [run <RID>]
  git -C $S/$ARM status --porcelain
done
```

## PASS / FAIL

PASS requires ALL of the following.

**Step 2:** the rows match the table exactly.

**STRONG:**
- `verifier_test.json` last line: `pass == true`, `adequacy == "ADEQUATE"`, and `overlap` contains `test_add`.
- `verdict.json`:
  - `levels` == `{"applies":"PROVEN","executes":"PROVEN","target":"PROVEN","preserve":"PROVEN","contract":"n/a"}`;
  - `levels_required` == `["applies","executes","target","preserve"]`;
  - `levels_ok == true`, `levels_decision == "publish"`.
- `execute.log` contains `[ok] publisher: level gate pass`.
- `run commits == 1`, and that commit touches `calc.py`.
- Status `published`.

**WEAK** (this is the gap closing):
- `verifier_test.json` last line: `pass == false`, `adequacy_unverified == true`, `adequacy == "INADEQUATE"`, and `overlap` contains `test_add`. Without the audit, this run would have passed.
- `verdict.json`:
  - `failed_nodes == 0`. The test node SUCCEEDED on the abstention and the publisher ran; this is the old leak path.
  - `levels.target == "UNVERIFIED"`, `levels.preserve == "UNVERIFIED"`, `levels.applies == "PROVEN"`;
  - `levels_ok == false`, `levels_decision == "abstain"`.
- `execute.log` contains `[ABSTAIN] publisher: level vector not proven`.
- `run commits == 0`.
- `git status --porcelain` shows ` M calc.py`: the edit is kept, not rolled back.
- Status `failed`.

**INCONCLUSIVE.** Rerun that arm once; none of these is a feature failure:
- The implementer did not land a fix: `post_rc != 0`, or `overlap` is empty or missing.
- The reviewer rejected: `review-verdict.json` is not pass/approve, `failed_nodes > 0`, and there is no publisher gate line. The gate never ran.
- STRONG adequacy is not ADEQUATE. That is a vt5 false-reject; record it for vt5.
- A lane or dispatch failure.

**FAIL:** anything else. For example:
- WEAK has `run commits >= 1` or status `published`;
- `levels` is missing from `verdict.json` with the knob on;
- STRONG has `levels_ok == false` although its `verifier_test.json` shows `pass:true` + overlap + ADEQUATE;
- WEAK `calc.py` was reverted while `failed_nodes == 0` (the abstention triggered a rollback);
- any level is PROVEN while its evidence file is missing.

## Cost / time

- Step 2: $0, about 1–2 min. Each audit is at most 14 pytest runs of a suite under 1 s.
- Each paid run (glm planner + minimax implementer + glm reviewer + judge, on a 20-line file): about $0.3–1.5 and 5–15 min.
- The `MO_AUTO_APPLY` sweep adds at most `MO_APPLY_PROBE_BUDGET_USD=2.0` per run, and its probe arms inherit both knobs.
- Worst case: about $7 and 45 min wall time.
- Judge commits by the `[run <RID>]` subject only, so sweep activity in the scratch repo cannot fake a publish.
- Cleanup after recording: `rm -rf /Volumes/docker-ssd/ps/vt6-smoke`. The preflight writes only under `$S/ctl-home`, never the live home.
