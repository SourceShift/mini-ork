#!/usr/bin/env python3
"""Deterministic tail stages for the self-improving-harness-research recipe.

The model nodes (shard extraction, clustering, per-group extraction) each read
one bounded artifact and write one JSON file. Everything that turns those
per-group technique extractions into (a) a single clustered, provenance-bound
technique compendium and (b) a book-ready outline lives here, so it is
replayable without a model and every citation is checked against the retrieved
corpus rather than trusted to a model.

Collection + sharding reuse ``frontier-llm-research/lib/research_pipeline.py``;
the technique catalog and cluster-split reuse
``rsi-technique-review/lib/rsi_review.py``. This module starts at the per-group
extraction outputs and replaces that recipe's mini-ork impact-review tail with a
practitioner-facing compendium and a generic (product-agnostic) book outline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Must match rsi_review.split's GROUP_COUNT: the clusterer emits exactly this
# many technique families and the book gets one chapter per family.
GROUP_COUNT = 10
ARXIV_ID = re.compile(r"arxiv:\d{4}\.\d{4,5}(?:v\d+)?")
EVIDENCE_STRENGTH = ("strong", "moderate", "weak")


class HarnessError(RuntimeError):
    """Raised when a tail artifact cannot meet the recipe's evidence contract."""


def _read_json(path: Path) -> Any:
    text = Path(path).read_text(encoding="utf-8").strip()
    # Models occasionally fence JSON despite the prompt; strip one fence.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{path} is not valid JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _unversioned(source_id: str) -> str:
    return re.sub(r"v\d+$", "", source_id)


def _corpus_sources(corpus_path: Path) -> dict[str, dict[str, Any]]:
    corpus = _read_json(corpus_path)
    sources = corpus.get("sources") if isinstance(corpus, dict) else None
    if not isinstance(sources, list):
        raise HarnessError("source corpus has no sources list")
    return {str(s["source_id"]): s for s in sources if isinstance(s, dict) and s.get("source_id")}


def _cite(source_id: str, corpus: dict[str, dict[str, Any]]) -> str:
    src = corpus.get(source_id) or {}
    url = str(src.get("url") or f"https://arxiv.org/abs/{source_id.split(':', 1)[-1]}")
    return f"[{source_id}]({url})"


# ---------------------------------------------------------------- compendium


