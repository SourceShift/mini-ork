"""Tests for the anytime-valid promotion certificate and per-class alpha
ledger (kickoff rsi-t1-certificate).

Closed-form math, temp sqlite, no network, no probe runs. The last section
drives ``apply_run`` with a stubbed probe scorer and the fixture schema copied
from ``tests/unit/test_cli_apply_py.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.cli import apply as ap
from mini_ork.learning import promotion_certificate as pc
from mini_ork.learning import probe_scorer

REPO = Path(__file__).resolve().parents[2]

_ENV_KEYS = (
    "MO_APPLY_CERTIFICATE", "MO_APPLY_CERT_ALPHA", "MO_APPLY_CERT_BET",
    "MO_APPLY_CERT_LEDGER", "MO_APPLY_CERT_PREFLIGHT",
    "MO_APPLY_CERT_ALPHA_ALLOW_LOOSE", "MO_APPLY_SIG_ALPHA", "MINI_ORK_DB",
    "MO_APPLY_REGRESSION_TOLERANCE",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── the math ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gains,losses,bet,expected", [
    (0, 0, 0.75, 1.0),
    (7, 0, 0.75, 50.26507568359375),
    (6, 0, 0.75, 28.722900390625),
    (5, 0, 0.75, 16.4130859375),
    (8, 0, 0.75, 87.96388244628906),
    (9, 0, 0.75, 153.93679428100586),
    (10, 0, 0.75, 269.38938999176025),
    (9, 1, 0.75, 38.484198570251465),
    (10, 1, 0.75, 67.34734749794006),
    (1, 1, 0.75, 0.4375),
    (0, 3, 0.75, 0.015625),
    (2, 1, 0.5, 1.125),
    (3, 1, 0.5, 1.6875),
])
def test_betting_e_value_table(gains, losses, bet, expected):
    assert pc.betting_e_value(gains, losses, bet) == pytest.approx(
        expected, rel=1e-12)


@pytest.mark.parametrize("gains,losses,bet", [
    (0, 0, 0.0), (0, 0, 1.0), (0, 0, -0.1), (0, 0, 1.5), (-1, 0, 0.75),
])
def test_betting_e_value_rejects(gains, losses, bet):
    with pytest.raises(ValueError):
        pc.betting_e_value(gains, losses, bet)


def test_spend_fraction():
    assert pc.spend_fraction(1) == pytest.approx(0.5, rel=1e-12)
    assert pc.spend_fraction(2) == pytest.approx(1 / 6, rel=1e-12)
    assert pc.spend_fraction(3) == pytest.approx(1 / 12, rel=1e-12)
    assert sum(pc.spend_fraction(k) for k in range(1, 100)) == pytest.approx(
        0.99, rel=1e-12)
    with pytest.raises(ValueError):
        pc.spend_fraction(0)


def test_slot_alpha():
    assert pc.slot_alpha(0.05, 1, "spend") == pytest.approx(0.025, rel=1e-12)
    assert pc.slot_alpha(0.05, 2, "spend") == pytest.approx(
        0.05 / 6, rel=1e-12)
    assert pc.slot_alpha(0.05, 3, "spend") == pytest.approx(
        0.05 / 12, rel=1e-12)
    assert pc.slot_alpha(0.05, 7, "none") == pytest.approx(0.05, rel=1e-12)
    with pytest.raises(ValueError):
        pc.slot_alpha(0.05, 1, "fdr")
    for k, bar in ((1, 40.0), (2, 120.0), (3, 240.0)):
        assert 1.0 / pc.slot_alpha(0.05, k, "spend") == pytest.approx(
            bar, rel=1e-12)


@pytest.mark.parametrize("m,threshold,expected", [
    (10, 20.0, 28), (10, 40.0, 8), (12, 20.0, 112), (12, 40.0, 46),
])
def test_ville_prefix_max_enumeration(m, threshold, expected):
    """Ville's inequality by exact enumeration of gain/loss sequences."""
    count = 0
    for mask in range(2 ** m):
        gains = losses = 0
        crossed = False
        for t in range(m):
            if (mask >> t) & 1:
                gains += 1
            else:
                losses += 1
            if pc.betting_e_value(gains, losses, 0.75) >= threshold:
                crossed = True
                break
        if crossed:
            count += 1
    assert count == expected
    assert count / 2 ** m <= 1.0 / threshold


def test_paired_counts():
    assert pc.paired_counts([0, 0, 1, 1], [1, 0, 0, 1]) == (1, 1)
    assert pc.paired_counts([0, 1, 1], [1, 1, 0]) == (1, 1)
    with pytest.raises(ValueError):
        pc.paired_counts([0, 1], [1])


