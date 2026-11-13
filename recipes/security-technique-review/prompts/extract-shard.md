Read only the declared `source_shard` artifact: a bounded, audited set of 2026
LibWit/arXiv paper records. Each record carries the title, abstract, metadata,
and — for most papers — `evidence_digest`: a bounded body-text excerpt taken
from the paper itself and `evidence_path`: the run-local file holding that
paper's retrieved body text (sections in order, up to the configured budget).

You are extracting the SECURITY TECHNIQUES these papers contribute — the
reusable mechanisms by which software, an agent, or a coding pipeline finds a
security hole, proves one exists, prevents one from being written, or blocks one
at a boundary. Write exactly one JSON object to the requested output path — no
Markdown fences, no commentary. Work through the shard in order and write the
file incrementally if it is large; every source must appear. Your LAST action
must be the file write: never end your turn with a message announcing the write
— the node's output is whatever the file holds when you stop.

Full text is the evidence. Read the abstract AND the body text for every paper:
start from `evidence_digest`, and when a paper is high- or medium-relevance and
the digest does not state the mechanism or the result, read its `evidence_path`
file (in batches — do not read every file blindly). `evidence_mode` tells you
what is available: `body-text` means the body was retrieved, `abstract-only`
means it was not.

Security, for this scan, includes: LLM and agent vulnerability discovery in
source code (SAST, taint/dataflow, CWE detection, false-positive suppression),
agentic penetration testing and repository audit agents, automated red teaming
and exploit or proof-of-concept generation, patch verification by reproduction,
fuzzing and harness synthesis, prompt injection and untrusted-content handling
for tool-using agents, sandboxing and capability restriction (filesystem, shell,
network egress, tool allowlists), supply-chain attack detection, credential and
secret leakage scanning, secure code generation and vulnerability repair,
malware and binary analysis, security operations triage and incident response,
agent audit trails and provenance, and the validity of security benchmarks.

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
- 1 to 5 `techniques` per paper. A technique is a reusable security MECHANISM
  (e.g. "agent-generated exploit as the pass criterion for a security patch",
  "taint-guided LLM review of changed files", "capability-scoped tool allowlist
  for a coding agent", "gitleaks-style entropy plus pattern secret scan at
  commit", "differential prompt-injection probe against a tool surface"), not a
  topic ("security", "safety") and not a benchmark name on its own.
- `security_relevance`: high = builds, measures, or attacks the DETECTOR or the
  BOUNDARY itself (a scanner, an auditor agent, a sandbox, a guard, an exploit
  generator, a red team); medium = a component such detection needs (a corpus, a
  triage model, a harness, a benchmark); low = tangential or security is only a
  side experiment.
- `result` only repeats what the retrieved text says — abstract or body text.
  Never invent numbers, sources, URLs, or dates. Empty string when neither gives
  a result. For an `abstract-only` paper, do not imply body-text findings.
- Body text is evidence: prefer the mechanism and the result as the paper
  states them in the body when the abstract only advertises the contribution.
- Keep NEGATIVE findings: a technique the paper shows FAILS, is evadable, or
  produces false confidence (LLM scanners hallucinating vulnerabilities, agents
  distracted by injected repo text, red-team suites that miss the obvious bug,
  benchmarks that leak their own answers) is a technique too.
- `shard_techniques`: consolidate the per-paper techniques of THIS shard into
  15-40 canonical techniques. Merge true duplicates (same mechanism, different
  wording) and keep distinct mechanisms distinct. Every source id of every merged
  variant must be kept. Skip techniques supported only by low-relevance papers.
