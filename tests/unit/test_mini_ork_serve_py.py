"""Parity gate: mini_ork.ported.mini_ork_serve vs bin/mini-ork-serve.

The uvicorn launch itself can't be parity-tested (it binds a port and blocks),
but every path before the exec — --help, unknown-flag, and the missing-state.db
preflight — is compared against the LIVE bash entrypoint via subprocess. The
composed uvicorn argv is asserted structurally.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_serve as srv  # noqa: E402

BIN = REPO / "bin" / "mini-ork-serve"


def _bash(args, home=None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO)}
    if home is not None:
        env["MINI_ORK_HOME"] = str(home)
    else:
        env.pop("MINI_ORK_HOME", None)
    r = subprocess.run(["bash", str(BIN), *args], capture_output=True, text=True, env=env)
    return r.stdout, r.stderr, r.returncode


def _py(args, home=None):
    old = dict(os.environ)
    if home is not None:
        os.environ["MINI_ORK_HOME"] = str(home)
    else:
        os.environ.pop("MINI_ORK_HOME", None)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = srv.main(list(args), root=str(REPO), _exec=False)
    finally:
        os.environ.clear(); os.environ.update(old)
    return buf.getvalue(), rc


def test_help_parity():
    out_b, _, rc_b = _bash(["--help"])
    out_p, rc_p = _py(["--help"])
    assert rc_b == rc_p == 0
    assert out_b == out_p


def test_unknown_flag_parity():
    out_b, err_b, rc_b = _bash(["--bogus"])
    out_p, rc_p = _py(["--bogus"])
    assert rc_b == rc_p == 2
    assert out_b == out_p  # both print usage to stdout after the error


def test_missing_db_preflight_parity(tmp_path):
    empty_home = tmp_path / ".mini-ork"; empty_home.mkdir()
    _, err_b, rc_b = _bash([], home=empty_home)
    _, rc_p = _py([], home=empty_home)
    assert rc_b == rc_p == 1  # no state.db → exit 1 on both


def test_uvicorn_argv_shape():
    argv = srv.uvicorn_argv("0.0.0.0", "7100", "--reload")
    assert argv[1:4] == ["-m", "uvicorn", "mini_ork.web.app:app"]
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "7100"
    assert argv[-1] == "--reload"
    # no --reload → flag absent
    assert "--reload" not in srv.uvicorn_argv("127.0.0.1", "7090", "")
