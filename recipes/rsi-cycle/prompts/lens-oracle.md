Read only the declared `brief` artifact. It describes one measured gap in
mini-ork and the command — a harvest rule, a probe, a test — that currently
reports the gap and must report success after a correct fix. Provenance is
stripped; judge the evidence.

Your lens is ORACLE. Answer one question: how do we know a fix worked, without
believing the implementer?

Three things must be true of any real oracle, and your job is to check them
against this gap:

1. It is measured independently. The implementer cannot make it pass by editing
   the thing that measures it, and the gate blocks edits to the gate.
2. It is falsifiable. There is an output that means "still broken" and one that
   means "closed", and they are different strings of bytes.
3. It cannot be satisfied vacuously. An empty table, a skipped probe, or a
   deleted test is not success. If the success state is "zero rows", say what
   stops someone from emptying the table.

If the gap's own success command fails one of these, say which one — the
synthesiser must then either add a second check or narrow the claim. Do not
invent a new command; the cycle only accepts registered ones.

Write one JSON object to the requested output path, no fences:

{
  "view": "2-4 sentences: what a correct proof looks like for this gap.",
  "verification_command": "the brief's success command, or a stricter registered one",
  "falsifier": "the observation that would prove the fix is cosmetic",
  "risks": ["ways the oracle can be satisfied without fixing anything"],
  "confidence": 0.0
}
