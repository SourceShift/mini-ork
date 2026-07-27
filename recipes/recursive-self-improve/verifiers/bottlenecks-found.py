#!/usr/bin/env python3
# verifiers/bottlenecks-found.py — gate that the bottleneck scanner
# produced an actionable ranked list and the opus synthesizer ranked
# at least one patch.
#
# Python port of bottlenecks-found.sh (bash-removal WS8). Same rc semantics,
# evidence text, and JSON output.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by the native execute runtime)
#
# Output: JSON to stdout. Exit 0 always (caller reads .pass from JSON).

import json
import os
import re

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-bottlenecks-found.log")
ev = open(EVIDENCE, "w")

missing = []

# Dispatcher names: researcher node `bottleneck_lens` → lens-bottleneck.md;
# `arxiv_lens` → lens-arxiv.md (per the _lens-suffix heuristic in
# mini_ork/cli/execute.py).
SCAN = os.path.join(RUN_DIR, "lens-bottleneck.md")
SYNTH = os.path.join(RUN_DIR, "synthesis.md")
ARXIV = os.path.join(RUN_DIR, "lens-arxiv.md")
# Back-compat: also accept the older names so an in-flight loop pinned
# to a prior workflow.yaml does not have its verifier-result inverted.
if not os.path.isfile(SCAN) and os.path.isfile(os.path.join(RUN_DIR, "bottleneck-scan.md")):
    SCAN = os.path.join(RUN_DIR, "bottleneck-scan.md")
if not os.path.isfile(ARXIV) and os.path.isfile(os.path.join(RUN_DIR, "arxiv-refs.md")):
    ARXIV = os.path.join(RUN_DIR, "arxiv-refs.md")
if not os.path.isfile(ARXIV) and os.path.isfile(os.path.join(RUN_DIR, "arxiv-research.md")):
    ARXIV = os.path.join(RUN_DIR, "arxiv-research.md")

if not os.path.isfile(SCAN):
    missing.append("lens-bottleneck.md")
if not os.path.isfile(SYNTH):
    missing.append("synthesis.md")
# lens-arxiv.md is OPTIONAL. Iter 4 demonstrated the failure mode:
# Codex returned "Selected model is at capacity" for the arxiv lane after
# successfully running 6 arxiv-search-tool/search_papers MCP calls, so
# lens-arxiv.md was never written. The opus synth ran anyway (degraded-
# inputs branch) and produced a 5-patch ranking. Forcing arxiv to be
# mandatory blocks the loop on transient provider issues. The
# new-infra-requires-arxiv rule is enforced INSIDE the synth prompt, not
# at the verifier — synth drops infra patches to lower-ranked when
# arxiv-refs is absent, which is the correct contract boundary.
if not os.path.isfile(ARXIV):
    ev.write("lens-arxiv.md absent — accepting (synth handles degraded inputs)\n")

# Converged → pass with a soft signal so the outer runner terminates.
converged = 0
if os.path.isfile(SCAN) and re.search(r"^## Status: converged",
                                      open(SCAN, encoding="utf-8", errors="replace").read(),
                                      re.I | re.M):
    converged = 1
    ev.write("scanner reported convergence\n")

ranked_rows = 0
if os.path.isfile(SYNTH):
    # Count rows in the ranked patch table (lines starting with `| 1 ` … `| 5 `)
    synth_text = open(SYNTH, encoding="utf-8", errors="replace").read()
    ranked_rows = sum(1 for line in synth_text.splitlines() if re.match(r"^\| *[1-5] +\|", line))
    ev.write(f"synthesis ranked_rows={ranked_rows}\n")
ev.flush()


# Sanitize-then-check pattern. Codex agents (the executable-wrapper
# family used by codex_lens, arch_lens, arxiv_lens, bottleneck_lens
# planner-lane on planner=codex) reliably leak the `★ Insight ────`
# rule banner and `<z-insight>{...}</z-insight>` JSON envelope from
# their CLI runtime output into the Write tool's content because
# learning-mode framing is part of their emission contract. Iter 3
# rejected for this reason on 3 of 5 lenses; iter 2's own patch #3
# only addressed the verifier's narrow scope, not the source emission.
# Per arXiv 2604.01350 (Yang 2026, shared-state contamination) +
# 2605.16746 (Wang 2026, memory laundering): the durable fix is a
# post-write sanitizer that strips the framing before durable consumers
# see it, while preserving every byte of the agent's actual analysis.
def _sanitize_artifact(f):
    if not os.path.isfile(f):
        return
    with open(f, encoding="utf-8", errors="replace") as fh:
        src = fh.read()

    # Strip <z-insight>...</z-insight> blocks (greedy across lines).
    src2 = re.sub(r"<z-insight>.*?</z-insight>\s*", "", src, flags=re.DOTALL)
    # Strip "★ Insight ─────…" banner pairs: from a line starting with
    # ★ Insight ─ up to (and including) the next line of only ─ chars.
    src2 = re.sub(
        r"^★ Insight ─+\s*\n.*?^─+\s*\n",
        "",
        src2,
        flags=re.DOTALL | re.MULTILINE,
    )
    # Remaining single-line ★ Insight banners with no closer — drop them.
    src2 = re.sub(r"^★ Insight ─.*\n", "", src2, flags=re.MULTILINE)

    if src2 != src:
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(src2)
        ev.write(f"sanitized: {f}\n")
        ev.flush()


_polluted_remaining = []
for _polluted in (SCAN, SYNTH, ARXIV,
                  os.path.join(RUN_DIR, "lens-perf.md"),
                  os.path.join(RUN_DIR, "lens-correctness.md"),
                  os.path.join(RUN_DIR, "lens-arch.md")):
    if not os.path.isfile(_polluted):
        continue
    _sanitize_artifact(_polluted)
    # Anything still matching after sanitization is un-strippable corruption
    # (deeply embedded envelope, novel pattern) — those still reject.
    if re.search(r"^(★ Insight ─|<z-insight>)",
                 open(_polluted, encoding="utf-8", errors="replace").read(), re.M):
        _polluted_remaining.append(os.path.basename(_polluted))

if _polluted_remaining:
    missing.append(f"un-strippable envelope leak in: {' '.join(_polluted_remaining)}")

# Pass condition: either converged, or we have all 3 artifacts AND >=1 ranked patch
passed = 0
if converged == 1:
    passed = 1
elif not missing and ranked_rows >= 1:
    passed = 1

ev.close()
print(json.dumps({
    "verifier": "bottlenecks-found",
    "pass": passed == 1,
    "evidence_path": EVIDENCE,
    "ranked_patches": ranked_rows,
    "converged": converged == 1,
    "missing": missing,
}))
