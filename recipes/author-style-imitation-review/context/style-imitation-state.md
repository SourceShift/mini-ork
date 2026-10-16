# Target state — what is already known to work when imitating an author's style

This is the snapshot both impact reviewers score against. It is written, not
measured: it records the state of practice so a reviewer can mark a technique
`already-standard` in spirit, or `overclaimed` when the literature ignores
something the list below says is settled. Override it per run with
`MO_STYLE_STATE_FILE` when reviewing a different target system.

## The problem being reviewed

Goal: given a handful (roughly 5-50) of texts written by one person, generate new
text that reads as that person's, without the author's further involvement. The
anchor evaluation is **arxiv:2509.14543** (*Catch Me If You Can? Not Yet: LLMs
Still Struggle to Imitate the Implicit Writing Styles of Everyday Authors*,
EMNLP 2025): >40,000 generations per model, >400 real-world authors, domains
spanning news, email, forums, and blogs.

Its headline finding is the bar every technique in this review has to clear:
LLMs approximate personal style in **structured formats (news, email)** but
**struggle with informal writing (blogs, forums)**, and the failure is in the
*implicit* part of style — the part a person could not write down as an
instruction. Its prompting-strategy ablations (notably the number of
demonstrations) show the gap does not close by adding examples.

## The measurement regime

The anchor paper's four complementary metrics are the default acceptance test.
A technique claiming to work should be judged on whether it moves these, and on
whether it moves them for *informal* domains, not just news:

1. **Authorship attribution** — can a classifier assign generated text to the
   right author among many candidates? Sensitive to text length; long outputs
   inflate it. A technique that only wins on long generations has not won.
2. **Authorship verification** — is this text by the *same* author as held-out
   samples? The harder, open-set sibling of attribution, and the one that resists
   gaming by copying surface features of the prompt.
3. **Style matching** — similarity to the author's style representation. The
   most gameable of the four: a model can score well here while producing text no
   human would accept as the author's.
4. **AI detection** — does the output still read as machine-written? This is the
   *inverse* signal: a technique that improves style matching while making the
   text more detectable has solved the wrong problem.

Human preference / authorship panel judgement, where papers report it, outranks
all four. Absent a human check, a claim resting only on style matching is weak.

## Known to work — treat as the baseline, not a discovery

- **In-context conditioning on several of the author's own texts.** The whole
  field's baseline; the anchor paper's entire evaluation is built on it. A
  review that presents few-shot conditioning as a finding has found nothing.
- **Better together than alone: matching the genre of the author's samples to the
  target text.** News samples for news output, email for email. The reported
  collapse in blogs and forums is at least partly a genre-alignment failure.
- **Explicit style descriptors as a *complement* to examples, not a replacement.**
  Describing the style in words helps on the dimensions that are nameable
  (formality, sentence length, hedging, punctuation habits) and helps least on
  exactly the dimensions the anchor paper identifies as the hard ones.
- **Fine-tuning or parameter-efficient adaptation (LoRA, adapters) on a
  per-author corpus beats prompting for style fidelity** when the author supplies
  enough text. It is the strongest known lever and also the one with the highest
  data requirement and the worst privacy story.
- **Iterative revision against a style critic** — generate, score against the
  author's samples, revise — reliably improves measured style matching over a
  single pass, at a linear cost increase in generations.

## Measurement-only — often mistaken for working

These are reported as evidence of success but do not establish it on their own:

- **Perplexity / likelihood of held-out author text under the model.** Moves
  independently of whether readers attribute the output to the author.
- **Style embedding cosine similarity.** Saturates: many distinct texts sit at
  similar distance from an author centroid, including texts no reader would
  accept.
- **Attribution accuracy on *long* generated passages.** Length alone carries
  authorship signal; a length-matched control is required before believing it.
- **Detector-evasion rate as a proxy for human-likeness.** Detectors are
  poorly calibrated and adversarially brittle; beating one is not evidence of
  writing like a person. Detection is an *acceptance floor*, never an objective.

## Known-broken — documented failure zones

- **More demonstrations is not monotonically better.** The anchor paper's
  demonstration ablations are the reference here. Expect saturation, and expect
  the informal domains to degrade first as the context fills with loosely
  related samples.
- **Style imitation flattens the author.** Techniques optimized on aggregate
  similarity push output toward the author's *mean* register and lose the
  within-author variation (tone shifts, occasional long sentences, idiosyncratic
  asides) that makes text read as a specific person.
- **Content leakage and memorization.** Conditioning on an author's corpus, and
  especially fine-tuning on it, reproduces distinctive phrases — and in the worst
  case, identifies the author and their private details. Any technique here needs
  an explicit answer to "what stops it from copying?" before it is adopted.
- **Transfer claims that do not survive a change of author, domain, language, or
  model family.** This is the single most common overclaim in the field: a
  method tuned and evaluated on one dataset, one language, one model.
- **Short input samples.** Below roughly a paragraph, attribution/verification
  becomes near-chance and every style metric is noise.

## How to read priority in this review

Priority = mean impact × mean evidence confidence / effort, ×1.15 when three or
more independent papers report the mechanism, ×0.5 when a reviewer marks the
claim overclaimed. High priority means **worth testing first for your author**,
never *proven*. The only real proof is running the adoption sketch and reading
the four metrics on the author you actually care about — including the informal
domain the anchor paper shows is hardest.
