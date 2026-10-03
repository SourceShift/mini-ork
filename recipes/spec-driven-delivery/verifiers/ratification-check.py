#!/usr/bin/env python3
"""ratification-check — every source requirement is covered or flagged.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ratification_check).
For each ${MINI_ORK_RUN_DIR}/spec-cards/<spec_id>.json: validates against
schemas/spec-card.schema.json; source_hash still matches the source file
bytes (spec drift = fail); every acceptance entry has exactly one gate; every
deliverable's acceptance_refs resolve and every acceptance id is referenced by
a deliverable. Reads ${MINI_ORK_RUN_DIR}/ratification/<spec_id>.json (kept
out of the card, whose schema allows no extra keys): fail unless its
`ratification` list is empty or every entry has `acknowledged: true`.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Also checked per card: the file stem equals spec_id; the card's spec_id,
source_path and source_hash agree with spec-index.json, and every indexed
spec has a card; acceptance, clause and deliverable ids are unique;
clause_refs resolve; the card has at least one acceptance entry and one
deliverable; deliverable depends_on edges resolve (cross-spec
`<spec_id>/<id>` included) and form no cycle (K1 find_cycles). The
ratification record's spec_id and source_hash must match the card. A missing
spec-index.json or an empty spec-cards/ exits 2; a missing or unreadable
card-side file is a per-spec violation. Cards are never rewritten here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    Malformed,
    card_files,
    run_dir,
    sha256_file,
    specdir_module,
    try_load_json,
)


def _dups(ids: list[str]) -> list[str]:
    return sorted({i for i in ids if ids.count(i) > 1})


def check_card(card: dict, stem: str, entry: dict | None) -> list[str]:
    """Structural and drift violations of one schema-valid card."""
    out = []
    sid = card["spec_id"]
    if stem != sid:
        out.append(f"file stem '{stem}' != spec_id '{sid}'")
    if entry is None:
        out.append(f"spec_id '{sid}' is not in spec-index.json")
    else:
        for key in ("source_path", "source_hash"):
            if card[key] != entry[key]:
                out.append(f"{key} differs from spec-index.json")
    source = Path(card["source_path"])
    if not source.is_file():
        out.append(f"spec drift: source file missing: {source}")
    elif sha256_file(source) != card["source_hash"]:
        out.append("spec drift: source_hash no longer matches the source file bytes")

    clause_ids = [c["id"] for bucket in card["clauses"].values() for c in bucket]
    acc_ids = [a["id"] for a in card["acceptance"]]
    del_ids = [d["id"] for d in card["deliverables"]]
    for label, ids in (("clause", clause_ids), ("acceptance", acc_ids), ("deliverable", del_ids)):
        for dup in _dups(ids):
            out.append(f"duplicate {label} id '{dup}'")
    if not acc_ids:
        out.append("no acceptance entries")
    if not del_ids:
        out.append("no deliverables")
    for a in card["acceptance"]:
        if not isinstance(a.get("gate"), dict):
            out.append(f"acceptance '{a['id']}' does not have exactly one gate")
        for ref in a["clause_refs"]:
            if ref not in clause_ids:
                out.append(f"acceptance '{a['id']}' clause_ref '{ref}' does not resolve")
    referenced = set()
    for d in card["deliverables"]:
        for ref in d["acceptance_refs"]:
            referenced.add(ref)
            if ref not in acc_ids:
                out.append(f"deliverable '{d['id']}' acceptance_ref '{ref}' does not resolve")
    for aid in acc_ids:
        if aid not in referenced:
            out.append(f"acceptance '{aid}' is not referenced by any deliverable")
    return out


def check_depends_on(cards: dict[str, dict]) -> dict[str, list[str]]:
    index_mod = specdir_module("index")
    known = {f"{sid}/{d['id']}" for sid, c in cards.items() for d in c["deliverables"]}
    out: dict[str, list[str]] = {}
    for sid, card in cards.items():
        for d in card["deliverables"]:
            for dep in d["depends_on"]:
                node = dep if "/" in dep else f"{sid}/{dep}"
                if node not in known:
                    out.setdefault(sid, []).append(
                        f"deliverable '{d['id']}' depends_on '{dep}' does not resolve")
    for cycle in index_mod.find_cycles(index_mod.deliverable_graph(cards.values())):
        for sid in sorted({node.split("/", 1)[0] for node in cycle}):
            out.setdefault(sid, []).append(f"deliverable dependency cycle {' -> '.join(cycle)}")
    return out


def check_ratification(rd: Path, card: dict) -> list[str]:
    sid = card["spec_id"]
    record, problem = try_load_json(rd / "ratification" / f"{sid}.json")
    if problem:
        return [f"ratification record: {problem}"]
    if not isinstance(record, dict) or not isinstance(record.get("ratification"), list):
        return ["ratification record: expected an object with a 'ratification' list"]
    out = []
    if record.get("spec_id") != sid:
        out.append("ratification record: spec_id does not match the card")
    if record.get("source_hash") != card["source_hash"]:
        out.append("ratification record: source_hash does not match the card")
    for i, item in enumerate(record["ratification"]):
        if not isinstance(item, dict) or item.get("acknowledged") is not True:
            excerpt = item.get("source_excerpt", "") if isinstance(item, dict) else ""
            out.append(f"unacknowledged uncovered requirement [{i}]: {str(excerpt)[:120]!r}")
    return out


def body():
    rd = run_dir()
    files = card_files(rd)
    if not files:
        raise Malformed(f"no SpecCards in {rd / 'spec-cards'}")
    index_path = rd / "spec-index.json"
    if not index_path.is_file():
        raise Malformed(f"required input missing: {index_path}")
    index_mod = specdir_module("index")
    try:
        index_specs = index_mod.read_index(index_path)["specs"]
    except index_mod.SpecIndexError as exc:
        raise Malformed(f"spec-index.json unreadable: {exc}") from None
    validate_card = specdir_module().validate_card

    violations: dict[str, list[str]] = {}
    valid: dict[str, dict] = {}
    for path in files:
        card, problem = try_load_json(path)
        if problem or not isinstance(card, dict):
            violations.setdefault(path.stem, []).append(problem or "card is not a JSON object")
            continue
        errors = validate_card(card)
        if errors:
            violations.setdefault(path.stem, []).extend(f"schema: {e}" for e in errors[:10])
            continue
        sid = card["spec_id"]
        if sid in valid:
            violations.setdefault(sid, []).append(f"duplicate card for spec_id (also {path.name})")
            continue
        valid[sid] = card
        found = check_card(card, path.stem, index_specs.get(sid)) + check_ratification(rd, card)
        violations.setdefault(sid, []).extend(found)
    for sid, found in check_depends_on(valid).items():
        violations.setdefault(sid, []).extend(found)
    for sid in sorted(set(index_specs) - set(valid)):
        violations.setdefault(sid, []).append("indexed spec has no valid SpecCard")

    specs = {sid: {"ok": not v, "violations": v} for sid, v in sorted(violations.items())}
    failing = [sid for sid, s in specs.items() if not s["ok"]]
    detail = {"card_count": len(files), "specs": specs,
              "violation_count": sum(len(s["violations"]) for s in specs.values())}
    if failing:
        first = specs[failing[0]]["violations"][0]
        return False, f"{len(failing)} spec(s) not ratifiable; {failing[0]}: {first}", detail
    return True, f"{len(valid)} SpecCard(s) ratified against their sources", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
