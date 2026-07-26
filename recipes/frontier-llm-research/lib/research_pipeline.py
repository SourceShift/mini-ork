#!/usr/bin/env python3
"""Deterministic stages for the frontier-LLM research recipe.

The model-facing nodes only summarize already retrieved records. Collection,
sharding, schema validation, and final document assembly stay deterministic so
every output can be traced to a LibWit source URL and a run-local artifact.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PipelineError(RuntimeError):
    """Raised when an artifact cannot meet the recipe's evidence contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"JSON artifact {path} must contain an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _published_day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _unversioned_arxiv_url(record: dict[str, Any]) -> str:
    url = str(record.get("url") or record.get("abstract_url") or "").strip()
    arxiv_id = str(record.get("arxiv_id") or record.get("id") or "").strip()
    if not arxiv_id and url:
        arxiv_id = url.rstrip("/").rsplit("/", 1)[-1]
    arxiv_id = arxiv_id.split("v", 1)[0]
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    return url


def _source_id(record: dict[str, Any], url: str) -> str:
    supplied = str(record.get("paper_uid") or "").strip()
    if supplied:
        return supplied
    arxiv_id = str(record.get("arxiv_id") or record.get("id") or "").strip().split("v", 1)[0]
    return f"arxiv:{arxiv_id}" if arxiv_id else url


def _records_from_batch(payload: Any, query_specs: list[dict[str, Any]]) -> Iterable[tuple[dict[str, Any], str]]:
    """Tolerate both documented batch envelopes used by LibWit API revisions."""
    if not isinstance(payload, dict):
        raise PipelineError("LibWit search response must be a JSON object")
    groups = payload.get("results") or payload.get("data") or payload.get("items")
    if not isinstance(groups, list):
        raise PipelineError("LibWit search response has no results list")
    for index, group in enumerate(groups):
        default_topic = str(query_specs[index].get("topic") or "search") if index < len(query_specs) else "search"
        if isinstance(group, dict) and isinstance(group.get("results"), list):
            topic = str(group.get("topic") or group.get("query") or default_topic)
            for record in group["results"]:
                if isinstance(record, dict):
                    yield record, topic
        elif isinstance(group, dict) and "title" in group:
            yield group, default_topic
        elif isinstance(group, list):
            for record in group:
                if isinstance(record, dict):
                    yield record, default_topic


