Read the declared `brief` and `lens_panel` artifacts. The panel holds four
independent answers to the same gap — impact, root cause, fix shape, oracle —
reduced to anonymous ids L1..L4 so position carries no authority. The brief
carries the hard constraints: repo root, the success command, the file limit.

Produce ONE proposal. Your job is not to average the panel. It is to resolve it:

- Where the panel agrees, take the agreement — but only if it is argued, not
  restated. Four providers agreeing on "add the missing writer" is evidence;
  four providers agreeing on "improve robustness" is not.
- Where the panel disagrees, decide and say why. A fix-shape that contradicts a
  root cause is the most important disagreement to resolve, because it means one
  of them is wrong about the mechanism.
- Where the panel is uncertain, narrow the claim rather than widening the fix.
  A proposal that claims less and is provable beats one that claims everything.

Hard requirements, checked mechanically after you:

- `files` must be repo-relative paths that exist today, at most 5, all needed
  for the one change. No new files unless the root cause requires one.
- `verification_command` must be the brief's success command unless an oracle
  lens made a specific case for a stricter registered one.
- `do_not_touch` must include anything that measures the gap — the gate code,
  frozen config, or the tests that define success.

Write one JSON object to the requested output path, no fences:

{
  "candidate_id": "<copy the brief's candidate_id exactly>",
  "files": ["repo-relative paths — 5 at most"],
  "change_summary": "3-5 sentences a competent implementer can act on: what changes, where, and why it closes the gap.",
  "root_cause": "1-3 sentences: the mechanism, as the panel best established it.",
  "ac_ids": ["AC1", "AC2", "AC3"],
  "do_not_touch": ["paths the fix must not modify"],
  "verification_command": "<the success command>",
  "dissent": "one sentence: which lens you did not follow, and why."
}

`dissent` is required even when there was no dissent — write "none" and mean it.
It is the only place the losing argument survives, and it is what a critic
reads first when deciding whether the proposal was a real synthesis.
