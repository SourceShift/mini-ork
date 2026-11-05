"""The ``bin/mini-ork-*`` compatibility launchers must survive PYTHONSAFEPATH.

mini-ork runs every ``python -m mini_ork.cli.*`` child with ``PYTHONSAFEPATH=1``
(``mini_ork/cli/main.py::_module_env``) so that a ``mini_ork/`` tree sitting in
the child's cwd cannot shadow the engine. Under that flag Python does *not*
prepend the script's own directory to ``sys.path`` — so a launcher's
``from _mini_ork_subcommand import main`` raises ``ModuleNotFoundError``. The
launchers were therefore broken in exactly the environment the engine creates
for its own children.

Measured 2026-11-05: three rsi cycles quarantined a patch on
``tests/test_recovery_closure.py::test_resume_cost_pause_unchanged``, which
spawns ``bin/mini-ork-resume`` as a child; the same id passed in 0.27 s from a
plain shell, where ``PYTHONSAFEPATH`` is unset.

The test drives a copy of ``bin/`` against a stub engine, so it proves the
launcher actually reaches ``bin/mini-ork`` with the right subcommand — not
merely that some line of text is present.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_ARGV_FILE"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
"""


def _launchers(bin_dir: Path) -> list[Path]:
    found = [p for p in sorted(bin_dir.glob("mini-ork-*"))
             if "_mini_ork_subcommand" in p.read_text(encoding="utf-8")]
    return found


def test_every_compatibility_launcher_reaches_the_engine_under_python_safe_path(tmp_path):
    bin_dir = tmp_path / "bin"
    shutil.copytree(REPO / "bin", bin_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "mini-ork"))
    stub = bin_dir / "mini-ork"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    record = tmp_path / "argv.json"

    launchers = _launchers(bin_dir)
    # A silently empty glob would make this test pass while proving nothing.
    assert len(launchers) >= 20, f"only found {len(launchers)} launchers"

    for launcher in launchers:
        record.unlink(missing_ok=True)
        proc = subprocess.run(
            [sys.executable, str(launcher)],
            env={**os.environ, "PYTHONSAFEPATH": "1", "STUB_ARGV_FILE": str(record)},
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (launcher.name, proc.stderr[-800:])
        assert record.is_file(), (launcher.name, proc.stderr[-800:])
        argv = json.loads(record.read_text(encoding="utf-8"))
        # The shim forwards the subcommand to the engine, not the filename.
        assert argv == [launcher.name[len("mini-ork-"):]], (launcher.name, argv)


def test_a_launcher_that_skips_the_path_setup_is_caught(tmp_path):
    """The guard above must fail on the unfixed shape, or it proves nothing.

    This is the shape every launcher had before the fix: a bare shim import.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(REPO / "bin" / "_mini_ork_subcommand.py", bin_dir)
    broken = bin_dir / "mini-ork-broken"
    broken.write_text(
        '#!/usr/bin/env python3\n'
        'from _mini_ork_subcommand import main\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main("broken"))\n',
        encoding="utf-8",
    )
    stub = bin_dir / "mini-ork"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    record = tmp_path / "argv.json"

    proc = subprocess.run(
        [sys.executable, str(broken)],
        env={**os.environ, "PYTHONSAFEPATH": "1", "STUB_ARGV_FILE": str(record)},
        capture_output=True, text=True, timeout=60,
    )

    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr
    assert not record.exists()
