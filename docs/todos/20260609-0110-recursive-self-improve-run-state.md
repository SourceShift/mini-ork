# Recursive self-improve loop — live run state

**Initial launch:** 2026-06-08 ~22:52 local → caught two bugs on iter 1, killed at iter 2 start. See "Bugs caught live" below.
**Relaunch:** 2026-06-09 ~07:47 local
**Soft cap:** 3h
**Hard cap:** 5h
**Log:** `/tmp/mini-ork-self-improve-LATEST.log` (truncated on relaunch)
**Runner cwd:** `/Users/admin/ps/mini-ork` (PATH resolution)
**Worktree base HEAD:** `8e0c7e7 fix(self-improve): use mini-ork run, gate verifier chain, cap test cost`
**Monitor task id:** `bdnlbpf05` (persistent — fires on every iter / cap / commit / err / converged event)

## Bugs caught live on iter 1 → fixed in `8e0c7e7`

| Bug | Symptom | Fix |
|---|---|---|
| Wrong execute invocation | `mini-ork-execute --recipe X --kickoff Y` errors `Unknown flag: --recipe`; every iter exec_rc=2; DAG never dispatched | Switch to documented lifecycle entrypoint `bin/mini-ork run <recipe> <kickoff>` |
| Verifier chain ran on failed iters | Iter 1 burned ~20 min running full self-tests-pass against an un-patched worktree because bottle=0 didn't gate the rest | Short-circuit: bottle fails → skip self-tests-pass + no-regression |
| self-tests-pass too expensive | Walked every `tests/integration/test_*.sh` per iter (~20 min) | Curated fast subset (unit + recipe smokes), MINI_ORK_DRY_RUN=1 forced; override via `MINI_ORK_SELF_IMPROVE_TEST_GLOBS` env |

Smoke regression added: assertion that the runner contains `run recursive-self-improve` and does NOT contain `--recipe recursive-self-improve`. Result: **29 OK / 0 FAIL.**

## Provider credentials (verified pre-launch)

| Lane | Status |
|---|---|
| `minimax_lens` → MiniMax-M3 | `MINIMAX_API_KEY` set (125 chars) |
| `kimi_lens` → Kimi K2.6 | `KIMI_API_KEY` set (51 chars) |
| `codex_lens` → OpenAI Codex | `OPENAI_API_KEY` set (164 chars), `~/.codex/auth.json` present with tokens |
| `opus_lens` → Claude Opus 4.7 | uses ambient `claude --print` auth (this session's plan) |

`config/agents.recursive-self-improve.yaml` staged into `.mini-ork/config/agents.yaml`
by the runner. Migration `0017_self_improve_learning.sql` applied.

## Active branches

Each successful iter lands on `self-improve/iter-<N>-<ts>`. `--auto-merge` was
**not** passed, so branches stay quarantined for human review. Iter 1 is at
`self-improve/iter-1-20260608225224`.

## What to do when you come back

1. **Check the loop status** — read the tail of `/tmp/mini-ork-self-improve-LATEST.log`.
   The final block (after `=========`) summarises iters_run + wall_clock + per-iter
   outcomes.
2. **Inspect outcomes via SQLite:**
   ```bash
   sqlite3 .mini-ork/state.db \
     "SELECT iter, outcome, notes, branch_name FROM self_improve_runs ORDER BY iter;"
   sqlite3 .mini-ork/state.db \
     "SELECT iter, rank, category, title, outcome, severity, confidence
      FROM learning_record ORDER BY iter, rank;"
   sqlite3 .mini-ork/state.db \
     "SELECT arxiv_id, title, COUNT(*) refs, SUM(used_in_patch) hits
      FROM self_improve_arxiv_refs GROUP BY arxiv_id, title
      ORDER BY hits DESC, refs DESC;"
   ```
3. **Review successful branches:**
   ```bash
   git branch --list 'self-improve/iter-*'
   git log self-improve/iter-1-* --oneline
   git diff main...self-improve/iter-1-*
   ```
   Merge with `git merge --no-ff self-improve/iter-<N>-<ts>` if you accept; delete
   with `git branch -D self-improve/iter-<N>-<ts>` if you don't.
4. **Failed iter forensics** — every iter (success or fail) writes:
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/bottleneck-scan.md`
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/lens-{minimax,kimi,codex}.md`
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/arxiv-refs.md`
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/synthesis.md`
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/patches/iter-<N>.diff` (only on reject/fail)
   - `.mini-ork/runs/self-improve-iter-<N>-<ts>/implementer-report.md`
5. **Resume** — if the loop crashed or was Ctrl-C'd mid-iter:
   ```bash
   bin/mini-ork-self-improve --resume --soft-cap-hours 3 --hard-cap-hours 5
   ```

## Cost cap

`config/agents.recursive-self-improve.yaml`:
- `per_run_usd: 6.00` (one iter)
- `per_epic_usd: 50.00` (one outer-loop session)
- `daily_cap_usd: 60.00` (global)

Realistic spend on a full 5h session: $25-50, dominated by Opus synthesis.

## Known caveats for this run

1. **Two-checkout setup.** This repo is also cloned at `/Volumes/docker-ssd/ps/mini-ork`.
   The runner used `/Users/admin/ps/mini-ork` because PATH resolved there first; the
   commit I made via cwd matches HEAD in both. If you see divergence in HEAD between
   the two paths, rsync from this run's directory.
2. **Per-iter timeout = 1h** when budget allows. If a single iter (most likely the
   Opus synth) hangs, `timeout(1)` will kill it at the 1h mark and mark
   `outcome=timed_out`. The loop moves on.
3. **Convergence shortcut.** If the bottleneck scanner emits
   `## Status: converged`, the loop exits cleanly before the soft cap.
4. **No auto-merge.** Branches stay until you review.
