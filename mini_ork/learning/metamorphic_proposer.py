"""Safe auto-proposal of metamorphic relations (Layer 2 auto-wiring).

Makes Layer 2 fire without a hand-written spec by letting an LLM *propose* which
relations to check — but only as VALIDATED DATA, never executable code. The
proposer returns relation NAMES (checked against the vetted
``metamorphic.RELATION_LIBRARY`` whitelist) plus seed inputs (plain JSON). Unknown
names are dropped and nothing model-authored is ever ``exec``'d, so the
truth-grounded property holds: the LLM can only pick from audited relations, and
execution remains the oracle (arXiv 2603.24774 — LLM proposes, execution
certifies).

Pure module: the LLM dispatch and the target-function import happen in the caller
(the eval node or the metamorphic verifier); this file builds the prompt,
validates the proposal, and shapes the JSON spec the verifier consumes.
"""

from __future__ import annotations

import json

from mini_ork.learning import metamorphic as mm


def build_proposer_prompt(function_name: str, function_source: str) -> str:
    """Ask the LLM to SELECT relations (by name, from the library) and seed inputs
    for a function under test. It may only choose from the catalog; it cannot
    invent relations."""
    catalog = "\n".join(f"  - {c['name']}: {c['description']}"
                        for c in mm.library_catalog())
    return (
        f"You are proposing metamorphic tests for a function under repair. Pick the "
        f"invariances from the LIBRARY BELOW that must hold for this function, and a "
        f"few seed inputs to test them on. Choose only relations that are actually "
        f"valid for this function's semantics — a wrong relation is worse than none.\n\n"
        f"## Function: {function_name}\n{function_source}\n\n"
        f"## Relation library (you may ONLY select these by name)\n{catalog}\n\n"
        f"Each seed input is passed as the SINGLE argument to the function (wrap "
        f"multiple operands as a list/tuple). Return ONLY strict JSON:\n"
        f'{{"relations": ["<name>", ...], "seed_inputs": [<json value>, ...]}}\n'
    )


def _extract_json_object(text: str) -> str | None:
    """First balanced ``{...}`` object, string-aware (prefers a ```json fence)."""
    if not text:
        return None
    fence = text.find("```json")
    start = text.find("{", fence + 7) if fence != -1 else -1
    if start == -1:
        start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_proposal(text: str) -> dict:
    """Validate an LLM proposal into a safe {relations, seed_inputs}.

    THE SAFETY BOUNDARY: relation names are filtered to the vetted library
    (unknown/garbage/`; rm -rf` names are dropped, never executed); seed inputs
    are kept only if they are plain JSON scalars/containers. A proposal that
    yields no known relations returns empty lists — the verifier then no-ops
    (vacuous) rather than testing nothing meaningful."""
    raw = _extract_json_object(text or "")
    if raw is None:
        return {"relations": [], "seed_inputs": []}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {"relations": [], "seed_inputs": []}
    if not isinstance(obj, dict):
        return {"relations": [], "seed_inputs": []}
    names = obj.get("relations") if isinstance(obj.get("relations"), list) else []
    relations = [n for n in names if isinstance(n, str) and n in mm.RELATION_LIBRARY]
    seeds_in = obj.get("seed_inputs") if isinstance(obj.get("seed_inputs"), list) else []
    seed_inputs = [s for s in seeds_in if _is_json_data(s)]
    return {"relations": relations, "seed_inputs": seed_inputs}


def _is_json_data(x) -> bool:
    """Only plain JSON data is accepted as a seed input (no surprises)."""
    if isinstance(x, (str, int, float, bool)) or x is None:
        return True
    if isinstance(x, list):
        return all(_is_json_data(v) for v in x)
    if isinstance(x, dict):
        return all(isinstance(k, str) and _is_json_data(v) for k, v in x.items())
    return False


def to_spec(module: str, function: str, proposal: dict) -> dict:
    """Shape the validated proposal into the JSON spec the metamorphic verifier
    consumes: target + seed inputs + relation names (all data)."""
    return {
        "target": {"module": module, "function": function},
        "seed_inputs": proposal.get("seed_inputs", []),
        "relations": proposal.get("relations", []),
    }