def test_normalize_task_class():
    assert pc.normalize_task_class(" code-fix ") == "code_fix"
    assert pc.normalize_task_class("framework_edit") == "framework_edit"
    with pytest.raises(ValueError):
        pc.normalize_task_class("")


def test_resolve_config_defaults():
    assert pc.resolve_config({}) == {
        "enabled": False, "alpha": 0.05, "bet": 0.75,
        "ledger": "spend", "errors": [],
    }


def test_resolve_config_enabled_flag():
    assert pc.resolve_config({"MO_APPLY_CERTIFICATE": "1"})["enabled"] is True
    assert pc.resolve_config({})["enabled"] is False


def test_resolve_config_invalid_alpha():
    cfg = pc.resolve_config({"MO_APPLY_CERT_ALPHA": "2"})
    assert cfg["alpha"] == pytest.approx(0.05, rel=1e-12)
    assert len(cfg["errors"]) == 1
    assert cfg["errors"][0].startswith("MO_APPLY_CERT_ALPHA")


def test_resolve_config_invalid_bet():
    cfg = pc.resolve_config({"MO_APPLY_CERT_BET": "1"})
    assert cfg["bet"] == pytest.approx(0.75, rel=1e-12)
    assert len(cfg["errors"]) == 1
    assert cfg["errors"][0].startswith("MO_APPLY_CERT_BET")


def test_resolve_config_invalid_ledger():
    cfg = pc.resolve_config({"MO_APPLY_CERT_LEDGER": "fdr"})
    assert cfg["ledger"] == "spend"
    assert len(cfg["errors"]) == 1
    assert cfg["errors"][0].startswith("MO_APPLY_CERT_LEDGER")


def test_resolve_config_empty_values_take_defaults():
    cfg = pc.resolve_config({
        "MO_APPLY_CERT_ALPHA": "", "MO_APPLY_CERT_BET": "",
        "MO_APPLY_CERT_LEDGER": "",
    })
    assert cfg["alpha"] == pytest.approx(0.05, rel=1e-12)
    assert cfg["bet"] == pytest.approx(0.75, rel=1e-12)
    assert cfg["ledger"] == "spend"
    assert cfg["errors"] == []


def _migration_statements() -> list[str]:
    """The CREATE statements of db/migrations/0089, comments stripped.

    The module and the migration each carry the schema because the module must
    be able to create the table on a DB that predates the migration. Two hand-
    maintained copies drift; one test that compares them statement by
    statement is what makes the drift impossible."""
    text = (REPO / "db" / "migrations"
            / "0089_promotion_certificates.sql").read_text()
    body = "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("--"))
    out, buf, in_trigger = [], [], False
    for line in body.splitlines():
        buf.append(line)
        s = line.strip().upper()
        if s.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if s == "END;":
                out.append("\n".join(buf).strip())
                buf, in_trigger = [], False
        elif s.endswith(";"):
            out.append("\n".join(buf).strip())
            buf = []
    return [s for s in out if s and not s.upper().startswith("INSERT")]


def test_ddl_matches_migration():
    """The module DDL is the migration's CREATE statements, byte for byte."""
    assert "schema_migrations" not in pc.DDL
    want = [" ".join(s.split()) for s in _migration_statements()]
    got = [" ".join(s.split()) for s in pc.DDL_STATEMENTS]
    assert got == want


def test_ensure_table_on_pre_migration_db(tmp_path):
    con = sqlite3.connect(tmp_path / "predates.db")
    try:
        pc.ensure_table(con)
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','index','trigger')")}
        assert "promotion_certificates" in names
        assert "promotion_certificates_no_update" in names
        assert "promotion_certificates_no_delete" in names
        pc.ensure_table(con)  # idempotent
        assert con.execute(
            "SELECT COUNT(*) FROM promotion_certificates").fetchone()[0] == 0
    finally:
        con.close()


# ── the ledger ──────────────────────────────────────────────────────────────

def _certify(con, task_class, gains=7, losses=0, n=12, candidate_id="cand-1",
             alpha_total=0.05, bet=0.75, ledger="spend", now=1):
    before = [0] * n
    after = [1] * gains + [0] * losses + [0] * (n - gains - losses)
    return pc.certify(con, task_class=task_class, candidate_id=candidate_id,
                      before=before, after=after, alpha_total=alpha_total,
                      bet=bet, ledger=ledger, now=now)


