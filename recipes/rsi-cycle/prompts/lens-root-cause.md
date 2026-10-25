Read only the declared `brief` artifact. It describes one measured gap in
mini-ork: the evidence, the files it points at, and the command whose output
would change if the gap closed. Provenance is stripped; judge the evidence.

Your lens is ROOT CAUSE. Answer one question: why does the code produce this
evidence? Not "what is missing" — why the current implementation cannot produce
it. Distinguish the three cases that look identical from the outside:

- the producer exists but is never called on the path that matters,
- the producer is called but its output is dropped or overwritten downstream,
- the producer does not exist at all.

Name the symbol, module, or call site you believe is responsible, and say what
you did not verify. If the evidence does not determine the cause, say which two
candidates remain and what would separate them — an honest "two possibilities"
beats a confident guess, because the synthesiser can carry it forward and the
implementer can check it.

Write one JSON object to the requested output path, no fences:

{
  "view": "2-5 sentences: the mechanism that produces this evidence.",
  "symbols": ["module.symbol or file:anchor you believe is responsible"],
  "files": ["repo-relative paths a fix would touch"],
  "risks": ["wrong-cause hypotheses you could not rule out"],
  "confidence": 0.0
}

`confidence` is 0..1 and should be low when the evidence is a count, not a trace.
