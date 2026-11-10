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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class PipelineError(RuntimeError):
    """Raised when an artifact cannot meet the recipe's evidence contract."""


def _min_sources() -> int:
    return int(os.environ.get("MINI_ORK_RESEARCH_MIN_SOURCES", "200"))


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


# ---------------------------------------------------------------- body text


def _token_count(text: str) -> int:
    """Deterministic token estimate: the chunk API reports token counts when it
    has them, but a chunk without one must still fit the same budget."""
    return max(1, len(text) // 4)


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _evidence_config(plan: dict[str, Any]) -> dict[str, Any]:
    spec = plan.get("fulltext")
    spec = spec if isinstance(spec, dict) else {}
    return {
        "enabled": _truthy(os.environ.get("MINI_ORK_RESEARCH_FULLTEXT"), bool(spec.get("enabled", True))),
        "max_tokens_per_paper": int(
            os.environ.get("MINI_ORK_RESEARCH_FULLTEXT_TOKENS") or spec.get("max_tokens_per_paper") or 6000
        ),
        "digest_tokens": int(
            os.environ.get("MINI_ORK_RESEARCH_FULLTEXT_DIGEST_TOKENS") or spec.get("digest_tokens") or 120
        ),
        "workers": max(
            1, int(os.environ.get("MINI_ORK_RESEARCH_FULLTEXT_WORKERS") or spec.get("workers") or 4)
        ),
        "fetch_missing": _truthy(
            os.environ.get("MINI_ORK_RESEARCH_FULLTEXT_FETCH"), bool(spec.get("fetch_missing", True))
        ),
        "timeout_seconds": float(os.environ.get("MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC") or 90),
    }


def _arxiv_key(source_id: str, url: str) -> str:
    if source_id.startswith("arxiv:"):
        return source_id.split(":", 1)[1]
    return str(url or "").rstrip("/").rsplit("/", 1)[-1].split("v", 1)[0]


def _libwit_call(
    api_base: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, Any]:
    """One LibWit call with bounded retries. Returns (status, payload); status 0
    means every attempt failed at the transport level."""
    request = Request(
        f"{api_base}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mini-ork/0.1 (+https://github.com/adelin-d/mini-ork)",
        },
        method=method,
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return response.getcode(), json.loads(raw) if raw.strip() else {}
        except HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 422):
                return exc.code, None
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(1.5 * (attempt + 1))
    return 0, None


def _body_chunks(api_base: str, token: str, arxiv_id: str, timeout: float) -> list[dict[str, Any]]:
    code, payload = _libwit_call(api_base, token, "GET", f"/paper/{quote(arxiv_id)}/chunks", None, timeout)
    if code != 200 or not isinstance(payload, dict):
        return []
    chunks = payload.get("chunks") or payload.get("results") or []
    return [
        chunk
        for chunk in chunks
        if isinstance(chunk, dict) and str(chunk.get("content") or "").strip()
    ]


def _ingest_body(api_base: str, token: str, arxiv_id: str, timeout: float) -> bool:
    code, payload = _libwit_call(
        api_base,
        token,
        "POST",
        "/fetch",
        {"arxiv_id": arxiv_id, "force": False},
        max(timeout, 240),
    )
    return code == 200 and isinstance(payload, dict) and bool(payload.get("title") or payload.get("arxiv_id"))


_SKIP_SECTION = re.compile(r"reference|bibliograph|acknowledg|appendix", re.I)
_PREFERRED_SECTION = re.compile(
    r"abstract|introduc|motivat|method|approach|experiment|evaluat|result|analysis|limit|discussion|conclusion",
    re.I,
)


def _ordered_body_chunks(chunks: list[dict[str, Any]], max_tokens: int) -> list[dict[str, Any]]:
    ordered = sorted(chunks, key=lambda chunk: int(chunk.get("index") or 0))
    usable = [c for c in ordered if not _SKIP_SECTION.search(str(c.get("section") or ""))]
    if not usable:
        usable = ordered
    preferred = [c for c in usable if _PREFERRED_SECTION.search(str(c.get("section") or ""))]
    candidates = preferred or usable
    selected: list[dict[str, Any]] = []
    used = 0
    for chunk in candidates:
        cost = int(chunk.get("tokens") or 0) or _token_count(str(chunk.get("content") or ""))
        if selected and used + cost > max_tokens:
            continue
        selected.append(chunk)
        used += cost
        if used >= max_tokens:
            break
    return selected


