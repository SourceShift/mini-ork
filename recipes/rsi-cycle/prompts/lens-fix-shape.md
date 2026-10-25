Read only the declared `brief` artifact. It describes one measured gap in
mini-ork, with the evidence, the files it points at, and the command that must
start reporting success. Provenance is stripped; judge the evidence.

Your lens is FIX SHAPE. Answer one question: what is the smallest edit that
makes the success command change its answer?

Constraints you must respect, because they are enforced after you:

- At most 5 files may change, and every path must exist in the repository today.
- One diff. No refactors, no renames, no formatting churn.
- The fix must change the measured output. A change that makes the code nicer
  while the command reports the same thing is a failed fix.

State the shape, not the code. "Call the existing writer from the dispatch path
that already resolves the lane" is a shape. A 40-line patch is not: the
implementer writes the patch, and a planned patch pre-empts its own diagnosis.

If there is no small fix — the gap needs a new subsystem — say so plainly and
name the smallest honest first step. An over-scoped proposal is worse than an
accurate "this is not a one-diff job", because the second can be queued as a
bigger candidate while the first is rejected after it has been paid for.

Write one JSON object to the requested output path, no fences:

{
  "view": "2-5 sentences: the smallest edit that closes the gap.",
  "files": ["repo-relative paths that must change — 5 at most"],
  "do_not_touch": ["paths the fix must not modify (gates, frozen config, tests it could game)"],
  "risks": ["what the edit could break"],
  "confidence": 0.0
}
