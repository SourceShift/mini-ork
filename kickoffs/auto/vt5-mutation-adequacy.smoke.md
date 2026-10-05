# VT5 live smoke: suite-adequacy audit inside a real `mini-ork run code-fix`

This smoke tests the MERGED feature end to end. It uses a real planner/implementer/reviewer (LLM lanes), real `pytest` execution in a real scratch git repo, and the real code-fix test verifier with `MO_SUITE_ADEQUACY=1`. There are two arms with the identical bug: STRONG (the suite kills mutants) and WEAK (the suite exercises the bug but asserts nothing else).
Expected: STRONG → `ADEQUATE` + normal pass. WEAK → `INADEQUATE`, and its green is downgraded to UNVERIFIED (`pass:false`).
A $0 deterministic preflight runs first. It measures the false-pass delta (knob off vs on on the same patched tree) before any LLM money is spent.

## 0. Preconditions

```bash
E=/Volumes/docker-ssd/ps/mini-ork; PY=$E/.venv/bin/python
git -C $E pull --ff-only     # make worktree-merge does NOT advance the local main checkout that MINI_ORK_ROOT runs
test -f $E/mini_ork/gates/suite_adequacy.py && grep -q '_green_pass' $E/recipes/code-fix/verifiers/test.py && echo ENGINE-OK
```

Lanes come from `$E/.mini-ork/config/agents.yaml`: worker=minimax, planner/reviewer=glm, judge=minimax. If a run dies in dispatch, run `sqlite3 $E/.mini-ork/state.db "SELECT provider,model_id,status,error_message FROM llm_calls WHERE run_id='<RID>'"`, set `MO_FALLBACK_CODING` / `MO_FALLBACK_REVIEW` to live lanes, and rerun. Do NOT set `MINI_ORK_SECRETS`; the default resolves the home's `config/secrets.local.sh`.

## 1. Scratch targets ($0)

Use `/Volumes/docker-ssd/ps/`, NOT `/tmp` or `/var`: colima only shares `/Volumes/docker-ssd`.

```bash
S=/Volumes/docker-ssd/ps/vt5-smoke; rm -rf $S && mkdir -p $S/kickoffs
cat > $S/calc.py <<'EOF'
"""Tiny arithmetic helpers: the vt5 suite-adequacy smoke target."""


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
for ARM in strong weak strong-ctl weak-ctl; do
  R=$S/$ARM; mkdir -p $R/tests; : > $R/conftest.py; cp $S/calc.py $R/calc.py; cp $S/test_${ARM%-ctl}.py $R/tests/test_calc.py
  git -C $R init -q && git -C $R config user.email smoke@local && git -C $R config user.name smoke
  git -C $R add -A && git -C $R commit -qm "vt5 smoke target ($ARM)"
done
for ARM in strong-ctl weak-ctl; do perl -pi -e 's/return a - b/return a \+ b/' $S/$ARM/calc.py; done   # hand fix, uncommitted
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

The STRONG suite kills every one of the 22 mutation sites in the fixed `calc.py` (hand-checked). The WEAK suite can kill at most the 2 `add` mutants, so the expected score is ≤ 2/12 ≈ 0.167.

## 2. $0 deterministic preflight: false-pass delta, real verifier script, no LLM

```bash
export MINI_ORK_TEST_CMD="$PY -m pytest -p no:cacheprovider"    # no -q: the replay parses per-test -v lines
for ARM in strong-ctl weak-ctl; do for K in 0 1; do
  (cd $S/$ARM && MO_SUITE_ADEQUACY=$K MINI_ORK_HOME=$S/ctl-home MINI_ORK_RUN_ID=ctl-$ARM-$K PYTHONPATH=$E \
     $PY $E/recipes/code-fix/verifiers/test.py 2>/dev/null | tail -n 1 \
   | jq -c --arg a $ARM --arg k $K '{arm:$a, knob:$k, pass, v:.suite_adequacy.verdict, score:.suite_adequacy.score, overlap:(.replay.overlap|length), summary:.error_summary}')
done; done
```

Expected rows (anything else = FAIL; stop and do not spend on step 3):
- `strong-ctl 0`: `pass:true`, `v:null`.
- `strong-ctl 1`: `pass:true`, `v:"ADEQUATE"`, `score:1`.
- `weak-ctl 0`: `pass:true`, `v:null`. This is today's false pass.
- `weak-ctl 1`: `pass:false`, `v:"INADEQUATE"`, `overlap>=1`, summary starts with `unverified: suite-inadequate`.

Record the delta: 1 false pass removed (weak), 0 false rejects (strong).

## 3. Paid real runs (sequential)

```bash
export MINI_ORK_ROOT=$E MINI_ORK_ENGINE_ROOT=$E MINI_ORK_HOME=$E/.mini-ork \
  MINI_ORK_PROFILE_GATE=0 MINI_ORK_NONINTERACTIVE=1 \
  MO_APPLY_ENABLED=1 MO_AUTO_APPLY=1 MO_DAILY_BUDGET_USD=2000 \
  MO_SUITE_ADEQUACY=1 MINI_ORK_TEST_CMD="$PY -m pytest -p no:cacheprovider"
