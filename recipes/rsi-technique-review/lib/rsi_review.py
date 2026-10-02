#!/usr/bin/env python3
"""Deterministic stages of the rsi-technique-review recipe.

The LLM nodes (shard extraction, clustering, per-group extraction, the two
impact reviewers, the report writer) only ever read one bounded artifact and
write one JSON/Markdown artifact. Everything between them — validation,
coverage accounting, cluster splitting, evidence checking, score merging, the
final assembly, and the completeness verdict — lives here so it is replayable
without a model.

Collection and sharding reuse ``frontier-llm-research/lib/research_pipeline.py``
unchanged; this module starts at the shard-extraction outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

GROUP_COUNT = 10
VERDICTS = ("implement-now", "next", "watch", "skip")
NOVELTY = ("new", "extends", "already-shipped")
EFFORT_WEIGHT = {"S": 1.0, "M": 1.5, "L": 2.5}
ARXIV_ID = re.compile(r"arxiv:\d{4}\.\d{4,5}(?:v\d+)?")


def _unversioned(source_id: str) -> str:
    """LibWit occasionally keeps a version suffix (arxiv:2604.05112v1) on a
    corpus source_id; compare citations with the suffix stripped on both sides."""
    return re.sub(r"v\d+$", "", source_id)


class ReviewError(RuntimeError):
    pass


def _min_coverage() -> float:
    return float(os.environ.get("MO_RSI_REVIEW_MIN_COVERAGE", "0.95"))


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8").strip()
    # Models occasionally fence the JSON despite the prompt; strip one fence.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"{path} is not valid JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _corpus_sources(corpus_path: Path) -> dict[str, dict[str, Any]]:
    corpus = _read_json(corpus_path)
    sources = corpus.get("sources") if isinstance(corpus, dict) else None
    if not isinstance(sources, list):
        raise ReviewError("source corpus has no sources list")
    return {str(s["source_id"]): s for s in sources if isinstance(s, dict) and s.get("source_id")}


# ---------------------------------------------------------------- catalog


def catalog(extraction_paths: list[Path], corpus_path: Path, catalog_out: Path, papers_out: Path) -> None:
    """Merge shard extractions into a technique catalog + paper index.

    Tolerates a small number of papers a shard worker skipped (recorded as
    coverage gaps) but fails below MO_RSI_REVIEW_MIN_COVERAGE, and drops any
    source id the corpus does not contain rather than let it reach a citation.
    """
    corpus = _corpus_sources(corpus_path)
    papers: dict[str, dict[str, Any]] = {}
    techniques: list[dict[str, Any]] = []
    dropped_ids: set[str] = set()
    for path in sorted(extraction_paths):
        payload = _read_json(path)
        shard_id = str(payload.get("shard_id") or path.stem)
        for paper in payload.get("papers") or []:
            if not isinstance(paper, dict):
                continue
            sid = str(paper.get("source_id") or "")
            if sid not in corpus:
                dropped_ids.add(sid)
                continue
            if sid in papers:
                continue
            src = corpus[sid]
            papers[sid] = {
                "source_id": sid,
                "title": src.get("title") or paper.get("title"),
                "url": src.get("url") or paper.get("url"),
                "published_at": src.get("published_at") or paper.get("published_at"),
                "rank": src.get("rank") or paper.get("rank"),
                "summary": str(paper.get("summary") or "").strip(),
                "rsi_relevance": paper.get("rsi_relevance") or paper.get("relevance") or "unknown",
                "techniques": [
                    {k: t.get(k) for k in ("name", "mechanism", "result")}
                    for t in (paper.get("techniques") or [])
                    if isinstance(t, dict) and t.get("name")
                ],
                "shard_id": shard_id,
            }
        for tech in payload.get("shard_techniques") or []:
            if not isinstance(tech, dict) or not tech.get("technique"):
                continue
            ids = [str(i) for i in tech.get("source_ids") or []]
            valid = [i for i in ids if i in corpus]
            dropped_ids.update(i for i in ids if i not in corpus)
            if not valid:
                continue
            techniques.append(
                {
                    "technique": str(tech["technique"]).strip(),
                    "mechanism": str(tech.get("mechanism") or "").strip(),
                    "source_ids": sorted(set(valid)),
                    "shard_id": shard_id,
                }
            )
    covered = len(papers)
    expected = len(corpus)
    if expected == 0 or covered / expected < _min_coverage():
        raise ReviewError(f"shard extraction covered {covered}/{expected} papers; below coverage floor {_min_coverage()}")
    if not techniques:
        raise ReviewError("no shard techniques survived validation")
    techniques.sort(key=lambda t: (-len(t["source_ids"]), t["technique"].lower()))
    for index, tech in enumerate(techniques, start=1):
        tech["key"] = f"T{index:04d}"
    _write_json(
        catalog_out,
        {
            "technique_count": len(techniques),
            "paper_count": covered,
            "coverage": {"expected": expected, "covered": covered, "missing": sorted(set(corpus) - set(papers))},
            "dropped_unknown_source_ids": sorted(i for i in dropped_ids if i),
            "techniques": [
                {"key": t["key"], "technique": t["technique"], "mechanism": t["mechanism"],
                 "support": len(t["source_ids"]), "source_ids": t["source_ids"]}
                for t in techniques
            ],
        },
    )
    _write_json(papers_out, {"paper_count": covered, "papers": papers})


# ---------------------------------------------------------------- split


def split(clusters_path: Path, catalog_path: Path, papers_path: Path, out_dir: Path, unclustered_out: Path) -> None:
    """Validate the clusterer's grouping and write one bounded file per group."""
    clusters = _read_json(clusters_path)
    cat = _read_json(catalog_path)
    papers = _read_json(papers_path)["papers"]
    by_key = {t["key"]: t for t in cat["techniques"]}
    groups = clusters.get("groups") if isinstance(clusters, dict) else None
    if not isinstance(groups, list) or len(groups) != GROUP_COUNT:
        raise ReviewError(f"clusterer must emit exactly {GROUP_COUNT} groups, got {len(groups) if isinstance(groups, list) else 'none'}")
    assigned: set[str] = set()
    for index, group in enumerate(groups, start=1):
        keys = []
        for key in group.get("technique_keys") or []:
            key = str(key)
            if key in by_key and key not in assigned:
                assigned.add(key)
                keys.append(key)
        if not keys:
            raise ReviewError(f"group {index} ({group.get('name')}) has no valid technique keys")
        members = [by_key[k] for k in keys]
        source_ids = sorted({sid for t in members for sid in t["source_ids"] if sid in papers})
        group_id = f"G{index:02d}"
        _write_json(
            out_dir / f"group-{index:02d}.json",
            {
                "group_id": group_id,
                "name": str(group.get("name") or group_id),
                "scope": str(group.get("scope") or ""),
                "technique_count": len(members),
                "paper_count": len(source_ids),
                "techniques": members,
                "papers": [
                    {
                        "source_id": sid,
                        "title": papers[sid]["title"],
                        "published_at": papers[sid]["published_at"],
                        # Bounded read for the group extractor: the mechanism
                        # text already lives on the catalog techniques above.
                        "summary": papers[sid]["summary"][:500],
                        "techniques": [
                            {"name": t.get("name"), "result": t.get("result") or ""}
                            for t in papers[sid]["techniques"]
                        ],
                    }
                    for sid in source_ids
                ],
            },
        )
    unassigned = [k for k in by_key if k not in assigned]
    if len(unassigned) > 0.05 * len(by_key):
        raise ReviewError(f"clusterer left {len(unassigned)}/{len(by_key)} techniques unassigned (>5%)")
    _write_json(unclustered_out, {"unclustered": [by_key[k] for k in unassigned]})


