"""Unit tests: mini_ork.cli.serve (bash parity halves removed; formerly vs bin/mini-ork-serve).

The uvicorn launch itself can't be unit-tested (it binds a port and blocks),
but every path before the exec — --help, unknown-flag, and the missing-state.db
preflight — is asserted semantically. The composed uvicorn argv is asserted
structurally.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import serve as srv


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


def test_help():
    out, rc = _py(["--help"])
    assert rc == 0
    assert "Usage: mini-ork serve" in out
    assert "--port N" in out


def test_unknown_flag():
    out, rc = _py(["--bogus"])
    assert rc == 2
    assert "Usage: mini-ork serve" in out  # usage printed to stdout after the error


def test_missing_db_preflight(tmp_path):
    empty_home = tmp_path / ".mini-ork"; empty_home.mkdir()
    _, rc = _py([], home=empty_home)
    assert rc == 1  # no state.db → exit 1


def test_uvicorn_argv_shape():
    argv = srv.uvicorn_argv("0.0.0.0", "7100", "--reload")
    assert argv[1:4] == ["-m", "uvicorn", "mini_ork.web.app:app"]
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "7100"
    assert argv[-1] == "--reload"
    # no --reload → flag absent
    assert "--reload" not in srv.uvicorn_argv("127.0.0.1", "7090", "")
