#!/usr/bin/env bash
# cs_passthrough.sh — identity strategy: emit input as output.
# Baseline for the registry; no transformation. Use when a recipe
# wants explicit "this lens sees the raw kickoff" semantics.

cs_passthrough_prepare() {
  local input="${1:?input_path required}"
  local output="${2:?output_path required}"
  local lens="${3:-unknown}"
  cp "$input" "$output"
  echo "[cs_passthrough] $lens: $input → $output (identity)" >&2
}
