"""Regression tests ported from the blind spec tests for the t0f-installer kickoff (scripts/full_install.py, pyproject.toml).

Authored from the kickoff alone, before any implementation exists.
Repo files are resolved from the working directory, which the runner sets to
the target repo root.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The ceiling moves independently of this fix (dependabot raised it `<0.3` ->
# `<0.5` on 2026-10-27). The kickoff pins the *shape*: every `verifiers`
# requirement carries the py3.14 marker.
VERIFIERS_REQ = re.compile(r'verifiers>=0\.2\.0,<\d+(?:\.\d+)*')

# Load the module the kickoff names, at its declared path scripts/full_install.py
# (the kickoff refers to it as `installer`).
_SPEC = importlib.util.spec_from_file_location("full_install_spec", REPO / "scripts" / "full_install.py")
installer = importlib.util.module_from_spec(_SPEC)
sys.modules["full_install_spec"] = installer
_SPEC.loader.exec_module(installer)


def test_runtime_pointer_keeps_the_venv_symlink(tmp_path):
    r"""Kickoff: "the pointer's text is `str((venv / "bin" / "python").absolute()) + "\n"`; it does not contain `python3.99`."
    """
    base = tmp_path / "base"
    base.mkdir()
    base_interpreter = base / "python3.99"
    base_interpreter.write_text("#!/bin/sh\nexit 0\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(base_interpreter, venv / "bin" / "python")

    root = tmp_path / "root"
    root.mkdir()

    pointer = installer.write_runtime_pointer(root, venv, dry_run=False)

    assert pointer == root / ".mini-ork" / "runtime-python"
    text = pointer.read_text()
    assert text == str((venv / "bin" / "python").absolute()) + "\n"
    assert "python3.99" not in text


def test_pyproject_verifiers_lines_carry_the_3_14_marker():
    """Kickoff: "Every line that contains `verifiers>=0.2.0,<0.3` also contains `python_version < "3.14"`, and there are exactly 2 such lines."
    """
    text = (REPO / "pyproject.toml").read_text()
    lines = [line for line in text.splitlines() if VERIFIERS_REQ.search(line)]
    assert len(lines) == 2, lines
    for line in lines:
        assert 'python_version < "3.14"' in line, line


def test_both_extras_pin_verifiers_below_python_3_14():
    """Kickoff: "Both `verifiers` requirement strings, in the `runtime` extra and in the `full` extra, become `verifiers>=0.2.0,<0.3; python_version < "3.14"`. No other line changes."
    """
    lines = (REPO / "pyproject.toml").read_text().splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.strip() == "[project.optional-dependencies]"
    )
    opener = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*\[")
    owners = {}
    current = None
    for line in lines[start + 1:]:
        if line.startswith("[") and line.rstrip().endswith("]"):
            break  # next TOML table: the optional-dependencies section is over
        match = opener.match(line)
        if match:
            current = match.group(1)
        if VERIFIERS_REQ.search(line):
            assert 'python_version < "3.14"' in line, line
            owners.setdefault(current, []).append(line)
    assert owners.get("runtime"), owners
    assert owners.get("full"), owners