def compendium(group_paths: list[Path], corpus_path: Path, compendium_out: Path, techniques_out: Path) -> None:
    """Merge the per-group extractions into one clustered technique set.

    Drops any evidence id the corpus does not contain (no invented citations can
    reach the book), requires every retained technique to carry at least one
    corpus-backed source, and fails if a whole group came back empty.
    """
    corpus = _corpus_sources(corpus_path)
    groups_meta: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for path in sorted(group_paths):
        payload = _read_json(Path(path))
        group_id = str(payload.get("group_id") or "")
        if not re.fullmatch(r"G\d{2}", group_id):
            raise HarnessError(f"{path} has no valid group_id (got {group_id!r})")
        kept: list[dict[str, Any]] = []
        for index, tech in enumerate(payload.get("techniques") or [], start=1):
            if not isinstance(tech, dict) or not tech.get("technique"):
                continue
            evidence = sorted({str(i) for i in (tech.get("evidence") or []) if str(i) in corpus})
            if not evidence:
                continue
            tid = str(tech.get("id") or "")
            if not re.fullmatch(rf"{group_id}-T\d{{2}}", tid) or tid in seen_ids:
                tid = f"{group_id}-T{index:02d}"
            seen_ids.add(tid)
            strength = str(tech.get("evidence_strength") or "weak").lower()
            kept.append(
                {
                    "id": tid,
                    "group_id": group_id,
                    "technique": str(tech["technique"]).strip(),
                    "mechanism": str(tech.get("mechanism") or "").strip(),
                    "reported_results": str(tech.get("reported_results") or "").strip(),
                    "evidence_strength": strength if strength in EVIDENCE_STRENGTH else "weak",
                    "applicability": str(tech.get("applicability") or "").strip(),
                    "implementation_sketch": str(
                        tech.get("implementation_sketch") or tech.get("adoption_sketch") or ""
                    ).strip(),
                    "cautions": str(tech.get("cautions") or "").strip(),
                    "evidence": evidence,
                }
            )
        if not kept:
            raise HarnessError(f"{path} ({group_id}) produced no techniques with corpus-backed evidence")
        flat.extend(kept)
        groups_meta.append(
            {
                "group_id": group_id,
                "name": str(payload.get("name") or group_id),
                "scope": str(payload.get("scope") or ""),
                "technique_ids": [t["id"] for t in kept],
            }
        )
    if len(groups_meta) != GROUP_COUNT:
        raise HarnessError(f"expected {GROUP_COUNT} group extractions, got {len(groups_meta)}")
    groups_meta.sort(key=lambda g: g["group_id"])
    cited_papers = sorted({sid for t in flat for sid in t["evidence"]})
    _write_json(
        techniques_out,
        {
            "technique_count": len(flat),
            "group_count": len(groups_meta),
            "corpus_source_count": len(corpus),
            "cited_paper_count": len(cited_papers),
            "groups": groups_meta,
            "techniques": flat,
        },
    )

    by_id = {t["id"]: t for t in flat}
    lines = [
        "# Self-Improving Agent Harnesses — 2026 Technique Compendium",
        "",
        "## Scope",
        "",
        f"{len(flat)} canonical techniques across {len(groups_meta)} mechanism families, distilled from "
        f"{len(corpus)} distinct 2026 arXiv papers ({len(cited_papers)} of them cited as direct evidence below). "
        "Every technique is bound to the arXiv source ids its evidence came from; ids the retrieved corpus did not "
        "contain were dropped before assembly, so no citation here is invented. Reported results are quoted as the "
        "abstracts state them, not reproduced.",
        "",
        "## Technique Families",
        "",
    ]
    for group in groups_meta:
        lines += [f"### {group['group_id']} — {group['name']}", ""]
        if group["scope"]:
            lines += [group["scope"], ""]
        for tid in group["technique_ids"]:
            tech = by_id[tid]
            evidence = ", ".join(_cite(sid, corpus) for sid in tech["evidence"][:8])
            if len(tech["evidence"]) > 8:
                evidence += f" +{len(tech['evidence']) - 8} more"
            lines += [f"#### {tech['id']} · {tech['technique']}  _({tech['evidence_strength']} evidence)_", ""]
            lines += [f"- **Mechanism:** {tech['mechanism'] or 'not described'}"]
            lines += [f"- **Reported results:** {tech['reported_results'] or 'not stated in abstracts'}"]
            if tech["applicability"]:
                lines += [f"- **When to use:** {tech['applicability']}"]
            if tech["implementation_sketch"]:
                lines += [f"- **How to apply:** {tech['implementation_sketch']}"]
            if tech["cautions"]:
                lines += [f"- **Cautions:** {tech['cautions']}"]
            lines += [f"- **Evidence:** {evidence}", ""]
    lines += [
        "## Evidence Limits",
        "",
        "Per-paper extraction read arXiv titles, abstracts, and metadata only. Technique mechanisms and reported "
        "numbers are as stated in those abstracts; no full-text claims and no numbers absent from the retrieved "
        "records were added.",
    ]
    _write_text(compendium_out, "\n".join(lines))


# ---------------------------------------------------------------- book spec


