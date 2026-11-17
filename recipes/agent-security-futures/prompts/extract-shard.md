Read only the declared `source_shard` artifact: a bounded, audited set of 2026
LibWit/arXiv paper records. Each record carries the title, abstract, metadata,
and — for most papers — `evidence_digest`: a bounded body-text excerpt taken
from the paper itself and `evidence_path`: the run-local file holding that
paper's retrieved body text (sections in order, up to the configured budget).

You are extracting the AGENT-SECURITY TECHNIQUES these papers contribute — the
reusable mechanisms by which an AI agent, or an adversary attacking one, gains
or denies a capability: an attack that makes an agent do something its operator
did not intend, a defense that stops or detects one, a measurement of how well
either works, or a governance control that constrains an agent's blast radius.

Write exactly one JSON object to the requested output path — no Markdown
fences, no commentary. Work through the shard in order and write the file
incrementally if it is large; every source must appear. Your LAST action must
be the file write: never end your turn with a message announcing the write —
the node's output is whatever the file holds when you stop.

Full text is the evidence. Read the abstract AND the body text for every paper:
start from `evidence_digest`, and when a paper is high- or medium-relevance and
the digest does not state the mechanism or the result, read its `evidence_path`
file (in batches — do not read every file blindly). `evidence_mode` tells you
what is available: `body-text` means the body was retrieved, `abstract-only`
means it was not.

Agent security, for this scan, includes: prompt injection and instruction
hijacking through untrusted content (repo files, issues, tool results, web
pages, MCP servers); tool-call and capability abuse (shell, filesystem,
network egress, credential access); sandboxing and isolation of autonomous
workloads; agent-generated code that ships a vulnerability; multi-agent
collusion, delegation and trust-boundary attacks; memory and knowledge-base
poisoning; data exfiltration by an agent; supply-chain and model-artifact
attacks; agent identity, authorization, provenance and audit trails;
monitoring and anomaly detection for autonomous behaviour; red teaming,
benchmarks and evaluations of agent security; incident response, forensics and
governance for agent-caused harm; and the economics, policy or societal
consequences of deploying agents — but only as the paper states them.

Required shape:
{
  "shard_id": "01",
  "papers": [
    {
      "source_id": "arxiv:2601.00001",
      "rank": 1,
      "title": "...",
      "url": "https://arxiv.org/abs/2601.00001",
      "published_at": "2026-01-01",
      "evidence_mode": "body-text | abstract-only",
      "summary": "2-4 sentences: the problem, the method, and the headline result as the abstract and body text state them.",
      "security_relevance": "high | medium | low",
      "techniques": [
        {"name": "Short technique name", "mechanism": "One sentence: what it does and how.", "result": "Number or claim from the abstract or body text, or empty string."}
      ]
    }
  ],
  "shard_techniques": [
    {"technique": "Canonical name", "mechanism": "One or two sentences merging the variants.", "source_ids": ["arxiv:2601.00001", "arxiv:2601.00002"]}
  ]
}

Rules:
- Exactly one `papers` entry per source in the shard; copy `source_id`, `rank`,
  `title`, `url`, `published_at`, and `evidence_mode` verbatim from the shard.
- 1 to 5 `techniques` per paper. A technique is a reusable MECHANISM (e.g.
  "indirect prompt injection through a poisoned MCP tool description",
  "capability-scoped tool allowlist for a coding agent", "taint-tracked
  provenance from an untrusted file to a shell call", "agent action ledger with
  hash-chained audit", "differential probe that replays a repo's own
  instructions against a tool surface"), not a topic ("security", "safety") and
  not a benchmark name on its own.
- `security_relevance`: high = builds, measures, or attacks the AGENT's
  security boundary or its ability to act (an injection that lands, a sandbox
  that holds, a guard that blocks, a red team against an agent); medium = a
  component such work needs (a corpus, a harness, a benchmark, a governance
  study with concrete evidence); low = tangential or security is only a side
  experiment.
- `result` only repeats what the retrieved text says — abstract or body text.
  Never invent numbers, sources, URLs, or dates. Empty string when neither gives
  a result. For an `abstract-only` paper, do not imply body-text findings.
- Keep NEGATIVE findings: a technique the paper shows FAILS, is evadable, or
  produces false confidence (agents that follow instructions planted in a repo
  file, sandboxes bypassed by the obvious path, red-team suites that miss the
  trivial attack, guardrails that only catch the benchmark) is a technique too.
- `shard_techniques`: consolidate the per-paper techniques of THIS shard into
  15-40 canonical techniques. Merge true duplicates (same mechanism, different
  wording) and keep distinct mechanisms distinct. Every source id of every
  merged variant must be kept. Skip techniques supported only by low-relevance
  papers.