def _body_digest(chunks: list[dict[str, Any]], digest_tokens: int) -> str:
    """A bounded, ordered preview: the paper's opening plus its strongest
    conclusion/limitation/result chunk. Enough for extraction to see what the
    body says without inlining the whole paper into the shard artifact."""
    if not chunks:
        return ""
    picks: list[dict[str, Any]] = list(chunks[:2])
    closing = [
        chunk
        for chunk in chunks
        if re.search(r"limit|conclu|result|discussion", str(chunk.get("section") or ""), re.I)
    ]
    if closing and closing[-1] is not picks[-1]:
        picks.append(closing[-1])
    lines: list[str] = []
    used = 0
    for chunk in picks:
        text = re.sub(r"\s+", " ", str(chunk.get("content") or "")).strip()
        cost = int(chunk.get("tokens") or 0) or _token_count(text)
        budget = digest_tokens - used
        if budget <= 0:
            break
        if cost > budget:
            text = " ".join(text[: budget * 4].split())
            cost = budget
        section = str(chunk.get("section") or "").strip() or "body"
        lines.append(f"[{section}] {text}")
        used += cost
    return "\n".join(lines)


def _evidence_markdown(source: dict[str, Any], chunks: list[dict[str, Any]]) -> str:
    lines = [
        f"# {source['title']}",
        "",
        f"- source_id: {source['source_id']}",
        f"- url: {source['url']}",
        f"- published: {source.get('published_at') or 'unknown'}",
        f"- authors: {', '.join(str(a) for a in (source.get('authors') or []))[:400]}",
        "",
        "## Abstract",
        "",
        str(source.get("abstract") or "(no abstract available)").strip(),
        "",
        "## Body text (LibWit chunks, ordered by position)",
        "",
    ]
    for chunk in chunks:
        section = str(chunk.get("section") or "").strip() or "(unnamed section)"
        lines += [f"### {section}", "", str(chunk.get("content") or "").strip(), ""]
    return "\n".join(lines)


