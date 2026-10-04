#!/usr/bin/env python3
"""Materialize gates/<spec_id>.json deterministically from the spec authors' own probes.

Replaces the LLM test_author node (3/3 consensus 2026-10-04, see
docs/decisions/20261004-sdd-gates-deterministic.md): on a real 34-spec campaign
the LLM author cost $30.73 for 1/34 gate files, and an LLM in the gate-
authorship chain risks fidelity loss no structural verifier can detect. The
spec .md files are written under a lint that requires executable ```bash
fences; those fences — the spec AUTHOR's probes — are the gate source of
truth. Zero LLM calls here.

Resolution, per SpecCard acceptance id:
  cmd/contract kinds —
    1. a fence segment labeled for the AC (a ``# AC3`` / ``# AC1+AC2:`` marker
       line inside a fence; the fence's pre-marker prelude is prepended);
    2. else the spec's unlabeled fences joined (the spec-level verify script;
       the same probe may legitimately serve several ACs — the 1-probe-per-AC
       contract counts rows, and a spec-level script asserts all its ACs);
    3. else the AC lands in ``unprobeable`` (NO_EXECUTABLE_FENCE).
  ui kind —
    1. a labeled fence segment when the author wrote one;
    2. else a deterministic template built ONLY from the author's literal
       tokens: the first data-testid="…" and the first route literal found in
       the AC text (then the spec body). Template:
         agent-browser open "<route>" && agent-browser snapshot -i | grep -q '<testid>'
       A route starting with / is prefixed with ${SDD_FE_BASE}. No tokens →
       ``unprobeable`` (UI_TOKENS_MISSING).
Expect, per probe: the last success literal printed by the probe itself
(python ``print('… pass …')`` / shell ``echo "… pass …"``) regex-escaped, else
``exit 0`` (fences are self-asserting scripts; is_exit_only() admits it).
``precondition`` in the AC text or its marker line tags the probe.

Source drift guard: the spec file's sha256 must equal card.source_hash.
Verdict: one JSON line; pass iff every card yields a gates file whose ACs are
all probed and ``unprobeable`` is empty. Exit 0 pass / 1 fail / 2 malformed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    Malformed,
    atomic_write_json,
    deliverables_for,
    load_cards,
    run_dir,
    run_main,
    sha256_file,
)

_FENCE_RE = re.compile(r"```(?:bash|sh)\n(.*?)```", re.S)
_MARKER_RE = re.compile(r"^\s*#\s*(AC\d+(?:\s*[+,]\s*AC\d+)*)\b.*$", re.M)
_AC_ID_RE = re.compile(r"AC\d+")
_TESTID_RE = re.compile(r'data-testid="([^"]+)"')
_ROUTE_RE = re.compile(
    r'(?:\$SDD_FE_BASE|\$\{SDD_FE_BASE\}|https?://[^\s"\'`/]+)(/en/[^\s"\'`)\]]*)|(?<![\w/])(/en/[^\s"\'`)\]]*)')
_SUCCESS_LIT_RE = re.compile(r"""(?:print\(|echo\s+)["']([^"']*pass[^"']*)["']""", re.I)


def _fences(text: str) -> list[str]:
    return [f.strip("\n") for f in _FENCE_RE.findall(text)]


def _split_labeled(fence: str) -> tuple[dict[str, str], bool]:
    """AC id → labeled segment (prelude prepended). Second value: fence had markers."""
    markers = list(_MARKER_RE.finditer(fence))
    if not markers:
        return {}, False
    prelude = fence[: markers[0].start()].rstrip("\n")
    out: dict[str, str] = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(fence)
        segment = fence[m.start():end].rstrip("\n")
        body = (prelude + "\n" + segment).strip("\n") if prelude.strip() else segment
        for ac in _AC_ID_RE.findall(m.group(1)):
            out[ac] = body
    return out, True


def _ui_template(ac_text: str, spec_text: str) -> tuple[str, str] | None:
    source = ac_text + "\n" + spec_text
    testid = _TESTID_RE.search(ac_text) or _TESTID_RE.search(source)
    route_m = _ROUTE_RE.search(ac_text) or _ROUTE_RE.search(source)
    if not testid or not route_m:
        return None
    route = route_m.group(1) or route_m.group(2)
    probe = (f'agent-browser open "${{SDD_FE_BASE}}{route}" && '
             f'agent-browser wait \'[data-testid="{testid.group(1)}"]\'')
    return probe, "exit 0"


