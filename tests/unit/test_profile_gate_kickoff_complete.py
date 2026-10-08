"""C8 — a kickoff that declares its verification command is not interrogated.

The profile gate's auto-answer path spends one LLM call per node (every
goal-loop child re-plans), and when that call fails the gate BLOCKS a kickoff
that never needed an answer. A kickoff that states its own verification command
already answers the three standard run_profile questions (success criteria,
scope, how you prove it), so the interrogation is skipped and the profile is
readied — with the questions deferred, not dropped.

Escalation is unchanged for a kickoff that does NOT declare a verification
command: `MO_PROFILE_REQUIRE_ANSWERS=1` restores the LLM interrogation, and a
genuine gap still ends `plan_status=needs_answers`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from mini_ork.cli import plan
from mini_ork.gates import profile_gate

REPO = Path(__file__).resolve().parents[2]

_VALID = {
    "objective": "Ship widget", "assumptions": ["a"],
    "decomposition": [{"id": "s1", "description": "do", "node_type": "implementer",
                       "depends_on": []}],
    "dependencies": [], "risk_notes": [],
    "artifact_contract": {"outputs": ["x"], "success_verifiers": ["v"]},
    "verifier_contract": {"checks": [{"id": "c1", "description": "check it"}]},
}


# ── 1. the kickoff detector ─────────────────────────────────────────────────


def test_heading_forms_declare_a_verification_command():
    for text in (
        "## Verification commands\n\n- `pytest tests/`\n",
        "### Verification Command\n\n`make test`\n",
        "**Verification Command:** `pnpm test`\n",
        "# Proof of success\n\n- all gates green\n",
        "## How we will be verified\n\n- the CI job\n",
    ):
        assert profile_gate.declares_verification_command(text), text


def test_prose_and_empty_do_not_declare():
    for text in (
        "",
        "# Do the thing\n\n## Success\n- works\n",
        # mentioning a runner in prose is not a declaration
        "We sometimes run pytest while developing.\n",
        "# Verification\n\nSee the docs.\n",
    ):
        assert not profile_gate.declares_verification_command(text), text


# ── 2. the profile normalisation ────────────────────────────────────────────


def _write_profile(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "prof.json"
    p.write_text(json.dumps(payload))
    return p


def test_normalize_readies_and_defers_the_questions(tmp_path):
    p = _write_profile(tmp_path, {
        "profile_status": "needs_answers", "confidence": 0.4,
        "human_questions": ["Which module?"], "recipe": "code-fix",
        "deferred_questions": ["earlier question"],
    })

    assert profile_gate.normalize_kickoff_complete(str(p)) == "ready"

    after = json.loads(p.read_text())
    assert after["profile_status"] == "ready"
    assert after["human_questions"] == []
    # nothing is dropped — the deferred list carries both
    assert after["deferred_questions"] == ["earlier question", "Which module?"]
    assert after["confidence"] >= 0.9          # clears the gate's confidence floor
    assert "verification command" in after["profile_status_normalized"]
    assert after["recipe"] == "code-fix"       # every other key preserved


def test_normalize_is_a_noop_for_a_ready_profile(tmp_path):
    p = _write_profile(tmp_path, {"profile_status": "ready", "human_questions": []})

    assert profile_gate.normalize_kickoff_complete(str(p)) == "ready"
    assert json.loads(p.read_text()) == {"profile_status": "ready", "human_questions": []}


def test_normalize_tolerates_missing_and_malformed(tmp_path):
    assert profile_gate.normalize_kickoff_complete("") == ""
    assert profile_gate.normalize_kickoff_complete(str(tmp_path / "nope.json")) == ""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert profile_gate.normalize_kickoff_complete(str(bad)) == ""
    assert bad.read_text() == "{not json"      # untouched


# ── 3. through the real planner (production entrypoint) ─────────────────────

_VERIFY_KICKOFF = (
    "# Do the thing\n\n## Success\n- works\n\n"
    "## Verification commands\n\n- `pytest tests/`\n"
)


def _home(tmp: Path, name: str) -> tuple[str, str]:
    h = tmp / name / ".mini-ork"
    h.mkdir(parents=True)
    db = str(h / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(h), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return str(h), db


def _run_plan(tmp: Path, name: str, kickoff_text: str, profile: dict,
              extra: dict | None = None) -> tuple[int, list, Path]:
    home, db = _home(tmp, name)
    k = tmp / (name + "-kick.md")
    k.write_text(kickoff_text)
    prof = tmp / (name + "-prof.json")
    prof.write_text(json.dumps(profile))
    out = str(tmp / (name + "-plan.json"))
    calls: list = []

    def dispatch(_task_class, node_type, _prompt):
        calls.append(node_type)
        if node_type == "profile_answerer":
            return 0, json.dumps({"answers": [], "auto_answered": True})
        return 0, json.dumps(_VALID)

    env = {
        "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": home, "MINI_ORK_DB": db,
        "MINI_ORK_TASK_CLASS": "code_fix", "MO_INJECT_LEARNINGS": "0",
        "MINI_ORK_PROFILE_GATE": "1", "MINI_ORK_PROFILE_PATH": str(prof),
        "MINI_ORK_NONINTERACTIVE": "1", "MO_AUTO_ANSWER_PROFILE": "1",
        "MINI_ORK_RUN_ID": "run-c8", "MO_LEVEL_VECTOR": "0",
    }
    env.update(extra or {})
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        rc = plan.main([str(k), "--out", out], root=str(REPO), dispatch=dispatch)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return rc, calls, prof


def test_a_self_verifying_kickoff_skips_the_profile_llm_call(tmp_path):
    """The kickoff declares its proof → no profile_answerer dispatch at all."""
    rc, calls, prof = _run_plan(
        tmp_path, "selfverifying", _VERIFY_KICKOFF,
        {"profile_status": "needs_answers", "confidence": 0.4,
         "human_questions": ["What command proves success?"]},
    )

    assert rc == 0, rc
    assert "profile_answerer" not in calls, calls
    assert "planner" in calls, calls
    after = json.loads(prof.read_text())
    assert after["profile_status"] == "ready"
    assert after["deferred_questions"] == ["What command proves success?"]


def test_a_kickoff_without_a_declared_proof_still_interrogates(tmp_path):
    """Control: the escalation path is untouched for a genuinely open kickoff."""
    rc, calls, _ = _run_plan(
        tmp_path, "openkick", "# Do the thing\n\n## Success\n- works\n",
        {"profile_status": "needs_answers", "confidence": 0.4,
         "human_questions": ["What command proves success?"]},
    )

    assert rc == 0, rc
    assert "profile_answerer" in calls, calls


def test_the_require_answers_knob_restores_the_interrogation(tmp_path):
    rc, calls, _ = _run_plan(
        tmp_path, "knob", _VERIFY_KICKOFF,
        {"profile_status": "needs_answers", "confidence": 0.4,
         "human_questions": ["What command proves success?"]},
        extra={"MO_PROFILE_REQUIRE_ANSWERS": "1"},
    )

    assert rc == 0, rc
    assert "profile_answerer" in calls, calls
