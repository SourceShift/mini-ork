"""Contract tests for the metric-anchor audit (H5).

Hermetic: no DB, no lane, no network, no real run. The editable install
resolves ``mini_ork`` to main, so insert the repo root on ``sys.path`` before
importing.
"""
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.learning import metric_anchor as ma


def test_anchor_contaminated():
    assert ma.anchor_contaminated({"scored": ["a", "b"], "held_out": ["b", "c"]}) is True
    assert ma.anchor_contaminated({"scored": ["a"], "held_out": ["b"]}) is False
    assert ma.anchor_contaminated({"scored": ["a"]}) is None
    assert ma.anchor_contaminated({"held_out": ["b"]}) is None


def test_under_anchored():
    assert ma.under_anchored({"held_out": list("abcdefghi")}) is True    # 9 < 10
    assert ma.under_anchored({"held_out": list("abcdefghij")}) is False  # 10
    assert ma.under_anchored({}) is None


def test_agreement():
    assert ma.agreement({"agreements": []}) is None
    assert ma.agreement({}) is None
    assert ma.agreement({"agreements": [True, False]}) == 0.5


def test_discrimination():
    assert ma.discrimination({"agreements": [True, True, True]}) == 0.0
    assert ma.discrimination({"agreements": [True, False]}) == 0.5
    assert ma.discrimination({"agreements": []}) is None


def test_sign_test_p():
    assert ma._sign_test_p(5, 0) == pytest.approx(2 * 0.5 ** 5)
    assert ma._sign_test_p(0, 0) is None
    assert ma._sign_test_p(2, 2) == pytest.approx(1.0)


def test_anchor_audit_unmeasured():
    history = [
        {"gen": 0, "scored": ["a"]},
        {"gen": 1, "held_out": ["b"]},
        {"gen": 2},
    ]
    r = ma.anchor_audit(history)
    assert r["n"] == 0
    assert r["rate_contaminated"] is None
    assert r["contaminated"] is False


def test_anchor_audit_contaminated():
    history = [
        {"gen": 0, "scored": ["x"], "held_out": list("abcdefghij")},
        {"gen": 1, "scored": ["y"], "held_out": list("abcdefghij")},
        {"gen": 2, "scored": ["a"], "held_out": list("abcdefghij")},  # reads its anchor
    ]
    r = ma.anchor_audit(history)
    assert r["n"] == 3
    assert r["n_contaminated"] == 1
    assert r["contaminated"] is True


def test_vacuity_vacuous():
    vacuous = [
        {"gen": i, "agreements": [True, True]} for i in range(ma.MIN_GENERATIONS)
    ]
    assert ma.vacuity(vacuous)["vacuous"] is True

    healthy = [
        {"gen": i, "agreements": [True, False]} for i in range(ma.MIN_GENERATIONS)
    ]
    assert ma.vacuity(healthy)["vacuous"] is False


def test_vacuity_short():
    short = [
        {"gen": i, "agreements": [True, False]} for i in range(ma.MIN_GENERATIONS - 1)
    ]
    r = ma.vacuity(short)
    assert r["delta"] is None
    assert r["latest"] is None
    assert r["p"] is None


def test_intact_none_on_unauditable():
    assert ma.intact([]) is None
    rows_without_anchor = [
        {"gen": i, "agreements": [True, False]} for i in range(ma.MIN_GENERATIONS)
    ]
    assert ma.intact(rows_without_anchor) is None


def test_intact_true_and_false():
    clean = [
        {"gen": 0, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 1, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 2, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 3, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
    ]
    assert ma.intact(clean) is True

    contaminated = [dict(r) for r in clean]
    contaminated[0]["scored"] = ["a"]  # reads a held-out id
    assert ma.intact(contaminated) is False

    vacuous = [
        {"gen": i, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, True]}
        for i in range(ma.MIN_GENERATIONS)
    ]
    assert ma.intact(vacuous) is False

    under = [
        {"gen": i, "scored": ["x"], "held_out": list("abcdefghi"),
         "agreements": [True, False]}
        for i in range(ma.MIN_GENERATIONS)
    ]
    assert ma.intact(under) is None  # under-anchored: undecided, never True


def test_audit_undecided_and_clean():
    r = ma.audit([])
    assert r["intact"] is None
    assert "insufficient_history" in r["undecided"]

    clean = [
        {"gen": i, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]}
        for i in range(ma.MIN_GENERATIONS)
    ]
    r2 = ma.audit(clean)
    assert r2["intact"] is True
    assert r2["undecided"] == []


def test_cli_main(tmp_path):
    from mini_ork.cli import metric_anchor as cli

    fixture = tmp_path / "history.json"
    fixture.write_text(json.dumps([
        {"gen": 0, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 1, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 2, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
        {"gen": 3, "scored": ["x"], "held_out": list("abcdefghij"),
         "agreements": [True, False]},
    ]))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main([str(fixture), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "intact" in payload

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = cli.main([str(tmp_path / "nope.json"), "--json"])
    assert rc2 == 0
    assert buf2.getvalue() == ""
