"""``mini_ork.recovery.diagnoser_llm`` — the evidence-grounded model diagnoser.

Hermetic: every case builds a temp home + temp sqlite and injects ``dispatch``,
so no test touches the network or a real run directory. Fixture scaffolding
mirrors ``tests/unit/test_diagnose.py`` / ``tests/unit/test_auto_repair.py``.

The rules under test are the kickoff's, and they are all one rule seen from
different sides: **a model diagnosis is admissible only when it is grounded in
the run's own evidence.** Everything else — an ungrounded excerpt, an out-of-enum
field, a lane that would be diagnosing its own work, a broken or non-JSON reply —
degrades to ``None`` so the rule set's honest ``unclassified`` stands.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from mini_ork.dispatch.models import DispatchResult
from mini_ork.recovery import auto_repair as ar
from mini_ork.recovery import diagnose as dx
from mini_ork.recovery import diagnoser_llm as dl
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

RUN = "run-1792000000-d1a900"
T0 = 1_792_000_000

# A note that (a) the rule set deliberately leaves ``unclassified`` (see
# ``diagnose``'s note vocabulary) and (b) is long enough to be quoted verbatim.
NOTES = "lifecycle exited without a verdict; the implementer never reported back"

# Two lanes on the same ``gateway`` family with DIFFERENT models (the default
# configuration: the diagnoser lane ``glm`` vs the implementer lane ``minimax``),
# plus a pair that resolves to the SAME model — the self-diagnosis case.
_PROVIDERS = """
providers:
  glm:
    kind: anthropic-compat
    family: gateway
    model: GLM-5.3
    base_url: https://example.invalid/anthropic
    api_key_env: GLM_API_KEY
  minimax:
    kind: anthropic-compat
    family: gateway
    model: MiniMax-M3
    base_url: https://example.invalid/anthropic
    api_key_env: MINIMAX_API_KEY
  twin_a:
    kind: anthropic-compat
    family: gateway
    model: TWIN-MODEL-1
    base_url: https://example.invalid/anthropic
    api_key_env: TWIN_KEY
  twin_b:
    kind: anthropic-compat
    family: gateway
    model: TWIN-MODEL-1
    base_url: https://example.invalid/anthropic
    api_key_env: TWIN_KEY
"""

_LANES = {"implementer": "minimax", "verifier": "glm", "planner": "glm"}


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Pin the loop's env so nothing leaks in from the caller.

    ``MO_AUTO_REPAIR`` is ``"0"`` suite-wide (``tests/conftest.py``) — the loop's
    own tests opt back in. ``MO_MODEL_DIAGNOSER`` defaults ON, so only the test
    that exercises the off switch sets it. An ambient ``MINI_ORK_PROVIDERS``
    would outrank the temp home's registry, so it is dropped.
    """
    monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)
    monkeypatch.delenv("MO_MODEL_DIAGNOSER", raising=False)
    monkeypatch.delenv("MO_DIAGNOSER_LANE", raising=False)
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    monkeypatch.delenv("MO_DIAGNOSER_TIMEOUT_S", raising=False)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    # ``providers._load_providers_registry`` resolves $MINI_ORK_HOME, so pinning
    # it here is what makes the temp registry — not the repo's — the source of
    # lane→model truth, exactly as a real run resolves it.
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    (h / "config" / "providers.yaml").write_text(_PROVIDERS, encoding="utf-8")
    return h


def _seed(home: Path, *, run_id: str = RUN, status: str = "failed",
          notes: str = NOTES, lanes: dict[str, str] | None = None) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps({
        "recipe": "framework-edit",
        "provider_policy": {"lanes": dict(lanes if lanes is not None else _LANES)},
    }))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", status, 0.5, T0, T0 + 100, T0 + 80,
         "framework_edit", "", "latest", notes or None),
    )
    con.commit()
    con.close()
    return run_dir


def _event(home: Path, run_id: str, seq: int, event_type: str, payload: dict[str, Any]) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-{run_id}-{seq}", run_id, event_type, json.dumps(payload), T0 + seq),
    )
    con.commit()
    con.close()


def _rule_diag(home: Path, run_id: str = RUN) -> dict[str, Any]:
    """The rule diagnosis for a seeded run — the unclassified fall-through."""
    result = dx.diagnose(home, run_id, write=False)
    assert result["layer"] == "unclassified", result
    return result


