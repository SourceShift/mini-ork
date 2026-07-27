#!/usr/bin/env python3
# verifiers/lens_outputs_complete.py — assert all 10 lens verdicts +
# synthesizer panel-verdict + publisher report were written.
#
# Python port of lens_outputs_complete.sh (bash-removal WS8). Same evidence
# text and rc semantics (jq queries reimplemented with the json module).
#
# Exit 0 = pass; non-zero = fail (per mini-ork verifier contract).
# Emits an evidence log at $MINI_ORK_VERIFIER_EVIDENCE for the
# reviewer + reflect cycle.

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.environ.get("MINI_ORK_VERIFIER_EVIDENCE") or \
    os.path.join(RUN_DIR, "verifier-lens-outputs-complete.log")

ev = open(EVIDENCE, "w")

missing = 0
for n in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
    f = os.path.join(RUN_DIR, f"lens-{n}-verdict.json")
    if os.path.isfile(f) and os.path.getsize(f) > 0:
        try:
            d = json.load(open(f, encoding="utf-8"))
            schema_ok = bool(d.get("verdict")) and d.get("score_0_to_10") is not None
        except Exception:
            d, schema_ok = {}, False
        if schema_ok:
            ev.write(f"ok lens-{n}-verdict.json (verdict={d.get('verdict')}, score={d.get('score_0_to_10')})\n")
        else:
            ev.write(f"fail lens-{n}-verdict.json present but schema-incomplete\n")
            missing += 1
    else:
        ev.write(f"fail lens-{n}-verdict.json missing or empty\n")
        missing += 1

# Synthesizer output
panel = os.path.join(RUN_DIR, "panel-verdict.json")
if os.path.isfile(panel) and os.path.getsize(panel) > 0:
    try:
        d = json.load(open(panel, encoding="utf-8"))
        schema_ok = bool(d.get("overall_verdict")) and d.get("weighted_score") is not None
    except Exception:
        d, schema_ok = {}, False
    if schema_ok:
        ev.write(f"ok panel-verdict.json (overall={d.get('overall_verdict')}, score={d.get('weighted_score')})\n")
    else:
        ev.write("fail panel-verdict.json present but schema-incomplete\n")
        missing += 1
else:
    ev.write("fail panel-verdict.json missing — synthesizer did not produce final verdict\n")
    missing += 1

# Publisher output (markdown report) — soft check; non-fatal
report = os.path.join(RUN_DIR, "chapter-validation-report.md")
if os.path.isfile(report) and os.path.getsize(report) > 0:
    n_lines = open(report, encoding="utf-8", errors="replace").read().count("\n")
    ev.write(f"ok chapter-validation-report.md present ({n_lines} lines)\n")
else:
    ev.write("warn chapter-validation-report.md missing — publisher did not produce human-readable report\n")

ev.write("\n")
ev.write(f"summary: {missing} required artifact(s) missing of 11 (10 lenses + 1 synthesizer)\n")
ev.close()

sys.exit(missing)
