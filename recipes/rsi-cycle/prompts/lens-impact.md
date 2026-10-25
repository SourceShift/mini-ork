Read only the declared `brief` artifact. It describes one measured gap in
mini-ork — evidence, the files the harvester points at, and the command that
would report success if the gap closed. The brief is anonymised on purpose: you
are not told which source produced it and it does not matter to your question.

Your lens is IMPACT. Answer one question: what does leaving this gap open cost
the loop, and what gets better the moment it closes?

Be concrete about consequence, not severity. "Severity 89" is already known and
is not an argument. Say what a user or a cycle does wrong today because of this,
how often it happens, and whether the evidence is strong enough that the cost is
real rather than theoretical. If the honest answer is "small", say small — a
lens that inflates every gap is useless to the synthesiser.

Do not propose an implementation. Do not rank it against other gaps.

Write one JSON object to the requested output path, no fences:

{
  "view": "2-4 sentences: the cost of leaving this open, and the benefit of closing it.",
  "files": ["repo-relative paths you believe are involved, if the evidence shows them"],
  "risks": ["what could get worse, or what would still be broken after a fix"],
  "confidence": 0.0
}

`confidence` is 0..1: how much the brief's own evidence supports your view. Low
confidence with a clear reason is more useful than false certainty.
