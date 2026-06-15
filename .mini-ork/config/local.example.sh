#!/usr/bin/env bash
# local.example.sh — template for per-developer local env preferences.
#
# Copy to $MINI_ORK_HOME/config/local.sh (default:
# .mini-ork/config/local.sh) and uncomment the lines you want enabled.
# That path is gitignored (.gitignore: .mini-ork/config/local.sh) so
# different machines can have different defaults without conflict.
#
# Difference vs secrets.local.sh: this file holds NON-secret env
# preferences. Secrets stay in secrets.local.sh. Both are gitignored.
#
# Sourced by .githooks/pre-push right after MINI_ORK_HOME is established,
# so any var exported here takes effect for every push from this clone.

# --- Layer 3 reviewer ---------------------------------------------------

# Enable the LLM-panel mode by default on this machine. Default is 0
# (heuristic-only) for cost; enabling here gives this machine the
# 4-family panel (codex + kimi + glm + minimax) on every push.
#export MO_REVIEW_LLM_LENSES=1

# Customize the panel composition. Add opus / sonnet for hard-judgment
# reviews; gemini is banned by policy (enforced inside _run_llm_panel).
#export MO_REVIEW_PANEL="codex kimi glm minimax"
#export MO_REVIEW_LENS_TIMEOUT_S=180

# Auto-dispatch a mini-ork fix epic when the reviewer blocks (forwards
# open issues to bug_reports for promote -> scheduler).
#export MO_REVIEW_AUTO_FIX=1

# --- Other hook layers --------------------------------------------------

# Skip the L2 4-lens README panel by default (still costs ~$0.25/push
# when the gatekeeper says PANEL_NEEDED). L1 mechanical check always
# runs regardless.
#export MO_README_PANEL_SKIP=1

# When the L2 panel returns INDETERMINATE (provider errors), treat as
# block instead of fail-open. Default is fail-open.
#export MO_README_PANEL_INDETERMINATE=block