# ---------------------------------------------------------------- pack


def pack(group_paths: list[Path], corpus_path: Path, state_path: Path, unclustered_path: Path | None, out: Path) -> None:
    """Merge per-group extractions into the review pack both reviewers read."""
    corpus = _corpus_sources(corpus_path)
    groups_out = []
    flat = []
    seen_ids: set[str] = set()
    for path in sorted(group_paths):
        payload = _read_json(path)
        group_id = str(payload.get("group_id") or "")
        if not re.fullmatch(r"G\d{2}", group_id):
            raise ReviewError(f"{path} has no valid group_id")
        kept = []
        for index, tech in enumerate(payload.get("techniques") or [], start=1):
            if not isinstance(tech, dict) or not tech.get("technique"):
                continue
            evidence = sorted({str(i) for i in tech.get("evidence") or [] if str(i) in corpus})
            if not evidence:
                continue
            tid = str(tech.get("id") or f"{group_id}-T{index:02d}")
            if not tid.startswith(group_id) or tid in seen_ids:
                tid = f"{group_id}-T{index:02d}"
            seen_ids.add(tid)
            entry = {
                "id": tid,
                "group_id": group_id,
                "technique": str(tech["technique"]).strip(),
                "mechanism": str(tech.get("mechanism") or "").strip(),
                "reported_results": str(tech.get("reported_results") or "").strip(),
                "evidence_strength": tech.get("evidence_strength") or "weak",
                "evidence": evidence,
                "miniork_surface": str(tech.get("miniork_surface") or "").strip(),
                "adoption_sketch": str(tech.get("adoption_sketch") or "").strip(),
            }
            kept.append(entry)
            flat.append(entry)
        if not kept:
            raise ReviewError(f"{path} produced no techniques with valid evidence")
        groups_out.append({"group_id": group_id, "name": payload.get("name"), "scope": payload.get("scope"), "technique_ids": [t["id"] for t in kept]})
    if len(groups_out) != GROUP_COUNT:
        raise ReviewError(f"expected {GROUP_COUNT} group extractions, got {len(groups_out)}")
    unclustered = _read_json(unclustered_path).get("unclustered", []) if unclustered_path and unclustered_path.exists() else []
    _write_json(
        out,
        {
            "technique_count": len(flat),
            "groups": sorted(groups_out, key=lambda g: g["group_id"]),
            "techniques": flat,
            "unclustered_catalog_entries": unclustered,
            "miniork_state_markdown": state_path.read_text(encoding="utf-8"),
        },
    )