def collect(plan_path: Path, output_path: Path) -> None:
    plan = _read_json(plan_path)
    query_specs = plan.get("queries")
    if not isinstance(query_specs, list) or not query_specs:
        raise PipelineError("collection plan needs a non-empty queries list")
    required_count = int(plan.get("required_source_count") or 0)
    if required_count < 1:
        raise PipelineError("collection plan needs required_source_count >= 1")
    token = os.environ.get("ARXIV_API_TOKEN", "").strip()
    if not token:
        raise PipelineError("ARXIV_API_TOKEN is required; configure it outside the recipe and retry")
    api_base = os.environ.get("MINI_ORK_LIBWIT_API_BASE", "https://arxiv.libwit.ai/api").rstrip("/")
    timeout_seconds = float(os.environ.get("MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC", "45"))
    top = int(plan.get("results_per_query") or 40)
    requests = [
        {
            "query": str(spec.get("query") or ""),
            "top": top,
            "hybrid": True,
            "source": str(plan.get("source") or "arxiv"),
            "date_from": str(plan.get("date_from") or "2026-01-01"),
        }
        for spec in query_specs
    ]
    if any(not item["query"] for item in requests):
        raise PipelineError("every collection query needs text")
    request = Request(
        f"{api_base}/search/batch",
        data=json.dumps({"queries": requests, "max_workers": int(plan.get("max_workers") or 4)}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise PipelineError(f"LibWit API rejected collection request: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PipelineError(f"LibWit API collection request failed: {exc}") from exc

    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    cutoff = _published_day(plan.get("date_from"))
    candidates: dict[str, dict[str, Any]] = {}
    for record, topic in _records_from_batch(payload, query_specs):
        published_at = str(record.get("published") or record.get("published_at") or record.get("date") or "")[:10]
        published = _published_day(published_at)
        if published is None or published.year != 2026 or (cutoff and published < cutoff):
            continue
        title = str(record.get("title") or "").strip()
        url = _unversioned_arxiv_url(record)
        if not title or not url:
            continue
        source_id = _source_id(record, url)
        candidate = candidates.setdefault(
            source_id,
            {
                "source_id": source_id,
                "title": title,
                "url": url,
                "published_at": published_at,
                "retrieved_at": retrieved_at,
                "abstract": str(record.get("abstract") or "").strip()[:8000],
                "authors": record.get("authors") or [],
                "primary_category": str(record.get("primary_category") or record.get("category") or ""),
                "doi": str(record.get("doi") or ""),
                "citation_count": _number(record.get("citation_count")),
                "influential_citation_count": _number(record.get("influential_citation_count")),
                "relevance_score": _number(record.get("score")),
                "topics": set(),
            },
        )
        candidate["topics"].add(topic)
        candidate["relevance_score"] = max(candidate["relevance_score"], _number(record.get("score")))
        candidate["citation_count"] = max(candidate["citation_count"], _number(record.get("citation_count")))
        candidate["influential_citation_count"] = max(
            candidate["influential_citation_count"], _number(record.get("influential_citation_count"))
        )

    today = datetime.now(UTC).date()
    ranked: list[dict[str, Any]] = []
    for candidate in candidates.values():
        published = _published_day(candidate["published_at"])
        recency_days = max((published - date(2026, 1, 1)).days if published else 0, 0)
        candidate["ranking_score"] = round(
            len(candidate["topics"]) * 1000
            + candidate["relevance_score"] * 200
            + math.log1p(candidate["citation_count"]) * 20
            + math.log1p(candidate["influential_citation_count"]) * 20
            + min(recency_days, (today - date(2026, 1, 1)).days),
            3,
        )
        candidate["topics"] = sorted(candidate["topics"])
        ranked.append(candidate)
    ranked.sort(key=lambda item: (-item["ranking_score"], item["published_at"], item["source_id"]))
    selected = ranked[:required_count]
    for rank, source in enumerate(selected, start=1):
        source["rank"] = rank
    if len(selected) < required_count:
        raise PipelineError(
            f"LibWit returned only {len(selected)} distinct 2026 papers; need {required_count}. "
            "Do not run model summaries against an incomplete corpus."
        )
    _write_json(
        output_path,
        {
            "source_count": len(selected),
            "required_source_count": required_count,
            "retrieved_at": retrieved_at,
            "collection_plan": plan_path.name,
            "ranking_policy": plan.get("ranking_policy"),
            "sources": selected,
            "coverage_gaps": [],
            "search_notes": [
                "Collected through the LibWit batch-search endpoint.",
                "Only papers published in 2026 were eligible.",
                "Ranking combines topical coverage, source relevance, citation signals, and recency.",
            ],
        },
    )


def shard(input_path: Path, output_paths: list[Path]) -> None:
    corpus = _read_json(input_path)
    sources = corpus.get("sources")
    required_count = int(corpus.get("required_source_count") or 0)
    if not isinstance(sources, list) or len(sources) < required_count:
        raise PipelineError("source corpus is incomplete and cannot be sharded")
    if not output_paths:
        raise PipelineError("at least one shard output is required")
    if len(sources) % len(output_paths) != 0:
        raise PipelineError("source count must divide evenly across declared shard outputs")
    width = len(sources) // len(output_paths)
    for index, output_path in enumerate(output_paths, start=1):
        slice_start = (index - 1) * width
        shard_sources = sources[slice_start : slice_start + width]
        _write_json(
            output_path,
            {
                "shard_id": f"{index:02d}",
                "source_count": len(shard_sources),
                "required_source_count": len(shard_sources),
                "sources": shard_sources,
            },
        )


def _summary_records(path: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _read_json(path)
    shard_id = str(payload.get("shard_id") or "")
    summaries = payload.get("summaries")
    techniques = payload.get("shard_techniques")
    if not shard_id or not isinstance(summaries, list) or not isinstance(techniques, list):
        raise PipelineError(f"summary artifact {path} has an invalid root shape")
    for summary in summaries:
        if not isinstance(summary, dict):
            raise PipelineError(f"summary artifact {path} contains a non-object summary")
        missing = [key for key in ("source_id", "title", "url", "published_at", "retrieved_at", "summary_paragraph") if not summary.get(key)]
        instructions = summary.get("prompt_instructions")
        if missing or not isinstance(instructions, list) or not 1 <= len(instructions) <= 20:
            raise PipelineError(f"summary artifact {path} has an invalid source record {summary.get('source_id', '?')}")
        if any(not isinstance(item, str) or not item.strip() for item in instructions):
            raise PipelineError(f"summary artifact {path} has blank prompt instructions")
    return shard_id, summaries, techniques


def rollup(input_paths: list[Path], output_path: Path) -> None:
    all_summaries: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for input_path in input_paths:
        shard_id, summaries, techniques = _summary_records(input_path)
        source_ids = [str(summary["source_id"]) for summary in summaries]
        duplicates = seen_ids.intersection(source_ids)
        if duplicates:
            raise PipelineError(f"duplicate source IDs across summary shards: {', '.join(sorted(duplicates))}")
        seen_ids.update(source_ids)
        all_summaries.extend(summaries)
        shards.append({"shard_id": shard_id, "source_ids": source_ids, "techniques": techniques})
    all_summaries.sort(key=lambda item: (int(item.get("rank") or 999999), str(item["source_id"])))
    if len(all_summaries) < 200:
        raise PipelineError(f"summary rollup contains {len(all_summaries)} papers; need at least 200")
    _write_json(
        output_path,
        {
            "summary_count": len(all_summaries),
            "source_index": [
                {key: summary.get(key) for key in ("source_id", "rank", "title", "url", "published_at", "topics")}
                for summary in all_summaries
            ],
            "shards": sorted(shards, key=lambda item: item["shard_id"]),
        },
    )


def _letters(count: int) -> Iterable[str]:
    for index in range(count):
        value = index
        label = ""
        while True:
            label = chr(ord("A") + (value % 26)) + label
            value = value // 26 - 1
            if value < 0:
                yield label
                break


def assemble(summary_paths: list[Path], techniques_path: Path, output_path: Path) -> None:
    all_summaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for summary_path in summary_paths:
        _, summaries, _ = _summary_records(summary_path)
        for summary in summaries:
            source_id = str(summary["source_id"])
            if source_id in seen_ids:
                raise PipelineError(f"aggregation would duplicate {source_id}")
            seen_ids.add(source_id)
            all_summaries.append(summary)
    if len(all_summaries) < 200:
        raise PipelineError(f"aggregation would contain only {len(all_summaries)} source summaries")
    techniques = techniques_path.read_text(encoding="utf-8").strip()
    if not techniques:
        raise PipelineError("unified techniques artifact is empty")
    all_summaries.sort(key=lambda item: (int(item.get("rank") or 999999), str(item["source_id"])))
    lines = [
        "# 2026 Frontier LLM Inference, Planning, and Prompting Research",
        "",
        "## Corpus Scope",
        "",
        f"This run retains {len(all_summaries)} distinct LibWit/arXiv source records published in 2026. "
        "Each entry is source-bound to the retrieved metadata and abstract supplied to its shard worker.",
        "",
        "## Deduplicated Prompt-Writing Guidance",
        "",
        techniques,
        "",
        "## Per-Source Summaries",
        "",
    ]
    for summary in all_summaries:
        topics = ", ".join(str(topic) for topic in summary.get("topics") or [])
        lines.extend(
            [
                f"### {int(summary.get('rank') or 0):03d}. {summary['title']}",
                "",
                f"Source: [{summary['source_id']}]({summary['url']}) | Published: {summary['published_at']} | Retrieved: {summary['retrieved_at']}",
                f"Topics: {topics or 'not classified'}",
                "",
                str(summary["summary_paragraph"]).strip(),
                "",
                "How to write a proper prompt: " + " ".join(
                    f"{label}) {instruction.strip()}" for label, instruction in zip(_letters(len(summary["prompt_instructions"])), summary["prompt_instructions"])
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Evidence Limits",
            "",
            "The deterministic collector records title, abstract, metadata, URLs, and dates. The per-source summaries do not claim full-text findings that are absent from those records.",
        ]
    )
    _write_text(output_path, "\n".join(lines))


def verify(aggregation_path: Path) -> None:
    text = aggregation_path.read_text(encoding="utf-8")
    paper_count = text.count("\n### ")
    prompt_count = text.count("How to write a proper prompt:")
    if paper_count < 200 or prompt_count != paper_count:
        raise PipelineError(
            f"aggregation completeness failed: {paper_count} source sections and {prompt_count} prompt sections"
        )
    if "## Deduplicated Prompt-Writing Guidance" not in text:
        raise PipelineError("aggregation has no deduplicated guidance section")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--plan", required=True, type=Path)
    collect_parser.add_argument("--output", required=True, type=Path)
    shard_parser = subparsers.add_parser("shard")
    shard_parser.add_argument("--input", required=True, type=Path)
    shard_parser.add_argument("--output", required=True, type=Path, action="append")
    rollup_parser = subparsers.add_parser("rollup")
    rollup_parser.add_argument("--input", required=True, type=Path, action="append")
    rollup_parser.add_argument("--output", required=True, type=Path)
    assemble_parser = subparsers.add_parser("assemble")
    assemble_parser.add_argument("--summary", required=True, type=Path, action="append")
    assemble_parser.add_argument("--techniques", required=True, type=Path)
    assemble_parser.add_argument("--output", required=True, type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--aggregation", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            collect(args.plan, args.output)
        elif args.command == "shard":
            shard(args.input, args.output)
        elif args.command == "rollup":
            rollup(args.input, args.output)
        elif args.command == "assemble":
            assemble(args.summary, args.techniques, args.output)
        else:
            verify(args.aggregation)
    except PipelineError as exc:
        print(f"frontier-research: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
