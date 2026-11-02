"""Unit tests for the ``mini-ork instruments`` subcommand.

The instrument implementations are read-only in this change, so where a probe
needs a controlled outcome they are stubbed at their home modules. What is
under test is the CLI's wiring: exit codes, the ``--db`` resolution chain, the
exact record paths consumers already read, and fail-closed behaviour. The
persistence helpers (``write_latest`` / ``write_score``) are left real, so the
path and atomicity contracts are exercised rather than bypassed.
"""

from __future__ import annotations

import json
import os

from mini_ork.gates import oracle_integrity, verifier_mutation

PASSING_AUDIT = {
    "pass": True,
    "detected": ["frozen_result_swap"],
    "missed": [],
    "controls_ok": ["known_good_proven"],
    "controls_failed": [],
    "judge": "oracle.judge",
    "audited_at": 1,
}

MEASURED_ADMITTED = {
    "measured": True,
    "kill_score": 1.0,
    "killed": 3,
    "survived": 0,
    "abstained": 0,
    "total": 3,
    "mutations": [],
}
MEASURED_NOT_ADMITTED = dict(MEASURED_ADMITTED, kill_score=0.0)
# The shape ``score`` returned before the empty-denominator fix: the loop ran,
# so it said "measured", but no mutation applied and there is no score. The CLI
# must not believe the flag on its own — ``admit(None)`` is True.
MEASURED_NO_SCORE = dict(MEASURED_ADMITTED, kill_score=None, killed=0,
                         survived=0, abstained=0, total=0)
UNMEASURED = {
    "measured": False,
    "kill_score": None,
    "reason": "baseline not proven (status=missing)",
    "killed": 0,
    "survived": 0,
    "abstained": 0,
    "total": 0,
    "mutations": [],
}


def _cli(argv):
    """Run the CLI in-process; a ``SystemExit`` counts as its exit code."""
    from mini_ork.cli import instruments

    try:
        return instruments.main(list(argv))
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)


def _clear_db_env(monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)


def _stub_audit(monkeypatch, result=None, error=None):
    calls = []

    def fake_run_default_audit(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(oracle_integrity, "run_default_audit", fake_run_default_audit)
    return calls


def _stub_score(monkeypatch, result):
    calls = []

    def fake_score(observable, **kwargs):
        calls.append(observable)
        return result

    monkeypatch.setattr(verifier_mutation, "score", fake_score)
    return calls


# ── oracle-integrity ─────────────────────────────────────────────────────────


def test_oracle_integrity_pass_writes_latest_and_exits_zero(tmp_path, monkeypatch,
                                                            capsys):
    _clear_db_env(monkeypatch)
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))
    db = str(tmp_path / "state.db")

    assert _cli(["oracle-integrity", "--db", db]) == 0

    latest = tmp_path / "oracle-integrity" / "latest.json"
    assert latest.is_file()
    with open(str(latest), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["pass"] is True
    assert record["missed"] == []
    assert record["controls_failed"] == []
    assert "oracle-integrity" in capsys.readouterr().out


def test_oracle_integrity_fail_exits_one_and_still_records(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    _stub_audit(
        monkeypatch,
        result={"pass": False, "missed": ["frozen_result_swap"],
                "controls_failed": []},
    )
    db = str(tmp_path / "state.db")

    assert _cli(["oracle-integrity", "--db", db]) == 1

    with open(str(tmp_path / "oracle-integrity" / "latest.json"),
              encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["pass"] is False
    assert record["missed"] == ["frozen_result_swap"]


def test_oracle_integrity_failing_record_warns_that_promotion_stops(
        tmp_path, monkeypatch, capsys):
    """A diagnostic that arms the quarantine must say so, not do it silently."""
    _clear_db_env(monkeypatch)
    monkeypatch.delenv("MO_ORACLE_INTEGRITY", raising=False)
    _stub_audit(monkeypatch,
                result={"pass": False, "missed": ["stale_evidence_replay"],
                        "controls_failed": []})

    assert _cli(["oracle-integrity",
                 "--db", str(tmp_path / "state.db")]) == 1

    err = capsys.readouterr().err
    assert "FAILING record" in err
    assert "stale_evidence_replay" in err
    assert "MO_ORACLE_INTEGRITY=0" in err


def test_oracle_integrity_failing_record_is_quiet_when_consumer_off(
        tmp_path, monkeypatch, capsys):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("MO_ORACLE_INTEGRITY", "0")
    _stub_audit(monkeypatch,
                result={"pass": False, "missed": ["stale_evidence_replay"],
                        "controls_failed": []})

    assert _cli(["oracle-integrity",
                 "--db", str(tmp_path / "state.db")]) == 1

    assert "FAILING record" not in capsys.readouterr().err


def test_oracle_integrity_passing_record_does_not_warn(tmp_path, monkeypatch,
                                                       capsys):
    _clear_db_env(monkeypatch)
    monkeypatch.delenv("MO_ORACLE_INTEGRITY", raising=False)
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))

    assert _cli(["oracle-integrity",
                 "--db", str(tmp_path / "state.db")]) == 0

    assert "FAILING record" not in capsys.readouterr().err


def test_oracle_integrity_runtime_error_exits_two_writes_nothing(
        tmp_path, monkeypatch, capsys):
    _clear_db_env(monkeypatch)
    _stub_audit(monkeypatch, error=RuntimeError("judge unavailable"))
    db = str(tmp_path / "state.db")

    assert _cli(["oracle-integrity", "--db", db]) == 2

    assert "judge unavailable" in capsys.readouterr().err
    assert not (tmp_path / "oracle-integrity" / "latest.json").exists()


def test_oracle_integrity_write_error_exits_two(tmp_path, monkeypatch, capsys):
    """A record that cannot be written is exit 2, never a silent success."""
    _clear_db_env(monkeypatch)
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))
    # A file where the record directory should be: makedirs must fail.
    (tmp_path / "oracle-integrity").write_text("not a directory", encoding="utf-8")

    assert _cli(["oracle-integrity", "--db", str(tmp_path / "state.db")]) == 2

    assert capsys.readouterr().err.strip()