# ---------------------------------------------------------------- merge reviews


def _score_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        impact = int(entry["impact"])
        confidence = int(entry["evidence_confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    effort = str(entry.get("effort") or "M").upper()[:1]
    verdict = str(entry.get("verdict") or "watch")
    novelty = str(entry.get("novelty_vs_shipped") or "new")
    return {
        "impact": max(1, min(5, impact)),
        "evidence_confidence": max(1, min(5, confidence)),
        "effort": effort if effort in EFFORT_WEIGHT else "M",
        "verdict": verdict if verdict in VERDICTS else "watch",
        "novelty_vs_shipped": novelty if novelty in NOVELTY else "new",
        "landing_surface": str(entry.get("landing_surface") or ""),
        "rationale": str(entry.get("rationale") or ""),
    }


def priority(scores: list[dict[str, Any]]) -> float:
    """Rank key for a technique given one or two reviewer scores.

    impact × confidence / effort, zeroed when every reviewer says the
    mechanism is already shipped, halved when any reviewer says it only
    extends something shipped.
    """
    impact = sum(s["impact"] for s in scores) / len(scores)
    confidence = sum(s["evidence_confidence"] for s in scores) / len(scores)
    effort = sum(EFFORT_WEIGHT[s["effort"]] for s in scores) / len(scores)
    value = impact * confidence / effort
    novelties = {s["novelty_vs_shipped"] for s in scores}
    if novelties == {"already-shipped"}:
        return 0.0
    if novelties & {"extends", "already-shipped"}:
        value *= 0.5
    return round(value, 3)


def merge_reviews(review_paths: list[Path], pack_path: Path, out: Path) -> None:
    pack_data = _read_json(pack_path)
    techniques = {t["id"]: t for t in pack_data["techniques"]}
    per_tech: dict[str, dict[str, dict[str, Any]]] = {tid: {} for tid in techniques}
    reviewers = []
    for path in sorted(review_paths):
        payload = _read_json(path)
        name = str(payload.get("reviewer") or path.stem)
        reviewers.append(name)
        for entry in payload.get("scores") or []:
            tid = str(entry.get("id") or "")
            scored = _score_entry(entry) if tid in per_tech else None
            if scored:
                per_tech[tid][name] = scored
    if len(reviewers) < 2:
        raise ReviewError("impact review needs two independent reviewers")
    rows = []
    unscored = []
    for tid, by_reviewer in per_tech.items():
        if not by_reviewer:
            unscored.append(tid)
            continue
        scores = list(by_reviewer.values())
        verdict_rank = [VERDICTS.index(s["verdict"]) for s in scores]
        impacts = [s["impact"] for s in scores]
        rows.append(
            {
                **techniques[tid],
                "reviews": by_reviewer,
                "priority": priority(scores),
                "mean_impact": round(sum(impacts) / len(impacts), 2),
                # Consensus verdict = the more conservative of the two.
                "verdict": VERDICTS[max(verdict_rank)],
                "single_rater": len(scores) == 1,
                "disagreement": len(scores) > 1 and (max(impacts) - min(impacts) >= 2 or max(verdict_rank) - min(verdict_rank) >= 2),
            }
        )
    if len(unscored) > 0.1 * len(techniques):
        raise ReviewError(f"{len(unscored)}/{len(techniques)} techniques received no valid score from either reviewer")
    rows.sort(key=lambda r: (-r["priority"], -r["mean_impact"], r["id"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    _write_json(
        out,
        {
            "reviewers": reviewers,
            "technique_count": len(rows),
            "unscored": unscored,
            "disagreement_count": sum(r["disagreement"] for r in rows),
            "groups": pack_data["groups"],
            "ranked": rows,
        },
    )


# ---------------------------------------------------------------- assemble + verify


def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip()


def assemble(report_path: Path, merged_path: Path, corpus_path: Path, out: Path) -> None:
    merged = _read_json(merged_path)
    corpus = _corpus_sources(corpus_path)
    report = report_path.read_text(encoding="utf-8").strip()
    if not report:
        raise ReviewError("impact report is empty")
    group_names = {g["group_id"]: g.get("name") or g["group_id"] for g in merged["groups"]}
    lines = [
        report,
        "",
        "## Appendix A — Full Ranked Technique Table",
        "",
        f"Reviewers: {', '.join(merged['reviewers'])}. Priority = mean impact × mean evidence confidence / effort weight "
        "(S=1, M=1.5, L=2.5); ×0.5 when a reviewer marks it as extending shipped work, 0 when all mark it shipped. "
        "Verdict is the most conservative reviewer's. ⚠ = reviewers disagree by ≥2 impact points or ≥2 verdict levels.",
        "",
        "| Rank | ID | Technique | Group | Priority | Impact | Verdict | Evidence |",
        "|---:|---|---|---|---:|---:|---|---|",
    ]
    for row in merged["ranked"]:
        flag = " ⚠" if row["disagreement"] else (" (1 rater)" if row["single_rater"] else "")
        evidence = ", ".join(row["evidence"][:4]) + (f" +{len(row['evidence']) - 4}" if len(row["evidence"]) > 4 else "")
        lines.append(
            f"| {row['rank']} | {row['id']} | {_cell(row['technique'])} | {_cell(group_names.get(row['group_id'], row['group_id']))} "
            f"| {row['priority']} | {row['mean_impact']} | {row['verdict']}{flag} | {evidence} |"
        )
    lines += ["", "## Appendix B — Techniques by Group", ""]
    by_id = {r["id"]: r for r in merged["ranked"]}
    for group in merged["groups"]:
        lines += [f"### {group['group_id']} — {group.get('name')}", "", str(group.get("scope") or ""), ""]
        for tid in group["technique_ids"]:
            row = by_id.get(tid)
            if not row:
                continue
            lines += [
                f"- **{tid} {row['technique']}** (priority {row['priority']}, {row['verdict']}) — {row['mechanism']}",
                f"  - Results reported: {row['reported_results'] or 'not stated in abstracts'}",
                f"  - mini-ork surface: {row['miniork_surface'] or 'n/a'}",
                "  - Evidence: " + ", ".join(f"[{sid}]({corpus[sid].get('url')})" for sid in row["evidence"] if sid in corpus),
            ]
        lines.append("")
    lines += [
        "## Evidence Limits",
        "",
        "Per-paper extraction read LibWit titles, abstracts, and metadata only; reported numbers are as stated in abstracts, "
        f"not reproduced. Impact scores are the judgment of {len(merged['reviewers'])} non-Anthropic model reviewers "
        f"({', '.join(merged['reviewers'])}) against a hand-written snapshot of mini-ork's shipped mechanisms.",
    ]
    _write_text(out, "\n".join(lines))


def verify(final_path: Path, merged_path: Path, corpus_path: Path) -> None:
    text = final_path.read_text(encoding="utf-8")
    merged = _read_json(merged_path)
    corpus = _corpus_sources(corpus_path)
    for heading in ("## Executive Summary", "## Implement Now", "## Appendix A", "## Appendix B"):
        if heading not in text:
            raise ReviewError(f"final review is missing section {heading!r}")
    groups = re.findall(r"^### G\d{2} — ", text, flags=re.M)
    if len(groups) != GROUP_COUNT:
        raise ReviewError(f"final review lists {len(groups)} groups, expected {GROUP_COUNT}")
    cited = {_unversioned(i) for i in ARXIV_ID.findall(text)}
    unknown = sorted(cited - {_unversioned(i) for i in corpus})
    if unknown:
        raise ReviewError(f"final review cites {len(unknown)} ids absent from the corpus: {', '.join(unknown[:10])}")
    known_ids = {r["id"] for r in merged["ranked"]}
    body = text.split("## Appendix A", 1)[0]
    mentioned = set(re.findall(r"\bG\d{2}-T\d{2}\b", body))
    if not mentioned:
        raise ReviewError("report body references no technique ids")
    stray = sorted(mentioned - known_ids)
    if stray:
        raise ReviewError(f"report body references unknown technique ids: {', '.join(stray)}")
    print(json.dumps({"groups": len(groups), "techniques": len(known_ids), "cited_papers": len(cited), "report_ids": len(mentioned)}))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("catalog")
    p.add_argument("--extraction", type=Path, action="append", required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--catalog-out", type=Path, required=True)
    p.add_argument("--papers-out", type=Path, required=True)
    p = sub.add_parser("split")
    p.add_argument("--clusters", type=Path, required=True)
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--papers", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--unclustered-out", type=Path, required=True)
    p = sub.add_parser("pack")
    p.add_argument("--group", type=Path, action="append", required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--unclustered", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("merge-reviews")
    p.add_argument("--review", type=Path, action="append", required=True)
    p.add_argument("--pack", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--merged", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--final", type=Path, required=True)
    p.add_argument("--merged", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "catalog":
            catalog(args.extraction, args.corpus, args.catalog_out, args.papers_out)
        elif args.cmd == "split":
            split(args.clusters, args.catalog, args.papers, args.out_dir, args.unclustered_out)
        elif args.cmd == "pack":
            pack(args.group, args.corpus, args.state, args.unclustered, args.out)
        elif args.cmd == "merge-reviews":
            merge_reviews(args.review, args.pack, args.out)
        elif args.cmd == "assemble":
            assemble(args.report, args.merged, args.corpus, args.out)
        elif args.cmd == "verify":
            verify(args.final, args.merged, args.corpus)
    except ReviewError as exc:
        sys.stderr.write(f"rsi-technique-review: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
