#!/usr/bin/env python3
# Property-based plus mutation testing on touched files. Slowest verifier below
# the LLM panel, so it only fires after tier1 and tier2 are green.
#
# Python port of tier3-property-mutation.sh (bash-removal WS8). Same rc
# semantics, evidence text, and JSON output (jq queries reimplemented with the
# json module).

import json
import os
import shlex
import shutil
import sys

# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH
# (`mini_ork/cli/execute.py`, _run_verifier_ref), but walk up to the engine root
# anyway so a hand-run or a caller that forgets it still imports.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)
from mini_ork.verify.pm import classify_suite_spawn, exec_argv, run_logged  # noqa: E402
from mini_ork.verify.langs import rust_property_targets  # noqa: E402

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

EVIDENCE = os.path.join(RUN_DIR, "tier3-evidence.log")
ev = open(EVIDENCE, "w")

try:
    summary = json.load(open(os.path.join(RUN_DIR, "implementer-summary.json"), encoding="utf-8"))
except Exception:
    summary = {}
READY = summary.get("ready_for_tier1", False)
_touched = summary.get("touched_files") or []
TOUCHED = "\n".join(_touched) if isinstance(_touched, list) else ""

if READY is not True or not TOUCHED.strip():
    ev.write("implementer-summary is not ready for tier3 or touched_files is empty\n")
    ev.close()
    print(json.dumps({
        "verifier": "tier3-property-mutation",
        "pass": False,
        "evidence_path": EVIDENCE,
        "reason": "no implementer artifact ready_for_tier1=true with non-empty touched_files",
        "property_rc": None,
        "mutation_rc": None,
        "checks_ran": [],
        "checks_skipped": ["property", "mutation"],
        "vacuous": True,
    }))
    sys.exit(0)

passed = True
# An rc belongs to a check that RAN. Neither check runs on most repos (no
# stryker config, no wired property run), and reporting `mutation_rc: 0` for a
# skip is indistinguishable from "mutation testing ran and passed" — which is
# how a vacuous pass reads to the tier-4 panel and to `_latest_evidence_json`'s
# `pass is True` gate in doc-to-features-loop. `None` means "not attempted";
# the top-level `vacuous` flag says whether ANY check actually executed.
# (kimi's hard-rule finding on run-1791821930-72687, 2026-10-12.)
property_rc = None
mutation_rc = None
mutation_argv = []  # set only on the branch that runs stryker
mutation_tail = ""
checks_ran = []
checks_skipped = []

pkg = ""
try:
    pkg = open("package.json", encoding="utf-8", errors="replace").read()
except OSError:
    pass

if os.path.isfile("node_modules/.bin/fast-check") or '"fast-check"' in pkg:
    ev.write(f"property tests not yet wired for this repo; touched files: {TOUCHED}\n")
    ev.flush()
    checks_skipped.append("property: fast-check present but no property run is wired")
else:
    checks_skipped.append("property: no fast-check")

# ── Rust property targets ──────────────────────────────────────────────────
# A Rust property suite is an integration test under `<crate>/tests/` named like
# one or using `proptest!`/`quickcheck!`. `cargo test --test <stem>` runs
# exactly that target in the crates this run touched — the Rust analogue of the
# TS property run above, and the first time tier3 has anything to execute on a
# repo without node.
rust_property_argv = []
rust_property_tail = ""
rust_targets = rust_property_targets(
    [p for p in TOUCHED.split() if p.endswith(".rs")]
) if TOUCHED.strip() else []
if rust_targets:
    if not shutil.which("cargo"):
        checks_skipped.append("property: Rust property target present but cargo is not on PATH")
        ev.write("rust property target found but cargo is not on PATH - skipping\n")
        ev.flush()
    else:
        for package, target in rust_targets:
            argv = ["cargo", "test", "--quiet", "-p", package, "--test", target]
            ev.write(f"$ {shlex.join(argv)}\n")
            ev.flush()
            rc, tail = run_logged(argv, EVIDENCE, stream=ev)
            checks_ran.append(f"property:{package}:{target}")
            rust_property_argv, rust_property_tail = argv, tail
            property_rc = rc
            if rc != 0:
                passed = False
                break
elif TOUCHED.strip():
    checks_skipped.append("property: no Rust property target in the touched crates")

if os.path.isfile("stryker.conf.json") or os.path.isfile("stryker.conf.js"):
    ev.write("running mutation tests; this may take 5-30 min\n")
    mutation_argv = exec_argv(".", "stryker", "run")
    ev.write(f"$ {shlex.join(mutation_argv)}\n")
    ev.flush()
    mutation_rc, mutation_tail = run_logged(mutation_argv, EVIDENCE, stream=ev)
    checks_ran.append("mutation")
    if mutation_rc != 0:
        passed = False
else:
    ev.write("no mutation tooling configured - skipping\n")
    ev.flush()
    checks_skipped.append("mutation: no stryker.conf.json/.js")

ev.close()
payload = {
    "verifier": "tier3-property-mutation",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "property_rc": property_rc,
    "mutation_rc": mutation_rc,
    "checks_ran": checks_ran,
    "checks_skipped": checks_skipped,
    "vacuous": not checks_ran,
}
# Stryker (or cargo) not installed (rc 127) or killed before it scored anything
# is not a verdict on the code — abstain rather than refute
# (mini_ork/verify/pm.py). A real failure in either check outranks an abstention.
if not passed:
    kinds = []
    for check_argv, rc, tail in ((mutation_argv, mutation_rc, mutation_tail),
                                 (rust_property_argv, property_rc, rust_property_tail)):
        if rc is None or rc == 0:
            continue
        kinds.append(classify_suite_spawn(check_argv, rc, tail) or "code")
    if "code" in kinds:
        payload["failure_kind"] = "code"
    elif kinds:
        payload["failure_kind"] = kinds[0]
print(json.dumps(payload))
sys.exit(0)
