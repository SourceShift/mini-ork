"""The IDE's HTTP read surface answers exactly what the CLI answers.

``web/routes/ide.py`` exists so the desktop IDE can read the board without a
Python interpreter per read. It is only worth having if it is the *same* read:
any drift between the two paths is a panel that shows one thing over loopback
and another over a subprocess, which is the silent divergence this repo refuses
elsewhere. These tests pin the equivalence for every verb the IDE drives over
HTTP.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig
from mini_ork.web.routes import ide

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _via_cli(home: Path, argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = board_cmd.main(argv + ["--home", str(home)], "")
    return rc, json.loads(out.getvalue())


def _via_http(home: Path, argv: list[str]) -> dict:
    return json.loads(ide._read(home, argv).body)


def _without_clock(payload: dict) -> dict:
    """`generated_at` is the moment of the read, so two reads of the same board
    differ in it by construction — the one key an equivalence check must not
    compare."""
    return {k: v for k, v in payload.items() if k != "generated_at"}


@pytest.mark.parametrize("argv", [
    ["show", "--json", "--shell"],
    ["show", "--json"],
    ["page", "inbox"],
    ["runs", "--json", "--offset", "0", "--limit", "5"],
])
def test_http_read_matches_the_cli(home: Path, argv: list[str]) -> None:
    _, expected = _via_cli(home, argv)
    assert _without_clock(_via_http(home, argv)) == _without_clock(expected)


@pytest.mark.parametrize("argv", [
    ["node", "no-such-run", "no-such-node"],
    ["node", "no-such-run", "no-such-node", "--view", "stream", "--offset", "3"],
])
def test_http_node_read_matches_the_cli_including_the_failure_shape(
    home: Path, argv: list[str],
) -> None:
    """A node that cannot be read reports `ok: false` — the IDE branches on it,
    so the two paths must agree on the failure too, not just on success."""
    _, expected = _via_cli(home, argv)
    assert expected.get("ok") is False
    assert _via_http(home, argv) == expected


def test_validate_reports_the_same_usage_error_main_prints(
    home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """`validate` and `main` share one message: a request over loopback must
    not be held to a weaker contract than the same read over a subprocess."""
    assert board_cmd.main(["page", "--home", str(home)], "") == 2
    printed = capsys.readouterr().err.strip()
    args = board_cmd.build_parser().parse_args(["page"])
    assert board_cmd.validate(args) == printed


def test_payload_for_verb_rejects_a_bad_argv(home: Path) -> None:
    with pytest.raises(ValueError):
        board_cmd.payload_for_verb(home, ["page"])


def test_route_reports_a_bad_argv_as_a_client_error(home: Path) -> None:
    """A 400, not a 500 and not a silently empty 200: the caller asked for
    something this surface cannot serve."""
    with pytest.raises(HTTPException) as caught:
        ide._read(home, ["page"])
    assert caught.value.status_code == 400
