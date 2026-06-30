#!/usr/bin/env bash
# profile_gate.sh — planner profile-gate normalization.
#
# Bug it fixes: the planner can emit profile_status=needs_answers while asking
# ZERO human_questions — i.e. it read the kickoff, decided it had everything,
# and asked nothing. The downstream gate (bin/mini-ork-plan) then runs the
# interactive / auto-answer branches, which have nothing to ask, fall through to
# "[skip] no answers provided", and BLOCK dispatch — even though the LLM
# reasoning succeeded. A profile with zero questions has nothing to answer, so
# "needs_answers" is unsatisfiable and wrong: normalize it to ready.
#
# Scope: this ONLY resolves the needs_answers + 0-questions contradiction. The
# independent confidence-floor gate (MINI_ORK_PLAN_CONFIDENCE_FLOOR) is untouched
# — a genuinely low-confidence plan still blocks. Defines a pure function with no
# top-level side effects, so it is safe to source early.

# mo_profile_normalize_zero_questions <profile_path>
# Echoes the (possibly corrected) profile_status. If status is needs_answers but
# human_questions is empty, rewrites the profile file to ready (persisting so the
# downstream execute gate + UI agree) and echoes "ready". Empty path / unreadable
# file → echoes nothing (caller keeps its current status).
mo_profile_normalize_zero_questions() {
  local profile_path="${1:-}"
  [ -n "$profile_path" ] && [ -f "$profile_path" ] || { printf ''; return 0; }
  python3 - "$profile_path" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
except Exception:
    print("")
    sys.exit(0)
status = str(p.get("profile_status") or "")
questions = p.get("human_questions") or []
if status == "needs_answers" and not questions:
    p["profile_status"] = "ready"
    p["human_questions"] = []
    p["profile_status_normalized"] = "needs_answers->ready (0 questions: nothing to answer)"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2)
            f.write("\n")
    except Exception:
        pass
    status = "ready"
print(status)
PY
}
