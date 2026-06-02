#!/usr/bin/env bash
# cs_chunk_fixed.sh — fixed-N-line chunking. Each lens gets a different
# chunk based on a stable hash of (lens_name). N defaults to 80 lines.
# Override via CS_CHUNK_FIXED_LINES env.

cs_chunk_fixed_prepare() {
  local input="${1:?input_path required}"
  local output="${2:?output_path required}"
  local lens="${3:-default}"
  local n="${CS_CHUNK_FIXED_LINES:-80}"

  local total_lines
  total_lines=$(wc -l < "$input")
  if [ "$total_lines" -le "$n" ]; then
    # Short enough — every lens gets the whole thing
    cp "$input" "$output"
    echo "[cs_chunk_fixed] $lens: input ≤ $n lines, no chunking" >&2
    return 0
  fi

  # Stable per-lens chunk selection: hash(lens_name) mod n_chunks
  local n_chunks=$(( (total_lines + n - 1) / n ))
  local chunk_idx
  chunk_idx=$(printf '%s' "$lens" | python3 -c "
import hashlib, sys
h = hashlib.sha256(sys.stdin.read().encode()).hexdigest()
print(int(h[:8], 16))
")
  chunk_idx=$(( chunk_idx % n_chunks ))
  local start=$(( chunk_idx * n + 1 ))
  local end=$(( start + n - 1 ))

  {
    echo "# Chunk ${chunk_idx} of ${n_chunks} (lines ${start}-${end} of ${total_lines}) — lens: ${lens}"
    echo ""
    sed -n "${start},${end}p" "$input"
  } > "$output"
  echo "[cs_chunk_fixed] $lens: chunk ${chunk_idx}/${n_chunks} → $output" >&2
}
