#!/usr/bin/env python3
# verifiers/link-verifier.py — relative-link integrity for `docs` recipe.
#
# Python port of link-verifier.sh (bash-removal WS8). Same status text, JSON
# summary, and rc semantics (jq queries reimplemented with the json module).
#
# Walks every doc file named in the plan's verifier_contract.checks[] where
# `kind == "link_integrity"`, extracts every `[label](path)` markdown link,
# and confirms each relative path resolves to a real file/directory.
# External URLs (http/https/mailto), anchors (#section), and reserved
# autolinks (e.g. <https://...>) are skipped.
#
# rc=0 when ALL relative links resolve. rc=1 on ANY broken link.
#
# Env (same as grep-assert.py):
#   MINI_ORK_PLAN_PATH    path to the plan JSON
#   MINI_ORK_HOME         project home
#   MINI_ORK_RUN_ID       current run id

import json
import os
import re
import sys

MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
PLAN_PATH = os.environ.get("MINI_ORK_PLAN_PATH") or \
    os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID, "plan.json")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_link.log")

_log = open(LOG_PATH, "a")


def emit(line):
    """tee -a LOG_PATH"""
    print(line)
    _log.write(line + "\n")
    _log.flush()


if not os.path.isfile(PLAN_PATH):
    print(json.dumps({"verifier": "link", "status": "skipped",
                      "reason": f"plan not found: {PLAN_PATH}"}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)

# Collect doc files named for link checking; dedupe.
try:
    plan = json.load(open(PLAN_PATH, encoding="utf-8"))
except Exception:
    plan = {}
checks = ((plan.get("verifier_contract") or {}).get("checks") or [])
docs = sorted({c.get("file") for c in checks
               if isinstance(c, dict) and c.get("kind") == "link_integrity" and c.get("file")})

if not docs:
    emit(json.dumps({"verifier": "link", "status": "skipped",
                     "reason": "no link_integrity assertions in plan"}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)

n_total = 0
n_broken = 0
broken_details = []

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")

for doc in docs:
    if not os.path.isfile(doc):
        n_broken += 1
        broken_details.append(f"doc file itself missing: {doc}")
        emit(f"  [FAIL] doc not found: {doc}")
        continue

    doc_dir = os.path.dirname(doc)
    # Extract every [text](url) form.
    text = open(doc, encoding="utf-8", errors="replace").read()
    links = [m.group(2) for m in LINK_RE.finditer(text)]

    for link in links:
        n_total += 1
        if link.startswith(("http://", "https://", "mailto:", "tel:", "ftp://", "ftps://", "sftp://")):
            continue
        if link.startswith("#"):
            continue
        # Strip fragment / query if present
        path_only = link.split("#")[0].split("?")[0]
        if not path_only:
            continue
        # Resolve relative to the doc's directory
        if path_only.startswith("/"):
            resolved = path_only
        else:
            resolved = os.path.join(doc_dir, path_only)
        if not os.path.exists(resolved):
            n_broken += 1
            broken_details.append(f"in {doc}: {link} → {resolved} (not found)")
            emit(f"  [FAIL] broken link: {doc} → {link}")

if n_broken == 0:
    emit(json.dumps({"verifier": "link", "status": "pass", "docs": len(docs),
                     "links_checked": n_total, "broken": 0}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)
else:
    emit(json.dumps({"verifier": "link", "status": "fail", "docs": len(docs),
                     "links_checked": n_total, "broken": n_broken,
                     "failures": broken_details}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(1)