def _pack(home: Path, run_id: str = RUN) -> str:
    return dl.build_evidence_pack(home, run_id, {"layer": "unclassified"})


def _excerpt(home: Path, snippet: str, run_id: str = RUN) -> str:
    """A verbatim, long-enough excerpt taken from the real pack."""
    pack = _pack(home, run_id)
    index = pack.find(snippet)
    assert index != -1, f"{snippet!r} is not in the pack"
    return pack[index:index + max(len(snippet), 30)]


def _reply_with(excerpt: str, **over: Any) -> dict[str, Any]:
    """A scripted reply citing ``excerpt`` — the hermetic stand-in for a model."""
    obj = {
        "layer": "engine",
        "stage": "execute",
        "node": "implementer",
        "routes_to": "fix_epic",
        "confidence": 0.9,
        "summary": "the run stopped without a verdict",
        "evidence": [{"kind": "note", "ref": "task_runs.notes", "excerpt": excerpt}],
    }
    obj.update(over)
    return obj


def _cite_from_prompt(prompt: str) -> str:
    """A verbatim citation lifted out of the RUN NOTES body inside ``prompt``."""
    pack = prompt.split("=== EVIDENCE PACK ===", 1)[1]
    notes = pack.split("=== RUN NOTES ===\n", 1)[1]
    line = notes.splitlines()[0].strip()
    assert len(line) >= 20, "the prompt carried no citable evidence line"
    return line[:120]


class _Dispatch:
    """An injected ``dispatch`` — records calls, returns a scripted reply."""

    def __init__(self, reply: Any = None, *, ok: bool = True, error: str = "",
                 cost_usd: float = 0.02, raise_exc: BaseException | None = None):
        self.calls: list[Any] = []
        self._reply = reply
        self._ok = ok
        self._error = error
        self._cost = cost_usd
        self._raise = raise_exc

    def __call__(self, request: Any) -> DispatchResult:
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
        text = self._reply
        if callable(text):
            text = text(request)
        if not isinstance(text, str):
            text = json.dumps(text)
        return DispatchResult(ok=self._ok, rc=0 if self._ok else 2, text=text,
                              error=self._error, model=request.model,
                              cost_usd=self._cost)


# ── the happy path: a grounded reply becomes an M1 model diagnosis ─────────


def test_grounded_reply_becomes_a_model_diagnosis(home: Path) -> None:
    _seed(home)
    excerpt = _excerpt(home, NOTES)
    stub = _Dispatch(_reply_with(excerpt, layer="provider", stage="execute",
                                 routes_to="retry", confidence=0.7))

    result = dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is not None
    assert result["source"] == "model"
    assert result["rule_id"] == "M1"
    assert result["layer"] == "provider"        # the reply's layer, not the rule's
    assert result["stage"] == "execute"
    assert result["routes_to"] == "retry"
    assert result["confidence"] == 0.7
    assert result["model_lane"] == "glm"
    assert result["cost_usd"] == pytest.approx(0.02)
    assert result["version"] == dx.DIAGNOSIS_VERSION
    assert result["run_id"] == RUN
    assert len(stub.calls) == 1
    assert stub.calls[0].model == "glm"
    assert excerpt in stub.calls[0].prompt
    for item in result["evidence"]:
        assert set(item) == {"kind", "ref", "excerpt"}
        assert item["excerpt"] in stub.calls[0].prompt


def test_the_dispatch_is_bounded_and_lands_in_the_ledger(home: Path, monkeypatch) -> None:
    """``decide`` runs this inline: a short timeout, few turns, and a ledger row."""
    _seed(home)
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)), cost_usd=0.03)

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is not None
    request = stub.calls[0]
    assert request.timeout_s == dl.DEFAULT_TIMEOUT_S
    assert request.max_turns <= 3
    # Read-only, no MCP — never the implementer default (Write/Edit) — from an
    # empty scratch dir that is gone once the call returns.
    assert request.env["MO_RESOLVED_NODE_TOOLS"] == "Read|"
    assert request.env["MO_RESUME_SESSION_ID"] == ""
    assert request.cwd and not Path(request.cwd).exists()
    assert not Path(request.cwd).is_relative_to(home)

    monkeypatch.setenv("MO_DIAGNOSER_TIMEOUT_S", "45")
    dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)
    assert stub.calls[1].timeout_s == 45.0

    con = sqlite3.connect(home / "state.db")
    rows = con.execute("SELECT run_id, cost_usd FROM llm_calls "
                       "WHERE feature_name = 'model_diagnoser'").fetchall()
    con.close()
    assert rows and all(r == (RUN, pytest.approx(0.03)) for r in rows)


