#!/usr/bin/env python3
# Verifier for skill-smoke: proves the grant-isolation property end to end.
#
#   granted_lens  (skills: [skill-smoke-demo])  -> lens-granted.md MUST
#     contain the marker, which exists only inside the granted skill's
#     SKILL.md — the harness could only quote it if the executor really
#     materialized the skill into the node's checkout while it ran.
#   control_lens  (no skills:)                  -> lens-control.md MUST NOT
#     contain the marker: least privilege enforced by absence.
#
# Deterministic — no LLM cost. Emits JSON to stdout (consumed by
# mini_ork/cli/execute.py) + writes the canonical
# verifier-result-skills-isolation.json sidecar to the run dir.

import json
import os
import sys

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", ".")
MARKER = "SKILL-SMOKE-DEMO-MARKER-7F3A"
SKILL_NAME = "skill-smoke-demo"

reasons = []
checks = {}


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _lens_checks(stem, must_contain_marker):
    path = os.path.join(RUN_DIR, f"lens-{stem}.md")
    key_exists = f"{stem}_exists"
    if not os.path.isfile(path):
        reasons.append(f"lens-{stem}.md missing at {path}")
        checks[key_exists] = False
        return
    checks[key_exists] = True
    content = _read(path)
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines or not lines[0].lstrip().startswith("#"):
        reasons.append(f"lens-{stem}.md first non-blank line is not a markdown header")
        checks[f"{stem}_shape"] = False
    if len(lines) < 4:
        reasons.append(f"lens-{stem}.md has {len(lines)} non-blank lines (<4)")
        checks[f"{stem}_shape"] = False
    checks.setdefault(f"{stem}_shape", True)
    has_marker = MARKER in content
    if must_contain_marker:
        if not has_marker:
            reasons.append(
                f"lens-{stem}.md lacks the marker {MARKER} — the granted skill "
                "never reached the harness (materialization or grant failed)"
            )
        checks[f"{stem}_marker_present"] = has_marker
        if SKILL_NAME not in content:
            reasons.append(f"lens-{stem}.md does not name the granted skill {SKILL_NAME}")
        checks[f"{stem}_names_skill"] = SKILL_NAME in content
    else:
        if has_marker:
            reasons.append(
                f"lens-{stem}.md CONTAINS the marker — an ungranted node saw the "
                "skill: the isolation property is broken"
            )
        checks[f"{stem}_marker_absent"] = not has_marker


granted = _lens_checks("granted", must_contain_marker=True)
control = _lens_checks("control", must_contain_marker=False)

passed = not reasons
result = json.dumps({
    "verifier": "skills-isolation",
    "pass": passed,
    "reasons": reasons,
    "checks": checks,
})
print(result)
# Persist the sidecar for the obs UI's Why? panel to consume
try:
    with open(os.path.join(RUN_DIR, "verifier-result-skills-isolation.json"), "w") as f:
        f.write(result + "\n")
except OSError:
    pass
# Exit reflects the verdict so the executor's verifier gate sees failures.
sys.exit(0 if passed else 1)
