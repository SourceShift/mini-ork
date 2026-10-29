"""The sealed acceptor's state: secret, epochs, looks, canary.

Every test is hermetic (``tmp_path``) and every failure path is asserted to
raise rather than to guess: a silently reset look counter is how a gate would
spend its whole error budget without anyone noticing.
"""
from __future__ import annotations

import json
import re

import pytest

import mini_ork.dispatch.vault as vault_mod
from mini_ork.rsi.impact_state import (
    DEFAULT_SECRET_NAME,
    STATE_VERSION,
    ImpactStateError,
    bench_state,
    canary_scores,
    corpus_hash,
    impact_dir,
    load_secret,
    load_state,
    new_state,
    record_canary,
    record_look,
    rotate,
    save_state,
    state_path,
)


# --------------------------------------------------------------------------
# load_secret — simple tier
# --------------------------------------------------------------------------


def test_first_call_creates_a_0600_secret_of_64_hex_chars(tmp_path):
    value = load_secret(tmp_path)
    path = impact_dir(tmp_path) / "secret"
    text = path.read_text(encoding="utf-8")
    assert isinstance(value, bytes)
    assert re.fullmatch(r"[0-9a-f]{64}", text)
    assert value == text.encode()
    assert path.stat().st_mode & 0o777 == 0o600
    assert impact_dir(tmp_path).stat().st_mode & 0o777 == 0o700


def test_a_second_call_returns_the_same_secret(tmp_path):
    assert load_secret(tmp_path) == load_secret(tmp_path)


def test_a_group_readable_secret_is_refused(tmp_path):
    load_secret(tmp_path)
    path = impact_dir(tmp_path) / "secret"
    path.chmod(0o644)
    with pytest.raises(ImpactStateError, match="permissions too open"):
        load_secret(tmp_path)


def test_an_empty_secret_file_is_refused(tmp_path):
    directory = impact_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "secret"
    path.write_text("   \n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ImpactStateError, match="empty secret"):
        load_secret(tmp_path)


# --------------------------------------------------------------------------
# load_secret — secure tier
# --------------------------------------------------------------------------


def test_secure_tier_reads_the_stripped_vault_value(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_mod, "vault_environment",
                        lambda environ=None: {DEFAULT_SECRET_NAME: " s3cret "})
    assert load_secret(tmp_path, secure_store="vault://t") == b"s3cret"
    assert not impact_dir(tmp_path).exists()


def test_secure_tier_missing_secret_is_an_error_that_names_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_mod, "vault_environment", lambda environ=None: {})
    with pytest.raises(ImpactStateError) as excinfo:
        load_secret(tmp_path, secure_store="vault://mini-ork/test")
    message = str(excinfo.value)
    assert "secure tier" in message
    assert "vault://mini-ork/test" in message
    assert "s3cret" not in message
    assert not (impact_dir(tmp_path) / "secret").exists()


def test_secure_tier_wraps_a_vault_exception(tmp_path, monkeypatch):
    def boom(environ=None):
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(vault_mod, "vault_environment", boom)
    with pytest.raises(ImpactStateError, match="secure tier"):
        load_secret(tmp_path, secure_store="vault://t")


# --------------------------------------------------------------------------
# corpus_hash
# --------------------------------------------------------------------------


def test_corpus_hash_is_order_independent():
    assert corpus_hash({"a": "1", "b": "2"}) == corpus_hash({"b": "2", "a": "1"})


def test_corpus_hash_changes_when_a_stratum_changes():
    assert corpus_hash({"a": "1"}) != corpus_hash({"a": "2"})


def test_corpus_hash_is_16_hex_chars():
    value = corpus_hash({"a": "1"})
    assert len(value) == 16
    assert re.fullmatch(r"[0-9a-f]+", value)


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


def test_fresh_home_loads_the_new_state(tmp_path):
    assert load_state(tmp_path) == new_state()


def test_save_then_load_round_trips_and_is_0600(tmp_path):
    state = new_state()
    bench_state(state, "bench", "corpus-1")
    assert save_state(tmp_path, state) == state_path(tmp_path)
    assert state_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert load_state(tmp_path) == state


