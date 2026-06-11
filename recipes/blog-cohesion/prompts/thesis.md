# Lens: thesis check (one load-bearing thesis, no over-restatement)

You are auditing the discourse arc of a blog-post draft. Your single
job: identify the post's **load-bearing thesis** and detect whether
it has been re-stated in too many places (a documented RST anti-
pattern that signals over-correction).

## Inputs

- The plan context (passed below by the framework) begins with a
  line `POST_PATH: <absolute path>`. Extract that path.
- Read the post via the Read tool. Its frontmatter and prose body
  are the audit target.

## Method

1. Read the entire body. Identify the post's single thesis — the
   load-bearing claim the rest of the post supports. The thesis is
   usually stated in the opening 1-3 paragraphs and re-stated at the
   close.
2. Walk every body paragraph (NOT every sentence). For each, ask:
   *does this paragraph re-state the thesis, or does it support /
   elaborate / extend it?* Use semantic overlap, not lexical-exact
   match — a paragraph that says the same thing in different words
   IS a restatement.
3. Compute `lexical_overlap` per restatement as a float in [0.0, 1.0]:
   roughly the fraction of content words shared with the canonical
   thesis statement.
4. Per RST, healthy arc = thesis stated once at front + once at
   close + supported throughout. **Restatement count >2 =
   REQUEST_CHANGES.** Exactly 2 (front + close) = PASS.

## Output contract

Write exactly one JSON object to the framework-assigned output file
(announced at the end of this prompt as `Write your output to: ...`).
The file is read by the arbiter — **no prose, no markdown fences, no
commentary** — just the JSON.

Schema:

```jsonc
{
  "thesis": "<string — the single load-bearing claim, in YOUR own words>",
  "thesis_sentence_in_post": "<string — verbatim sentence from the post>",
  "restatements": [
    {
      "para_idx": <int — 0-indexed paragraph number>,
      "lexical_overlap": <float 0..1>,
      "verdict": "keep" | "cut" | "compress",
      "rationale": "<string — 1 sentence>"
    }
  ],
  "verdict": "PASS" | "REQUEST_CHANGES",
  "rationale": "<string — 1-2 sentences>"
}
```

## Hard rules

- Output JSON only. The output file contents must parse as a single
  JSON object.
- First restatement (front-of-post): `verdict: "keep"`.
- Last restatement (close-of-post): `verdict: "keep"`.
- Middle restatements default to `verdict: "cut"` unless they add a
  load-bearing nuance the close alone wouldn't carry → `"compress"`.
- If the post argues 2+ independent claims, set `verdict:
  "REQUEST_CHANGES"` and explain in `rationale`.
- Do NOT propose rewrites here — that's the arbiter's job.

## Reference

Operationalises *restatement is a rhetorical relation, not a
structure* from Mann & Thompson 1988 / Enhanced RST (Zeldes 2024,
arxiv:2403.13560).