# ── the grounding rule: an excerpt that is not in the pack is not evidence ──


def test_excerpt_absent_from_the_pack_is_rejected(home: Path) -> None:
    _seed(home)
    stub = _Dispatch(_reply_with("this sentence appears nowhere in the evidence pack at all"))

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None


@pytest.mark.parametrize("framing", [
    "=== RUN EVENTS (last 40) ===",                     # a section header
    "no rule could cite evidence for this failure",     # the rule set's own summary
    f"run_id={RUN}",                                    # the RUN framing section
])
def test_citing_the_packs_framing_is_not_evidence(home: Path, framing: str) -> None:
    """Headers and the rule verdict are in the pack, but they are not the run's evidence."""
    _seed(home)
    _event(home, RUN, 1, "run_started", {"recipe": "framework-edit"})
    rule_diag = _rule_diag(home)
    assert framing in dl.build_evidence_pack(home, RUN, rule_diag)
    stub = _Dispatch(_reply_with(framing))

    result, note = dl.diagnose_with_model_verbose(home, RUN, rule_diag, dispatch=stub)

    assert result is None
    assert "not verbatim" in note
    assert len(stub.calls) == 1


def test_a_short_excerpt_is_not_a_citation(home: Path) -> None:
    """``timeout`` is a token, not a citation — it is trivially guessed."""
    _seed(home)
    stub = _Dispatch(_reply_with("timeout"))

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None


def test_ungrounded_reply_keeps_the_rule_result(home: Path, monkeypatch) -> None:
    """``diagnose(allow_model=True)`` keeps ``unclassified`` when the model cannot ground."""
    _seed(home)
    stub = _Dispatch(_reply_with("a confident claim the pack does not contain"))
    _spy_dispatch(monkeypatch, stub)

    result = dx.diagnose(home, RUN, write=False, allow_model=True)

    assert len(stub.calls) == 1
    assert result["layer"] == "unclassified"
    assert result["rule_id"] == dx.RULE_UNCLASSIFIED
    assert result["source"] == ""


# ── validation: any malformed reply is None ────────────────────────────────


@pytest.mark.parametrize("over", [
    {"layer": "banana"},                            # outside the vocabulary
    {"layer": "unclassified"},                      # making no diagnosis is not one
    {"stage": "deploy"},                            # outside the stage enum
    {"routes_to": "merge_it"},                      # outside the routes enum
    {"confidence": 1.4},                            # outside [0, 1]
    {"confidence": "high"},                         # not a number
    {"confidence": True},                           # a bool is not a belief
    {"evidence": []},                               # no evidence at all
    {"evidence": [{"kind": "note", "ref": "x"}]},   # no excerpt
])
def test_malformed_replies_are_rejected(home: Path, over: dict[str, Any]) -> None:
    _seed(home)
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES), **over))

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None


@pytest.mark.parametrize("text", ["not json at all", "", "[]", "{broken"])
def test_non_json_replies_are_rejected(home: Path, text: str) -> None:
    _seed(home)
    stub = _Dispatch(text)

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None


def test_a_fenced_reply_is_still_parsed(home: Path) -> None:
    """A model that wraps its JSON in a fence (or prose) is still readable."""
    _seed(home)
    body = json.dumps(_reply_with(_excerpt(home, NOTES)))
    stub = _Dispatch(f"Here is my answer:\n```json\n{body}\n```\n")

    result = dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is not None and result["source"] == "model"


def test_a_failed_dispatch_is_none_not_an_exception(home: Path) -> None:
    _seed(home)
    assert dl.diagnose_with_model(
        home, RUN, _rule_diag(home),
        dispatch=_Dispatch("", ok=False, error="lane preflight failed")) is None
    assert dl.diagnose_with_model(
        home, RUN, _rule_diag(home),
        dispatch=_Dispatch(raise_exc=RuntimeError("boom"))) is None


# ── a classified run is never dispatched to a model ────────────────────────


