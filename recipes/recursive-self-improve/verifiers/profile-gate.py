#!/usr/bin/env python3
# verifiers/profile-gate.py — regression guard for planner profile readiness.
#
# Python port of profile-gate.sh (bash-removal WS8). Same fixtures, checks, rc
# semantics, and JSON output.
#
# Output: JSON to stdout. Exit 0 on pass, 1 on fail.

import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

WT = os.environ.get("MINI_ORK_SELF_IMPROVE_WORKTREE") or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
TMPROOT = tempfile.mkdtemp(prefix="mini-ork-profile-gate-")
atexit.register(shutil.rmtree, TMPROOT, True)

passed = 1
notes = []

os.makedirs(os.path.join(TMPROOT, "root", "lib"), exist_ok=True)
os.makedirs(os.path.join(TMPROOT, "home", "runs", "profile-gate-needs"), exist_ok=True)
os.makedirs(os.path.join(TMPROOT, "home", "runs", "profile-gate-ready"), exist_ok=True)
TEST_DB = os.path.join(TMPROOT, "home", "state.db")

con = sqlite3.connect(TEST_DB)
con.execute("CREATE TABLE node_runs (run_id TEXT, node_id TEXT, node_type TEXT, lane TEXT)")
con.commit()
con.close()

with open(os.path.join(TMPROOT, "root", "lib", "trace_store.sh"), "w") as f:
    f.write("trace_write() { return 0; }\n")

RUN_PYTHON_PLAN = '''
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from mini_ork.cli import plan as mini_ork_plan


def dispatch(_task_class, _node_type, _prompt):
    Path(os.environ["PROFILE_GATE_DISPATCH_MARKER"]).write_text("called\\n")
    con = sqlite3.connect(os.environ["MINI_ORK_DB"])
    con.execute(
        "INSERT INTO node_runs (run_id, node_id, node_type, lane) "
        "VALUES (?, 'planner', 'planner', 'codex')",
        (os.environ.get("MINI_ORK_RUN_ID", ""),),
    )
    con.commit()
    con.close()
    return 0, json.dumps({
        "objective": "ready profile dispatch fixture",
        "assumptions": [],
        "decomposition": [],
        "dependencies": [],
        "risk_notes": [],
        "artifact_contract": {"outputs": [], "success_verifiers": []},
        "verifier_contract": {
            "checks": [{"id": "ready", "description": "ready profile dispatch happened"}]
        },
    })


raise SystemExit(mini_ork_plan.main(sys.argv[2:], root=os.environ["MINI_ORK_ROOT"], dispatch=dispatch))
'''
with open(os.path.join(TMPROOT, "run-python-plan.py"), "w") as f:
    f.write(RUN_PYTHON_PLAN)

with open(os.path.join(TMPROOT, "root", "lib", "llm-dispatch.sh"), "w") as f:
    f.write('''llm_dispatch() {
  printf 'called\\n' >> "${PROFILE_GATE_DISPATCH_MARKER:?PROFILE_GATE_DISPATCH_MARKER required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB required}" "${MINI_ORK_RUN_ID:-}" <<'PY'
import sqlite3
import sys

db, run_id = sys.argv[1:3]
con = sqlite3.connect(db)
con.execute(
    "INSERT INTO node_runs (run_id, node_id, node_type, lane) VALUES (?, 'planner', 'planner', 'codex')",
    (run_id,),
)
con.commit()
con.close()
PY
  cat <<'JSON'
{
  "objective": "ready profile dispatch fixture",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": { "outputs": [], "success_verifiers": [] },
  "verifier_contract": { "checks": [{ "id": "ready", "description": "ready profile dispatch happened" }] }
}
JSON
}
''')

with open(os.path.join(TMPROOT, "kickoff.md"), "w") as f:
    f.write('''# Profile gate regression
## Definition of Done
- The planner skips dispatch while the run profile needs answers.
''')

READY_PROFILE = os.path.join(TMPROOT, "run_profile-ready.json")
with open(READY_PROFILE, "w") as f:
    f.write(json.dumps({
        "profile_status": "ready",
        "confidence": 0.9,
        "human_questions": [],
        "success_criteria": ["plan verifier_contract exists"],
    }, indent=2) + "\n")


def _node_row_count(where):
    con = sqlite3.connect(TEST_DB)
    row = con.execute(f"SELECT COUNT(*) FROM node_runs WHERE {where}").fetchone()
    con.close()
    return row[0]


