Read the declared `brief` and `lens_panel` artifacts. The panel holds three
independent answers to the same gap — root cause, fix shape, oracle —
reduced to anonymous ids L1..L3 so position carries no authority. The brief
carries the hard constraints: repo root, the success command, the file limit.

Produce ONE proposal. Your job is not to average the panel. It is to resolve it:

- Where the panel agrees, take the agreement — but only if it is argued, not
  restated. Three providers agreeing on "add the missing writer" is evidence;
  three providers agreeing on "improve robustness" is not.
- Where the panel disagrees, decide and say why. A fix-shape that contradicts a
  root cause is the most important disagreement to resolve, because it means one
  of them is wrong about the mechanism.
- Where the panel is uncertain, narrow the claim rather than widening the fix.
  A proposal that claims less and is provable beats one that claims everything.

Hard requirements, checked mechanically after you:

- `files` must be repo-relative paths that exist today, all needed for the one
  change. A file the fix must CREATE — a migration, a new test module — goes in
  `new_files`, never in `files`. `files` plus `new_files` together are at most 5.
- Never name a directory (`db/migrations`, `tests/unit`). A directory is not a
  file to change, and claiming one collides with every other worktree editing a
  file under it. Name the concrete path, including the exact new filename.
- `verification_command` must be the brief's success command unless an oracle
  lens made a specific case for a stricter registered one.
- `do_not_touch` must include anything that measures the gap — the gate code,
  frozen config, or the tests that define success.
- Never invent a value the gap says is absent. When the gap is "this signal is
  missing", the fix must record the real value where one exists and the absence
  where none does — NULL, or the column's own "unresolved" marker. Do not derive
  a plausible substitute from a neighbouring field: on a fused dispatch `model`
  is the comma-joined chain of lanes, so its first element is the lane tried
  first, not the lane that served — and on a pre-spawn refusal or a dispatcher
  crash nothing served at all. An inferred attribution is indistinguishable
  downstream from a measured one, which makes it worse than the missing column.

Write one JSON object to the requested output path, no fences:

{
  "candidate_id": "<copy the brief's candidate_id exactly>",
  "files": ["repo-relative paths that exist today"],
  "new_files": ["repo-relative paths the fix creates — may be empty"],
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