def test_rule_classified_run_never_dispatches(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "publish-abstain.json").write_text(json.dumps({
        "reason": "authored patch did not apply",
        "detail": "the implementer's patch no longer applies to the target tree",
    }))
    (run_dir / "verdict.json").write_text(json.dumps({
        "verdict": "pass",
        "levels": {"applies": "PROVEN", "executes": "PROVEN", "target": "PROVEN",
                   "preserve": "PROVEN", "contract": "PROVEN"},
    }))
    classified = dx.diagnose(home, RUN, write=False)
    assert classified["layer"] != "unclassified", classified
    stub = _Dispatch({"layer": "engine"})

    assert dl.diagnose_with_model(home, RUN, classified, dispatch=stub) is None
    assert stub.calls == []


# ── the self-diagnosis guard: a model never diagnoses its own work ─────────


def test_lane_resolving_to_the_implementers_model_is_refused(home: Path, monkeypatch) -> None:
    """Same MODEL (a different lane name), same ``gateway`` family → no dispatch."""
    _seed(home, lanes={"implementer": "twin_a", "verifier": "twin_b"})
    stub = _Dispatch(_reply_with("whatever the pack says"))
    monkeypatch.setenv("MO_DIAGNOSER_LANE", "twin_b")

    result, note = dl.diagnose_with_model_verbose(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is None
    assert "own work" in note
    assert stub.calls == []


def test_a_different_model_on_the_same_family_does_dispatch(home: Path) -> None:
    """``glm`` (GLM-5.3) vs ``minimax`` (MiniMax-M3) — both family ``gateway``.

    Comparing the *family* instead of the model would refuse this — the default
    configuration — and silently disable the diagnoser, which is why the guard
    reads the provider entry's ``model``.
    """
    _seed(home)
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))

    result = dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is not None and result["model_lane"] == "glm"
    assert len(stub.calls) == 1


def test_a_lane_pointing_at_the_implementer_lane_is_refused(home: Path, monkeypatch) -> None:
    _seed(home)
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))
    monkeypatch.setenv("MO_DIAGNOSER_LANE", "minimax")   # the implementer's lane

    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None
    assert stub.calls == []


def test_an_unknown_implementer_lane_is_refused(home: Path) -> None:
    """No ``provider_policy.lanes.implementer`` → independence cannot be shown."""
    run_dir = _seed(home)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "framework-edit"}))
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))

    result, note = dl.diagnose_with_model_verbose(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is None
    assert "implementer lane is unknown" in note
    assert stub.calls == []


def test_a_role_named_lane_resolves_through_the_runs_lanes(home: Path, monkeypatch) -> None:
    """``MO_DIAGNOSER_LANE=reviewer`` dispatches the entry the role names."""
    _seed(home, lanes={"implementer": "minimax", "reviewer": "glm", "worker": "minimax"})
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))
    monkeypatch.setenv("MO_DIAGNOSER_LANE", "reviewer")

    result = dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is not None and result["model_lane"] == "reviewer"
    assert stub.calls[0].model == "glm"

    monkeypatch.setenv("MO_DIAGNOSER_LANE", "worker")   # the implementer's entry
    assert dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub) is None
    assert len(stub.calls) == 1


def test_without_a_registry_lane_names_stand_in_for_models(home: Path, monkeypatch) -> None:
    """No registry → the lane name is the model, and distinct names still dispatch."""
    _seed(home)
    monkeypatch.setattr(dl, "_registry", lambda: {})
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))

    result = dl.diagnose_with_model(home, RUN, _rule_diag(home), dispatch=stub)

    assert result is not None
    assert stub.calls[0].model == "glm"


# ── at most once per run ───────────────────────────────────────────────────


def test_second_diagnose_reuses_the_persisted_model_diagnosis(home: Path, monkeypatch) -> None:
    _seed(home)
    stub = _Dispatch(_reply_with(_excerpt(home, NOTES)))
    _spy_dispatch(monkeypatch, stub)

    first = dx.diagnose(home, RUN, write=True, allow_model=True)
    # A later, read-only pass (the shape ``auto_repair.decide`` uses) must ALSO
    # reuse it: the persisted diagnosis — not the in-memory result — is the
    # at-most-once record.
    second = dx.diagnose(home, RUN, write=False, allow_model=True)

    assert first["source"] == "model"
    assert second["source"] == "model"
    assert second["rule_id"] == "M1"
    assert len(stub.calls) == 1
    persisted = json.loads((home / "runs" / RUN / "diagnosis.json").read_text())
    assert persisted["source"] == "model"


