#!/usr/bin/env bash
# cs_reorder_shuffle.sh — same chunks, randomised order (stable seed
# per lens). Anti-positional-bias: position-1 chunks tend to dominate
# attention in long-context models; shuffling per lens removes that
# coupling across the panel.

cs_reorder_shuffle_prepare() {
  local input="${1:?input_path required}"
  local output="${2:?output_path required}"
  local lens="${3:-default}"

  python3 - "$input" "$output" "$lens" <<'PY'
import sys, hashlib, random, re
input_path, output_path, lens = sys.argv[1:4]
text = open(input_path).read()

# Split at blank-line paragraphs
paras = [p for p in re.split(r'(\n\s*\n)', text) if p]  # keep separators

if len(paras) <= 3:
    # Nothing useful to shuffle
    with open(output_path, "w") as f:
        f.write(text)
    sys.stderr.write(f"[cs_reorder_shuffle] {lens}: < 4 segments, no shuffle\n")
    sys.exit(0)

# Stable per-lens RNG seed
seed = int(hashlib.sha256(lens.encode()).hexdigest()[:8], 16)
rng = random.Random(seed)

# Separate content paragraphs from separators; shuffle content only.
content = paras[::2]  # 0, 2, 4, ...
seps = paras[1::2]    # 1, 3, 5, ...
rng.shuffle(content)

# Re-assemble interleaved
out_parts = []
for i, c in enumerate(content):
    out_parts.append(c)
    if i < len(seps):
        out_parts.append(seps[i])
shuffled = "".join(out_parts)

with open(output_path, "w") as f:
    f.write(f"<!-- cs_reorder_shuffle: lens={lens} seed={seed} -->\n\n")
    f.write(shuffled)
sys.stderr.write(f"[cs_reorder_shuffle] {lens}: {len(content)} paragraphs reordered → {output_path}\n")
PY
}