def _expect_for(probe: str) -> str:
    lits = _SUCCESS_LIT_RE.findall(probe)
    return re.escape(lits[-1]) if lits else "exit 0"


def _materialize(card: dict, spec_text: str) -> tuple[dict, list[str]]:
    sid = card["spec_id"]
    labeled: dict[str, str] = {}
    unlabeled: list[str] = []
    for fence in _fences(spec_text):
        segs, had = _split_labeled(fence)
        if had:
            labeled.update(segs)
        else:
            unlabeled.append(fence)
    spec_script = "\n".join(unlabeled).strip("\n")
    probes, unprobeable, problems = [], [], []
    for a in card["acceptance"]:
        aid, kind, text = a["id"], a["gate"]["kind"], a.get("text", "")
        tags = []
        if "precondition" in text.lower() or "precondition" in labeled.get(aid, "").lower().split("\n")[0]:
            tags.append("precondition")
        probe = labeled.get(aid)
        expect = None
        if probe is None:
            # Whole-spec script for ANY unlabeled AC: all fences joined under
            # set -euo pipefail. Fences share state (resolver vars defined in
            # one fence, used in another) and a plain join reports only the
            # LAST fence's exit code — both failed dispatch triage on the
            # first live campaign (run-sdd10x-202610040931 ASKs).
            whole = "\n".join(_fences(spec_text)).strip("\n")
            if whole:
                probe = whole if whole.lstrip().startswith("set -e") \
                    else "set -euo pipefail\n" + whole
            elif kind == "ui":
                # literal-token template ONLY for a fence-less spec, and only
                # onto an FE route — never a BE /api path
                tpl = _ui_template(text, spec_text)
                if tpl:
                    probe, expect = tpl
        if probe is None:
            reason = "UI_TOKENS_MISSING" if kind == "ui" else "NO_EXECUTABLE_FENCE"
            unprobeable.append({"acceptance_ref": aid, "reason": reason})
            problems.append(f"{sid}:{aid}: {reason}")
            continue
        probes.append({
            "gate_id": aid, "acceptance_ref": aid,
            "deliverable_refs": deliverables_for(card, aid),
            "kind": kind, "probe": probe,
            "expect": expect or _expect_for(probe),
            "tags": tags,
            "provenance": "spec-fence" if aid in labeled or kind != "ui" or expect is None
                          else "spec-literal-template",
        })
    gates = {"spec_id": sid, "source_hash": card["source_hash"],
             "probes": probes, "unprobeable": unprobeable,
             "provenance": "gates-materialize@deterministic"}
    return gates, problems


def main() -> tuple:
    rd = run_dir()
    cards = load_cards(rd)
    if not cards:
        raise Malformed("no spec cards")
    all_problems: list[str] = []
    specs: dict[str, dict] = {}
    written = 0
    for sid, card in sorted(cards.items()):
        src = Path(card["source_path"])
        if not src.is_file():
            all_problems.append(f"{sid}: source file missing: {src}")
            specs[sid] = {"ok": False, "violations": ["source file missing"]}
            continue
        if sha256_file(src) != card["source_hash"]:
            all_problems.append(f"{sid}: source drifted since compile")
            specs[sid] = {"ok": False, "violations": ["source drifted since compile"]}
            continue
        gates, problems = _materialize(card, src.read_text(encoding="utf-8"))
        atomic_write_json(rd / "gates" / f"{sid}.json", gates)
        written += 1
        all_problems.extend(problems)
        specs[sid] = {"ok": not problems, "violations": problems,
                      "probes": len(gates["probes"]), "unprobeable": len(gates["unprobeable"])}
    ok = not all_problems
    reason = (f"{written} gates file(s) materialized deterministically" if ok else
              f"{len(all_problems)} acceptance(s) unprobeable/drifted; first: {all_problems[0]}")
    return ok, reason, {"written": written, "specs": specs}


if __name__ == "__main__":
    raise SystemExit(run_main(main))
