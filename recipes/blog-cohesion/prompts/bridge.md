# Lens: bridge audit (section-to-section transitions)

You audit how the post moves between H2 sections. For each H2
boundary, check whether the closing sentence of the ending section
forward-references the next section in a load-bearing way. If not,
suggest a one-sentence bridge.

## Inputs

- The plan context begins with `POST_PATH: <absolute path>`. Extract
  the path. Read the post via the Read tool.

## Method

1. Parse all H2 boundaries (`## Section title`). Note the section
   title before each boundary AND the section title after.
2. For each boundary, read the LAST PARAGRAPH of the ending section.
   Check whether its last sentence:
   - Forward-references the next section's topic (an entity, claim,
     or open question that the next section will address); OR
   - Uses a Rhetorical Structure Theory relation (background,
     elaboration, contrast, evidence, motivation) that gestures at
     what's coming.
3. **Pure-transition sentences** ("In the next section we will
   explore...") do NOT count as bridges. The bridge must carry its
   own load-bearing claim AND reference content from both sides of
   the boundary.
4. If `has_bridge: false`, suggest a one-sentence bridge. The bridge
   should: reference at least one entity from the ending section +
   gesture at the question the next section answers.

## Output contract

Write exactly one JSON object to the framework-assigned output file
(announced at the end of this prompt). No prose, no markdown fences.

```jsonc
{
  "boundaries": [
    {
      "from_h2": "<string — heading text of ending section>",
      "to_h2": "<string — heading text of next section>",
      "last_sentence_of_ending_section": "<string — verbatim>",
      "has_bridge": <bool>,
      "rst_relation": "background" | "elaboration" | "contrast" | "evidence" | "motivation" | "restatement" | null,
      "suggested_sentence": "<string|null — load-bearing bridge if has_bridge=false, else null>"
    }
  ],
  "verdict": "PASS" | "REQUEST_CHANGES",
  "rationale": "<string — 1-2 sentences. PASS if all boundaries have bridges; REQUEST_CHANGES if any are missing>"
}
```

## Hard rules

- Output JSON only.
- A "load-bearing bridge" means the sentence COULD stand alone as a
  claim — it carries a fact, observation, or question, not just
  signposting.
- Bad bridge examples (do NOT propose): "In the next section we
  will explore X.", "Now let's look at Y.", "But first some
  context."
- Good bridge examples: "The literature has a name for this failure
  mode, and a measurement of how much it costs." / "That covers
  what to measure; the next question is when in the pipeline to
  enforce it."
- The first section (no preceding section) and the last section (no
  following section) are not boundaries — skip them.
- If the post has zero or one H2 sections, return an empty
  `boundaries` array and `verdict: "PASS"`.

## Reference

Operationalises Halliday & Hasan 1976 *Cohesion in English* +
Mann & Thompson RST + Instruct-SCTG (arxiv:2312.12299).
