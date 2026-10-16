#!/usr/bin/env python3
"""Deterministic stages of the author-style-imitation review.

Shape of the pipeline (why it is split this way):

* **Retrieval is deterministic and free.** Five searcher nodes each issue one
  LibWit batch-search call covering ten queries, so a >=1000-paper corpus costs
  zero model tokens and one round-trip of wall time. An LLM issuing 50 searches
  and parsing 50 JSON envelopes would cost far more and fail more often.
* **Compression happens exactly once.** The shard extractors are the only nodes
  that read the full corpus. Everything downstream reads bounded, distilled
  artifacts (group files carry a 500-char summary per paper, never the abstract).
* **Judgment happens on the distilled set.** The two impact reviewers score
  ~60-120 canonical techniques, not 1000 papers, so reviewer cost is O(techniques)
  rather than O(papers).

Every stage here is replayable without a model: given the same inputs it writes
the same artifact. The LLM nodes only ever read one declared artifact and write
one JSON/Markdown file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# The anchor paper for this review: "Catch Me If You Can? Not Yet: LLMs Still
# Struggle to Imitate the Implicit Writing Styles of Everyday Authors" (EMNLP
# 2025). Recorded in the corpus metadata as a sanity signal, never a hard gate.
SEED_SOURCE_ID = "arxiv:2509.14543"

VERDICTS = ("adopt-now", "next", "watch", "skip")
EVIDENCE_VERDICTS = ("supported", "unclear", "overclaimed")
EFFORT_WEIGHT = {"S": 1.0, "M": 1.5, "L": 2.5}
# A technique replicated across independent papers is the strongest available
# signal that it works; a single-paper claim is not.
REPLICATION_BONUS = 1.15
OVERCLAIM_PENALTY = 0.5
ARXIV_ID = re.compile(r"arxiv:\d{4}\.\d{4,5}(?:v\d+)?")


class PipelineError(RuntimeError):
    """Raised when an artifact cannot meet the recipe's evidence contract."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def group_count() -> int:
    return _int_env("MO_STYLE_GROUP_COUNT", 8)


def shard_count() -> int:
    return _int_env("MO_STYLE_SHARD_COUNT", 10)


def batch_count() -> int:
    return _int_env("MO_STYLE_SEARCH_BATCHES", 5)


def _min_coverage() -> float:
    return float(os.environ.get("MO_STYLE_MIN_COVERAGE", "0.95"))


def _min_sources() -> int:
    return _int_env("MINI_ORK_RESEARCH_MIN_SOURCES", 1000)


# ------------------------------------------------------------- io helpers


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8").strip()
    # Models occasionally fence the JSON despite the prompt; strip one fence.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path} is not valid JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _corpus_sources(corpus_path: Path) -> dict[str, dict[str, Any]]:
    corpus = _read_json(corpus_path)
    sources = corpus.get("sources") if isinstance(corpus, dict) else None
    if not isinstance(sources, list):
        raise PipelineError("source corpus has no sources list")
    return {str(s["source_id"]): s for s in sources if isinstance(s, dict) and s.get("source_id")}


def _unversioned(source_id: str) -> str:
    """LibWit occasionally keeps a version suffix (arxiv:2604.05112v1) on a
    corpus source_id; compare citations with the suffix stripped on both sides."""
    return re.sub(r"v\d+$", "", source_id)