def test_db_default_resolves_db_then_home_then_process_default(tmp_path,
                                                               monkeypatch):
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))

    # 1. MINI_ORK_DB wins when set.
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "envdb" / "state.db"))
    assert _cli(["oracle-integrity"]) == 0
    assert (tmp_path / "envdb" / "oracle-integrity" / "latest.json").is_file()

    # 2. Without MINI_ORK_DB: $MINI_ORK_HOME/state.db.
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    assert _cli(["oracle-integrity"]) == 0
    assert (home / "oracle-integrity" / "latest.json").is_file()

    # 3. Neither: the process-default home relative to the cwd.
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.chdir(str(tmp_path))
    assert _cli(["oracle-integrity"]) == 0
    assert (tmp_path / ".mini-ork" / "oracle-integrity" / "latest.json").is_file()


def test_explicit_db_flag_overrides_the_default_chain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))

    assert _cli(
        ["oracle-integrity", "--db=" + str(tmp_path / "flagged" / "state.db")]
    ) == 0

    assert (tmp_path / "flagged" / "oracle-integrity" / "latest.json").is_file()
    assert not (home / "oracle-integrity").exists()


def test_oracle_integrity_second_run_rewrites_atomically(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    db = str(tmp_path / "state.db")
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT, audited_at=1))
    assert _cli(["oracle-integrity", "--db", db]) == 0

    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT, audited_at=2))
    assert _cli(["oracle-integrity", "--db", db]) == 0

    with open(str(tmp_path / "oracle-integrity" / "latest.json"),
              encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["audited_at"] == 2
    assert not (tmp_path / "oracle-integrity" / "latest.json.tmp").exists()


def test_oracle_integrity_json_is_the_only_stdout(tmp_path, monkeypatch, capsys):
    _clear_db_env(monkeypatch)
    _stub_audit(monkeypatch, result=dict(PASSING_AUDIT))
    db = str(tmp_path / "state.db")

    assert _cli(["oracle-integrity", "--db", db, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["pass"] is True
    assert payload["path"].endswith("oracle-integrity/latest.json")
    assert "oracle-integrity" in payload["path"]


# ── verifier-mutation ────────────────────────────────────────────────────────


def _observable(tmp_path, payload=None):
    path = tmp_path / "obs.json"
    path.write_text(json.dumps(payload or {"expect_body": {}}), encoding="utf-8")
    return path


def test_verifier_mutation_measured_admitted_exits_zero_writes_nothing(
        tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    calls = _stub_score(monkeypatch, MEASURED_ADMITTED)
    obs = _observable(tmp_path)
    before = sorted(os.listdir(str(tmp_path)))

    assert _cli(["verifier-mutation", "--observable", str(obs)]) == 0

    assert calls and calls[0] == {"expect_body": {}}
    assert sorted(os.listdir(str(tmp_path))) == before


def test_verifier_mutation_measured_not_admitted_exits_one(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    _stub_score(monkeypatch, MEASURED_NOT_ADMITTED)

    assert _cli(["verifier-mutation", "--observable",
                 str(_observable(tmp_path))]) == 1


def test_verifier_mutation_threshold_env_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_VERIFIER_MUTATION_MIN", "0.95")
    _stub_score(monkeypatch, dict(MEASURED_ADMITTED, kill_score=0.9))

    assert _cli(["verifier-mutation", "--observable",
                 str(_observable(tmp_path))]) == 1


def test_verifier_mutation_unmeasured_exits_two_and_writes_no_sidecar(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    _stub_score(monkeypatch, UNMEASURED)
    obs = _observable(tmp_path)
    card = tmp_path / "card.json"

    assert _cli(["verifier-mutation", "--observable", str(obs),
                 "--card", str(card)]) == 2

    assert not (tmp_path / "card.json.mutation.json").exists()
    assert "not measured" in capsys.readouterr().err


def test_verifier_mutation_measured_flag_with_no_score_is_not_admitted(
        tmp_path, monkeypatch, capsys):
    """``measured: True`` with a null score is unmeasured, not an admitted pass.

    Writing that sidecar would replace a real earlier measurement with a null,
    and ``admit(None)`` is True, so a card that once scored badly would come
    back permanently unblockable.
    """
    _stub_score(monkeypatch, MEASURED_NO_SCORE)
    obs = _observable(tmp_path)
    card = tmp_path / "card.json"

    assert _cli(["verifier-mutation", "--observable", str(obs),
                 "--card", str(card)]) == 2

    assert not (tmp_path / "card.json.mutation.json").exists()
    captured = capsys.readouterr()
    assert "admitted" not in captured.out
    assert "not measured" in captured.err
    assert "no mutation applied" in captured.out


def test_verifier_mutation_measured_no_score_reports_a_reason(
        tmp_path, monkeypatch, capsys):
    """The unmeasured path names why, instead of printing an empty field."""
    _stub_score(monkeypatch, dict(MEASURED_NO_SCORE, reason=""))
    obs = _observable(tmp_path)

    assert _cli(["verifier-mutation", "--observable", str(obs)]) == 2

    captured = capsys.readouterr()
    assert "reason: no mutation applied" in captured.out


def test_verifier_mutation_missing_observable_exits_two(tmp_path, monkeypatch,
                                                        capsys):
    calls = _stub_score(monkeypatch, MEASURED_ADMITTED)

    assert _cli(["verifier-mutation", "--observable",
                 str(tmp_path / "nope.json")]) == 2

    assert not calls
    assert capsys.readouterr().err.strip()


def test_verifier_mutation_invalid_json_exits_two(tmp_path, monkeypatch, capsys):
    calls = _stub_score(monkeypatch, MEASURED_ADMITTED)
    obs = tmp_path / "broken.json"
    obs.write_text("{'definitely not json'", encoding="utf-8")

    assert _cli(["verifier-mutation", "--observable", str(obs)]) == 2

    assert not calls
    assert capsys.readouterr().err.strip()


def test_verifier_mutation_card_writes_sidecar_and_prints_path(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    _stub_score(monkeypatch, MEASURED_ADMITTED)
    obs = _observable(tmp_path)
    card = tmp_path / "card.json"

    assert _cli(["verifier-mutation", "--observable", str(obs),
                 "--card", str(card)]) == 0

    sidecar = tmp_path / "card.json.mutation.json"
    assert sidecar.is_file()
    with open(str(sidecar), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["kill_score"] == 1.0
    assert "card.json.mutation.json" in capsys.readouterr().out


def test_verifier_mutation_json_reports_measurement_and_admission(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    _stub_score(monkeypatch, MEASURED_ADMITTED)

    assert _cli(["verifier-mutation", "--observable",
                 str(_observable(tmp_path)), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["measured"] is True
    assert payload["admitted"] is True
    assert payload["kill_score"] == 1.0


def test_verifier_mutation_requires_observable(monkeypatch, capsys):
    calls = _stub_score(monkeypatch, MEASURED_ADMITTED)

    assert _cli(["verifier-mutation"]) == 2

    assert not calls
    assert "--observable" in capsys.readouterr().err


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_help_and_unknown_subcommand():
    from mini_ork.cli import instruments

    assert instruments.main(["--help"]) == 0
    assert instruments.main([]) == 2
    assert instruments.main(["nope"]) == 2
    assert instruments.main(["oracle-integrity", "--bogus"]) == 2


def test_readonly_producers_keep_their_public_surface():
    for name in ("run_default_audit", "write_latest", "audit"):
        assert callable(getattr(oracle_integrity, name, None)), name
    for name in ("score", "admit", "write_score", "read_score"):
        assert callable(getattr(verifier_mutation, name, None)), name
