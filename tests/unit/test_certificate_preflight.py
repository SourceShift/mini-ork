"""The arm-time certificate pre-flight (synthesis Win 2).

The point of the pre-flight is that it costs nothing: it reads config, the
ledger's next slot and the shipped probe set, and refuses an arm that could
never certify. These tests pin the refusal, the exact reasons it must not
refuse, and the dead zone at the shipped probe cap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path



from mini_ork.learning import promotion_certificate as pc
from mini_ork.learning import probe_scorer

REPO = Path(__file__).resolve().parents[2]


def _cfg(**over):
    cfg = {"enabled": True, "alpha": 0.05, "bet": 0.75, "ledger": "spend",
           "errors": []}
    cfg.update(over)
    return cfg


# ── the verdict ─────────────────────────────────────────────────────────────

def test_ok_when_the_probe_set_can_reach_the_bar():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=7, slot=1)
    assert v["ok"] is True
    assert v["required_gains"] == 7
    assert v["errors"] == []


def test_refuses_when_the_probe_set_cannot_reach_the_bar():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=2, slot=1)
    assert v["ok"] is False
    (err,) = v["errors"]
    assert "needs 7 clean gains" in err
    assert "only 2 non-anchor gate probe(s)" in err
    assert "MO_APPLY_PROBE_MAX_TASKS caps that number" in err
    assert "ship more non-anchor" in err


def test_refuses_config_errors():
    v = pc.preflight(task_class="code_fix",
                     cfg=_cfg(errors=["MO_APPLY_CERT_ALPHA='x' must be a number"]),
                     gate_probes=7, slot=1)
    assert not v["ok"]
    assert any("misconfigured" in e for e in v["errors"])


def test_refuses_an_unreachable_ledger():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=7, slot=1,
                     db_error="OperationalError: unable to open database file")
    assert not v["ok"]
    assert any("ledger unreachable" in e for e in v["errors"])


def test_refuses_an_alpha_above_the_guardrail():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(alpha=0.5),
                     gate_probes=7, slot=1)
    assert not v["ok"]
    assert any("guardrail" in e and pc.ALPHA_LOOSE_ENV in e for e in v["errors"])


def test_the_loose_override_accepts_a_high_alpha():
    # alpha 0.5 at slot 1 → bar 4; 1.9^2 = 3.61 misses it, 1.9^3 = 6.86 clears.
    v = pc.preflight(task_class="code_fix", cfg=_cfg(alpha=0.5),
                     gate_probes=3, slot=1, allow_loose_alpha=True)
    assert v["ok"] is True
    assert v["required_gains"] == 3


def test_ledger_none_warns_but_does_not_refuse():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(ledger="none"),
                     gate_probes=6, slot=1)
    assert v["ok"] is True
    assert v["required_gains"] == 6
    assert len(v["warnings"]) == 1
    assert "union bound" in v["warnings"][0]


def test_a_later_slot_needs_more_gains_and_may_refuse():
    v = pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=7, slot=10)
    assert v["ok"] is False
    assert v["required_gains"] == 14


# ── the shipped configuration is the dead zone (Win 5a) ────────────────────

def test_the_shipped_code_fix_probe_set_cannot_certify(monkeypatch):
    """2 gate probes vs a 7-gain bar: the pre-flight is the only cheap answer.

    This is the finding (A-1) the review opened with: at shipped settings the
    certificate can only ever quarantine, and without a pre-flight the way you
    find out is a full probe sweep (~$135 / ~13 h) that ends in 'unreachable'.
    """
    monkeypatch.setattr(probe_scorer, "_ROOT", str(REPO), raising=False)
    monkeypatch.delenv("MO_APPLY_PROBE_MAX_TASKS", raising=False)
    monkeypatch.delenv("MO_APPLY_PROBE_GIT_ORACLE", raising=False)
    count = probe_scorer.gate_probe_count("code_fix")
    assert count == 2

    v = pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=count,
                     slot=1)
    assert v["ok"] is False
    assert v["required_gains"] == 7


def test_the_cap_cannot_invent_probes(monkeypatch):
    """MO_APPLY_PROBE_MAX_TASKS raises a ceiling; it cannot add a probe.

    The recipe ships exactly two non-anchor probes, so no cap value makes the
    7-gain bar reachable — which is why the refusal names "ship more probes"
    rather than "raise the cap".
    """
    monkeypatch.setattr(probe_scorer, "_ROOT", str(REPO), raising=False)
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "7")
    monkeypatch.delenv("MO_APPLY_PROBE_GIT_ORACLE", raising=False)
    count = probe_scorer.gate_probe_count("code_fix")
    assert count == 2
    assert pc.preflight(task_class="code_fix", cfg=_cfg(), gate_probes=count,
                        slot=1)["ok"] is False


# ── next_slot / ledger_warning ─────────────────────────────────────────────

def test_next_slot_starts_at_one_without_a_table(tmp_path):
    con = sqlite3.connect(tmp_path / "bare.db")
    try:
        assert pc.next_slot(con, "code_fix") == 1
    finally:
        con.close()


def test_next_slot_advances_after_a_test(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        pc.certify(con, task_class="code_fix", candidate_id="a",
                   before=[0] * 12, after=[1] * 7 + [0] * 5,
                   alpha_total=0.05, bet=0.75, ledger="spend", now=1)
        assert pc.next_slot(con, "code-fix") == 2  # same class, other spelling
        assert pc.next_slot(con, "other") == 1
    finally:
        con.close()


def test_ledger_warning_is_none_for_spend():
    assert pc.ledger_warning("spend") is None
    assert "union bound" in pc.ledger_warning("none")