def test_a_run_that_already_has_a_model_diagnosis_never_dispatches(
        home: Path, monkeypatch) -> None:
    run_dir = _seed(home)
    (run_dir / "diagnosis.json").write_text(json.dumps({
        "version": dx.DIAGNOSIS_VERSION, "run_id": RUN, "rule_id": "M1",
        "layer": "provider", "stage": "execute", "node": None, "source": "model",
        "confidence": 0.8, "evidence": [], "routes_to": "retry",
        "summary": "the reaped lifecycle", "computed_at": "2026-01-01T00:00:00Z",
    }))
    stub = _Dispatch(_reply_with("unused"))
    _spy_dispatch(monkeypatch, stub)

    result = dx.diagnose(home, RUN, write=False, allow_model=True)

    assert stub.calls == []
    assert result["source"] == "model"
    assert result["layer"] == "provider"


# ── the evidence pack ──────────────────────────────────────────────────────


def test_pack_redacts_secret_shaped_assignments(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "impl-implementer.log").write_text(
        "starting implementer\nAPI_KEY=sk-live-9f3c2b1a\npassword: hunter2\n"
        "the implementer never reported back\n"
    )

    pack = _pack(home)

    assert "<redacted>" in pack
    assert "sk-live-9f3c2b1a" not in pack
    assert "hunter2" not in pack
    assert "the implementer never reported back" in pack


@pytest.mark.parametrize("secret_chars", [6_000, 40_000])
def test_pack_redacts_before_it_cuts(home: Path, secret_chars: int) -> None:
    """A cut that drops ``API_KEY=`` must not leave its value behind.

    6 000 chars crosses the section's tail cut; 40 000 crosses the log reader's
    32 KB window, whose first line then starts inside the value.
    """
    run_dir = _seed(home)
    secret = "Zq" * (secret_chars // 2)
    (run_dir / "impl-implementer.log").write_text(f"API_KEY={secret}\nthe implementer never reported back\n")
    (run_dir / "verdict.json").write_text(json.dumps({"verdict": "fail", "api_key": "sk-abcdef0123456789xyz"}))
    _event(home, RUN, 1, "node_end", {"node_id": "implementer", "finish_reason": "timeout",
                                      "token": "tok-should-not-leak"})

    pack = _pack(home)

    assert "ZqZqZqZq" not in pack
    assert "sk-abcdef0123456789xyz" not in pack
    assert "tok-should-not-leak" not in pack
    assert "the implementer never reported back" in pack


def test_a_chatty_lifecycle_cannot_crowd_out_the_log(home: Path) -> None:
    """Every source has its own budget — 40 fat events leave room for the rest."""
    run_dir = _seed(home)
    for i in range(60):
        _event(home, RUN, i, "node_progress", {"node_id": "implementer", "blob": "x" * 500})
    (run_dir / "impl-implementer.log").write_text("Traceback: the newest line of the log\n")

    pack = _pack(home)

    assert len(pack) <= dl.PACK_MAX_CHARS
    assert "=== RUN NOTES ===" in pack
    assert "Traceback: the newest line of the log" in pack


def test_pack_bounds_the_log_tail_and_its_own_size(home: Path) -> None:
    run_dir = _seed(home)
    lines = [f"line {i:04d} of the implementer log" for i in range(400)]
    (run_dir / "impl-implementer.log").write_text("\n".join(lines))
    _event(home, RUN, 1, "node_end", {"node_id": "implementer", "finish_reason": "timeout"})
    (run_dir / "repair.json").write_text(json.dumps({"attempts": [{"action": "revise"}]}))
    (run_dir / "verdict.json").write_text(json.dumps({"verdict": "fail"}))

    pack = _pack(home)

    assert len(pack) <= dl.PACK_MAX_CHARS
    assert "line 0399 of the implementer log" in pack      # the tail is kept
    assert "line 0000 of the implementer log" not in pack  # the head is not
    assert "=== RUN NOTES ===" in pack
    assert "=== RUN EVENTS" in pack
    assert "=== verdict.json ===" in pack
    assert "=== repair.json ===" in pack


def test_pack_never_raises_on_a_missing_run(home: Path) -> None:
    pack = dl.build_evidence_pack(home, "run-does-not-exist", None)

    assert isinstance(pack, str)
    assert len(pack) <= dl.PACK_MAX_CHARS


# ── the auto_repair gate ───────────────────────────────────────────────────


@pytest.fixture
def stubbed_loop(monkeypatch):
    """The auto-repair test pattern: no real workflow, no hint computation."""
    import mini_ork.recovery.retry_hint as rh
    nodes = [{"name": "implementer", "type": "implementer"}]
    monkeypatch.setattr(rh, "_recipe_workflow", lambda home, recipe: (nodes, []))
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: None)