def _published_day(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


# ------------------------------------------------------------- query batches


def plan_batches(plan_path: Path, out_dir: Path) -> None:
    """Split the collection plan into one file per searcher agent.

    A batch is the unit of work of one searcher node: `plan.batch_size` queries
    (10 by default) issued as a single LibWit batch request.
    """
    plan = _read_json(plan_path)
    batches = plan.get("query_batches")
    if batches is not None:
        if not isinstance(batches, list) or not batches:
            raise PipelineError("collection plan query_batches must be a non-empty list of lists")
    else:
        queries = plan.get("queries")
        if not isinstance(queries, list) or not queries:
            raise PipelineError("collection plan needs query_batches or a non-empty queries list")
        size = _int_env("MO_STYLE_BATCH_SIZE", 10)
        if size < 1:
            raise PipelineError("batch size must be >= 1")
        batches = [queries[i : i + size] for i in range(0, len(queries), size)]
    expected = batch_count()
    if len(batches) < expected:
        raise PipelineError(f"collection plan declares {len(batches)} batches; the workflow dispatches {expected} searchers")
    written = 0
    for index, batch in enumerate(batches, start=1):
        if not isinstance(batch, list) or not batch:
            raise PipelineError(f"batch {index} is empty")
        specs = []
        for spec in batch:
            if not isinstance(spec, dict) or not str(spec.get("query") or "").strip():
                raise PipelineError(f"batch {index} has a query spec without text")
            specs.append({"topic": str(spec.get("topic") or "search"), "query": str(spec["query"]).strip()})
        _write_json(
            out_dir / f"batch-{index:02d}.json",
            {
                "batch_id": f"{index:02d}",
                "query_count": len(specs),
                "source": str(plan.get("source") or "arxiv"),
                "date_from": str(plan.get("date_from") or ""),
                "results_per_query": int(plan.get("results_per_query") or 40),
                "queries": specs,
            },
        )
        written += 1
    print(json.dumps({"batches": written, "queries": sum(len(b) for b in batches)}))


# ------------------------------------------------------------- retrieval


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


def _libwit_batch(queries: list[dict[str, Any]], *, top: int, source: str, date_from: str, max_workers: int) -> list[Any]:
    token = os.environ.get("ARXIV_API_TOKEN", "").strip()
    if not token:
        raise PipelineError("ARXIV_API_TOKEN is required; configure it outside the recipe and retry")
    api_base = os.environ.get("MINI_ORK_LIBWIT_API_BASE", "https://arxiv.libwit.ai/api").rstrip("/")
    timeout_seconds = float(os.environ.get("MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC", "900"))
    requests = [
        {"query": str(spec["query"]), "top": top, "hybrid": True, "source": source, "date_from": date_from}
        for spec in queries
    ]
    request = Request(
        f"{api_base}/search/batch",
        data=json.dumps({"queries": requests, "max_workers": max_workers}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mini-ork/0.1 (+https://github.com/adelin-d/mini-ork)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise PipelineError(f"LibWit API rejected search request: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PipelineError(f"LibWit API search request failed: {exc}") from exc
    groups = payload.get("results") or payload.get("data") or payload.get("items")
    if not isinstance(groups, list):
        raise PipelineError("LibWit search response has no results list")
    return groups


def _candidates_from_records(payload: dict[str, Any], specs: list[dict[str, Any]], date_from: str) -> dict[str, dict[str, Any]]:
    cutoff = _published_day(date_from)
    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    candidates: dict[str, dict[str, Any]] = {}
    for record, topic in _records_from_batch(payload, specs):
        published_at = str(record.get("published") or record.get("published_at") or record.get("date") or "")[:10]
        published = _published_day(published_at)
        if published is None or (cutoff and published < cutoff) or published > datetime.now(UTC).date():
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
                "venue": str(record.get("venue") or ""),
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
    return candidates


def _rank(candidate: dict[str, Any], date_from: str) -> None:
    published = _published_day(candidate["published_at"])
    start = _published_day(date_from) or date(2020, 1, 1)
    recency_days = max((published - start).days if published else 0, 0)
    span = max((datetime.now(UTC).date() - start).days, 1)
    # Topical coverage dominates: a paper matched by queries from several
    # batches sits closer to the centre of the topic than a one-query hit.
    candidate["ranking_score"] = round(
        len(candidate["topics"]) * 1000
        + candidate["relevance_score"] * 200
        + math.log1p(candidate["citation_count"]) * 20
        + math.log1p(candidate["influential_citation_count"]) * 20
        + min(recency_days, span),
        3,
    )


def search(batch_path: Path, plan_path: Path, out: Path) -> None:
    """One searcher agent: a single LibWit batch call over this batch's queries."""
    batch = _read_json(batch_path)
    specs = batch.get("queries")
    if not isinstance(specs, list) or not specs:
        raise PipelineError(f"{batch_path} has no queries")
    plan = _read_json(plan_path)
    date_from = str(batch.get("date_from") or plan.get("date_from") or "2020-01-01")
    top = int(batch.get("results_per_query") or plan.get("results_per_query") or 40)
    source = str(batch.get("source") or plan.get("source") or "arxiv")
    max_workers = int(plan.get("max_workers") or 4)
    groups = _libwit_batch(specs, top=top, source=source, date_from=date_from, max_workers=max_workers)
    candidates = _candidates_from_records({"results": groups}, specs, date_from)
    for candidate in candidates.values():
        _rank(candidate, date_from)
        candidate["topics"] = sorted(candidate["topics"])
    ranked = sorted(candidates.values(), key=lambda item: (-item["ranking_score"], item["source_id"]))
    _write_json(
        out,
        {
            "batch_id": str(batch.get("batch_id") or batch_path.stem),
            "query_count": len(specs),
            "queries": [spec["query"] for spec in specs],
            "candidate_count": len(ranked),
            "sources": ranked,
        },
    )
    print(json.dumps({"batch": str(batch.get("batch_id")), "candidates": len(ranked)}))


def merge(partials: list[Path], plan_path: Path, out: Path) -> None:
    """Union the searcher outputs, re-rank globally, cut to the required count.

    Re-ranking here (rather than per batch) is what turns "a paper found by one
    query in one batch" into a graded relevance signal: cross-batch agreement is
    the best cheap proxy for centrality that this corpus exposes.
    """
    plan = _read_json(plan_path)
    required = int(plan.get("required_source_count") or 0)
    floor = _min_sources()
    if required < floor:
        # The recipe's contract is a >=1000-paper corpus; a plan that asks for
        # less would silently shrink every downstream stage's evidence base.
        raise PipelineError(f"collection plan requires {required} sources; the recipe floor is {floor}")
    date_from = str(plan.get("date_from") or "2020-01-01")
    merged: dict[str, dict[str, Any]] = {}
    batch_ids: list[str] = []
    for path in sorted(partials):
        payload = _read_json(path)
        batch_ids.append(str(payload.get("batch_id") or path.stem))
        for source in payload.get("sources") or []:
            if not isinstance(source, dict) or not source.get("source_id"):
                continue
            existing = merged.get(source["source_id"])
            if existing is None:
                merged[source["source_id"]] = source
                continue
            existing["topics"] = sorted(set(existing.get("topics") or []) | set(source.get("topics") or []))
            existing["citation_count"] = max(existing["citation_count"], source.get("citation_count", 0))
            existing["influential_citation_count"] = max(
                existing["influential_citation_count"], source.get("influential_citation_count", 0)
            )
            existing["relevance_score"] = max(existing["relevance_score"], source.get("relevance_score", 0))
    for source in merged.values():
        _rank(source, date_from)
    ranked = sorted(merged.values(), key=lambda item: (-item["ranking_score"], item["published_at"], item["source_id"]))
    if len(ranked) < required:
        raise PipelineError(
            f"the searchers returned {len(ranked)} distinct papers in range; need {required}. "
            "Widen the collection plan (more queries or a higher results_per_query) and re-run."
        )
    selected = ranked[:required]
    for rank, source in enumerate(selected, start=1):
        source["rank"] = rank
    seed_present = any(_unversioned(s["source_id"]) == SEED_SOURCE_ID for s in selected)
    _write_json(
        out,
        {
            "source_count": len(selected),
            "required_source_count": required,
            "raw_candidate_count": len(ranked),
            "searcher_batches": batch_ids,
            "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "collection_plan": plan_path.name,
            "ranking_policy": plan.get("ranking_policy"),
            "seed_paper_present": seed_present,
            "seed_paper": SEED_SOURCE_ID,
            "sources": selected,
            "coverage_gaps": [],
            "search_notes": [
                f"Collected through {len(batch_ids)} independent LibWit batch searches, 10 queries each.",
                f"Only papers published on/after {date_from} were eligible.",
                "Ranking combines cross-batch topical coverage, LibWit relevance, citation signals, and recency.",
                f"Anchor paper {SEED_SOURCE_ID} present: {seed_present}.",
            ],
        },
    )
    print(json.dumps({"selected": len(selected), "candidates": len(ranked), "seed_present": seed_present}))


# ------------------------------------------------------------- shard


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
        shard_sources = sources[(index - 1) * width : index * width]
        _write_json(
            output_path,
            {
                "shard_id": f"{index:02d}",
                "source_count": len(shard_sources),
                "required_source_count": len(shard_sources),
                "sources": shard_sources,
            },
        )
    print(json.dumps({"shards": len(output_paths), "per_shard": width}))


# ---------------------------------------------------------------- catalog


def catalog(extraction_paths: list[Path], corpus_path: Path, catalog_out: Path, papers_out: Path) -> None:
    """Merge shard extractions into a technique catalog + paper index.

    Tolerates a small number of papers a shard worker skipped (recorded as
    coverage gaps) but fails below MO_STYLE_MIN_COVERAGE, and drops any source
    id the corpus does not contain rather than let it reach a citation.
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
                "relevance": paper.get("relevance") or "unknown",
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
        raise PipelineError(f"shard extraction covered {covered}/{expected} papers; below coverage floor {_min_coverage()}")
    if not techniques:
        raise PipelineError("no shard techniques survived validation")
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
                {
                    "key": t["key"],
                    "technique": t["technique"],
                    "mechanism": t["mechanism"],
                    "support": len(t["source_ids"]),
                    "source_ids": t["source_ids"],
                }
                for t in techniques
            ],
        },
    )
    _write_json(papers_out, {"paper_count": covered, "papers": papers})


# ---------------------------------------------------------------- split


def split(clusters_path: Path, catalog_path: Path, papers_path: Path, out_dir: Path, unclustered_out: Path) -> None:
    """Validate the clusterer's grouping and write one bounded file per group."""
    expected_groups = group_count()
    clusters = _read_json(clusters_path)
    cat = _read_json(catalog_path)
    papers = _read_json(papers_path)["papers"]
    by_key = {t["key"]: t for t in cat["techniques"]}
    groups = clusters.get("groups") if isinstance(clusters, dict) else None
    if not isinstance(groups, list) or len(groups) != expected_groups:
        raise PipelineError(
            f"clusterer must emit exactly {expected_groups} groups, got {len(groups) if isinstance(groups, list) else 'none'}"
        )
    assigned: set[str] = set()
    for index, group in enumerate(groups, start=1):
        keys = []
        for key in group.get("technique_keys") or []:
            key = str(key)
            if key in by_key and key not in assigned:
                assigned.add(key)
                keys.append(key)
        if not keys:
            raise PipelineError(f"group {index} ({group.get('name')}) has no valid technique keys")
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
        raise PipelineError(f"clusterer left {len(unassigned)}/{len(by_key)} techniques unassigned (>5%)")
    _write_json(unclustered_out, {"unclustered": [by_key[k] for k in unassigned]})
    print(json.dumps({"groups": expected_groups, "unassigned": len(unassigned)}))


# ---------------------------------------------------------------- pack


def pack(
    group_paths: list[Path],
    corpus_path: Path,
    state_path: Path,
    unclustered_path: Path | None,
    out: Path,
) -> None:
    """Merge per-group extractions into the review pack both reviewers read."""
    expected_groups = group_count()
    corpus = _corpus_sources(corpus_path)
    groups_out = []
    flat = []
    seen_ids: set[str] = set()
    for path in sorted(group_paths):
        payload = _read_json(path)
        group_id = str(payload.get("group_id") or "")
        if not re.fullmatch(r"G\d{2}", group_id):
            raise PipelineError(f"{path} has no valid group_id")
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
                "transferability": str(tech.get("transferability") or "").strip(),
                "adoption_sketch": str(tech.get("adoption_sketch") or "").strip(),
            }
            kept.append(entry)
            flat.append(entry)
        if not kept:
            raise PipelineError(f"{path} produced no techniques with valid evidence")
        groups_out.append(
            {
                "group_id": group_id,
                "name": payload.get("name"),
                "scope": payload.get("scope"),
                "technique_ids": [t["id"] for t in kept],
            }
        )
    if len(groups_out) != expected_groups:
        raise PipelineError(f"expected {expected_groups} group extractions, got {len(groups_out)}")
    unclustered = (
        _read_json(unclustered_path).get("unclustered", [])
        if unclustered_path and unclustered_path.exists()
        else []
    )
    _write_json(
        out,
        {
            "technique_count": len(flat),
            "replicated_technique_count": sum(1 for t in flat if len(t["evidence"]) >= 3),
            "groups": sorted(groups_out, key=lambda g: g["group_id"]),
            "techniques": flat,
            "unclustered_catalog_entries": unclustered,
            "target_state_markdown": state_path.read_text(encoding="utf-8"),
        },
    )
    print(json.dumps({"techniques": len(flat), "groups": len(groups_out)}))


# ---------------------------------------------------------------- merge reviews


def _score_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    try:
        impact = int(entry["impact"])
        confidence = int(entry["evidence_confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    effort = str(entry.get("effort") or "M").upper()[:1]
    verdict = str(entry.get("verdict") or "watch")
    evidence_verdict = str(entry.get("evidence_verdict") or "unclear")
    return {
        "impact": max(1, min(5, impact)),
        "evidence_confidence": max(1, min(5, confidence)),
        "effort": effort if effort in EFFORT_WEIGHT else "M",
        "verdict": verdict if verdict in VERDICTS else "watch",
        "evidence_verdict": evidence_verdict if evidence_verdict in EVIDENCE_VERDICTS else "unclear",
        "landing_surface": str(entry.get("landing_surface") or ""),
        "rationale": str(entry.get("rationale") or ""),
    }


def priority(scores: list[dict[str, Any]], evidence_count: int) -> float:
    """Rank key for a technique given one or two reviewer scores.

    impact × confidence / effort, boosted when several independent papers report
    the mechanism (replication is the strongest available evidence that a
    technique works) and halved when a reviewer judges the claim overclaimed.
    """
    impact = sum(s["impact"] for s in scores) / len(scores)
    confidence = sum(s["evidence_confidence"] for s in scores) / len(scores)
    effort = sum(EFFORT_WEIGHT[s["effort"]] for s in scores) / len(scores)
    value = impact * confidence / effort
    if evidence_count >= 3:
        value *= REPLICATION_BONUS
    if any(s["evidence_verdict"] == "overclaimed" for s in scores):
        value *= OVERCLAIM_PENALTY
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
        raise PipelineError("impact review needs two independent reviewers")
    rows = []
    unscored = []
    for tid, by_reviewer in per_tech.items():
        if not by_reviewer:
            unscored.append(tid)
            continue
        scores = list(by_reviewer.values())
        verdict_rank = [VERDICTS.index(s["verdict"]) for s in scores]
        impacts = [s["impact"] for s in scores]
        evidence_count = len(techniques[tid]["evidence"])
        rows.append(
            {
                **techniques[tid],
                "reviews": by_reviewer,
                "priority": priority(scores, evidence_count),
                "mean_impact": round(sum(impacts) / len(impacts), 2),
                # Consensus verdict = the more conservative of the two.
                "verdict": VERDICTS[max(verdict_rank)],
                "single_rater": len(scores) == 1,
                "disagreement": len(scores) > 1
                and (max(impacts) - min(impacts) >= 2 or max(verdict_rank) - min(verdict_rank) >= 2),
            }
        )
    if len(unscored) > 0.1 * len(techniques):
        raise PipelineError(f"{len(unscored)}/{len(techniques)} techniques received no valid score from either reviewer")
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
            "replicated_technique_count": sum(1 for r in rows if len(r["evidence"]) >= 3),
            "groups": pack_data["groups"],
            "ranked": rows,
        },
    )
    print(json.dumps({"ranked": len(rows), "disagreements": sum(r["disagreement"] for r in rows)}))


# ---------------------------------------------------------------- assemble + verify


def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip()


def assemble(report_path: Path, merged_path: Path, corpus_path: Path, out: Path) -> None:
    merged = _read_json(merged_path)
    corpus = _corpus_sources(corpus_path)
    report = report_path.read_text(encoding="utf-8").strip()
    if not report:
        raise PipelineError("report is empty")
    group_names = {g["group_id"]: g.get("name") or g["group_id"] for g in merged["groups"]}
    lines = [
        report,
        "",
        "## Appendix A — Full Ranked Technique Table",
        "",
        f"Reviewers: {', '.join(merged['reviewers'])}. Priority = mean impact × mean evidence confidence / effort weight "
        "(S=1, M=1.5, L=2.5); ×1.15 when ≥3 independent papers report the mechanism (replication), ×0.5 when a reviewer "
        "judges the claim overclaimed. Verdict is the most conservative reviewer's. "
        "⚠ = reviewers disagree by ≥2 impact points or ≥2 verdict levels.",
        "",
        "| Rank | ID | Technique | Group | Priority | Impact | Papers | Verdict | Evidence |",
        "|---:|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in merged["ranked"]:
        flag = " ⚠" if row["disagreement"] else (" (1 rater)" if row["single_rater"] else "")
        evidence = ", ".join(row["evidence"][:4])
        if len(row["evidence"]) > 4:
            evidence += f" +{len(row['evidence']) - 4}"
        lines.append(
            f"| {row['rank']} | {row['id']} | {_cell(row['technique'])} | {_cell(group_names.get(row['group_id'], row['group_id']))} "
            f"| {row['priority']} | {row['mean_impact']} | {len(row['evidence'])} | {row['verdict']}{flag} | {evidence} |"
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
                f"  - Reported results: {row['reported_results'] or 'not stated in abstracts'}",
                f"  - Transfer to a style-replication system: {row['transferability'] or 'not stated'}",
                "  - Evidence: " + ", ".join(f"[{sid}]({corpus[sid].get('url')})" for sid in row["evidence"] if sid in corpus),
            ]
        lines.append("")
    lines += [
        "## Evidence Limits",
        "",
        "Per-paper extraction read LibWit titles, abstracts, and metadata only; reported numbers are as stated in abstracts, "
        f"not reproduced. Priority scores are the judgment of {len(merged['reviewers'])} independent reviewers "
        f"({', '.join(merged['reviewers'])}) against a written snapshot of what is already known to work. "
        "A high priority means 'worth testing first', never 'proven to work in your setting'.",
    ]
    _write_text(out, "\n".join(lines))


def verify(final_path: Path, merged_path: Path, corpus_path: Path) -> None:
    expected_groups = group_count()
    text = final_path.read_text(encoding="utf-8")
    merged = _read_json(merged_path)
    corpus = _corpus_sources(corpus_path)
    for heading in ("## Executive Summary", "## Techniques That Work", "## Appendix A", "## Appendix B"):
        if heading not in text:
            raise PipelineError(f"final review is missing section {heading!r}")
    groups = re.findall(r"^### G\d{2} — ", text, flags=re.M)
    if len(groups) != expected_groups:
        raise PipelineError(f"final review lists {len(groups)} groups, expected {expected_groups}")
    cited = {_unversioned(i) for i in ARXIV_ID.findall(text)}
    unknown = sorted(cited - {_unversioned(i) for i in corpus})
    if unknown:
        raise PipelineError(f"final review cites {len(unknown)} ids absent from the corpus: {', '.join(unknown[:10])}")
    known_ids = {r["id"] for r in merged["ranked"]}
    body = text.split("## Appendix A", 1)[0]
    mentioned = set(re.findall(r"\bG\d{2}-T\d{2}\b", body))
    if not mentioned:
        raise PipelineError("report body references no technique ids")
    stray = sorted(mentioned - known_ids)
    if stray:
        raise PipelineError(f"report body references unknown technique ids: {', '.join(stray)}")
    print(
        json.dumps(
            {
                "groups": len(groups),
                "techniques": len(known_ids),
                "cited_papers": len(cited),
                "report_ids": len(mentioned),
                "corpus_papers": len(corpus),
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan-batches")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)

    p = sub.add_parser("search")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)

    p = sub.add_parser("merge")
    p.add_argument("--partial", type=Path, action="append", required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)

    p = sub.add_parser("shard")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, action="append", required=True)

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
        if args.cmd == "plan-batches":
            plan_batches(args.plan, args.out_dir)
        elif args.cmd == "search":
            search(args.batch, args.plan, args.out)
        elif args.cmd == "merge":
            merge(args.partial, args.plan, args.out)
        elif args.cmd == "shard":
            shard(args.input, args.output)
        elif args.cmd == "catalog":
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
    except PipelineError as exc:
        sys.stderr.write(f"author-style-imitation-review: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