def _chapter_key_papers(technique_ids: list[str], by_id: dict[str, dict[str, Any]], corpus: dict[str, dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    freq: dict[str, int] = {}
    for tid in technique_ids:
        for sid in by_id[tid]["evidence"]:
            freq[sid] = freq.get(sid, 0) + 1
    ranked = sorted(freq, key=lambda sid: (-freq[sid], -float(corpus.get(sid, {}).get("citation_count") or 0), sid))
    out = []
    for sid in ranked[:limit]:
        src = corpus.get(sid, {})
        out.append({"source_id": sid, "title": str(src.get("title") or ""), "url": str(src.get("url") or "")})
    return out


def book_spec(techniques_path: Path, corpus_path: Path, config_path: Path, outline_out: Path, brief_out: Path) -> None:
    """Turn the clustered technique set into a product-agnostic book outline.

    One technique chapter per cluster, bracketed by an introduction and a
    synthesis chapter. The outline is deliberately free of any product-specific
    API shape: a downstream book-compose step maps it onto whatever generate-plan
    payload its platform wants.
    """
    data = _read_json(techniques_path)
    corpus = _corpus_sources(corpus_path)
    config = _read_json(config_path)
    groups = data.get("groups") or []
    techs = data.get("techniques") or []
    if len(groups) != GROUP_COUNT:
        raise HarnessError(f"techniques payload has {len(groups)} clusters, expected {GROUP_COUNT}")
    by_id = {t["id"]: t for t in techs}

    topic = str(config.get("topic") or "Self-Improving Agent Harnesses")
    audience = str(config.get("target_audience") or "intermediate")
    audience_label = str(config.get("audience_label") or "practitioners building LLM agent systems")

    chapters: list[dict[str, Any]] = []
    chapters.append(
        {
            "index": 1,
            "kind": "frontmatter",
            "cluster_id": None,
            "title": str(config.get("intro_title") or "Introduction: Why the Harness Is the Unit That Learns"),
            "synopsis": (
                "Define the harness (prompts, tools, memory, skills, sub-agents, control flow) as the layer that "
                "mediates a frozen model, and motivate why improving the harness online — rather than retraining the "
                "model — is the practical path to self-improving agents. Frame the rest of the book as a tour of the "
                f"{GROUP_COUNT} mechanism families practitioners can adopt today."
            ),
            "technique_ids": [],
            "key_papers": [],
        }
    )
    for offset, group in enumerate(groups, start=1):
        tids = group["technique_ids"]
        chapters.append(
            {
                "index": 1 + offset,
                "kind": "technique-cluster",
                "cluster_id": group["group_id"],
                "title": str(group["name"]),
                "scope": str(group.get("scope") or ""),
                "synopsis": (
                    f"Cover the {len(tids)} techniques in the '{group['name']}' family: for each, its mechanism, the "
                    "results reported in the source papers, when a practitioner should reach for it, how to build it "
                    "into a real agent system, and its failure modes."
                ),
                "technique_count": len(tids),
                "technique_ids": tids,
                "key_papers": _chapter_key_papers(tids, by_id, corpus),
            }
        )
    chapters.append(
        {
            "index": len(chapters) + 1,
            "kind": "synthesis",
            "cluster_id": None,
            "title": str(config.get("conclusion_title") or "Building Your Own Continual Harness: A Practitioner's Blueprint"),
            "synopsis": (
                "Compose the families into one reference architecture for a reset-free, self-improving harness: how the "
                "pieces (reward/verifier, memory/skills, prompt/workflow search, routing, containment, evaluation) fit, "
                "a build order, and the metrics that tell you it is actually improving. Close on open problems and "
                "self-improvement limits (collapse, reward hacking)."
            ),
            "technique_ids": [],
            "key_papers": [],
        }
    )

    custom_requirements = _custom_requirements(topic, audience_label, groups, config)
    outline = {
        "topic": topic,
        "anchor_paper": str(config.get("anchor_paper") or ""),
        "target_audience": audience,
        "audience_label": audience_label,
        "scope": str(config.get("scope") or "comprehensive"),
        "target_language": str(config.get("target_language") or "en"),
        "publisher_style": str(config.get("publisher_style") or "oreilly-hands-on"),
        "domain": str(config.get("domain") or "Artificial Intelligence"),
        "estimated_chapters": len(chapters),
        "cluster_count": GROUP_COUNT,
        "technique_count": data.get("technique_count"),
        "corpus_source_count": data.get("corpus_source_count"),
        "cited_paper_count": data.get("cited_paper_count"),
        "evidence_source": "LibWit arXiv corpus, 2026 publications only",
        "chapters": chapters,
        "custom_requirements": custom_requirements,
    }
    _write_json(outline_out, outline)

    brief = [
        f"# Book Brief — {topic}",
        "",
        f"**Audience:** {audience_label} (`{audience}`)  ",
        f"**Anchor paper:** {config.get('anchor_paper') or 'n/a'}  ",
        f"**Scope:** {outline['scope']} · **Style:** {outline['publisher_style']} · "
        f"**Chapters:** {len(chapters)} ({GROUP_COUNT} technique families + intro + synthesis)  ",
        f"**Evidence base:** {data.get('technique_count')} techniques from {data.get('cited_paper_count')} cited "
        f"2026 papers (of {data.get('corpus_source_count')} retrieved).",
        "",
        "## Chapter plan",
        "",
    ]
    for ch in chapters:
        brief += [f"### {ch['index']}. {ch['title']}"]
        if ch.get("cluster_id"):
            brief += [f"_Cluster {ch['cluster_id']} · {ch.get('technique_count', 0)} techniques_"]
        brief += ["", ch["synopsis"], ""]
        if ch.get("key_papers"):
            brief += ["Key papers:"]
            brief += [f"- {kp['source_id']} — {kp['title']}" for kp in ch["key_papers"]]
            brief += [""]
    _write_text(brief_out, "\n".join(brief))


def _custom_requirements(topic: str, audience_label: str, groups: list[dict[str, Any]], config: dict[str, Any]) -> str:
    cluster_lines = "\n".join(
        f"- Chapter for cluster {g['group_id']} — \"{g['name']}\": cover its {len(g['technique_ids'])} techniques "
        f"({', '.join(g['technique_ids'][:6])}{'…' if len(g['technique_ids']) > 6 else ''})."
        for g in groups
    )
    return (
        f"BOOK: {topic}\n"
        f"AUDIENCE: {audience_label}. Assume comfort with Python and LLM APIs; do not re-teach what an LLM is.\n"
        f"ANCHOR: Framed around {config.get('anchor_paper') or 'the self-improving-harness thesis'} — the harness "
        "(prompts, tools, memory, skills, sub-agents, control flow) as the unit that adapts online.\n"
        "\n"
        "STRUCTURE (hard requirement):\n"
        "- One chapter per technique family below, in the given order, plus an introduction and a closing "
        "synthesis/blueprint chapter.\n"
        f"{cluster_lines}\n"
        "\n"
        "EACH TECHNIQUE CHAPTER MUST:\n"
        "- Open with the practitioner problem the family solves and a mental model.\n"
        "- For every technique: its mechanism, the results the source papers report (quoted, with the arXiv id), "
        "when to use it, a concrete how-to-apply sketch (pseudocode or architecture, not a full framework), and its "
        "failure modes/cautions.\n"
        "- Include one comparison table across the family's techniques (axes: what it changes, evidence strength, "
        "effort, when to prefer it).\n"
        "- Cite only arXiv ids that appear in the supplied technique compendium; never invent a citation or a number.\n"
        "\n"
        "TONE: hands-on and decision-oriented. Prefer diagrams and worked examples over proofs. Keep negative results "
        "(model collapse, reward hacking, zero-gain skills) — they are part of the engineering judgment.\n"
        "\n"
        "SOURCE OF TRUTH: the accompanying technique-compendium.md and techniques.json are the evidence base; the "
        "book must not exceed their claims."
    )


# ---------------------------------------------------------------- verify


def verify(outline_path: Path, compendium_path: Path, corpus_path: Path) -> None:
    outline = _read_json(outline_path)
    compendium_text = Path(compendium_path).read_text(encoding="utf-8")
    corpus = _corpus_sources(corpus_path)
    chapters = outline.get("chapters") or []
    cluster_chapters = [c for c in chapters if c.get("kind") == "technique-cluster"]
    if len(cluster_chapters) != GROUP_COUNT:
        raise HarnessError(f"outline has {len(cluster_chapters)} technique-cluster chapters, expected {GROUP_COUNT}")
    for chapter in cluster_chapters:
        if not re.fullmatch(r"G\d{2}", str(chapter.get("cluster_id") or "")):
            raise HarnessError(f"cluster chapter {chapter.get('title')!r} has an invalid cluster_id")
        if not chapter.get("technique_ids"):
            raise HarnessError(f"cluster chapter {chapter.get('cluster_id')} carries no techniques")
    if not str(outline.get("custom_requirements") or "").strip():
        raise HarnessError("outline has empty custom_requirements")
    group_headings = re.findall(r"^### G\d{2} — ", compendium_text, flags=re.M)
    if len(group_headings) != GROUP_COUNT:
        raise HarnessError(f"compendium lists {len(group_headings)} families, expected {GROUP_COUNT}")
    cited = {_unversioned(i) for i in ARXIV_ID.findall(compendium_text)}
    if not cited:
        raise HarnessError("compendium cites no arXiv sources")
    unknown = sorted(cited - {_unversioned(i) for i in corpus})
    if unknown:
        raise HarnessError(f"compendium cites {len(unknown)} ids absent from the corpus: {', '.join(unknown[:10])}")
    print(
        json.dumps(
            {
                "chapters": len(chapters),
                "technique_clusters": len(cluster_chapters),
                "cited_papers": len(cited),
                "corpus": len(corpus),
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("compendium")
    p.add_argument("--group", type=Path, action="append", required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--compendium-out", type=Path, required=True)
    p.add_argument("--techniques-out", type=Path, required=True)
    p = sub.add_parser("book-spec")
    p.add_argument("--techniques", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--outline-out", type=Path, required=True)
    p.add_argument("--brief-out", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--outline", type=Path, required=True)
    p.add_argument("--compendium", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "compendium":
            compendium(args.group, args.corpus, args.compendium_out, args.techniques_out)
        elif args.cmd == "book-spec":
            book_spec(args.techniques, args.corpus, args.config, args.outline_out, args.brief_out)
        elif args.cmd == "verify":
            verify(args.outline, args.compendium, args.corpus)
    except HarnessError as exc:
        sys.stderr.write(f"self-improving-harness-research: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
