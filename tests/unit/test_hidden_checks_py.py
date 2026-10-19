"""Unit tests for the hidden regenerable ground-truth layer.

Hermetic: a scripted runner and an empty dispatch only -- no network, no model,
no run-local state. Covers the kickoff's five test groups:

1. ``precommit`` stability and sensitivity;
2. ``plan_for`` reproducibility and key-source precedence;
3. ``apply_variant`` behaviour neutrality;
4. oracle integration (pass / refute / unmeasured / knob / run_id=None);
5. certificate commitment and trial keys.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re

import pytest

from mini_ork.certify import hidden_checks
from mini_ork.certify.certificate import build_certificate
from mini_ork.certify.oracle import judge
from mini_ork.certify.verdict import PROVEN, REFUTED, UNVERIFIED
from mini_ork.runtime import ExecOutcome

# ── fixtures ─────────────────────────────────────────────────────────────────

HIDDEN_MARKER = "# hidden-check trial"

ISSUE = "the reported bug"
PATCH = (
    "diff --git a/mod.py b/mod.py\n"
    "+++ b/mod.py\n"
    "@@ -1 +1 @@\n"
    "-x\n"
    "+y\n"
)
POC = (
    "import json\n"
    "\n"
    "\n"
    "def test_repro():\n"
    "    assert json.dumps({'k': 1})\n"
)
#: No top-level test identifier: apply_variant must leave this unchanged.
POC_NO_IDS = (
    "import json\n"
    "\n"
    "\n"
    "def main():\n"
    "    return json.dumps({'k': 1})\n"
)

VARIANT_SRC = (
    "import json\n"
    "\n"
    "\n"
    "def helper(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def test_one():\n"
    "    assert helper(1) == 2\n"
    "\n"
    "\n"
    "class TestWidget:\n"
    "    def test_two(self):\n"
    "        assert helper(2) == 3\n"
    "\n"
    "\n"
    "def test_three():\n"
    "    literal = \"def test_one(\"\n"
    "    assert json.dumps({'k': literal})\n"
)

RENAMED_RE = re.compile(r"\b([A-Za-z_]\w*_h[0-9a-f]{8}_\d+)\b")
SUFFIX_RE = re.compile(r"_h[0-9a-f]{8}_\d+\Z")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """No leaked hiding config; downstream assay terms off for determinism."""
    for var in ("MO_HIDDEN_CHECK_SECRET", "MO_ASSAY_HIDDEN", "MO_HIDDEN_TRIALS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")
    monkeypatch.setenv("MO_ASSAY_DISCRIMINATION", "0")


def _seed_for(key: bytes) -> int:
    return int.from_bytes(
        hmac.new(key, b"mini-ork-hidden", hashlib.sha256).digest()[:8], "big"
    )


# ── 1. precommit ─────────────────────────────────────────────────────────────


def test_precommit_exact_formula_and_key_order():
    params = {"b": 2, "a": 1, "nested": {"k": [1, 2]}}
    expected = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert hidden_checks.precommit(params) == expected
    assert hidden_checks.precommit({"a": 1, "b": 2}) == hidden_checks.precommit(
        {"b": 2, "a": 1}
    )


def test_precommit_changes_with_any_value():
    assert hidden_checks.precommit({"a": 1}) != hidden_checks.precommit({"a": 2})
    assert hidden_checks.precommit({"a": 1}) != hidden_checks.precommit(
        {"a": 1, "z": 0}
    )


def test_default_trials_is_two():
    assert hidden_checks.DEFAULT_TRIALS == 2


# ── 2. plan_for ──────────────────────────────────────────────────────────────


def test_plan_for_secret_precedence_and_seed(monkeypatch):
    monkeypatch.setenv("MO_HIDDEN_CHECK_SECRET", "s3cret-key")
    plan = hidden_checks.plan_for(run_id="run-1", patch=PATCH)
    assert plan.key_source == "secret"
    assert plan.seed == _seed_for(b"s3cret-key")


def test_plan_for_empty_secret_falls_through_to_run_id(monkeypatch):
    monkeypatch.setenv("MO_HIDDEN_CHECK_SECRET", "")
    plan = hidden_checks.plan_for(run_id="run-1", patch=PATCH)
    assert plan.key_source == "run_id"
    assert plan.seed == _seed_for(b"run-1")


def test_plan_for_empty_run_id_falls_through_to_patch_digest(monkeypatch):
    monkeypatch.delenv("MO_HIDDEN_CHECK_SECRET", raising=False)
    plan = hidden_checks.plan_for(run_id="", patch=PATCH)
    assert plan.key_source == "patch_digest"
    # Never an empty key: the key is sha256(patch).
    digest = hashlib.sha256(PATCH.encode("utf-8")).hexdigest().encode("utf-8")
    assert plan.seed == _seed_for(digest)


def test_plan_for_none_run_id_uses_patch_digest(monkeypatch):
    monkeypatch.delenv("MO_HIDDEN_CHECK_SECRET", raising=False)
    assert hidden_checks.plan_for(run_id=None, patch=PATCH).key_source == "patch_digest"


def test_plan_for_commitment_is_precommit_of_params():
    explicit = hidden_checks.plan_for(run_id="run-1", patch=PATCH, params={"a": 1})
    assert explicit.commitment == hidden_checks.precommit({"a": 1})
    default = hidden_checks.plan_for(run_id="run-1", patch=PATCH)
    assert default.commitment == hidden_checks.precommit({})


def test_plan_for_reproducible_and_run_id_sensitive(monkeypatch):
    monkeypatch.delenv("MO_HIDDEN_CHECK_SECRET", raising=False)
    diff_with_test = (
        "+++ b/tests/test_x.py\n"
        "+def test_case():\n"
        "+    assert True\n"
    )
    one = hidden_checks.plan_for(run_id="run-1", patch=diff_with_test, trials=3)
    again = hidden_checks.plan_for(run_id="run-1", patch=diff_with_test, trials=3)
    two = hidden_checks.plan_for(run_id="run-2", patch=diff_with_test, trials=3)

    assert (one.seed, one.variants, one.commitment) == (
        again.seed, again.variants, again.commitment
    )
    assert one.seed != two.seed
    assert one.variants != two.variants
    names_one = set(RENAMED_RE.findall("\n".join(one.variants)))
    names_two = set(RENAMED_RE.findall("\n".join(two.variants)))
    assert names_one and names_two and names_one != names_two


def test_plan_for_trials_default_and_override():
    default = hidden_checks.plan_for(run_id="run-1", patch=PATCH)
    assert len(default.variants) == hidden_checks.DEFAULT_TRIALS
    assert len(hidden_checks.plan_for(run_id="run-1", patch=PATCH, trials=5).variants) == 5


# ── 3. apply_variant ─────────────────────────────────────────────────────────


def test_apply_variant_renames_top_level_identifiers_only():
    variant = hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=0)
    assert re.search(r"^def test_one_h[0-9a-f]{8}_0\(", variant, re.M)
    assert re.search(r"^def test_three_h[0-9a-f]{8}_0\(", variant, re.M)
    assert re.search(r"^class TestWidget_h[0-9a-f]{8}_0\b", variant, re.M)
    assert not re.search(r"^def test_one\(", variant, re.M)
    assert not re.search(r"^class TestWidget\b", variant, re.M)
    # Nested class methods are not top-level test identifiers.
    assert re.search(r"^    def test_two\(self\):", variant, re.M)


def test_apply_variant_marker_is_first_line():
    variant = hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=3)
    first = variant.split("\n", 1)[0]
    assert re.fullmatch(r"# hidden-check trial 3 [0-9a-f]{8}", first)
    assert variant.startswith(HIDDEN_MARKER)


def test_apply_variant_bodies_are_byte_identical():
    variant = hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=0)
    body = variant.split("\n", 1)[1]
    restored = body
    for new in sorted(set(RENAMED_RE.findall(variant)), reverse=True):
        restored = restored.replace(new, SUFFIX_RE.sub("", new))
    assert restored == VARIANT_SRC


def test_apply_variant_is_deterministic_and_seed_index_sensitive():
    one = hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=0)
    assert one == hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=0)
    assert one != hidden_checks.apply_variant(VARIANT_SRC, seed=43, index=0)
    other = hidden_checks.apply_variant(VARIANT_SRC, seed=42, index=1)
    assert other != one
    assert re.search(r"_h[0-9a-f]{8}_1\b", other)


def test_apply_variant_without_test_identifier_returns_source_unchanged():
    src = "import os\n\n\ndef add(a, b):\n    return a + b\n"
    assert hidden_checks.apply_variant(src, seed=42, index=0) == src


# ── 4. oracle integration ────────────────────────────────────────────────────


class HiddenRunner:
    """Scripted runner: hidden trials keyed by their marker, everything else passes."""

    up = True

    def __init__(self, *, term2: str = "passed", hidden=None):
        self.term2 = term2
        self.hidden = hidden if callable(hidden) else (lambda i: hidden or "passed")
        self.calls: list[tuple[str, str]] = []

    def run_test(self, src: str, patch: str = "") -> ExecOutcome:
        self.calls.append((src, patch))
        if src.startswith(HIDDEN_MARKER):
            match = re.match(r"# hidden-check trial (\d+)", src)
            status = self.hidden(int(match.group(1)))
        elif patch:
            status = self.term2
        else:
            status = "passed"
        ok = status == "passed"
        return ExecOutcome(
            status=status,
            ran=status in ("passed", "failed"),
            returncode=0 if ok else 1,
            output="" if ok else "E   simulated failure",
            exc="" if ok else "AssertionError",
        )

    def hidden_calls(self):
        return [src for src, _ in self.calls if src.startswith(HIDDEN_MARKER)]


def _empty_dispatch(prompt: str) -> str:
    return ""


def _judge(runner, *, run_id: str | None = "run-1", poc: str = POC):
    return judge(
        ISSUE,
        PATCH,
        runner=runner,
        poc=poc,
        mr_n=2,
        dispatch=_empty_dispatch,
        run_id=run_id,
    )


def test_judge_gains_run_id_keyword_only():
    params = inspect.signature(judge).parameters
    assert set(params) == {
        "issue", "patch", "runner", "mr_n", "gold", "poc",
        "dispatch", "context", "candidates", "run_id",
    }
    assert params["run_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["run_id"].default is None


def test_hidden_layer_does_not_run_when_term2_fails():
    runner = HiddenRunner(term2="failed")
    verdict = _judge(runner)
    assert verdict.verdict == REFUTED
    assert runner.hidden_calls() == []


def test_all_variants_pass_leaves_verdict_unchanged(monkeypatch):
    monkeypatch.setenv("MO_ASSAY_HIDDEN", "0")
    baseline = _judge(HiddenRunner())
    monkeypatch.delenv("MO_ASSAY_HIDDEN")

    runner = HiddenRunner()
    verdict = _judge(runner)
    assert len(runner.hidden_calls()) == hidden_checks.DEFAULT_TRIALS
    assert verdict.verdict == baseline.verdict
    assert verdict.detail["hidden_checks"]["measured"] == hidden_checks.DEFAULT_TRIALS


def test_variant_failure_is_refuted_with_hidden_evidence():
    runner = HiddenRunner(hidden=lambda i: "failed" if i == 1 else "passed")
    verdict = _judge(runner)
    assert verdict.verdict == REFUTED
    assert "hidden trial 1 failed" in verdict.reason
    assert "depends on the test's identity" in verdict.reason
    assert verdict.poc_plus == POC
    assert verdict.detail["hidden_checks"]["failed_trial"] == 1


@pytest.mark.parametrize("status", ["error", "test_defect", "apply_fail", "no_run"])
def test_harness_status_is_unmeasured_never_refuted(status):
    runner = HiddenRunner(hidden=lambda i, s=status: s)
    verdict = _judge(runner)
    assert verdict.verdict != REFUTED
    assert verdict.verdict == UNVERIFIED
    blob = verdict.reason + json.dumps(verdict.detail, default=str, sort_keys=True)
    assert "unmeasured" in blob.lower()
    assert "hidden_commitment=" in blob
    assert verdict.detail["hidden_checks"]["measured"] == 0


def test_unrenamed_variant_is_unmeasured_not_refuted():
    runner = HiddenRunner(term2="passed")
    verdict = _judge(runner, poc=POC_NO_IDS)
    assert verdict.verdict != REFUTED
    assert "unmeasured" in verdict.reason.lower()
    # No manufactured difference: the unchanged "variant" is never executed.
    assert runner.hidden_calls() == []


@pytest.mark.parametrize("value", ["0", "off"])
def test_disabled_knob_runs_no_hidden_calls(monkeypatch, value):
    monkeypatch.setenv("MO_ASSAY_HIDDEN", value)
    runner = HiddenRunner()
    verdict = _judge(runner)
    assert runner.hidden_calls() == []
    other = HiddenRunner()
    assert _judge(other, run_id=None).reason == verdict.reason


def test_knob_defaults_on():
    runner = HiddenRunner()
    _judge(runner)
    assert len(runner.hidden_calls()) == hidden_checks.DEFAULT_TRIALS


def test_run_id_none_uses_patch_digest_and_still_runs():
    plan = hidden_checks.plan_for(run_id=None, patch=PATCH)
    assert plan.key_source == "patch_digest"
    runner = HiddenRunner()
    verdict = _judge(runner, run_id=None)
    assert verdict.verdict != REFUTED
    assert len(runner.hidden_calls()) == hidden_checks.DEFAULT_TRIALS


def test_hidden_trials_env_var_controls_the_count(monkeypatch):
    monkeypatch.setenv("MO_HIDDEN_TRIALS", "3")
    runner = HiddenRunner()
    _judge(runner)
    assert len(runner.hidden_calls()) == 3


def test_non_integer_hidden_trials_raises(monkeypatch):
    monkeypatch.setenv("MO_HIDDEN_TRIALS", "not-a-number")
    with pytest.raises(ValueError):
        _judge(HiddenRunner())


def test_hidden_variants_are_neutral_variants_of_the_poc():
    runner = HiddenRunner()
    _judge(runner)
    hidden = [(src, patch) for src, patch in runner.calls if src.startswith(HIDDEN_MARKER)]
    assert hidden
    for src, patch in hidden:
        assert patch == PATCH
        lines = src.split("\n")
        assert re.fullmatch(r"# hidden-check trial \d+ [0-9a-f]{8}", lines[0])
        body = "\n".join(lines[1:])
        assert re.sub(r"_h[0-9a-f]{8}_\d+\b", "", body) == POC
        assert not re.search(r"^def test_repro\(", body, re.M)


# ── 5. certificate ───────────────────────────────────────────────────────────


def _cert_kwargs() -> dict:
    return dict(
        verdict="PROVEN",
        reason="ok",
        repo_path="/tmp/repo",
        remote=None,
        base_sha="a" * 40,
        head_sha="b" * 40,
        patch_text=(
            "diff --git a/mod.py b/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        ),
        issue_text="the reported bug",
        oracle_method="poc-plus-delta",
        mr_n=2,
        proven_bar=0.66,
        model="test-model",
        image="test-image",
        poc_plus="def test_poc(): pass",
        mr_pass_rate=1.0,
        invariants=[],
        cost_usd=0.0,
        llm_calls=0,
        duration_s=1.0,
    )


def _walk_items(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk_items(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk_items(item)


def _sealed_digest(cert: dict) -> str:
    payload = {k: v for k, v in cert.items() if k != "digest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_certificate_signature_gains_two_optional_kwargs():
    params = inspect.signature(build_certificate).parameters
    for name in ("hidden_check_commitment", "hidden_trials"):
        param = params.get(name)
        assert param is not None
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None


def test_certificate_omitted_is_previous_shape_and_digest():
    cert = build_certificate(**_cert_kwargs())
    assert set(cert["method"]) == {"oracle", "mr_n", "proven_bar", "model", "image"}
    assert set(cert["evidence"]) == {"probe", "mr_pass_rate", "invariants"}
    assert not any("hidden" in key.lower() for key, _ in _walk_items(cert))
    assert cert["digest"] == _sealed_digest(cert)


def test_certificate_provided_seals_both_keys_and_changes_payload():
    commitment = "c" * 64
    omitted = build_certificate(**_cert_kwargs())
    supplied = build_certificate(
        **_cert_kwargs(),
        hidden_check_commitment=commitment,
        hidden_trials=2,
    )
    items = {(key, value) for key, value in _walk_items(supplied) if "hidden" in key.lower()}
    assert ("hidden_check_commitment", commitment) in items
    assert ("hidden_trials", 2) in items
    assert supplied["digest"] == _sealed_digest(supplied)

    def strip_volatile(cert):
        return {k: v for k, v in cert.items() if k not in ("digest", "id", "issued_at")}

    assert json.dumps(strip_volatile(omitted), sort_keys=True) != json.dumps(
        strip_volatile(supplied), sort_keys=True
    )


def test_verdict_required_fields_unchanged():
    import dataclasses

    from mini_ork.certify.verdict import Verdict

    required = [
        field.name
        for field in dataclasses.fields(Verdict)
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    ]
    assert required == ["verdict", "reason"]
    assert PROVEN != REFUTED  # sanity: the verdict vocabulary is intact
