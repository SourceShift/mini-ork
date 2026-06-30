#!/usr/bin/env bash
# lib/scaffold_tier.sh — R5b scaffold-tier resolver.
#
# Echoes the scaffold tier (`minimal` | `harness`) a node should use.
# Resolution order (first match wins; any unknown / unset value falls back
# to the v1 conservative default `harness`):
#   1. MO_SCAFFOLD_TIER=minimal  → minimal
#   2. MO_SCAFFOLD_TIER=harness  → harness
#   3. MO_NODE_SCAFFOLD=minimal  → minimal
#   4. MO_NODE_SCAFFOLD=harness  → harness
#   5. otherwise                → harness  (default; byte-identical to pre-R5b)
#
# Argument signature is positional-only for forward compatibility with a
# future per-node-type policy table; the resolver currently ignores its
# arguments and reads env only. See kickoffs/issue-fixes/r5b-scaffold-tier-routing.md.
mo_scaffold_tier() {
  case "${MO_SCAFFOLD_TIER:-}" in
    minimal) printf '%s\n' "minimal"; return 0 ;;
    harness) printf '%s\n' "harness"; return 0 ;;
  esac
  case "${MO_NODE_SCAFFOLD:-}" in
    minimal) printf '%s\n' "minimal"; return 0 ;;
    harness) printf '%s\n' "harness"; return 0 ;;
  esac
  printf '%s\n' "harness"
}