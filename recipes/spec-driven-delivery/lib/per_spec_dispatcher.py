#!/usr/bin/env python3
"""Deterministic per-spec child dispatcher for spec-driven-delivery.

Walks every SpecCard in ``${MINI_ORK_RUN_DIR}/spec-cards/`` in spec-index
order, then walks each card's ``deliverables`` in topological order
(cross-spec ``<spec_id>/<id>`` edges resolved against the global set), and
dispatches each deliverable serially through
``bin/mini-ork run recursive-validate-impl <child-kickoff>``.

Contract (per ``prompts/per-spec-dispatcher.md``):

- One deliverable per child kickoff; the child run is the only thing allowed
  to implement it, never the dispatcher itself.
- SpecCard + gate file + source spec are read-only here; the child is the
  only writer of deliverable-scoped artifacts.
- A deliverable is dispatched only after every entry in its ``depends_on``
  has a ``delivered`` verdict. A failed or blocked dependency marks the
  dependent ``pending`` and skips it — never re-dispatched in this run.
- A spec-level ``Depends on:`` header in the card's source spec fans out to
  EVERY deliverable of the named specs (own ``depends_on`` UNION spec-level
  fan-out), so a dep-free deliverable can no longer race ahead of the specs
  its spec names (run-1791763809 survey-synthesis D0 dispatched before the
  lenses existed and synthesized nothing).
- ``MO_DISPATCH_WAVES`` (default ``1``): ``1`` dispatches serially in
  topological order, byte-identical to the historical loop; ``N >= 2``
  groups deliverables into dependency levels and runs up to ``N`` children
  concurrently per level. Verdict/status semantics are unchanged, and
  ``MO_PER_SPEC_DISPATCH_FAIL_FAST=1`` stops launching new children once a
  collected child has failed.
- When the SpecCard or gates lack information needed to write a correct
  child kickoff (e.g. an ``acceptance_refs`` entry with no matching gate),
  the dispatcher writes a structured ASK to
  ``${MINI_ORK_RUN_DIR}/asks/<spec_id>--<deliverable_id>.json`` and marks
  the deliverable ``blocked``.
- The dispatcher writes
  ``${MINI_ORK_RUN_DIR}/dispatch-results.json`` with the shape
  ``{deliverables: [{spec_id, deliverable_id, child_kickoff,
  child_run_id, child_run_dir, status, verdict_path, ask_path}]}``. The
  downstream ``dispatch-aggregator`` verifier recomputes every status
  from the child run dirs, so the dispatcher's own ``status`` claims are
  advisory.

Failures (load errors, malformed SpecCards, missing gates directory,
binary exec failures) emit a single non-zero exit so the workflow node
fails closed. Status within the file remains the source of truth for the
aggregator, not the exit code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_PENDING = "pending"

_DELIVERED_SET = frozenset({STATUS_DELIVERED})
_FAILED_OR_BLOCKED = frozenset({STATUS_FAILED, STATUS_BLOCKED})

# spec_id is the slug used in cross-spec edges; deliverable id is local to
# the card. Both names appear in filesystem paths (child run id, kickoff
# path, log path) and must be safe for bash + shell. The regex is the
# same one spec-card.schema.json enforces ($defs/spec_id and local_id).
_SAFE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SAFE_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# ── helpers ───────────────────────────────────────────────────────────────


def _safe_id(value: str, kind: str) -> str:
    pattern = _SAFE_ID_RE if kind == "spec" else _SAFE_LOCAL_ID_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"unsafe {kind} id: {value!r}")
    return value


def _safe_pair(spec_id: str, deliverable_id: str) -> tuple[str, str]:
    return _safe_id(spec_id, "spec"), _safe_id(deliverable_id, "deliverable")


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ── spec / gate loading ───────────────────────────────────────────────────


def _load_spec_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read ``spec-index.json`` and return the ordered ``specs`` mapping.

    Missing file is an empty mapping: the upstream spec_lint/specdir_ingest
    verifiers already fail closed on missing input, so an empty index here
    means there is nothing to dispatch.
    """
    path = run_dir / "spec-index.json"
    if not path.is_file():
        return {}
    raw = _load_json(path)
    specs = raw.get("specs") if isinstance(raw, dict) else None
    return specs if isinstance(specs, dict) else {}