TS=$(date +%Y%m%d%H%M%S)
for ARM in strong weak; do
  RID=vt5-$ARM-$TS
  (cd $S/$ARM && MO_TARGET_CWD=$S/$ARM MINI_ORK_RUN_ID=$RID \
     $E/bin/mini-ork run --json code-fix $S/kickoffs/$ARM.md > $S/$ARM.result.json 2> $S/$ARM.log)
  echo "$ARM rc=$? run=$RID"
done
```

## 4. Evidence

The run dir is `$E/.mini-ork/runs/<RID>/`.
- `verifier_test.json` is the executor's copy of the verifier evidence: stderr lines (`[test] running: …`) come first, and the JSON payload is the LAST line. Never `jq` the whole file.
- `suite_adequacy.json` is the full audit report.

```bash
for ARM in strong weak; do RID=vt5-$ARM-$TS; D=$E/.mini-ork/runs/$RID; echo "== $ARM $RID"
  tail -n 1 $D/verifier_test.json | jq '{pass, adequacy_unverified, error_summary, post_rc, overlap:.replay.overlap,
    verdict:.suite_adequacy.verdict, score:.suite_adequacy.score, k:.suite_adequacy.killed, s:.suite_adequacy.survived,
    i:.suite_adequacy.invalid, files:.suite_adequacy.files,
    survivors:[.suite_adequacy.survivors[]? | "\(.file):\(.line) \(.operator) \(.original) -> \(.mutated)"]}'
  jq '{verdict, reason, score, total, baseline_rc, canary_detected, mutant_timeout_s}' $D/suite_adequacy.json
  sqlite3 $E/.mini-ork/state.db "SELECT status, round(cost_usd,3) FROM task_runs WHERE id='$RID'"
  git -C $S/$ARM status --porcelain; git -C $S/$ARM log --oneline | head -3
done
ls "${TMPDIR:-/tmp}" | grep -c '^mo-suite-adequacy-'     # expect 0: audit copies are always removed
```

## PASS / FAIL

PASS requires ALL of the following:

- Step 2 rows exactly as expected.
- STRONG, last line of `verifier_test.json`:
  - `pass == true`;
  - `suite_adequacy.verdict == "ADEQUATE"`, `score >= 0.6` (expect 1.0);
  - `files == ["calc.py"]`;
  - `replay.overlap` contains `test_add`.
- STRONG, `suite_adequacy.json`: `canary_detected == true`, `baseline_rc == 0`.
- WEAK, last line of `verifier_test.json`:
  - `pass == false`, `adequacy_unverified == true`;
  - `error_summary` starts with `unverified: suite-inadequate`;
  - `verdict == "INADEQUATE"`, `score < 0.6`;
  - at least one survivor, each `calc.py:<line> <operator>`;
  - `replay.overlap` contains `test_add`, which proves the run would have passed without the audit.
- No `mo-suite-adequacy-*` temp dir is left, and neither scratch repo shows audit artefacts in `git status`.

INCONCLUSIVE (rerun that arm once; this is not a feature failure):
- `post_rc != 0`, or `replay.overlap` is empty or missing. The implementer did not land a fix the suite exercises, so the audit never ran.
- A lane/dispatch failure.

FAIL: anything else. Examples:
- STRONG not ADEQUATE (a false reject);
- WEAK `pass == true`;
- `suite_adequacy` missing with the knob on;
- `suite_adequacy.json` absent;
- leftover temp copies.

Not a criterion: the WEAK run's final status. The downgrade is an abstention (exit 0), and `_run_verifier_ref` falls back to the script rc because the evidence starts with stderr lines. So the test node succeeds and the glm reviewer still decides whether to publish. Record a `published` WEAK run as an observation for the follow-up "publisher must not publish/certify over a non-pass test verdict"; it does not fail this smoke.

## Cost / time

- Step 2: $0, about 1 min. Each audit is at most 14 pytest runs of a <1 s suite (about 10–20 s).
- Each paid run (glm planner + minimax implementer + glm reviewer + judge on a 20-line file): about $0.3–1.5 and 5–15 min. Recent real-repo code-fix runs cost $1.7–5.5.
- `MO_AUTO_APPLY=1` + `MO_APPLY_ENABLED=1` add a post-run code_fix auto-apply sweep: at most 1 target and 2 probe arms, capped at `MO_APPLY_PROBE_BUDGET_USD=2.0` per run. The probe arms inherit `MO_SUITE_ADEQUACY=1`.
- Worst case total: about $7, at most about 45 min wall time. Drop `MO_AUTO_APPLY` to skip the sweep; this feature does not need it.
- Cleanup after recording the evidence: `rm -rf /Volumes/docker-ssd/ps/vt5-smoke`.
