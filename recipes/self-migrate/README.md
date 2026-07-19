# `self-migrate` — verify-gated, integration-point-first self-migration

Closes one **integration fork** (a bash↔Python seam) at a time as a *single
complete unit*, so no half-migrated seam is ever left behind. Propose-not-commit:
emits a reviewable diff + a static-feature ledger + a verdict; never applies to
main or retires an entrypoint on the real checkout.

Full design + the integration-point map + the feature manifest:
[`docs/migration/self-migrate-feature-manifest.md`](../../docs/migration/self-migrate-feature-manifest.md).

## Why forks, not libs
Bottom-up leaf migration *splits* integration points — it makes a Python module
native but leaves the paired bash entrypoint (and every lib/test/UI ref to it)
live. This recipe advances a **single frontier root→down**: above it pure Python,
below it pure bash, nothing half-open. A fork closes only when every inbound ref
is repointed and the bash entrypoint retires.

## The pipeline (per fork)
1. **seam_mapper** (opus) → `integration-map.json` — every outbound seam + every
   inbound ref (incl. `bin/`, `lib/`, tests, sandbox, web UI).
2. **static_feature_ledger** (opus) → `static-feature-ledger.json` — classify
   every behavior **static** (cheap + byte-parity verifiable — the moat) vs
   **agentic** (a cost + verifiability liability + a cost-down candidate). This
   ledger is the migration's strategic payload.
3. **migrator** (codex) → `self-migrate.diff` — make Python sole, repoint every
   inbound ref, retire the bash entrypoint in the diff.
4. **verify** — `pre-retirement-parity.sh` captures byte parity while the Bash
   oracle still exists · `parity.sh` rechecks the migrated behavior · `feature-acceptance.sh`
   (the end-to-end feature probe + pytest + pyright) · `ledger-shape.sh` (the
   ledger is complete, every agentic row has a cost-down `opportunity`) ·
   `fork-closure.sh` (the retired entrypoint and every runtime reference are gone).
5. **reviewer** (opus) → `verdict.json` — `pass == parity ∧ acceptance ∧
   ledger_complete ∧ no_dangling_edge`.

## Lane policy (set in `$MINI_ORK_HOME/config/agents.yaml`)
| role | lane | backoff |
|---|---|---|
| implementer (`migrator`) | **codex** | codex |
| mapper / ledger / reviewer | **opus** (`opus_lens`) | codex |
| discovery lens (non-critical) | **GLM** (`glm_lens`) | codex |

## Run it
```bash
export MINI_ORK_ROOT="$PWD" MINI_ORK_HOME="$PWD/.mini-ork" MO_TARGET_CWD="$PWD"
export MO_ALLOW_FRAMEWORK_CWD=1 MO_FORK=verify      # self-edit + name the fork
"$MINI_ORK_ROOT/bin/mini-ork" run self-migrate recipes/self-migrate/example-kickoff.md
```
Recommended order (by blast radius, from the integration map): **verify** (cleanest)
→ reflect → classify → plan → cli → **execute** (the monster: 4 outbound seams incl.
`context_assembler` 786L, 37 inbound refs — last).

## Feature-acceptance probes
`gates/feature_acceptance.sh <fork|feature>` — the end-to-end probe suite that is
the migration's real finish line (unit-parity alone misses feature breaks).
`gates/feature_acceptance.sh all` runs every probe.