def _attach_body_text(
    sources: list[dict[str, Any]],
    papers_dir: Path,
    api_base: str,
    token: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Fetch each selected paper's body text and bind it to the corpus record.

    Retrieval failure is never fatal: the paper is marked abstract-only and the
    extraction prompt is told not to claim body-text findings for it."""
    if not config["enabled"]:
        for source in sources:
            source["evidence_mode"] = "abstract-only"
            source["evidence_path"] = ""
            source["evidence_chunks"] = 0
            source["evidence_tokens"] = 0
            source["evidence_digest"] = ""
            source["chunk_count"] = 0
        return {
            "enabled": False,
            "papers": len(sources),
            "body_text_papers": 0,
            "abstract_only_papers": len(sources),
            "body_text_ratio": 0.0,
            "notes": ["Body-text retrieval disabled; all extraction is abstract-only."],
        }

    papers_dir.mkdir(parents=True, exist_ok=True)
    failure_reasons: dict[str, int] = {}

    def enrich(source: dict[str, Any]) -> None:
        arxiv_id = _arxiv_key(source["source_id"], source["url"])
        chunks = _body_chunks(api_base, token, arxiv_id, config["timeout_seconds"])
        if not chunks and config["fetch_missing"]:
            if _ingest_body(api_base, token, arxiv_id, config["timeout_seconds"]):
                time.sleep(1.0)
                chunks = _body_chunks(api_base, token, arxiv_id, config["timeout_seconds"])
        if not chunks:
            failure_reasons["no_chunks"] = failure_reasons.get("no_chunks", 0) + 1
            source["evidence_mode"] = "abstract-only"
            source["evidence_path"] = ""
            source["evidence_chunks"] = 0
            source["evidence_tokens"] = 0
            source["evidence_digest"] = ""
            source["chunk_count"] = 0
            return
        selected = _ordered_body_chunks(chunks, config["max_tokens_per_paper"])
        if not selected:
            failure_reasons["no_selection"] = failure_reasons.get("no_selection", 0) + 1
            source["evidence_mode"] = "abstract-only"
            source["evidence_path"] = ""
            source["evidence_chunks"] = 0
            source["evidence_tokens"] = 0
            source["evidence_digest"] = ""
            source["chunk_count"] = len(chunks)
            return
        path = papers_dir / f"{arxiv_id}.md"
        path.write_text(_evidence_markdown(source, selected) + "\n", encoding="utf-8")
        source["evidence_mode"] = "body-text"
        source["evidence_path"] = str(path)
        source["evidence_chunks"] = len(selected)
        source["evidence_tokens"] = sum(
            int(chunk.get("tokens") or 0) or _token_count(str(chunk.get("content") or ""))
            for chunk in selected
        )
        source["evidence_digest"] = _body_digest(selected, config["digest_tokens"])
        source["chunk_count"] = len(chunks)

    workers = min(config["workers"], max(1, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(enrich, sources))

    body_text = sum(1 for source in sources if source.get("evidence_mode") == "body-text")
    coverage = {
        "enabled": True,
        "papers": len(sources),
        "body_text_papers": body_text,
        "abstract_only_papers": len(sources) - body_text,
        "body_text_ratio": round(body_text / len(sources), 4) if sources else 0.0,
        "max_tokens_per_paper": config["max_tokens_per_paper"],
        "digest_tokens": config["digest_tokens"],
        "fetch_missing": config["fetch_missing"],
        "failure_reasons": failure_reasons,
        "notes": [
            "Body text retrieved from the LibWit chunk API; each paper's excerpts are written under papers/.",
            "Abstract-only papers carry no body-text claim into extraction.",
        ],
    }
    return coverage


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
    max_workers = int(plan.get("max_workers") or 4)
    batch_limit = int(os.environ.get("MINI_ORK_LIBWIT_BATCH_LIMIT", "32"))
    merged_groups: list[Any] = []
    for start in range(0, len(requests), batch_limit):
        chunk = requests[start : start + batch_limit]
        request = Request(
            f"{api_base}/search/batch",
            data=json.dumps({"queries": chunk, "max_workers": max_workers}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "mini-ork/0.1 (+https://github.com/adelin-d/mini-ork)",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                chunk_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise PipelineError(f"LibWit API rejected collection request: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PipelineError(f"LibWit API collection request failed: {exc}") from exc
        groups = chunk_payload.get("results") or chunk_payload.get("data") or chunk_payload.get("items")
        if not isinstance(groups, list):
            raise PipelineError("LibWit search response has no results list")
        merged_groups.extend(groups)
    payload = {"results": merged_groups}

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
    evidence_config = _evidence_config(plan)
    evidence_coverage = _attach_body_text(
        selected,
        output_path.parent / "papers",
        api_base,
        token,
        evidence_config,
    )
    notes = [
        "Collected through the LibWit batch-search endpoint.",
        "Only papers published in 2026 were eligible.",
        "Ranking combines topical coverage, source relevance, citation signals, and recency.",
    ]
    if evidence_config["enabled"]:
        notes.append(
            f"Body text retrieved for {evidence_coverage['body_text_papers']}/{evidence_coverage['papers']} papers; "
            "extraction reads those excerpts, not the abstract alone."
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
            "evidence_coverage": evidence_coverage,
            "search_notes": notes,
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
    if len(all_summaries) < _min_sources():
        raise PipelineError(f"summary rollup contains {len(all_summaries)} papers; need at least {_min_sources()}")
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
    if len(all_summaries) < _min_sources():
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
            "The deterministic collector records title, abstract, metadata, URLs, dates, and the retrieved body-text "
            "excerpts for each paper (`evidence_mode` in `source-corpus.json`). Per-source summaries rest on that "
            "retrieved text; numbers are as stated in the paper, not reproduced by this run.",
        ]
    )
    _write_text(output_path, "\n".join(lines))


def verify(aggregation_path: Path) -> None:
    text = aggregation_path.read_text(encoding="utf-8")
    if "## Deduplicated Prompt-Writing Guidance" not in text:
        raise PipelineError("aggregation has no deduplicated guidance section")
    marker = "\n## Per-Source Summaries\n"
    if marker not in text:
        raise PipelineError("aggregation has no per-source summaries section")
    # Count only the papers region: the guidance section embeds
    # unified-techniques.md, whose own `### ` technique headings are not paper
    # sections (measured: 2122 vs 2000 on the first 2000-source run).
    papers = text.split(marker, 1)[1]
    paper_count = papers.count("\n### ")
    prompt_count = papers.count("How to write a proper prompt:")
    if paper_count < _min_sources() or prompt_count != paper_count:
        raise PipelineError(
            f"aggregation completeness failed: {paper_count} source sections and {prompt_count} prompt sections"
        )


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
