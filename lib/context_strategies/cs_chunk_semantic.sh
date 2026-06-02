#!/usr/bin/env bash
# cs_chunk_semantic.sh — paragraph/section-boundary chunking.
# Splits at blank-line boundaries (paragraphs) for markdown / prose,
# OR at markdown header lines (^# / ^## / ^###) when they appear.
# Each lens gets a different semantic chunk via stable hash.

cs_chunk_semantic_prepare() {
  local input="${1:?input_path required}"
  local output="${2:?output_path required}"
  local lens="${3:-default}"

  # Use python to split into semantic chunks
  python3 - "$input" "$output" "$lens" <<'PY'
import sys, hashlib, re
input_path, output_path, lens = sys.argv[1:4]
text = open(input_path).read()

# Prefer markdown-header splits; fall back to blank-line paragraphs.
header_re = re.compile(r'^#{1,6}\s', re.MULTILINE)
headers = list(header_re.finditer(text))

if len(headers) >= 2:
    # Split at header positions
    chunks = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i+1].start() if i+1 < len(headers) else len(text)
        chunks.append(text[start:end].rstrip())
    mode = "header"
else:
    # Fall back to blank-line paragraphs
    chunks = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    mode = "paragraph"

if len(chunks) <= 1:
    # Nothing to chunk meaningfully
    with open(output_path, "w") as f:
        f.write(text)
    sys.stderr.write(f"[cs_chunk_semantic] {lens}: only {len(chunks)} chunk, no variation\n")
    sys.exit(0)

# Stable per-lens chunk selection
h = hashlib.sha256(lens.encode()).hexdigest()
idx = int(h[:8], 16) % len(chunks)
chosen = chunks[idx]

with open(output_path, "w") as f:
    f.write(f"# Semantic chunk {idx+1} of {len(chunks)} (mode={mode}) — lens: {lens}\n\n")
    f.write(chosen)
    f.write("\n")
sys.stderr.write(f"[cs_chunk_semantic] {lens}: {mode}-chunk {idx+1}/{len(chunks)} → {output_path}\n")
PY
}
