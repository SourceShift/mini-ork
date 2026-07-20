# Harsh-critic panel verdict — ported executor + runtime cutover

**Date:** 2026-07-08
**Target:** `mini_ork/cli/execute.py` (`dispatch_node` + live path) under `MINI_ORK_RUNTIME=python`, vs bash `bin/mini-ork-execute`.
**Panel:** two independent adversarial reviewers (opus, separate contexts), refute-or-promote discipline. Cross-vendor lanes (kimi/codex) were unavailable this run — this is a single-family (Anthropic) panel; a kimi/codex second pass is still advisable but the findings below are code-grounded, not judgment calls, so family diversity does not change them.
**Gate #1 (live-dispatch harness):** PASS — a real sonnet dispatch through the ported live path called the LLM, wrote the artifact, charged cost $0.34, rc 0. The *wiring* is live; the *fidelity* is where the gaps are.

## VERDICT: NO-GO on flipping the LIVE dispatch default.

The deterministic surface (dry-run / plan / classify / init / review …) IS safe — the parity harness proves it and the default cutover for those is fine. The **live `dispatch_node` path** has 5 blocks-cutover defects. Bash must remain the live executor until they are fixed.

## Blocks-cutover findings (each with a concrete trigger)

| # | Defect | Port | Bash | Trigger → divergent outcome |
|---|--------|------|------|------------------------------|
| 1 | **Policy lane-routing is dead.** Port dispatches `lane = model_lane or node_type` raw; never calls the policy router. The entire GRPO/learning-governed routing loop is inert. | `mini_ork_execute.py:870,915` | `bin/mini-ork-execute:2219` (`dispatch_lane=$(_mo_policy_route_lane …)` before dispatch; default policy `learning_governed`) | Any researcher node, `model_lane` unset: bash dispatches the routed lane (e.g. `kimi_lens`/`opus_lens`); port dispatches `--node-type researcher`. **Wrong model invoked + core value-prop disabled.** |
| 2 | **Publisher is a stub.** Just `set_status("published")`. No oracle gates, no panel-verdict requirement, no artifact-contract copy, no git commit. | `mini_ork_execute.py:1000-1002` | `bin/mini-ork-execute:2909-2998, 3059-3068` (4 oracle gates → `return 1` on `safety_violation`; panel-verdict gate; `_publisher_try_commit_files` / source_artifact→outputs copy) | code-fix recipe: implementer edits never committed. artifact recipe: output file never written. `safety_violation` from a panel: bash BLOCKS, port marks `published`. **Ships unsafe or empty, reports success.** |
| 3 | **`is_synth` misclassifies the panel gate.** `"synth" in node_id` matches `tier4_synth`, which bash treats as a *panel approval gate*, not an ungated synth. | `mini_ork_execute.py:945,963-966` | `bin/mini-ork-execute:2704-2727,2799-2816` (`_is_panel_gate=1`, `_is_synth=0`, writes `panel-verdict.json`, runs the verdict gate) | recursive-validate-impl `tier4_synth` with verdict=reject: bash fails+rolls back; port returns `0/done` ungated AND writes `synthesis.md` not `panel-verdict.json` (so F2's publisher gate can't read it either). **Approval gate dead.** |
| 4 | **`MO_TARGET_CWD` never pinned.** Port reads `MO_TARGET_CWD or os.getcwd()` but nothing derives/exports it from the kickoff's git toplevel. | `mini_ork_execute.py:930-933` | `bin/mini-ork-execute:2632-2642` (`export MO_TARGET_CWD=$(… git rev-parse --show-toplevel)`, the CWT-A corruption fix) | `MINI_ORK_RUNTIME=python bin/mini-ork run <recipe> <foreign-repo-kickoff>` from the mini-ork repo: `os.getcwd()`=MINI_ORK_ROOT, so codex runs there AND `apply_impl_output` git-applies into **mini-ork's own tree**. Reintroduces the known repo-corruption hazard. |
| 5 | **Pre-dispatch gates absent.** No `.stop-requested`, intervention gate, capability assert, or stale-heartbeat watchdog. | `mini_ork_execute.py:864-908` | `bin/mini-ork-execute:2231-2236, 2258-2262, 2296-2318` | UI POSTs `/stop` mid-run: bash halts before next node; port ignores it, keeps spending budget. Node `requires_capabilities` a lane lacks: bash fails `config`; port dispatches anyway. **No soft-stop; incapable-lane dispatch.** |

Degrade-only: **F6** synth artifact naming hardcodes `synthesis.md` ignoring `artifact_contract.source_artifact` (`:946-947`) → downstream reads a missing/stale file for non-default contracts.

## Refuted (confirmed sound)

- **No other silent-no-op entrypoints.** All 13 delegated `mini_ork_<x>.py` have working `__main__`; the ~80 helper modules lacking `__main__` are unreachable by the shim's `basename|tr '-' '_'` mapping. (init/review already fixed, PR #151.)
- **Reviewer verdict string mapping is correct** — `_REVIEW_PASS`/`_REVIEW_REVISE`/fail+unknown→`verdict_fail` all match bash. It only breaks via the `is_synth` path (F3), not the string set.
- **Shim arg/env/cwd forwarding is correct** (`exec env PYTHONPATH=… python3 -m … "$@"`; process replacement, no set-e interaction). Minor: unset `MINI_ORK_ROOT` silently stays bash (observability, not correctness).

## Must-fix list before live cutover (ordered)

1. Apply `_mo_policy_route_lane` (or the ported `learning_static_lane` + governed policy) before `dispatch_fn`, so the routed lane — not the raw node_type — reaches `--node-type`.
2. Port the publisher: oracle gates (block on `safety_violation`), panel-verdict requirement, `_publisher_try_commit_files` + source_artifact→outputs copy.
3. Fix `is_synth` to distinguish panel-gate nodes (`tier4_synth` etc.) from true synth; write `panel-verdict.json` and run the verdict gate for panel gates.
4. Derive + export `MO_TARGET_CWD` from the kickoff's git toplevel before implementer/publisher dispatch.
5. Port the pre-dispatch gates: `.stop-requested`, intervention gate, capability assert, stale-heartbeat watchdog.
6. (degrade) Read `artifact_contract.source_artifact` for the synth output path.

## Harness blind spot (now documented)

`scripts/runtime-parity-harness.sh` deliberately skips live dispatch and `plan --dry-run` routes through `_dry_dispatch_node`, not `dispatch_node`. So it is structurally blind to the entire live-node side-effect class (edit-surface cwd, apply-impl target, publisher commit, verdict gating). The `live_dispatch_harness.py` gate covers the *researcher* wiring only; extend it to implementer (target-cwd) + publisher (commit) + a panel-gate verdict before trusting the live cutover.