def test_certify_slot1_certified(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        cert = _certify(con, "code_fix")
        assert set(cert) == {
            "task_class", "task_class_raw", "candidate_id", "gains", "losses",
            "n", "bet", "ledger", "alpha_total", "e_value", "run_id",
            "scorer", "dry_run", "probe_set_hash", "probe_ids", "consumed",
            "status", "slot", "alpha_k", "threshold", "required_gains",
            "ledger_warning", "certified"}
        assert cert["task_class"] == "code_fix"
        assert cert["candidate_id"] == "cand-1"
        assert cert["gains"] == 7 and cert["losses"] == 0 and cert["n"] == 12
        assert cert["e_value"] == pytest.approx(50.26507568359375, rel=1e-12)
        assert cert["consumed"] is True
        assert cert["slot"] == 1
        assert cert["alpha_k"] == pytest.approx(0.025, rel=1e-12)
        assert cert["threshold"] == pytest.approx(40.0, rel=1e-12)
        assert cert["certified"] is True
        rows = con.execute(
            "SELECT task_class, slot, candidate_id, gains, losses, n, bet, "
            "e_value, ledger, alpha_total, alpha_k, threshold, status, "
            "created_at FROM promotion_certificates").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "code_fix" and row[1] == 1 and row[2] == "cand-1"
        assert row[3] == 7 and row[4] == 0 and row[5] == 12
        assert row[7] == pytest.approx(50.26507568359375, rel=1e-12)
        assert row[8] == "spend"
        assert row[12] == "certified"
        assert row[13] == 1  # created_at = now
    finally:
        con.close()


def test_certify_ledger_tightens(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        first = _certify(con, "code_fix")
        assert first["slot"] == 1 and first["certified"] is True
        second = _certify(con, "code_fix")
        assert second["slot"] == 2
        assert second["threshold"] == pytest.approx(120.0, rel=1e-12)
        assert second["certified"] is False
        assert second["consumed"] is True
    finally:
        con.close()


def test_certify_ledger_is_per_class(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        _certify(con, "code_fix")
        other = _certify(con, "framework_edit")
        assert other["slot"] == 1
        assert other["certified"] is True
    finally:
        con.close()


def test_certify_normalizes_task_class(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        _certify(con, "code_fix")
        _certify(con, "code_fix")
        cert = _certify(con, "code-fix")
        assert cert["task_class"] == "code_fix"
        assert cert["slot"] == 3
    finally:
        con.close()


def test_certify_no_discordant_pairs_consumes_nothing(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        _certify(con, "code_fix")
        before_count = con.execute(
            "SELECT COUNT(*) FROM promotion_certificates").fetchone()[0]
        cert = pc.certify(con, task_class="code_fix", candidate_id="cand-2",
                          before=[1, 0, 1], after=[1, 0, 1],
                          alpha_total=0.05, bet=0.75, ledger="spend", now=1)
        assert cert["consumed"] is False
        assert cert["slot"] is None
        assert cert["alpha_k"] is None
        assert cert["threshold"] is None
        assert cert["certified"] is False
        assert cert["e_value"] == pytest.approx(1.0, rel=1e-12)
        assert con.execute(
            "SELECT COUNT(*) FROM promotion_certificates").fetchone()[0] == \
            before_count
    finally:
        con.close()


def test_certify_ledger_none_mode(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        for _ in range(4):
            _certify(con, "x")
        cert = _certify(con, "x", gains=6, ledger="none")
        assert cert["slot"] == 5
        assert cert["alpha_k"] == pytest.approx(0.05, rel=1e-12)
        assert cert["threshold"] == pytest.approx(20.0, rel=1e-12)
        assert cert["certified"] is True
    finally:
        con.close()


def test_certify_empty_task_class_raises(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        with pytest.raises(ValueError):
            pc.certify(con, task_class="", candidate_id="cand-1",
                       before=[0, 0], after=[1, 0], alpha_total=0.05,
                       bet=0.75, ledger="spend", now=1)
    finally:
        con.close()


def test_certify_unique_task_class_slot(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        _certify(con, "code_fix")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO promotion_certificates "
                "(task_class, slot, candidate_id, gains, losses, n, bet, "
                "e_value, ledger, alpha_total, alpha_k, threshold, "
                "status, created_at) "
                "VALUES ('code_fix', 1, 'dup', 0, 0, 0, 0.75, 1.0, 'spend', "
                "0.05, 0.025, 40.0, 'certified', 1)")
    finally:
        con.close()


# ── the gate ────────────────────────────────────────────────────────────────

def _V(g, l=0):
    assert l == 0
    return json.dumps({"before": [0] * 12, "after": [1] * g + [0] * (12 - g)})


def _gate(monkeypatch, ub, ua, pertask, task_class="code_fix",
          db="UNSET", **env):
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    kwargs = {} if db == "UNSET" else {"db": db}
    return json.loads(ap.evaluate_gate(
        "c1", ub, ua, pertask, task_class=task_class, **kwargs))


# Captured from the pre-certificate gate (the commit before the certificate
# landed), not from a second run of today's code: the review's B-9 finding was
# that comparing new-vs-new cannot detect a behaviour change on the off path.
_GOLDEN_FLAG_OFF = {
    "decision": "promoted",
    "rationale": (
        "non-regression cleared: utility_after=0.5833 >= utility_before=0.0000 "
        "(delta=+0.5833 >= threshold=+0.0000); 0 per-task regressions; "
        "McNemar p=0.0078"),
    "utility_before": 0.0,
    "utility_after": 0.583333,
    "utility_delta": 0.583333,
    "threshold": 0.0,
    "min_examples": 1,
    "regressed_tasks": 0,
    "regression_tolerance": 0,
    "control_n": 1,
    "sig_p": 0.007812,
    "sig_gains": 7,
    "sig_losses": 0,
}


def test_flag_off_matches_the_golden_output(tmp_path):
    p = str(tmp_path / "off.db")
    with_args = ap.evaluate_gate(
        "c1", 0.0, 7 / 12, _V(7, 0), task_class="code_fix", db=p)
    without = ap.evaluate_gate("c1", 0.0, 7 / 12, _V(7, 0))
    assert json.loads(with_args) == _GOLDEN_FLAG_OFF
    assert json.loads(without) == _GOLDEN_FLAG_OFF
    assert not __import__("os").path.exists(p)


def test_flag_on_under_certified_quarantines(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 0.5, _V(6, 0), db=p)
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith(
        "certificate unreachable at this probe count: need >= 7 clean gains, "
        "only 6 discordant pair(s) and max e=28.7229 < threshold=40.0000 "
        "(slot=1, alpha_k=0.025, bet=0.75); "
        "point-estimate gate said: non-regression cleared")
    assert out["cert"]["certified"] is False
    assert out["cert"]["status"] == "unreachable"
    assert out["cert"]["required_gains"] == 7


def test_flag_on_certified_promotes(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p)
    assert out["decision"] == "promoted"
    assert "; certificate e=50.2651 >= 40.0000 (slot 1)" in out["rationale"]
    assert out["cert"]["certified"] is True
    assert out["cert"]["candidate_id"] == "c1"


def test_ledger_tightens_on_second_call(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    first = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p)
    assert first["decision"] == "promoted"
    second = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p)
    assert second["decision"] == "quarantined"
    assert "slot=2" in second["rationale"]
    assert "threshold=120.0000" in second["rationale"]


def test_regression_still_wins_but_consumes_slot(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    pertask = json.dumps({"before": [1, 0] + [0] * 10,
                          "after": [0, 1] + [0] * 10})
    out = _gate(monkeypatch, 1 / 12, 1 / 12, pertask, db=p)
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("per-task no-regression gate")
    assert out["cert"]["consumed"] is True
    assert out["cert"]["gains"] == 1 and out["cert"]["losses"] == 1
    assert out["cert"]["e_value"] == pytest.approx(0.4375, rel=1e-12)
    con = sqlite3.connect(p)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM promotion_certificates").fetchone()[0] == 1
    finally:
        con.close()


def test_scalar_path_is_refused(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.5, 0.7, "", db=p)
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith(
        "no anytime-valid certificate: no per-task vectors")
    assert out["cert"] == {"error": "no per-task vectors"}


def test_unavailable_fails_closed(monkeypatch):
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=None)
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("certificate unavailable:")


def test_empty_task_class_fails_closed(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), task_class="", db=p)
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("certificate unavailable:")


def test_misconfigured_fails_closed(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p,
                MO_APPLY_CERT_ALPHA="2")
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith(
        "certificate misconfigured: MO_APPLY_CERT_ALPHA")
    assert not __import__("os").path.exists(p)


def test_a_measured_quarantine_is_not_relabelled_infra(tmp_path, monkeypatch):
    """A broken certificate on top of evidence must not erase edit memory.

    Review b2d09c95: the overlay stamped ``reason_class='infra'`` on every
    certificate failure, so a run whose per-task gate had *measured* a
    regression became an infrastructure row — invisible to
    ``_previously_failed``, which would then re-propose the same bad directive
    forever. The certificate did not decide this verdict; the measurement did.
    """
    p = str(tmp_path / "g.db")
    regressed = json.dumps({"before": [1, 0] + [0] * 10,
                            "after": [0, 1] + [0] * 10})
    out = _gate(monkeypatch, 1 / 12, 1 / 12, regressed, db=p,
                MO_APPLY_CERT_ALPHA="2")
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("per-task no-regression gate")
    assert out["cert"]["reason_class"] is None


def test_a_broken_certificate_over_a_promote_is_infra(tmp_path, monkeypatch):
    """Here the certificate's failure IS the reason the run refuses."""
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p,
                MO_APPLY_CERT_ALPHA="2")
    assert out["decision"] == "quarantined"
    assert out["cert"]["reason_class"] == "infra"


def test_a_measured_quarantine_stays_in_edit_memory(
        db, tmp_path, capsys, monkeypatch):
    """The end-to-end consequence: the row is evidence, so the sweep sees it."""
    target = tmp_path / "prompts" / "reviewer.md"
    target.parent.mkdir(parents=True)
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    monkeypatch.setenv("MO_APPLY_CERT_ALPHA", "2")      # misconfigured on purpose
    # The pre-flight is covered by its own tests; this one is about where the
    # reason_class lands once the gate has run.
    monkeypatch.setenv("MO_APPLY_CERT_PREFLIGHT", "0")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    # A measured per-task regression against the control arm.
    regressed = json.dumps({"before": [1, 0] + [0] * 10,
                            "after": [0, 1] + [0] * 10})
    monkeypatch.setattr(
        probe_scorer, "probe_score",
        lambda *a, **k: {"before": 1 / 12, "after": 1 / 12, "n": 12,
                         "pertask_json": regressed, "runs": [],
                         "cost_usd": 0.0, "control_n": 1})
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE apply_attempts ADD COLUMN reason_class TEXT")
    con.commit()
    con.close()
    _seed_gradient(db, target="prompts/reviewer.md")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    rows = _rows(db, "apply_attempts")
    assert rows[0]["decision"] == "quarantined"
    assert rows[0]["reason_class"] is None
    assert ap._previously_failed(
        "reviewer", "prompts/reviewer.md", "gr-1", db=db) is True


def test_composes_with_mcnemar(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    out = _gate(monkeypatch, 0.0, 7 / 12, _V(7, 0), db=p,
                MO_APPLY_SIG_ALPHA="0.05")
    assert out["decision"] == "promoted"
    assert "McNemar p=0.0078" in out["rationale"]
    assert "certificate e=50.2651" in out["rationale"]


# ── through apply_run ───────────────────────────────────────────────────────
# Fixture schema / helpers copied verbatim from tests/unit/test_cli_apply_py.py

FIXTURE_DDL = """
CREATE TABLE pattern_records (
  pattern_id            TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  evidence_trace_ids    TEXT NOT NULL DEFAULT '[]',
  frequency             INTEGER NOT NULL DEFAULT 1,
  first_seen            TEXT NOT NULL DEFAULT '',
  last_seen             TEXT NOT NULL DEFAULT '',
  output_type           TEXT NOT NULL,
  promoted_to           TEXT,
  status                TEXT NOT NULL DEFAULT 'observed'
);
CREATE TABLE emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER
);
CREATE TABLE gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);
CREATE TABLE workflow_memory (
  workflow_version_id       TEXT PRIMARY KEY,
  workflow_name             TEXT NOT NULL,
  base_version_id           TEXT,
  yaml_hash                 TEXT NOT NULL,
  yaml_blob                 TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  created_at                TEXT NOT NULL DEFAULT '',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  previous_stable_version_id TEXT
);
CREATE TABLE workflow_candidates (
  candidate_id              TEXT PRIMARY KEY,
  base_workflow_version_id  TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  benchmark_summary_id      TEXT,
  utility_delta             REAL NOT NULL DEFAULT 0.0,
  created_by                TEXT NOT NULL DEFAULT 'evolution_engine',
  created_at                TEXT NOT NULL DEFAULT ''
);
CREATE TABLE promotion_records (
  promotion_id          TEXT PRIMARY KEY,
  candidate_id          TEXT NOT NULL,
  from_version_id       TEXT NOT NULL,
  to_version_id         TEXT NOT NULL,
  utility_before        REAL NOT NULL DEFAULT 0.0,
  utility_after         REAL NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT,
  rationale             TEXT NOT NULL DEFAULT '',
  decision              TEXT NOT NULL,
  decided_at            TEXT NOT NULL DEFAULT '',
  decided_by            TEXT NOT NULL
);
-- Canonical db/migrations/0048_apply_attempts.sql DDL. Production DBs get the
-- table from the migration (the lib's idempotent guard is then a no-op), so
-- the fixture mirrors the migration — including source_kind='none'.
CREATE TABLE apply_attempts (
    attempt_id              TEXT PRIMARY KEY,
    task_class              TEXT NOT NULL,
    target_kind             TEXT NOT NULL
                            CHECK (target_kind IN ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
    target_name             TEXT NOT NULL,
    source_kind             TEXT NOT NULL
                            CHECK (source_kind IN ('pattern_records','emergent_patterns','gradient_records','synthesis_gate_verdict','none')),
    source_id               TEXT,
    candidate_id            TEXT REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
    promotion_id            TEXT REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
    base_workflow_version_id TEXT,
    utility_before          REAL,
    utility_after           REAL,
    utility_delta           REAL,
    decision                TEXT NOT NULL
                            CHECK (decision IN ('promoted','quarantined','rejected','pending_human_approval','dry_run','no_candidate')),
    rationale               TEXT NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
    created_at              TEXT NOT NULL DEFAULT ''
);
"""

# Env vars the apply module reads — scrubbed for isolation.
_APPLY_ENV = [
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT",
    "MO_APPLY_ENABLED", "MO_APPLY_DRY_RUN", "MO_APPLY_SCORER",
    "MO_APPLY_MODE",
    "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
    "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
    "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
    "MO_APPLY_FORCE_REGRESSION",
    "MO_AUTO_APPLY", "MO_AUTO_APPLY_MAX_TARGETS",
    "MO_APPLY_PROBE_MAX_TASKS", "MO_APPLY_PROBE_BUDGET_USD",
    "MO_APPLY_PROBE_TIMEOUT_S",
]


@pytest.fixture()
def envscrub(monkeypatch):
    for var in _APPLY_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture()
def db(tmp_path, envscrub):
    """A tmp sqlite DB with the minimal fixture schema; MINI_ORK_DB set."""
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE_DDL)
    con.commit()
    con.close()
    envscrub.setenv("MINI_ORK_DB", str(path))
    ap._SCHEMA_INIT = False
    yield str(path)
    ap._SCHEMA_INIT = False


def _rows(db_path, table):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        con.close()


def _seed_gradient(db_path, gradient_id="gr-1", target="agent.reviewer.prompt",
                   change="be more specific", confidence=0.42, task_class="reviewer",
                   signal="rubric"):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, target, signal, change, "[]", confidence, 100, task_class),
    )
    con.commit()
    con.close()


def _run(monkeypatch, db, tmp_path, *, after, pertask_json):
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer",
                   change="improve wording")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    probe_out = {"before": 0.0, "after": after, "n": 12,
                 "pertask_json": pertask_json, "ids": list(range(12)),
                 "runs": [], "cost_usd": 0.0}
    monkeypatch.setattr(probe_scorer, "probe_score",
                        lambda *a, **k: dict(probe_out))
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    # The probe scorer is stubbed, so the probe-set ceiling the pre-flight
    # checks is not the source of this test's vectors.
    monkeypatch.setenv("MO_APPLY_CERT_PREFLIGHT", "0")
    return target


def _summary(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.strip()]
    return json.loads(lines[-1])


def test_apply_run_certified_candidate_promotes(db, tmp_path, capsys,
                                                monkeypatch):
    target = _run(monkeypatch, db, tmp_path, after=7 / 12,
                  pertask_json=_V(7, 0))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = _summary(capsys)
    assert summary["decision"] == "promoted"
    rows = _rows(db, "promotion_certificates")
    assert len(rows) == 1
    assert rows[0]["task_class"] == "reviewer"
    assert rows[0]["candidate_id"] == summary["candidate_id"]
    assert rows[0]["status"] == "certified"


def test_apply_run_under_certified_quarantines(db, tmp_path, capsys,
                                               monkeypatch):
    target = _run(monkeypatch, db, tmp_path, after=0.5,
                  pertask_json=_V(6, 0))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = _summary(capsys)
    assert summary["decision"] == "quarantined"
    assert target.read_text() == "ORIGINAL PROMPT\n"
    rationale = _rows(db, "apply_attempts")[0]["rationale"]
    assert "certificate unreachable at this probe count" in rationale
    assert "need >= 7 clean gains" in rationale


# ── legibility: how far away is the bar? ────────────────────────────────────
# The review's finding A-1: at the shipped config the bar was 40 and the
# achievable e-value was 3.06, so the feature could only ever quarantine and
# said so without saying why. required_gains/max_e are the inverse of the
# threshold, and certify reports them.

@pytest.mark.parametrize("slot,want", [(1, 7), (2, 9), (10, 14),
                                       (100, 22), (1000, 31)])
def test_required_gains_grows_logarithmically(slot, want):
    assert pc.required_gains(slot, 0.05, 0.75, "spend") == want


def test_required_gains_is_exactly_enough():
    for slot in (1, 2, 10, 100):
        g = pc.required_gains(slot, 0.05, 0.75, "spend")
        alpha_k = pc.slot_alpha(0.05, slot, "spend")
        assert pc.betting_e_value(g, 0, 0.75) >= 1.0 / alpha_k
        if g > 1:
            assert pc.betting_e_value(g - 1, 0, 0.75) < 1.0 / alpha_k


def test_required_gains_ledger_none_does_not_tighten():
    """ledger='none' gives every test the whole alpha, so the bar is flat."""
    assert (pc.required_gains(1, 0.05, 0.75, "none")
            == pc.required_gains(1000, 0.05, 0.75, "none"))


def test_max_e_is_the_best_possible_split():
    assert pc.max_e(2, 0.75) == pytest.approx(3.0625)
    assert pc.max_e(0, 0.75) == 1.0
    assert pc.max_e(7, 0.75) == pytest.approx(pc.betting_e_value(7, 0, 0.75))


def test_certify_reports_unreachable_at_two_discordant_pairs(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        cert = _certify(con, "code_fix", gains=2, losses=0, n=12)
        assert cert["certified"] is False
        assert cert["status"] == "unreachable"
        assert cert["required_gains"] == 7
        assert pc.max_e(2, 0.75) < cert["threshold"]
    finally:
        con.close()


def test_certify_reports_uncertified_when_more_data_could_get_there(tmp_path):
    """8 discordant pairs could reach 1.75^8 = 88 > 40; 5 won, 3 lost, e < 1."""
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        before = [0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0]
        after = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
        cert = pc.certify(con, task_class="code_fix", candidate_id="c",
                          before=before, after=after, alpha_total=0.05,
                          bet=0.75, ledger="spend", now=1)
        assert cert["gains"] == 5 and cert["losses"] == 3
        assert cert["status"] == "uncertified"
        assert pc.max_e(8, 0.75) >= cert["threshold"]
    finally:
        con.close()


# ── the ledger is append-only and carries provenance ────────────────────────

def test_the_ledger_refuses_update_and_delete(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        _certify(con, "code_fix")
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE promotion_certificates SET gains=99")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM promotion_certificates")
        con.rollback()
        assert con.execute(
            "SELECT gains FROM promotion_certificates").fetchone()[0] == 7
    finally:
        con.close()


def test_certify_records_provenance(tmp_path):
    con = sqlite3.connect(tmp_path / "c.db")
    try:
        before = [0] * 12
        after = [1] * 7 + [0] * 5
        pc.certify(con, task_class="Code-Fix", candidate_id="cand-9",
                   before=before, after=after, alpha_total=0.05, bet=0.75,
                   ledger="spend", now=7, run_id="run-1", scorer="probe",
                   dry_run=True, probe_set_hash="abcd1234",
                   probe_ids=["p1", "p2"])
        con.commit()
        row = con.execute(
            "SELECT task_class, task_class_raw, run_id, scorer, dry_run, "
            "probe_set_hash, probe_ids, status FROM promotion_certificates"
        ).fetchone()
        assert row == ("Code_Fix", "Code-Fix", "run-1", "probe", 1,
                       "abcd1234", '["p1", "p2"]', "certified")
    finally:
        con.close()


# ── infra failures are not edit memory ─────────────────────────────────────

def _migrated_attempts_db(tmp_path) -> str:
    """The fixture schema plus the migration-0090 column."""
    path = str(tmp_path / "migrated.db")
    con = sqlite3.connect(path)
    con.executescript(FIXTURE_DDL)
    con.execute("ALTER TABLE apply_attempts ADD COLUMN reason_class TEXT")
    con.commit()
    con.close()
    return path


def _record(db_path, decision, reason_class):
    return ap.attempt_record(
        "reviewer", "prompt_file", "prompts/reviewer.md", "gradient_records",
        "gr-1", "", "", "", "", "", "", decision, "why", "0", "1",
        db=db_path, reason_class=reason_class)


def test_infra_failure_does_not_block_a_reproposal(tmp_path):
    p = _migrated_attempts_db(tmp_path)
    _record(p, "quarantined", "infra")
    assert ap._previously_failed(
        "reviewer", "prompts/reviewer.md", "gr-1", db=p) is False


def test_evidence_failure_still_blocks_a_reproposal(tmp_path):
    p = _migrated_attempts_db(tmp_path)
    _record(p, "quarantined", "evidence")
    assert ap._previously_failed(
        "reviewer", "prompts/reviewer.md", "gr-1", db=p) is True


def test_an_unclassified_row_still_blocks(tmp_path):
    """Pre-0090 rows (and anything not classified) keep today's semantics."""
    p = _migrated_attempts_db(tmp_path)
    _record(p, "rejected", None)
    assert ap._previously_failed(
        "reviewer", "prompts/reviewer.md", "gr-1", db=p) is True


def test_a_dead_harness_records_infra(db, tmp_path, capsys, monkeypatch):
    """Nothing measured → quarantined with reason_class='infra', so the sweep
    is not permanently silenced by one broken probe launch."""
    target = tmp_path / "prompts" / "reviewer.md"
    target.parent.mkdir(parents=True)
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_PERTASK_JSON", "")
    monkeypatch.setattr(ap, "_previously_failed", ap._previously_failed)
    # No probe set under the recipe dir → the scorer reports n=0.
    monkeypatch.setattr(probe_scorer, "probe_score",
                        lambda *a, **k: {"before": 0.0, "after": 0.0,
                                         "n": 0, "pertask_json": "",
                                         "runs": [], "cost_usd": 0.0,
                                         "control_n": 1})
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE apply_attempts ADD COLUMN reason_class TEXT")
    con.commit()
    con.close()
    _seed_gradient(db, target="prompts/reviewer.md")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    rows = _rows(db, "apply_attempts")
    assert rows[0]["decision"] == "quarantined"
    assert rows[0]["reason_class"] == "infra"
    assert ap._previously_failed(
        "reviewer", "prompts/reviewer.md", "gr-1", db=db) is False


# ── the evidence survives the verdict (Win 5c) ─────────────────────────────

def test_a_precert_quarantine_still_records_the_certificate(tmp_path,
                                                            monkeypatch):
    """A slot-consuming test must be visible even when it changed nothing.

    ``apply_attempts`` stores the rationale, not the cert blob (A-15/E-2), so
    the numbers have to travel in the rationale or the alpha spend is invisible
    to every later reader.
    """
    p = str(tmp_path / "q.db")
    out = _gate(monkeypatch, 0.5, 0.5, _V(6, 0), db=p)
    assert out["decision"] == "quarantined"
    assert "no measured improvement" in out["rationale"]
    assert "certificate e=" in out["rationale"]
    assert "verdict unchanged: quarantined" in out["rationale"]
    assert out["cert"]["gains"] == 6
    assert _rows(p, "promotion_certificates")[0]["slot"] == 1


def test_apply_attempts_carries_the_certificate_line(db, tmp_path, monkeypatch):
    """The end-to-end version: the audit row, not just the JSON result."""
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    monkeypatch.setenv("MO_APPLY_CERT_PREFLIGHT", "0")
    # Equal arms: the point-estimate gate quarantines BEFORE the certificate
    # runs, which is exactly the path whose alpha spend used to vanish.
    monkeypatch.setattr(probe_scorer, "probe_score", lambda *a, **k: {
        "before": 0.5, "after": 0.5, "n": 12, "pertask_json": _V(6, 0),
        "ids": list(range(12)), "runs": [], "cost_usd": 0.0})
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    rationale = _rows(db, "apply_attempts")[0]["rationale"]
    assert "certificate e=" in rationale
    assert "verdict unchanged: quarantined" in rationale


def test_preflight_refuses_before_any_probe_launch(db, tmp_path, monkeypatch):
    """Exit 2, zero probe calls, zero apply_attempts rows (Win 2)."""
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "probe")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    # A root with no recipes/ → no probe set at all → 0 gate probes.
    monkeypatch.setattr(probe_scorer, "_ROOT", str(tmp_path / "no-recipes"),
                        raising=False)
    launches = []
    monkeypatch.setattr(probe_scorer, "probe_score",
                        lambda *a, **k: launches.append(1) or {})
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 2
    assert launches == []
    assert _rows(db, "apply_attempts") == []


def test_a_dry_run_reports_the_preflight_but_does_not_refuse(
        db, tmp_path, monkeypatch):
    """A dry run spends nothing, so it stays a diagnostic (rc 0)."""
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    monkeypatch.setenv("MO_APPLY_SCORER", "mock")
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_APPLY_DRY_RUN", "1")
    monkeypatch.setenv("MO_APPLY_CERTIFICATE", "1")
    # The mock scorer never probes, so the pre-flight is not even consulted.
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0


# ── slot allocation under real concurrency (Win 5d) ────────────────────────

def test_two_connections_never_share_a_slot(tmp_path):
    """IMMEDIATE + retry must make slot allocation safe off one thread."""
    import threading

    path = str(tmp_path / "race.db")
    con = sqlite3.connect(path, timeout=30)
    pc.ensure_table(con)
    con.close()

    barrier = threading.Barrier(2)
    slots: list[int] = []
    errors: list[BaseException] = []

    def work(candidate: str) -> None:
        c = sqlite3.connect(path, timeout=30)
        try:
            barrier.wait(timeout=10)
            out = pc.certify(c, task_class="code_fix", candidate_id=candidate,
                             before=[0] * 12, after=[1] * 7 + [0] * 5,
                             alpha_total=0.05, bet=0.75, ledger="spend", now=1)
            slots.append(out["slot"])
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=work, args=(f"c{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert sorted(slots) == [1, 2]
    assert [r["slot"] for r in _rows(path, "promotion_certificates")] == [1, 2]
