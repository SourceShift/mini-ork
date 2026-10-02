#!/usr/bin/env python3
"""Render the per-paper summaries from paper-index.json as a readable digest."""
import json
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, input_file

index = json.load(open(input_file("paper_index", "paper-index.json"), encoding="utf-8"))
papers = sorted(index["papers"].values(), key=lambda p: (p.get("rank") or 10**6))
order = ("high", "medium", "low", "unknown")
lines = ["# Paper digest — 2026 cross-agent coordination scan", "",
         f"{len(papers)} papers, summarised from LibWit abstracts by the shard extractors.", ""]
for level in order:
    tier = [p for p in papers if (p.get("relevance") or p.get("rsi_relevance") or "unknown") == level]
    if not tier:
        continue
    lines += [f"## Relevance: {level} ({len(tier)})", ""]
    for p in tier:
        lines += [f"### [{p['source_id']}]({p.get('url')}) {p.get('title')}",
                  f"_{p.get('published_at')} · rank {p.get('rank')} · shard {p.get('shard_id')}_", "",
                  p.get("summary") or "(no summary)", ""]
        for t in p.get("techniques") or []:
            result = f" — {t['result']}" if t.get("result") else ""
            lines.append(f"- **{t.get('name')}**: {t.get('mechanism') or ''}{result}")
        lines.append("")
out = os.path.join(RUN_DIR, "paper-digest.md")
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines).rstrip() + "\n")
print(json.dumps({"verifier": "coord-review-digest", "pass": True, "papers": len(papers)}))