NEEDS_OUT = os.path.join(TMPROOT, "home", "runs", "profile-gate-needs", "plan.json")
NEEDS_MARKER = os.path.join(TMPROOT, "needs-dispatch.marker")
needs_env = {
    **os.environ,
    "MINI_ORK_ROOT": os.path.join(TMPROOT, "root"),
    "MINI_ORK_HOME": os.path.join(TMPROOT, "home"),
    "MINI_ORK_DB": TEST_DB,
    "MINI_ORK_RUN_ID": "profile-gate-needs",
    "MINI_ORK_PROFILE_GATE": "1",
    "MINI_ORK_NONINTERACTIVE": "1",
    "MO_AUTO_ANSWER_PROFILE": "0",
    "MINI_ORK_PROFILE_PATH": os.path.join(WT, "tests", "fixtures", "run_profile-needs-answers.json"),
    "PROFILE_GATE_DISPATCH_MARKER": NEEDS_MARKER,
}
with open(os.path.join(TMPROOT, "needs.err"), "w") as err_f:
    needs = subprocess.run(
        [sys.executable, os.path.join(TMPROOT, "run-python-plan.py"), WT,
         "--out", NEEDS_OUT, os.path.join(TMPROOT, "kickoff.md")],
        env=needs_env, stdout=subprocess.PIPE, stderr=err_f)
needs_rc = needs.returncode
NEEDS_STDOUT = needs.stdout.decode("utf-8", "replace")

if needs_rc != 0:
    passed = 0
    notes.append(f"needs_answers invocation exited {needs_rc}")

if os.path.isfile(NEEDS_MARKER) and os.path.getsize(NEEDS_MARKER) > 0:
    passed = 0
    notes.append("needs_answers profile called llm_dispatch")

needs_node_rows = _node_row_count(
    "run_id='profile-gate-needs' AND node_type='planner' AND lane IN ('opus','sonnet','codex','haiku','anthropic')")
if needs_node_rows != 0:
    passed = 0
    notes.append("needs_answers profile wrote planner node_runs rows")


def _needs_plan_blocked():
    with open(NEEDS_OUT, encoding="utf-8") as f:
        plan = json.load(f)
    assert plan["plan_status"] == "needs_answers"
    assert plan["blocked_by"] == "run_profile"
    assert plan["decomposition"] == []
    assert plan["verifier_contract"]["checks"]


try:
    _needs_plan_blocked()
except Exception:
    passed = 0
    notes.append("needs_answers plan.json missing blocked shape")

if '"plan_status":"needs_answers"' not in NEEDS_STDOUT:
    passed = 0
    notes.append("needs_answers stdout missing plan_status marker")

READY_OUT = os.path.join(TMPROOT, "home", "runs", "profile-gate-ready", "plan.json")
READY_MARKER = os.path.join(TMPROOT, "ready-dispatch.marker")
ready_env = {
    **os.environ,
    "MINI_ORK_ROOT": os.path.join(TMPROOT, "root"),
    "MINI_ORK_HOME": os.path.join(TMPROOT, "home"),
    "MINI_ORK_DB": TEST_DB,
    "MINI_ORK_RUN_ID": "profile-gate-ready",
    "MINI_ORK_PROFILE_GATE": "1",
    "MINI_ORK_PROFILE_PATH": READY_PROFILE,
    "PROFILE_GATE_DISPATCH_MARKER": READY_MARKER,
}
with open(os.path.join(TMPROOT, "ready.err"), "w") as err_f:
    ready_rc = subprocess.run(
        [sys.executable, os.path.join(TMPROOT, "run-python-plan.py"), WT,
         "--out", READY_OUT, os.path.join(TMPROOT, "kickoff.md")],
        env=ready_env, stdout=subprocess.DEVNULL, stderr=err_f).returncode

if ready_rc != 0:
    passed = 0
    notes.append(f"ready invocation exited {ready_rc}")

if not (os.path.isfile(READY_MARKER) and os.path.getsize(READY_MARKER) > 0):
    passed = 0
    notes.append("ready profile did not call llm_dispatch")

ready_node_rows = _node_row_count(
    "run_id='profile-gate-ready' AND node_type='planner' AND lane='codex'")
if ready_node_rows < 1:
    passed = 0
    notes.append("ready profile did not write planner node_runs dispatch row")

print(json.dumps({
    "verifier": "profile-gate",
    "pass": passed == 1,
    "needs_answers_plan": NEEDS_OUT,
    "ready_plan": READY_OUT,
    "node_runs_db": TEST_DB,
    "needs_stderr": os.path.join(TMPROOT, "needs.err"),
    "ready_stderr": os.path.join(TMPROOT, "ready.err"),
    "notes": notes,
}))

sys.exit(0 if passed == 1 else 1)
