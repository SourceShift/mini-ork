# E2 probe-set growth from failures

## Goal

Two probes per task class cannot support a two-arm comparison; the delta over n=2
is indistinguishable from noise, so every accept decision taken on it would be
theater. Grow the measured task classes to at least twelve probes each, drawn from
failures mini-ork has actually had, so the probes are hard by construction and
cannot saturate.

### Mechanisms (exact spec)

`harvest_probes(task_class, *, max_new=10)` in
`mini_ork/learning/benchmark_suite.py`. It reads `execution_traces`, clusters
recent failures by `mini_ork/learning/failure_classifier.py` output, and for each
cluster emits one probe kickoff plus a fixture directory into
`recipes/<task_class>/probes/` and `recipes/<task_class>/probes/fixtures/<stem>/`.
It dedups against existing probes with the similarity machinery in
`mini_ork/similarity.py`; a candidate above the dedup threshold is dropped. The
run is capped at `max_new`.

Admissibility: a probe whose fixture the current tree already passes measures
nothing. Before a generated probe is written, run it against the unpatched tree;
if the baseline outcome is a pass, discard the probe and record the rejection.
This filter is what keeps the probe set discriminating.

Fixtures are copied from the failing run's `MO_TARGET_CWD`, never reconstructed
from a model-authored diff. A model-authored fixture is a model-authored test,
which is the failure mode the whole roadmap exists to avoid.

### Tests

New `tests/unit/test_harvest_probes.py` over a seeded `execution_traces` with
synthetic failure rows. Assert: two distinct failure classes produce two probes; a
duplicate of an existing probe is dropped; a probe whose seeded baseline outcome
is a pass is discarded and counted in the rejection tally; `max_new` is honoured.

### Files in scope

- mini_ork/learning/benchmark_suite.py
- tests/unit/test_harvest_probes.py
- recipes/code-fix/probes/ (new probes only)

### Self-application measurement

The improvement here is probe coverage, and it is measurable directly. Baseline is
the count of probes the current tree passes. Accepted iff every newly added probe
FAILS on the baseline tree, and the total probe count for `code-fix` reaches at
least twelve.

Instrument: `mini_ork/learning/probe_scorer.py` with an empty patch, which by
Epic 1's null calibration yields the baseline outcome per probe.

Evidence artifact: `${MINI_ORK_RUN_DIR}/probe-harvest.json` with one row per
candidate probe (`{"stem": ..., "baseline_outcome": <float>, "admitted": <bool>}`)
and a final `{"n_probes": <int>, "n_admitted": <int>}`.

### Done When

- `code-fix` carries at least twelve frozen probes, each failing on the baseline.
- Every rejected candidate is recorded with its reason.
- The dedup threshold is a named constant, not an inline literal.

## Scope Hint

- `mini_ork/learning/benchmark_suite.py`
- `mini_ork/learning/failure_classifier.py`
- `mini_ork/similarity.py`
- `tests/unit/test_harvest_probes.py`
- `mini_ork/learning/probe_scorer.py`

## Verification commands

Run each from the worktree root. Use `python3.11` explicitly — the ambient
`python3` here is 3.9 and dies at collection.

```bash
# 1. unit layer for the new harvester
python3.11 -m pytest tests/unit/test_harvest_probes.py -q

# 2. the touched modules still compile and the existing scorer is unbroken
python3.11 -m pytest tests/unit/test_probe_scorer_code_arm.py -q
python3.11 -m py_compile mini_ork/learning/benchmark_suite.py \
  mini_ork/learning/failure_classifier.py mini_ork/similarity.py \
  mini_ork/learning/probe_scorer.py tests/unit/test_harvest_probes.py

# 3. THE GATE — probe coverage. Twelve probes for code-fix, and every fixture
#    present. Per-probe baseline outcomes are recorded by the harvester run
#    itself and checked in step 4; this step only asserts coverage exists.
python3.11 -c "
import glob, os
probes = sorted(glob.glob('recipes/code-fix/probes/*.md'))
print('probe count =', len(probes))
assert len(probes) >= 12, 'FAIL: fewer than twelve probes (%d)' % len(probes)
missing = [p for p in probes
           if not os.path.isdir(os.path.join('recipes/code-fix/probes/fixtures',
                                             os.path.splitext(os.path.basename(p))[0]))]
assert not missing, 'FAIL: probes with no fixture dir: %r' % missing
print('all probes have fixtures')
"

# 4. the epic's evidence artifact
python3.11 -c "
import json, os
p = os.path.join(os.environ.get('MINI_ORK_RUN_DIR', ''), 'probe-harvest.json')
assert os.path.isfile(p), 'MISSING evidence artifact: ' + p
d = json.load(open(p))
rows = d if isinstance(d, list) else d.get('rows', [])
bad = [r for r in rows if r.get('admitted') and r.get('baseline_outcome', 0.0) > 0.0]
assert not bad, 'FAIL: admitted probes the baseline already passes: %r' % bad
print('admitted:', sum(1 for r in rows if r.get('admitted')), 'of', len(rows))
"
```

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.

_Auto-generated by `mini-ork epics split` from EPIC-true-self-improvement.md._