def _spy_dispatch(monkeypatch, stub: "_Dispatch | None" = None) -> _Dispatch:
    """Install a recording ``dispatch_model`` — the seam ``decide`` reaches.

    Returns the recording stub. Callers that already built one (to script its
    reply) pass it in; the default answers with a grounded reply built from the
    prompt's own pack.
    """
    import mini_ork.dispatch.providers as providers
    stub = stub or _Dispatch(lambda request: json.dumps(
        _reply_with(_cite_from_prompt(request.prompt))))
    monkeypatch.setattr(providers, "dispatch_model", stub)
    return stub


def test_decide_dispatches_the_model_diagnoser_by_default(home: Path, monkeypatch,
                                                          stubbed_loop) -> None:
    _seed(home)
    stub = _spy_dispatch(monkeypatch)

    decision = ar.decide(home, RUN)

    assert len(stub.calls) == 1
    assert decision["diagnosis"]["source"] == "model"
    assert decision["diagnosis"]["rule_id"] == "M1"
    # The policy re-runs on the model diagnosis: engine + fix_epic → escalate.
    assert decision["action"] == "escalate"


def test_decide_reuses_the_model_diagnosis_apply_persisted(home: Path, monkeypatch,
                                                            stubbed_loop) -> None:
    """``apply`` lands ``diagnosis.json``; the next ``decide`` must not pay again."""
    _seed(home)
    stub = _spy_dispatch(monkeypatch)
    first = ar.decide(home, RUN)
    dx.write_diagnosis(home, RUN, first["diagnosis"])

    second = ar.decide(home, RUN)

    assert len(stub.calls) == 1
    assert second["diagnosis"]["source"] == "model"


def test_decide_never_dispatches_for_a_run_that_did_not_fail(home: Path, monkeypatch,
                                                             stubbed_loop) -> None:
    """A green run's diagnosis is trivially unclassified — and costs nothing."""
    _seed(home, status="published")
    stub = _spy_dispatch(monkeypatch)

    decision = ar.decide(home, RUN)

    assert stub.calls == []
    assert decision["action"] == "none"


def test_decide_never_dispatches_in_a_dry_run(home: Path, monkeypatch, stubbed_loop) -> None:
    _seed(home)
    stub = _spy_dispatch(monkeypatch)
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")

    ar.decide(home, RUN)

    assert stub.calls == []


def test_decide_never_dispatches_when_the_diagnoser_is_off(home: Path, monkeypatch,
                                                           stubbed_loop) -> None:
    _seed(home)
    stub = _spy_dispatch(monkeypatch)
    monkeypatch.setenv("MO_MODEL_DIAGNOSER", "0")

    decision = ar.decide(home, RUN)

    assert stub.calls == []
    assert decision["diagnosis"]["source"] != "model"


def test_decide_never_dispatches_for_a_parked_run(home: Path, monkeypatch,
                                                  stubbed_loop) -> None:
    """A run parked on its fix epic is inert: it must not spend a model call."""
    run_dir = _seed(home)
    (run_dir / "repair.json").write_text(json.dumps({
        "state": "waiting_on_fix", "fix_epic_id": "epic-1", "attempts": [],
    }))
    stub = _spy_dispatch(monkeypatch)

    decision = ar.decide(home, RUN)

    assert stub.calls == []
    assert decision["action"] == "none"
    assert decision["fix_epic_id"] == "epic-1"


def test_decide_never_dispatches_when_auto_repair_is_off(home: Path, monkeypatch,
                                                         stubbed_loop) -> None:
    _seed(home)
    stub = _spy_dispatch(monkeypatch)
    monkeypatch.setenv("MO_AUTO_REPAIR", "0")

    decision = ar.decide(home, RUN)

    assert stub.calls == []
    assert decision["action"] == "none"