@pytest.mark.parametrize("payload", [
    "not json {{{",
    json.dumps({"version": 2, "benchmarks": {}}),
    json.dumps({"version": 1, "benchmarks": []}),
])
def test_a_corrupt_state_file_fails_closed(tmp_path, payload):
    directory = impact_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    state_path(tmp_path).write_text(payload, encoding="utf-8")
    with pytest.raises(ImpactStateError) as excinfo:
        load_state(tmp_path)
    assert str(state_path(tmp_path)) in str(excinfo.value)


# --------------------------------------------------------------------------
# Per-benchmark entries
# --------------------------------------------------------------------------


def test_bench_state_starts_at_epoch_one():
    state = new_state()
    assert bench_state(state, "b", "c1")["epoch"] == 1


def test_same_corpus_leaves_the_counters_alone():
    state = new_state()
    bench_state(state, "b", "c1")
    record_look(state, "b", accepted=True)
    record_look(state, "b", accepted=True)
    entry = bench_state(state, "b", "c1")
    assert entry["epoch"] == 1
    assert entry["looks"] == 2


def test_a_new_corpus_rotates_the_epoch_zeroes_the_counters_and_keeps_canary():
    state = new_state()
    bench_state(state, "b", "c1")
    record_look(state, "b", accepted=True)
    record_look(state, "b", accepted=False)
    record_canary(state, "b", sha="deadbeef", score=0.5, at=11)
    entry = bench_state(state, "b", "c2")
    assert entry["epoch"] == 2
    assert entry["corpus"] == "c2"
    assert (entry["looks"], entry["accepted"], entry["rejected"]) == (0, 0, 0)
    assert entry["canary"] == [{"sha": "deadbeef", "score": 0.5, "at": 11}]


def test_record_look_counts_and_returns_the_new_looks():
    state = new_state()
    bench_state(state, "b", "c")
    assert record_look(state, "b", accepted=True) == 1
    assert record_look(state, "b", accepted=False) == 2
    assert state["benchmarks"]["b"]["accepted"] == 1
    assert state["benchmarks"]["b"]["rejected"] == 1


def test_rotate_bumps_the_epoch_and_zeroes_the_counters():
    state = new_state()
    bench_state(state, "b", "c")
    record_look(state, "b", accepted=True)
    entry = rotate(state, "b")
    assert entry["epoch"] == 2
    assert (entry["looks"], entry["accepted"], entry["rejected"]) == (0, 0, 0)


def test_rotate_an_unknown_benchmark_raises():
    with pytest.raises(ImpactStateError, match="no impact state for ghost"):
        rotate(new_state(), "ghost")


def test_record_look_on_an_unknown_benchmark_raises():
    with pytest.raises(ImpactStateError):
        record_look(new_state(), "ghost", accepted=True)


def test_record_canary_replaces_the_same_sha_and_appends_a_new_one():
    state = new_state()
    bench_state(state, "b", "c")
    record_canary(state, "b", sha="a", score=0.1, at=1)
    record_canary(state, "b", sha="a", score=0.2, at=2)
    record_canary(state, "b", sha="b", score=0.3, at=3)
    assert state["benchmarks"]["b"]["canary"] == [
        {"sha": "a", "score": 0.2, "at": 2},
        {"sha": "b", "score": 0.3, "at": 3},
    ]


def test_record_canary_keep_trims_to_the_last_entries():
    state = new_state()
    bench_state(state, "b", "c")
    for i in range(5):
        record_canary(state, "b", sha=f"s{i}", score=i / 10, at=i, keep=3)
    assert [e["sha"] for e in state["benchmarks"]["b"]["canary"]] == ["s2", "s3", "s4"]


def test_record_canary_on_an_unknown_benchmark_raises():
    with pytest.raises(ImpactStateError):
        record_canary(new_state(), "ghost", sha="s", score=1.0, at=1)


def test_canary_scores_are_returned_in_order():
    state = new_state()
    bench_state(state, "b", "c")
    record_canary(state, "b", sha="a", score=0.1, at=1)
    record_canary(state, "b", sha="b", score=0.25, at=2)
    assert canary_scores(state, "b") == [0.1, 0.25]


def test_canary_scores_are_empty_for_an_absent_benchmark():
    assert canary_scores(new_state(), "ghost") == []


def test_state_version_is_one():
    assert STATE_VERSION == 1
