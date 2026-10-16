Read only the declared `technique_catalog` artifact. It lists every canonical
technique extracted from ~1000 papers on imitating an author's writing style,
each with a stable `key` (T0001…), a mechanism, and its supporting paper count.

Define EXACTLY 8 technique families. Write one JSON object to the requested
output path, no fences:

{
  "families": [
    {
      "name": "Short family name (e.g. Style evidence & retrieval)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring families is.",
      "criteria": "How to recognise a technique that belongs here: the mechanism it must intervene in, and the mechanism that would instead place it in a neighbouring family.",
      "keywords": ["retrieval", "demonstration selection", "similarity", "reranker"],
      "exemplars": ["T0001", "T0007"]
    }
  ]
}

You are defining the families, NOT sorting the catalog. Do not emit a mapping
from technique keys to families, do not write a script, and do not attempt to
list the members of each family beyond a few `exemplars`. A later deterministic
stage assigns every technique to a family using your names, scopes, criteria and
keywords — so those fields are the whole deliverable, and their precision decides
how well the assignment lands.

Rules:
- Exactly 8 families. Group by MECHANISM FAMILY — where in the system the
  technique intervenes. The families this literature tends to contain, which you
  should adapt to what the catalog actually holds rather than force empty:
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
- Order families largest-first by the paper count each is expected to attract.
- `keywords`: 6-15 lowercase single words or short phrases that occur in the
  wording of techniques belonging to this family. These drive the assignment, so
  choose discriminative terms and avoid words shared by every family ("style",
  "text", "author", "model", "generation") — a keyword present in most
  techniques separates nothing.
- `criteria`: state the boundary explicitly. For each family, name the
  neighbouring family it is most often confused with and the distinguishing
  mechanism.
- `exemplars`: 3-8 catalog keys you are certain belong here. Use the keys the
  catalog actually contains; never invent one. Exemplars are weighted heavily, so
  prefer unambiguous techniques over interesting ones.
