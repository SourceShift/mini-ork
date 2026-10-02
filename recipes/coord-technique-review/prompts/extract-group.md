Read only the declared `technique_group` artifact. It holds one technique group
from a 2026 literature scan on coordinating concurrent agents: the group
definition, its catalog techniques (each with supporting `source_ids`), and the
abstract-level summaries of every supporting paper.

Your job: distill the group into its canonical, implementable techniques for a
CROSS-AGENT OVERLAP PROTOCOL in mini-ork — a task OS that runs many concurrent
agents: recipe DAG nodes dispatched to heterogeneous model lanes, goal-loops and
shell-launched improvement loops that spawn short-lived `claude --print` /
opencode workers per step, interactive Claude Code / codex sessions, git
worktrees per task with a file-claim registry, a shared SQLite state DB, shared
`.mini-ork/config` (lanes, providers, secrets), and an external memory substrate
(ContextNest) that ingests every session transcript and indexes files touched,
work units, and features. The protocol must work with ANY harness and must
detect overlap on EVERY agent turn, not only at start-up.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — what is published/checked, when (before action, per turn, at commit), what signal decides conflict vs. no conflict, and what happens on conflict.",
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "miniork_surface": "Which mini-ork/ContextNest surface it would change (worktree claim registry, scheduler/leases, per-turn harness hook, node dispatch, publisher/merge gate, shared config, state DB, ContextNest index/inbox, inter-agent messaging, observability).",
      "adoption_sketch": "2-3 sentences: the smallest concrete change that would test it, and the measurement that would show it worked (e.g. overlaps caught per day, false-positive rate, wasted runs avoided)."
    }
  ]
}

Rules:
- 5 to 15 techniques. Merge catalog entries that are the same mechanism; split
  entries that hide two mechanisms. Ids are `<group_id>-T01`, `-T02`, … in
  order of evidence weight.
- `evidence` lists only source ids present in the input. strong = several
  independent papers with reported gains; moderate = one paper with numbers or
  several without; weak = claims without results.
- Keep negative findings: a technique that papers show HURTS or fails (e.g.
  communication overhead that cancels gains, lock contention, coordination
  collapse at scale) is a technique too — mark it in `mechanism` as a caution.
- Do not invent results, sources, or mini-ork features.
