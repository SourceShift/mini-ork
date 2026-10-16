Read only the declared `technique_catalog` artifact. It lists every canonical
technique extracted from ~1000 papers on imitating an author's writing style,
each with a stable `key` (T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 8 technique groups. Write one JSON object to
the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Style evidence & retrieval)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 8 groups. Group by MECHANISM FAMILY — where in the system the
  technique intervenes. The families that this literature tends to contain,
  which you should adapt to what the catalog actually holds rather than force
  empty:
    1. **Style evidence & retrieval** — which samples of the author go into the
       context, how they are selected, ordered, and represented.
    2. **Demonstration construction** — how many examples, which ones, in what
       form, and how that count is scaled or ablated.
    3. **Explicit style description** — turning the author's style into
       descriptors, rubrics, or feature lists the model can follow.
    4. **Parameter-level adaptation** — fine-tuning, LoRA, adapters, style
       vectors, activation steering, distillation into a smaller model.
    5. **Decoding & generation control** — constrained decoding, attribute
       control, reward shaping, preference optimization for style.
    6. **Post-hoc revision & critique** — style critics, rewriting, revision
       loops, style-preserving editing.
    7. **Measurement & evaluation** — authorship attribution, authorship
       verification, style matching, AI detection, human evaluation.
    8. **Failure modes & limits** — what does not work: leakage, degradation with
       more demonstrations, over-imitation, privacy and memorization risk,
       stylistic flattening.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
