"""Parity gate: mini_ork.ported.mini_ork_cli vs bin/mini-ork.

The full `run` lifecycle drives the (still-bash) classify/plan/execute/verify
stages — an integration concern. Here we parity the deterministic surface:
dispatch (version/help/unknown/doctor) + --deadline validation vs live bash, and
the two embedded-python blocks (recipe resolution, run-profile generation)
EXTRACTED from bin/mini-ork and run as-is vs the ported functions — proving the
transcription byte-matches the bash's own python.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_cli as cli  # noqa: E402

BIN = REPO / "bin" / "mini-ork"


def _extract_py_blocks():
    src = BIN.read_text().splitlines()
    blocks, cur = [], None
    for ln in src:
        if cur is None and re.search(r"<<'PY'", ln):
            cur = []
        elif cur is not None and ln.strip() == "PY":
            blocks.append("\n".join(cur)); cur = None
        elif cur is not None:
            cur.append(ln)
    profile = next(b for b in blocks if "schema_version" in b)
    recipe = next(b for b in blocks if "task_class.yaml" in b and "raise SystemExit" in b)
    return profile, recipe


_PROFILE_PY, _RECIPE_PY = _extract_py_blocks()


def _run_block(block, *args, tmp_path):
    p = tmp_path / "b.py"; p.write_text(block)
    return subprocess.run(["python3", str(p), *map(str, args)], capture_output=True, text=True)


# ── dispatch parity ──

def _bash(*args):
    return subprocess.run(["bash", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "MINI_ORK_ROOT": str(REPO)})


def test_version_parity(capsys):
    rb = _bash("version")
    rc = cli.main(["version"], root=str(REPO)); out = capsys.readouterr().out
    assert rb.returncode == rc == 0
    assert rb.stdout == out


def test_help_and_unknown_parity(capsys):
    rb = _bash("help"); cli.main(["help"], root=str(REPO)); out = capsys.readouterr().out
    assert rb.stdout == out
    rb2 = _bash("bogus"); rc = cli.main(["bogus"], root=str(REPO))
    assert rb2.returncode == rc == 2


def test_doctor_parity(capsys):
    rb = _bash("doctor")
    cli.main(["doctor"], root=str(REPO)); out = capsys.readouterr().out
    assert rb.returncode == 0
    assert rb.stdout == out


def test_deadline_validation_parity():
    rb = _bash("run", "--deadline", "abc", "k.md")
    rc = cli.main(["run", "--deadline", "abc", "k.md"], root=str(REPO))
    assert rb.returncode == rc == 2
    rb2 = _bash("run", "--deadline")   # missing value
    rc2 = cli.main(["run", "--deadline"], root=str(REPO))
    assert rb2.returncode == rc2 == 2


# ── recipe-resolution block parity ──

def _recipes(tmp, mapping):
    root = tmp / "root"; (root / "recipes").mkdir(parents=True)
    for dirname, tc_name in mapping.items():
        d = root / "recipes" / dirname; d.mkdir()
        if tc_name is not None:
            (d / "task_class.yaml").write_text(f"name: {tc_name}\n")
    return root


def test_resolve_recipe_parity(tmp_path):
    root = _recipes(tmp_path, {"code-fix": "code_fix", "db-migration": "db_migration",
                               "empty-dir": None, "ui-audit": "ui_audit"})
    for tc in ("code_fix", "db_migration", "ui_audit", "does_not_exist", "empty_dir"):
        rb = _run_block(_RECIPE_PY, str(root), tc, tmp_path=tmp_path).stdout.strip()
        rp = cli.resolve_recipe(str(root), tc)
        assert rb == rp, f"tc={tc}: bash={rb!r} py={rp!r}"


# ── profile-gen block parity ──

_KICKOFF = """# Ship the widget

## Success
- widget renders
- tests pass

## In scope
- src/widget.py

## Verification commands
- `pytest tests/widget`
"""


def test_gen_profile_parity(tmp_path):
    root = _recipes(tmp_path, {"code-fix": "code_fix"})
    (root / "recipes" / "code-fix" / "artifact_contract.yaml").write_text("outputs:\n  - dist/widget.js\n")
    agents = root / "agents.yaml"
    agents.write_text("lanes:\n  implementer: [codex]\n")
    kickoff = tmp_path / "k.md"; kickoff.write_text(_KICKOFF)

    prof_b = tmp_path / "pb.json"; prof_p = tmp_path / "pp.json"
    # bash's own python block
    rb = _run_block(_PROFILE_PY, str(kickoff), str(root), "code-fix", "code_fix",
                    str(prof_b), str(agents), tmp_path=tmp_path)
    # ported function
    data = cli.gen_profile(str(kickoff), str(root), "code-fix", "code_fix", str(prof_p), str(agents))

    assert json.loads(prof_b.read_text()) == json.loads(prof_p.read_text())
    # ported stdout lines match the block's stdout
    assert f"profile_status={data['profile_status']}" in rb.stdout
    assert f"profile_confidence={data['confidence']:.2f}" in rb.stdout
    assert data["profile_status"] == "ready"          # success+scope+cmd present
    assert data["verification_command"] == ["pytest tests/widget"]
