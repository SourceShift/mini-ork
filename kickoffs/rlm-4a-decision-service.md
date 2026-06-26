# rlm-4a: stateless decision service (decide() read-path only)

## Goal

[framework-edit] Create `lib/decision_service.sh` — the single inference-time
surface both consumers (eng-team + book-gen) call to get a routing/panel/reward
decision from the learned policy. SCOPE IS ONE FILE: the new lib only. Do NOT
wire any caller in this kickoff (that is rlm-4b). Keep it small to stay within
the implementer turn budget.

`decide <node_type> <task_class> <objective_domain> [segment]` prints a JSON
object `{route, coalition_ok, reward_estimate, recursion_hint}` by reading the
current policy slice via the store-port and wrapping the existing brain libs:
- routing: call `lane_router_preferred_lane <task_class> <node_type>` (already
  objective_domain-aware after rlm-2); below the `sample_size >= 3` floor it
  returns the agents.yaml-configured lane (cold-start safe — do NOT invent a
  lane).
- coalition: call into `lib/coalition_gate.sh` to report whether the candidate
  panel is family-diverse enough.
- reward: read the slice's normalized `reward_g` summary via `lib/policy_store.sh`
  (SQLite default backend).
- holds NO per-request state; pure read + compute.

## Scope Hint

- `lib/decision_service.sh` (new file ONLY)

## Requirements

- Source the brain libs (`lib/policy_store.sh`, `lib/lane_router.sh`,
  `lib/coalition_gate.sh`, `lib/process_reward.sh`); do not duplicate their logic.
- Strict mode MUST be guarded behind the direct-exec check
  `[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail` so sourcing never
  leaks `set -u`/`pipefail` onto a caller (same convention as lane_router.sh /
  process_reward.sh / the rlm-3 policy_store.sh fix).
- Cold-start safety: when the policy slice has `sample_size < 3`, `decide`
  returns the configured default lane, never an empty/invented one.

## Verification commands

- `shellcheck lib/decision_service.sh`

## Done When

- `bash -n lib/decision_service.sh` passes and `shellcheck` is clean.
- Sourcing the lib into a lenient shell does NOT enable `set -u` (no leak):
  `bash -c 'set +u; source lib/decision_service.sh; : "${UNBOUND_X}"; echo ok'`
  prints `ok`.
- A unit smoke shows `decide` returns the agents.yaml-configured lane for a
  `(task_class,node_type)` slice with `sample_size < 3`, and the learned
  `lane_router_preferred_lane` value when `>= 3`.
