# NEVER_LAUNCHED kickoffs — confirmation by family (2026-10-10)

Triage rec#2: confirm the NEVER_LAUNCHED bucket from
`docs/todos/2026-10-09-kickoff-triage.md` and break it down by family.

## Method

Cross-source, not keyword grep:

- Enumerate `kickoffs/auto/*.md` (244 total).
- A kickoff counts as **launched** iff some `task_runs.kickoff_path` basename
  matches it (the launch record is the only reliable witness — commit matching
  was explicitly rejected in the triage).
- `never-launched = kickoffs − launched`.

## Result

**244 kickoffs · 146 launched · 98 never-launched** — reproduces the triage's 98
exactly.

### By family

| Family | Count | Notes |
|---|---:|---|
| `zed-*` | 27 | Zed-fork / IDE surface kickoffs; the largest single block |
| `rsi-*` | 18 | e1–e8, e2a, g1–g4, h1–h5, h9 (RSI programme expansion) |
| `arx-*` | 8 | |
| `cert-*` | 6 | c1–c6 certification series |
| `rlm-*` | 6 | shared-brain RLM series |
| `vt*` | 6 | vt1–vt6 verification-technique series |
| `cost-*` | 3 | cost-meter / cost-ledger |
| `recover-*` | 2 | |
| singletons | ~22 | acp, apply, artifact, context, docs, ff, framework, heldout, impl, install, lane, node, run, sqlite, t6–t9, u4c, u4d, verifier |

### Read

- The bucket is real and dominated by **two clusters**: the `zed-*` IDE surface
  (27) and the **RSI expansion set** (18). Together they are ~46% of the bucket.
- The RSI set (`rsi-e*`, `rsi-g*`, `rsi-h*`) is NOT the same as the REVIVE set
  (`rsi-i*`, which launched and failed) — these are *unlaunched* programme
  branches, so there is no prior diff to reuse; each is a fresh `run`.
- `cert-*` (6) and `vt*` (6) are small, well-bounded series — cheap to launch as
  a batch if the families are still wanted.

## Open question for the operator

Which families are still wanted? `zed-*` (27) is the biggest lever but is a
whole IDE surface; `rsi-e/g/h` (18) is programme expansion. Neither is a
"revive" — they are first launches, so they cost full runs, not the cheap
`recover` path the REVIVE set uses.
