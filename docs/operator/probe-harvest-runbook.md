# RSI Tier-1 #3d: OPERATOR RUNBOOK. Generate, review and commit the harvested probes

This is NOT a framework-edit kickoff. It is a data step. The deliverable is 20
to 40 generated files, which an implementer agent should not hand-write and
which would break the five-files-per-kickoff rule. Run it by hand, or through a
thin `mini-ork run` wrapper, after kickoffs -a, -b and -c are merged.

## 1. Generate (in a worktree, never in the main checkout)

```bash
make worktree SLUG=rsi-t1-probe-data OWNS="recipes/code-fix/probes recipes/framework-edit/probes evals/heldout/results"
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/rsi-t1-probe-data
H=/Volumes/docker-ssd/ps/mini-ork/.mini-ork
D=$(date +%F)
.venv/bin/python -m mini_ork.learning.probe_harvest --recipe code-fix --root "$PWD" --home "$H" --report evals/heldout/results/$D-probe-harvest-code-fix.dryrun.json
.venv/bin/python -m mini_ork.learning.probe_harvest --recipe code-fix --root "$PWD" --home "$H" --write --report evals/heldout/results/$D-probe-harvest-code-fix.json
.venv/bin/python -m mini_ork.learning.probe_harvest --recipe framework-edit --root "$PWD" --home "$H" --landing-ref origin/main --write --report evals/heldout/results/$D-probe-harvest-framework-edit.json
```

**What to expect.** These numbers come from simulating the selection on
2026-10-27 data, before git re-validation:

| Recipe | Candidates | New gate probes | Holdout probes | Gate total (with the 2 existing) |
|---|---|---|---|---|
| code-fix | 30 | 12 (7 baseline-failed, 5 solved) | 7 | 14 |
| framework-edit | 13 | at most 7 | 6 | at most 9 |

Re-validation can drop more candidates. When code-fix ends below 10 gate
probes, stop and look at the `validate:*` reasons in the report before
committing.

## 2. Review

- Read the `rejected` list. Every reason should be one of the documented ones.
- Open three of the written probe `.md` files. None may contain a sha, a task id
  or a hidden test path. The framework-edit ones keep their own test path.
- Confirm the shipped tests are still green with the flag off:
  `pytest -q -p no:asyncio tests/unit/test_probe_config.py tests/unit/test_probe_scorer_git_oracle.py tests/unit/test_probe_harvest.py tests/unit/test_probe_harvest_git.py`
- Confirm that with the flag on, the code-fix gate list is at least 12 long:
  `MO_APPLY_PROBE_GIT_ORACLE=1 MO_APPLY_PROBE_MAX_TASKS=50 .venv/bin/python -c "from mini_ork.learning import probe_scorer as ps; ps._ROOT='.'; print(sum(1 for _, a in ps._load_probes('code-fix') if not a))"`

## 3. Commit (stage explicit paths only)

```bash
git add recipes/code-fix/probes/probe-h*.md recipes/code-fix/probes/oracles \
        recipes/framework-edit/probes/probe-h*.md recipes/framework-edit/probes/oracles \
        evals/heldout/results/$D-probe-harvest-*.json
git diff --cached --stat   # only the paths above
git commit -m "data(rsi): harvest git-oracle probes for code-fix and framework-edit"
make worktree-merge SLUG=rsi-t1-probe-data MINI_ORK_TEST_CMD="python3.11 -m pytest -q -p no:asyncio tests/unit/test_probe_config.py tests/unit/test_probe_harvest.py"
```

## 4. Turning the probes on (a separate, explicit decision)

The probes are inert until `MO_APPLY_PROBE_GIT_ORACLE=1` is set. Before setting
it, consider the cost:

| Recipe | Cost per arm run | Runs per evaluation | Cost per evaluation |
|---|---|---|---|
| code-fix | mean $2.40 (median $1.80), mean about 14 min (2026-09-29 held-out baseline, 40 runs) | 14 gate probes at `control_n=3` gives 56 | about $135, about 13 h serial |
| framework-edit | $3–12, 20–60 min (`probe-config.yaml`) | 9 gate probes at `control_n=2` gives 27 | about $80–320 |

1. **Run the null calibration first.** Score an empty patch with
   `probe_score_code`, setting `MO_APPLY_PROBE_GIT_ORACLE=1`,
   `MO_APPLY_PROBE_MAX_TASKS=14`, `MO_APPLY_PROBE_CONTROL_N=1`, and a budget
   that fits. Accept the instrument only when the empty-patch delta is within
   `MO_APPLY_REGRESSION_TOLERANCE`.
2. **Then pair it with the certificate.** Set `MO_APPLY_CERTIFICATE=1`
   (`rsi-t1-certificate`). At n=14, slot 1 needs at least 7 gains with no
   losses.