def _load_cards(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Every ``spec-cards/<spec_id>.json`` keyed by spec_id.

    A malformed card or duplicate id raises: the dispatcher must fail
    closed rather than silently skip a deliverable.
    """
    cards_dir = run_dir / "spec-cards"
    cards: dict[str, dict[str, Any]] = {}
    if not cards_dir.is_dir():
        return cards
    for card_path in sorted(cards_dir.glob("*.json")):
        card = _load_json(card_path)
        if not isinstance(card, dict):
            raise ValueError(f"{card_path} is not a JSON object")
        spec_id = _str(card.get("spec_id"))
        if not spec_id:
            raise ValueError(f"{card_path} missing spec_id")
        if spec_id in cards:
            raise ValueError(f"duplicate spec_id {spec_id!r} in {card_path.name}")
        cards[spec_id] = card
    return cards


def _load_gates(run_dir: Path, spec_id: str) -> dict[str, Any]:
    """Read ``gates/<spec_id>.json`` (may be missing — test_validity failure)."""
    path = run_dir / "gates" / f"{spec_id}.json"
    if not path.is_file():
        return {}
    try:
        raw = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _gates_for_acceptance(gates_doc: dict[str, Any], acceptance_refs: list[str]) -> list[dict[str, Any]]:
    """Probes whose ``acceptance_ref`` matches one of the deliverable's refs.

    Copied verbatim so the child kickoff embeds the exact machine-checkable
    Definition of Done; this is the spec_fence — the test_author wrote
    these probes before any implementation, and the dispatcher must not
    paraphrase them.
    """
    probes = _as_list(gates_doc.get("probes"))
    refs = set(acceptance_refs)
    return [p for p in probes if isinstance(p, dict) and p.get("acceptance_ref") in refs]


# ── dependency resolution ────────────────────────────────────────────────


def _resolve_dep(dep: str, current_spec: str, all_cards: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Return ``(spec_id, deliverable_id)`` for a ``depends_on`` entry.

    A bare ``D2`` is local to ``current_spec``; ``other-spec/D1`` is
    cross-spec. Any other shape, or an unknown spec / deliverable, raises.
    """
    dep = _str(dep)
    if not dep:
        raise ValueError("depends_on entry is empty")
    if "/" in dep:
        spec_part, _, did = dep.partition("/")
        spec_id = _str(spec_part)
        deliverable_id = _str(did)
        if not spec_id or not deliverable_id:
            raise ValueError(f"malformed cross-spec dep: {dep!r}")
    else:
        spec_id = current_spec
        deliverable_id = dep
    if spec_id not in all_cards:
        raise ValueError(f"depends_on references unknown spec: {dep!r}")
    deliverable_ids = {d.get("id") for d in all_cards[spec_id].get("deliverables", [])}
    if deliverable_id not in deliverable_ids:
        raise ValueError(f"depends_on references unknown deliverable: {dep!r}")
    return spec_id, deliverable_id


_SPEC_DEPENDS_RE = re.compile(
    r"^\s*#{0,6}\s*\**\s*Depends on:?\s*\**\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _spec_level_dep_specs(
    card: dict[str, Any], all_cards: dict[str, dict[str, Any]]
) -> list[str]:
    """Spec ids named by the source spec's ``Depends on:`` header.

    A spec-level ``Depends on: a, b`` header is a contract on the WHOLE spec:
    nothing in it may dispatch before the named specs are delivered. The
    contract compiler attaches it per-deliverable at best — run-1791763809
    -12583's synthesis card carried ``depends_on`` on D1 only, so dep-free
    D0 dispatched before the lens specs produced anything and synthesized
    nothing (REFUTED no_changes; the repair loop then gave up). Reading the
    header straight from the card's ``source_path`` makes the guarantee
    deterministic regardless of what the compiler emitted. Names that do
    not resolve to a known spec card are skipped with a warn (the header
    may legitimately name external things); per-deliverable ``depends_on``
    keeps its own fail-closed validation.
    """
    source = _str(card.get("source_path"))
    if not source:
        return []
    src = Path(source)
    if not src.is_file():
        candidate = Path.cwd() / src
        if candidate.is_file():
            src = candidate
        else:
            return []
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    dep_specs: list[str] = []
    for match in _SPEC_DEPENDS_RE.finditer(text):
        for name in match.group(1).split(","):
            name = name.strip().strip("`*_")
            spec_id = name.split("/", 1)[0].strip() if "/" in name else name
            if not spec_id:
                continue
            if spec_id not in all_cards:
                print(f"per_spec_dispatcher: spec-level dep {spec_id!r} is not a "
                      f"known spec card; skipping", file=sys.stderr)
                continue
            if spec_id not in dep_specs:
                dep_specs.append(spec_id)
    return dep_specs


def _effective_deps(
    spec_id: str,
    deliverable: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    spec_dep_specs: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """The deliverable's own ``depends_on`` pairs UNIONED with the spec-level fan-out.

    Spec-level deps fan out to EVERY deliverable of the named spec: a spec
    is delivered when all of its deliverables are, so depending on a spec
    means depending on each of them. Raises on the same malformed own-dep
    shapes ``_resolve_dep`` rejects (fail-closed is unchanged); de-dupes
    order-stably so a pair named both ways collapses to one edge.
    """
    pairs: list[tuple[str, str]] = []
    for raw in _as_list(deliverable.get("depends_on")):
        pairs.append(_resolve_dep(_str(raw), spec_id, cards))
    for dep_spec in spec_dep_specs.get(spec_id, []):
        if dep_spec == spec_id:
            continue
        for dep in cards[dep_spec].get("deliverables", []):
            if isinstance(dep, dict):
                dep_did = _str(dep.get("id"))
                if dep_did:
                    pairs.append((dep_spec, dep_did))
    seen: set[tuple[str, str]] = set()
    return [p for p in pairs if not (p in seen or seen.add(p))]


def _topological_order(
    cards: dict[str, dict[str, Any]],
    index_order: list[str],
    spec_dep_specs: dict[str, list[str]] | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(spec_id, deliverable_id, deliverable)`` in dispatch order.

    The order is deterministic: the spec index order (then card order for
    cards that the index doesn't list), then a topological sort across
    ``depends_on`` edges. Cards with cycles or unknown deps raise so the
    dispatcher fails closed rather than deadlocking an entire run.

    Deliverables with no depends_on edges retain their relative card order;
    ready-set ties are broken by (spec_id, deliverable_id) to make the
    dispatch order reproducible across runs.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    cards_in_order: list[str] = []
    seen: set[str] = set()
    for spec_id in index_order:
        if spec_id in seen or spec_id not in cards:
            continue
        seen.add(spec_id)
        cards_in_order.append(spec_id)
    for spec_id in sorted(cards):
        if spec_id in seen:
            continue
        seen.add(spec_id)
        cards_in_order.append(spec_id)

    for spec_id in cards_in_order:
        for deliverable in cards[spec_id].get("deliverables", []):
            if not isinstance(deliverable, dict):
                continue
            did = _str(deliverable.get("id"))
            if not did:
                continue
            by_key[(spec_id, did)] = deliverable

    in_degree: dict[tuple[str, str], int] = {key: 0 for key in by_key}
    edges: dict[tuple[str, str], list[tuple[str, str]]] = {key: [] for key in by_key}
    effective: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, deliverable in by_key.items():
        pairs = _effective_deps(key[0], deliverable, cards, spec_dep_specs or {})
        effective[key] = pairs
        for dep_key in pairs:
            if dep_key == key:
                raise ValueError(f"{key[0]}/{key[1]} depends on itself")
            edges[dep_key].append(key)
            in_degree[key] += 1

    # Spec index order drives the dispatch order: a ready deliverable in a
    # spec that appears earlier in the index runs before a later spec's
    # deliverable, even when neither has depends_on edges. Within a spec,
    # ties are broken by (spec_id, deliverable_id) so the order is
    # reproducible across runs.
    spec_rank = {spec_id: idx for idx, spec_id in enumerate(cards_in_order)}

    def _sort_key(key: tuple[str, str]) -> tuple[int, str, str]:
        return (spec_rank.get(key[0], len(spec_rank)), key[0], key[1])

    ready: list[tuple[str, str]] = sorted(
        (k for k, deg in in_degree.items() if deg == 0),
        key=_sort_key,
    )
    ordered: list[tuple[str, str, dict[str, Any]]] = []
    while ready:
        key = ready.pop(0)
        ordered.append((key[0], key[1], by_key[key]))
        next_keys = sorted(edges[key], key=_sort_key)
        for nxt in next_keys:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=_sort_key)
        edges[key] = []
    if len(ordered) != len(by_key):
        unresolved = sorted(k for k, deg in in_degree.items() if deg > 0)
        raise ValueError(f"dependency cycle / unresolved deps involving: {unresolved!r}")
    return ordered


def _wave_levels(
    ordered: list[tuple[str, str, dict[str, Any]]],
    cards: dict[str, dict[str, Any]],
    spec_dep_specs: dict[str, list[str]],
) -> list[list[tuple[str, str, dict[str, Any]]]]:
    """Group the topologically-ordered deliverables into dependency levels.

    A deliverable's level is ``1 + max(level of its effective deps)``; roots
    sit at level 0. Two members of the same level never depend on each other
    (all edges point strictly downward), so a level is safe to run fully
    concurrently — ``MO_DISPATCH_WAVES`` caps how many run at once.
    """
    level_of: dict[tuple[str, str], int] = {}
    levels: list[list[tuple[str, str, dict[str, Any]]]] = []
    for spec_id, deliverable_id, deliverable in ordered:
        deps = _effective_deps(spec_id, deliverable, cards, spec_dep_specs)
        depth = 0 if not deps else 1 + max(level_of.get(dep, 0) for dep in deps)
        level_of[(spec_id, deliverable_id)] = depth
        while len(levels) <= depth:
            levels.append([])
        levels[depth].append((spec_id, deliverable_id, deliverable))
    return levels


# ── child kickoff + dispatch ──────────────────────────────────────────────


def _write_child_kickoff(
    path: Path,
    spec_id: str,
    deliverable: dict[str, Any],
    card: dict[str, Any],
    gates_doc: dict[str, Any],
    ordered_deps: list[tuple[str, str]],
) -> None:
    """Write one deliverable's child kickoff.

    The kickoff is the SOLE anchor for the inner implementer: it embeds the
    SpecCard path + source_hash, the relevant clauses + acceptance entries,
    the gate probes verbatim, and a hard rule against editing the card /
    gates / source spec.
    """
    deliverable_id = _str(deliverable.get("id"))
    title = _str(deliverable.get("title")) or deliverable_id
    acceptance_refs = [_str(r) for r in _as_list(deliverable.get("acceptance_refs")) if _str(r)]
    depends_on = [_str(d) for d in _as_list(deliverable.get("depends_on")) if _str(d)]
    clauses = card.get("clauses") or {}
    functional = clauses.get("functional") or []
    quality = clauses.get("quality") or []
    constitutional = clauses.get("constitutional") or []
    architectural = clauses.get("architectural") or []
    acceptance = card.get("acceptance") or []

    cited_clause_ids: set[str] = set()
    cited_acceptance: list[dict[str, Any]] = []
    for entry in acceptance:
        if not isinstance(entry, dict):
            continue
        if _str(entry.get("id")) in acceptance_refs:
            cited_acceptance.append(entry)
            for clause_ref in _as_list(entry.get("clause_refs")):
                cited_clause_ids.add(_str(clause_ref))

    cited_clauses: list[dict[str, Any]] = []
    for bucket in (functional, quality, constitutional, architectural):
        for entry in bucket:
            if isinstance(entry, dict) and _str(entry.get("id")) in cited_clause_ids:
                cited_clauses.append(entry)

    probes = _gates_for_acceptance(gates_doc, acceptance_refs)
    card_path = _str(card.get("source_path"))
    source_hash = _str(card.get("source_hash"))

    lines: list[str] = []
    lines.append(f"# recursive-validate-impl child kickoff: {spec_id}--{deliverable_id}")
    lines.append("")
    lines.append("## Deliverable")
    lines.append("")
    lines.append(f"- spec_id: `{spec_id}`")
    lines.append(f"- deliverable_id: `{deliverable_id}`")
    lines.append(f"- title: {title}")
    lines.append(f"- depends_on: `{', '.join(depends_on) if depends_on else 'none'}`")
    lines.append("")
    lines.append("## SpecCard anchor (read-only)")
    lines.append("")
    lines.append(f"- card path: `{card_path}`")
    lines.append(f"- source_hash: `{source_hash}`")
    lines.append("- this card is the SOLE source of truth for this deliverable.")
    lines.append("- do not add requirements from chat history, earlier failed patches, or other specs.")
    lines.append("")
    lines.append("## Cited clauses")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(cited_clauses, indent=2, sort_keys=True, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Cited acceptance entries")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(cited_acceptance, indent=2, sort_keys=True, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Definition of Done probes (verbatim from gates)")
    lines.append("")
    if probes:
        lines.append("```json")
        lines.append(json.dumps(probes, indent=2, sort_keys=True, ensure_ascii=False))
        lines.append("```")
    else:
        lines.append("_No probes found for the cited acceptance_refs; this is a defect "
                     "and should be raised as an ASK by the parent dispatcher._")
    lines.append("")
    lines.append("## Hard rules")
    lines.append("")
    lines.append("- Do not edit the SpecCard, the gate file, or the source spec.")
    lines.append("- Keep the patch scoped to this deliverable; preserve unrelated user changes.")
    lines.append("- Do not invent additional requirements beyond the cited clauses / acceptance entries.")
    lines.append("- Do not propose new dependencies that are not in the deliverable's cited clauses.")
    lines.append("")
    lines.append("## Dependency context")
    lines.append("")
    if ordered_deps:
        lines.append("These sibling deliverables must reach `delivered` before this one can start:")
        lines.append("")
        for dep_spec, dep_did in ordered_deps:
            lines.append(f"- `{dep_spec}/{dep_did}`")
    else:
        lines.append("_No upstream dependencies; this deliverable is dispatch-ready._")
    lines.append("")
    lines.append("## Success")
    lines.append("")
    lines.append("- inner run verdict `pass: true` for every gate listed above.")
    lines.append("- the inner run writes `${MINI_ORK_RUN_DIR}/implementer-summary.json` "
                 "with `touched_files` and a git sha `commit`.")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_ask(run_dir: Path, spec_id: str, deliverable_id: str, ask: dict[str, Any]) -> Path:
    """Write a structured ASK artifact under ``asks/<spec>--<id>.json``."""
    ask_path = run_dir / "asks" / f"{spec_id}--{deliverable_id}.json"
    payload = {
        "spec_id": spec_id,
        "deliverable_id": deliverable_id,
        "question": _str(ask.get("question")),
        "blocking_refs": [_str(r) for r in _as_list(ask.get("blocking_refs")) if _str(r)],
        "options": [_str(o) for o in _as_list(ask.get("options")) if _str(o)],
        "evidence": [_str(e) for e in _as_list(ask.get("evidence")) if _str(e)],
        "raised_at": _utc_now(),
    }
    if not payload["question"]:
        raise ValueError(f"ASK for {spec_id}/{deliverable_id} missing 'question'")
    _atomic_write_json(ask_path, payload)
    return ask_path


def _child_command(root: Path, kickoff_path: Path) -> list[str]:
    override = os.environ.get("MINI_ORK_PER_SPEC_MINI_ORK_BIN")
    mini_ork = Path(override) if override else root / "bin" / "mini-ork"
    return [str(mini_ork), "run", "recursive-validate-impl", str(kickoff_path)]


def _parse_key_values(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _read_child_verdict(child_dir: Path) -> dict[str, Any] | None:
    """Accept ``verdict.json`` or ``panel-verdict.json`` — mirrors dispatch-aggregator."""
    verdict_path = child_dir / "verdict.json"
    if verdict_path.exists() and verdict_path.stat().st_size > 0:
        try:
            data = _load_json(verdict_path)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict) and isinstance(data.get("pass"), bool):
            data.setdefault("_source_path", str(verdict_path))
            return data

    panel_path = child_dir / "panel-verdict.json"
    if panel_path.exists() and panel_path.stat().st_size > 0:
        try:
            panel = _load_json(panel_path)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(panel, dict):
            if isinstance(panel.get("pass"), bool):
                merged = {"pass": panel["pass"], "_source_path": str(panel_path),
                          "_source": panel}
                return merged
            verdict = _str(panel.get("verdict")).lower()
            if verdict in {"pass", "passed", "approve", "approved", "delivered"}:
                return {"pass": True, "_source_path": str(panel_path), "_source": panel}
            if verdict in {"fail", "failed", "request_changes", "escalate", "reject", "rejected"}:
                return {"pass": False, "_source_path": str(panel_path), "_source": panel}
    return None


def _wait_for_child_verdict(child_dir: Path, log_path: Path) -> dict[str, Any] | None:
    """Poll for the verdict artifact with a timeout (default 30 minutes)."""
    timeout = int(os.environ.get("MO_CHILD_VERDICT_TIMEOUT_SEC", "1800"))
    poll = float(os.environ.get("MO_CHILD_VERDICT_POLL_SEC", "5"))
    deadline = time.monotonic() + max(timeout, 0)
    while time.monotonic() <= deadline:
        verdict = _read_child_verdict(child_dir)
        if verdict is not None:
            return verdict
        time.sleep(max(poll, 0.5))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[dispatcher] timed out waiting {timeout}s for child verdict in {child_dir}\n")
    return None


def _child_implementer_summary(child_dir: Path) -> dict[str, Any]:
    summary_path = child_dir / "implementer-summary.json"
    if not summary_path.is_file():
        return {}
    try:
        data = _load_json(summary_path)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _child_commit(verdict: dict[str, Any] | None, child_dir: Path) -> str | None:
    sha_re = re.compile(r"^[0-9a-f]{7,40}$")
    if verdict:
        for key in ("commit", "_source_commit"):
            value = verdict.get(key)
            if isinstance(value, str) and sha_re.fullmatch(value.strip().lower()):
                return value.strip().lower()
    summary = _child_implementer_summary(child_dir)
    value = summary.get("commit")
    if isinstance(value, str) and sha_re.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return None


# ── ASK detection ─────────────────────────────────────────────────────────


def _validate_for_dispatch(
    spec_id: str,
    deliverable: dict[str, Any],
    card: dict[str, Any],
    gates_doc: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Pre-dispatch sanity check.

    Returns ``(ok, ask)``; when ``ok`` is False the deliverable is blocked
    and ``ask`` describes the missing fact. The validation enforces the
    recipe prompt's authorship-separation rule: the dispatcher must NOT
    guess when the SpecCard or gates are insufficient — it must surface a
    structured ASK instead.
    """
    deliverable_id = _str(deliverable.get("id"))
    acceptance_refs = [_str(r) for r in _as_list(deliverable.get("acceptance_refs")) if _str(r)]
    if not acceptance_refs:
        return False, {
            "question": f"{spec_id}/{deliverable_id}: deliverable has no acceptance_refs; "
                        "contract_compiler must populate them from the SpecCard's acceptance list",
            "blocking_refs": [],
            "options": [],
            "evidence": [f"spec-cards/{spec_id}.json"],
        }

    acceptance = card.get("acceptance") or []
    acceptance_ids = {_str(a.get("id")) for a in acceptance if isinstance(a, dict)}
    missing = [r for r in acceptance_refs if r not in acceptance_ids]
    if missing:
        return False, {
            "question": f"{spec_id}/{deliverable_id}: acceptance_refs {missing!r} are "
                        "missing from the SpecCard's acceptance list",
            "blocking_refs": missing,
            "options": [],
            "evidence": [f"spec-cards/{spec_id}.json"],
        }

    if not gates_doc:
        return False, {
            "question": f"{spec_id}/{deliverable_id}: gates/{spec_id}.json is missing; "
                        "test_author must materialize probes before dispatch",
            "blocking_refs": acceptance_refs,
            "options": [],
            "evidence": [f"gates/{spec_id}.json"],
        }

    probes = _gates_for_acceptance(gates_doc, acceptance_refs)
    if not probes:
        return False, {
            "question": f"{spec_id}/{deliverable_id}: gates/{spec_id}.json has no probes for "
                        f"acceptance_refs {acceptance_refs!r}",
            "blocking_refs": acceptance_refs,
            "options": [],
            "evidence": [f"gates/{spec_id}.json"],
        }

    return True, None


# ── main dispatch loop ────────────────────────────────────────────────────


def _record_for(
    spec_id: str,
    deliverable_id: str,
    *,
    child_kickoff: Path | None,
    status: str,
    child_run_id: str | None = None,
    child_run_dir: str | None = None,
    verdict_path: str | None = None,
    ask_path: str | None = None,
) -> dict[str, Any]:
    return {
        "spec_id": spec_id,
        "deliverable_id": deliverable_id,
        "child_kickoff": str(child_kickoff) if child_kickoff else None,
        "child_run_id": child_run_id,
        "child_run_dir": child_run_dir,
        "status": status,
        "verdict_path": verdict_path,
        "ask_path": ask_path,
    }


def _launch_child(
    spec_id: str,
    deliverable_id: str,
    deliverable: dict[str, Any],
    card: dict[str, Any],
    gates_doc: dict[str, Any],
    run_dir: Path,
    root: Path,
    home: Path,
    parent_run_id: str,
    ordered_deps: list[tuple[str, str]],
) -> tuple[dict[str, Any], subprocess.Popen | None, Any, Path]:
    """Write the kickoff and spawn the child WITHOUT waiting on it.

    Returns ``(record, proc, log_handle, log_path)``. ``proc`` is ``None``
    only on a launch failure (the record is already ``failed``); otherwise
    the CALLER owns ``log_handle`` and must close it — normally by handing
    both to ``_collect_child``, which closes the log before reading it.
    """
    kickoff_path = run_dir / "child-kickoffs" / f"{spec_id}--{deliverable_id}.md"
    _write_child_kickoff(kickoff_path, spec_id, deliverable, card, gates_doc, ordered_deps)

    child_run_id = f"{parent_run_id}--{spec_id}--{deliverable_id}"
    child_dir = home / "runs" / child_run_id
    log_path = run_dir / "child-runs" / f"{spec_id}--{deliverable_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(root)
    env.setdefault("MINI_ORK_DB", str(home / "state.db"))
    env["MINI_ORK_RUN_ID"] = child_run_id
    env["MINI_ORK_TASK_RUN_ID"] = child_run_id
    env["MINI_ORK_PARENT_RUN_ID"] = parent_run_id
    env.pop("MINI_ORK_RUN_DIR", None)
    env.pop("MINI_ORK_PLAN_PATH", None)

    record = _record_for(
        spec_id,
        deliverable_id,
        child_kickoff=kickoff_path,
        status=STATUS_PENDING,
        child_run_id=child_run_id,
        child_run_dir=str(child_dir),
    )

    log = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            _child_command(root, kickoff_path),
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        log.write(f"\n[dispatcher] child launch failed: {exc!r}\n")
        log.close()
        record["status"] = STATUS_FAILED
        return record, None, None, log_path
    return record, proc, log, log_path


def _collect_child(
    record: dict[str, Any],
    proc: subprocess.Popen | None,
    log: Any,
    log_path: Path,
    home: Path,
) -> dict[str, Any]:
    """Wait for a launched child (exit + verdict) and finalize its record.

    Mirrors the old blocking ``_dispatch_one`` tail exactly: close the log,
    ``wait()``, re-parse ``child_run_id=``/``child_run_dir=`` from the log,
    poll for the verdict artifact, then derive ``status`` from
    ``verdict.pass AND returncode == 0``.
    """
    if proc is None:
        return record
    if log is not None:
        log.close()
    proc.wait()

    child_dir = Path(record["child_run_dir"])
    output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    parsed = _parse_key_values(output)
    if parsed.get("child_run_id"):
        record["child_run_id"] = parsed["child_run_id"]
        child_dir = Path(parsed.get("child_run_dir") or home / "runs" / parsed["child_run_id"])
        record["child_run_dir"] = str(child_dir)

    verdict = _wait_for_child_verdict(child_dir, log_path)
    record["commit"] = _child_commit(verdict, child_dir)

    if verdict is None:
        record["status"] = STATUS_FAILED if proc.returncode else STATUS_PENDING
        record["error"] = f"missing child verdict (rc={proc.returncode})"
        return record

    verdict_path = Path(record["child_run_dir"]) / "verdict.json"
    if not verdict_path.is_file():
        alt = Path(record["child_run_dir"]) / "panel-verdict.json"
        verdict_path = alt if alt.is_file() else verdict_path
    record["verdict_path"] = str(verdict_path) if verdict_path.is_file() else None
    passed = verdict.get("pass") is True
    record["status"] = STATUS_DELIVERED if passed and proc.returncode == 0 else STATUS_FAILED
    if record["status"] == STATUS_FAILED:
        record["error"] = f"child exited {proc.returncode}; pass={verdict.get('pass')}"
    return record


def _prepare(
    spec_id: str,
    deliverable_id: str,
    deliverable: dict[str, Any],
    card: dict[str, Any],
    gates_doc: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    spec_dep_specs: dict[str, list[str]],
    records: dict[tuple[str, str], dict[str, Any]],
    run_dir: Path,
) -> tuple[dict[str, Any], None] | tuple[None, list[tuple[str, str]]]:
    """Pre-dispatch checks shared by the serial and the wave paths.

    Returns ``(record, dep_pairs)``. A non-None ``record`` means DO NOT
    launch: ``pending`` (a dependency failed or is blocked — never
    re-dispatched in this run) or ``blocked`` (SpecCard/gates insufficient;
    a structured ASK artifact was written). ``(None, dep_pairs)`` means
    cleared for launch with those dependency pairs for the kickoff.

    Raises ``ValueError`` on a malformed dependency — the caller fails
    closed rather than guessing a dep.
    """
    dep_pairs = _effective_deps(spec_id, deliverable, cards, spec_dep_specs)
    for dep_spec, dep_did in dep_pairs:
        dep_status = records.get((dep_spec, dep_did), {}).get("status")
        if dep_status not in _DELIVERED_SET:
            record = _record_for(
                spec_id, deliverable_id,
                child_kickoff=None,
                status=STATUS_PENDING,
            )
            record["reason"] = "dependency failed or blocked"
            return record, None

    ok, ask = _validate_for_dispatch(spec_id, deliverable, card, gates_doc)
    if not ok and ask is not None:
        ask_path = _write_ask(run_dir, spec_id, deliverable_id, ask)
        record = _record_for(
            spec_id, deliverable_id,
            child_kickoff=None,
            status=STATUS_BLOCKED,
            ask_path=str(ask_path),
        )
        return record, None
    return None, dep_pairs


def _dispatch_one(
    spec_id: str,
    deliverable_id: str,
    deliverable: dict[str, Any],
    card: dict[str, Any],
    gates_doc: dict[str, Any],
    run_dir: Path,
    root: Path,
    home: Path,
    parent_run_id: str,
    ordered_deps: list[tuple[str, str]],
) -> dict[str, Any]:
    """Dispatch a single deliverable serially: launch, then collect."""
    record, proc, log, log_path = _launch_child(
        spec_id, deliverable_id, deliverable, card, gates_doc,
        run_dir, root, home, parent_run_id, ordered_deps,
    )
    return _collect_child(record, proc, log, log_path, home)


def dispatch() -> int:
    """Walk every SpecCard's deliverables and write ``dispatch-results.json``.

    Returns ``0`` on a clean exit (the verifier node decides pass/fail from
    the produced file), ``1`` when the dispatcher itself could not produce
    a usable record file (missing run dir, malformed SpecCards, dep cycle).
    """
    run_dir_raw = os.environ.get("MINI_ORK_RUN_DIR")
    if not run_dir_raw:
        print("per_spec_dispatcher: MINI_ORK_RUN_DIR is not set", file=sys.stderr)
        return 1
    run_dir = Path(run_dir_raw).resolve()
    if not run_dir.is_dir():
        print(f"per_spec_dispatcher: MINI_ORK_RUN_DIR is not a directory: {run_dir}",
              file=sys.stderr)
        return 1

    root = Path(os.environ.get("MINI_ORK_ROOT") or Path.cwd()).resolve()
    home = Path(os.environ.get("MINI_ORK_HOME") or Path.cwd() / ".mini-ork").resolve()
    parent_run_id = (os.environ.get("MINI_ORK_RUN_ID")
                     or os.environ.get("MINI_ORK_TASK_RUN_ID")
                     or run_dir.name)

    spec_index = _load_spec_index(run_dir)
    index_order = list(spec_index.keys())
    try:
        cards = _load_cards(run_dir)
    except (OSError, ValueError) as exc:
        print(f"per_spec_dispatcher: spec-cards load failed: {exc}", file=sys.stderr)
        return 1

    spec_dep_specs = {
        spec_id: _spec_level_dep_specs(card, cards) for spec_id, card in cards.items()
    }
    for spec_id, dep_specs in spec_dep_specs.items():
        if dep_specs:
            print(f"per_spec_dispatcher: spec {spec_id!r} inherits spec-level deps on "
                  f"{dep_specs}", file=sys.stderr)

    try:
        ordered = _topological_order(cards, index_order, spec_dep_specs)
    except ValueError as exc:
        print(f"per_spec_dispatcher: dependency resolution failed: {exc}", file=sys.stderr)
        return 1

    fail_fast = os.environ.get("MO_PER_SPEC_DISPATCH_FAIL_FAST", "0") != "0"
    try:
        waves = int(os.environ.get("MO_DISPATCH_WAVES", "1"))
    except ValueError:
        print("per_spec_dispatcher: MO_DISPATCH_WAVES is not an integer", file=sys.stderr)
        return 1
    if waves < 1:
        print(f"per_spec_dispatcher: MO_DISPATCH_WAVES must be >= 1, got {waves}",
              file=sys.stderr)
        return 1
    child_kickoffs_dir = run_dir / "child-kickoffs"
    child_kickoffs_dir.mkdir(parents=True, exist_ok=True)
    child_runs_dir = run_dir / "child-runs"
    child_runs_dir.mkdir(parents=True, exist_ok=True)
    asks_dir = run_dir / "asks"
    asks_dir.mkdir(parents=True, exist_ok=True)

    records: dict[tuple[str, str], dict[str, Any]] = {}
    gates_cache: dict[str, dict[str, Any]] = {}

    exit_code = 0
    if waves >= 2:
        levels = _wave_levels(ordered, cards, spec_dep_specs)
        print(f"per_spec_dispatcher: MO_DISPATCH_WAVES={waves} → {len(levels)} "
              f"dependency level(s), sizes {[len(level) for level in levels]}",
              file=sys.stderr)
        abort = False
        for level in levels:
            if abort:
                break
            for chunk_start in range(0, len(level), waves):
                chunk = level[chunk_start:chunk_start + waves]
                inflight: list[
                    tuple[tuple[str, str], dict[str, Any], subprocess.Popen, Any, Path]
                ] = []
                chunk_failed = False
                for spec_id, deliverable_id, deliverable in chunk:
                    card = cards[spec_id]
                    gates_doc = gates_cache.setdefault(spec_id, _load_gates(run_dir, spec_id))
                    try:
                        blocked_record, dep_pairs = _prepare(
                            spec_id, deliverable_id, deliverable, card, gates_doc,
                            cards, spec_dep_specs, records, run_dir,
                        )
                    except ValueError as exc:
                        print(f"per_spec_dispatcher: {spec_id}/{deliverable_id}: {exc}",
                              file=sys.stderr)
                        return 1
                    if blocked_record is not None:
                        records[(spec_id, deliverable_id)] = blocked_record
                        continue
                    try:
                        record, proc, log, log_path = _launch_child(
                            spec_id, deliverable_id, deliverable, card, gates_doc,
                            run_dir, root, home, parent_run_id, dep_pairs,
                        )
                    except (OSError, ValueError) as exc:
                        print(f"per_spec_dispatcher: dispatch {spec_id}/{deliverable_id} "
                              f"failed: {exc}", file=sys.stderr)
                        record = _record_for(
                            spec_id, deliverable_id,
                            child_kickoff=None,
                            status=STATUS_FAILED,
                        )
                        record["error"] = str(exc)
                        records[(spec_id, deliverable_id)] = record
                        exit_code = 1
                        chunk_failed = True
                        continue
                    if proc is None:
                        records[(spec_id, deliverable_id)] = record
                        exit_code = 1
                        chunk_failed = True
                        continue
                    inflight.append(((spec_id, deliverable_id), record, proc, log, log_path))
                for key, record, proc, log, log_path in inflight:
                    try:
                        record = _collect_child(record, proc, log, log_path, home)
                    except (OSError, ValueError) as exc:
                        print(f"per_spec_dispatcher: collect {key[0]}/{key[1]} failed: {exc}",
                              file=sys.stderr)
                        record["status"] = STATUS_FAILED
                        record["error"] = str(exc)
                    records[key] = record
                    if record["status"] != STATUS_DELIVERED:
                        exit_code = 1
                        chunk_failed = True
                if fail_fast and chunk_failed:
                    abort = True
                    break
    else:
        for spec_id, deliverable_id, deliverable in ordered:
            card = cards[spec_id]
            gates_doc = gates_cache.setdefault(spec_id, _load_gates(run_dir, spec_id))

            try:
                blocked_record, dep_pairs = _prepare(
                    spec_id, deliverable_id, deliverable, card, gates_doc,
                    cards, spec_dep_specs, records, run_dir,
                )
            except ValueError as exc:
                print(f"per_spec_dispatcher: {spec_id}/{deliverable_id}: {exc}", file=sys.stderr)
                return 1
            if blocked_record is not None:
                records[(spec_id, deliverable_id)] = blocked_record
                continue

            try:
                record = _dispatch_one(
                    spec_id, deliverable_id, deliverable, card, gates_doc,
                    run_dir, root, home, parent_run_id, dep_pairs,
                )
            except (OSError, ValueError) as exc:
                print(f"per_spec_dispatcher: dispatch {spec_id}/{deliverable_id} failed: {exc}",
                      file=sys.stderr)
                records[(spec_id, deliverable_id)] = _record_for(
                    spec_id, deliverable_id,
                    child_kickoff=None,
                    status=STATUS_FAILED,
                )
                records[(spec_id, deliverable_id)]["error"] = str(exc)
                exit_code = 1
                continue

            records[(spec_id, deliverable_id)] = record
            if record["status"] != STATUS_DELIVERED:
                exit_code = 1
                if fail_fast:
                    break

    deliverables_list = [records[key] for key in records]
    output = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "parent_run_id": parent_run_id,
        "deliverables": deliverables_list,
    }
    _atomic_write_json(run_dir / "dispatch-results.json", output)
    per_deliverable_dir = child_runs_dir
    for record in deliverables_list:
        per_deliverable = per_deliverable_dir / f"{record['spec_id']}--{record['deliverable_id']}.json"
        _atomic_write_json(per_deliverable, record)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(dispatch())