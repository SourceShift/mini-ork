#!/usr/bin/env python3
# verifiers/tier4-panel-quorum.py — fail-fast quorum gate for tier-4 panel.
#
# Python port of tier4-panel-quorum.sh (bash-removal WS8). Same rc semantics,
# evidence text, and JSON output.
#
# Counts tier4-{glm,kimi,codex,minimax}.md files that exist AND are
# non-empty (size > 100 bytes). Passes if count >= MO_TIER4_QUORUM
# (default 3 of 4). Emits JSON to stdout per the mini-ork verifier
# contract; exit code is always 0.
#
# Stops the recipe-level stall observed in run-1781333552-17796-identity-and-rbac
# and run-1781377882-65036-intervention-policies-gate where 2 of 4
# tier-4 lens reports were missing and tier4_synth hung forever.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory written by the native execute runtime
#   MO_TIER4_QUORUM  — minimum non-empty lens reports (default 3)
#
# Output: JSON to stdout with shape:
#   {"verifier":"tier4-panel-quorum","pass":true|false,"verdict":"pass|fail",
#    "evidence_path":"...","quorum_required":N,"quorum_met":M,
#    "present":["codex","minimax"],"missing":["kimi","glm"]}

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
QUORUM = int(os.environ.get("MO_TIER4_QUORUM", "3"))
MIN_SIZE_BYTES = int(os.environ.get("MO_TIER4_LENS_MIN_BYTES", "100"))
NAME = "tier4-panel-quorum"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")

LENSES = ("glm", "kimi", "codex", "minimax")
present = []
missing = []

with open(EVIDENCE, "w") as ev:
    for lens in LENSES:
        f = os.path.join(RUN_DIR, f"tier4-{lens}.md")
        if os.path.isfile(f):
            try:
                size = os.path.getsize(f)
            except OSError:
                size = 0
            if size > MIN_SIZE_BYTES:
                present.append(lens)
                ev.write(f"[present] tier4-{lens}.md size={size}\n")
                continue
            ev.write(f"[missing] tier4-{lens}.md exists but size={size} <= MIN ({MIN_SIZE_BYTES})\n")
        else:
            ev.write(f"[missing] tier4-{lens}.md does not exist\n")
        missing.append(lens)

met = len(present)
passed = met >= QUORUM
verdict = "pass" if passed else "fail"

print(json.dumps({
    "verifier": NAME,
    "pass": passed,
    "verdict": verdict,
    "evidence_path": EVIDENCE,
    "quorum_required": QUORUM,
    "quorum_met": met,
    "present": present,
    "missing": missing,
    "reasons": [] if passed else [f"tier-4 quorum {met}/{QUORUM} — missing lenses: {','.join(missing)}"],
}))
sys.exit(0)
