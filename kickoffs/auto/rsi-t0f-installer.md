# Installer: three bugs a fresh Linux install hits (found by a from-scratch Daytona install)

## Goal

A from-scratch install of mini-ork in a Daytona Linux sandbox (2026-10-23)
hit three bugs that a macOS dev checkout never exercises.

1. **The system-deps script runs under the wrong shell.** `install.sh` runs
   `sh "$TARGET/scripts/install-system-deps.sh"`, but that script is
   `#!/usr/bin/env bash` and uses bash-only syntax (`[[`, arrays and more:
   16 bash-isms). On Debian/Ubuntu `sh` is dash, so the system-deps step dies
   with a syntax error.
2. **The interpreter probe misses Python 3.13.** It tries `python3.12`,
   `python3.11`, then `python3`.
   - On an image whose `python3` is 3.14, the installer picks 3.14 even when
     `python3.13` is present.
   - The `verifiers` dependency, an optional engine for the Crucible runtime,
     does not install on 3.14, so the whole `full` extra fails.
   - `verifiers` is imported lazily (`mini_ork/runtime/engine.py`, and Crucible
     falls back to the docker CLI when it is absent), so skipping it on 3.14 is
     safe.
3. **The runtime pointer escapes the venv.** `scripts/full_install.py:write_runtime_pointer`
   writes `venv_python(venv).resolve()` to `.mini-ork/runtime-python`.
   - `<venv>/bin/python` is a symlink to the base interpreter, so `.resolve()`
     records the **base** interpreter. Run directly, it has `sys.prefix` = the
     base install, not the venv. (Verified on this machine: the resolved
     `.venv/bin/python` reports the Homebrew prefix.)
   - The launcher then re-execs a Python without mini-ork's dependencies.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `install.sh`

- **Fix 1.** Replace `sh "$TARGET/scripts/install-system-deps.sh"` with:
  - when `command -v bash` succeeds: `bash "$TARGET/scripts/install-system-deps.sh"`;
  - otherwise: print
    `mini-ork: scripts/install-system-deps.sh needs bash; install bash or re-run with INSTALL_SYSTEM_DEPS=0`
    to stderr and `exit 1`.
- **Fix 2.** The interpreter probe loop tries
  `python3.13 python3.12 python3.11 python3`, in that order. Change nothing else
  in that loop.
- The script must still pass the existing POSIX syntax check (it is executed by
  `sh`; only the system-deps child needs bash).

### 2. `pyproject.toml`

Both `verifiers` requirement strings, in the `runtime` extra and in the `full`
extra, become `verifiers>=0.2.0,<0.3; python_version < "3.14"`. No other line
changes.

### 3. `scripts/full_install.py`

In `write_runtime_pointer`, replace `venv_python(venv).resolve()` with
`venv_python(venv).absolute()`. The pointer must name the venv's own
`bin/python`, never a symlink's target. Nothing else changes.

## Files in scope (touch ONLY these)

- `install.sh`
- `pyproject.toml`
- `scripts/full_install.py`
- `tests/unit/test_full_install_py.py`
- `tests/unit/test_install_sh.py`

Two existing tests need a small, precise edit, and nothing else in them may
change:

- `tests/unit/test_full_install_py.py::test_runtime_pointer_selects_a_custom_venv`
  pins today's `.resolve()` value. Change its expected value to
  `str(installer.venv_python(venv).absolute())`.
- **Shadow a real `python3.13`.** In `tests/unit/test_install_sh.py`, every
  existing test or fixture that calls `_make_fake_python(bin_dir, "python3.12", version_ok=False)`
  must also call `_make_fake_python(bin_dir, "python3.13", version_ok=False)`.
  That covers the `remote` fixture and
  `test_remote_no_acceptable_python_exits_with_message`. Otherwise a real
  `python3.13` on the host PATH (Homebrew ships one) satisfies the new first
  probe, and those tests stop exercising the interpreter they set up.

Do NOT modify any other file.

## Tests

### In `tests/unit/test_full_install_py.py` (`installer` is the module that file already loads)

- **The pointer keeps the symlink.** Create `venv/bin/` under `tmp_path`, make
  `venv/bin/python` a symlink to a real file `tmp_path / "base" / "python3.99"`,
  and call `installer.write_runtime_pointer(root, venv, dry_run=False)`. Then:
  - the pointer's text is `str((venv / "bin" / "python").absolute()) + "\n"`;
  - it does not contain `python3.99`.

  Skip on Windows (`sys.platform == "win32"`).
- **The `verifiers` marker.** Read `pyproject.toml` as text. Every line that
  contains `verifiers>=0.2.0,<0.3` also contains `python_version < "3.14"`, and
  there are exactly 2 such lines.

### In `tests/unit/test_install_sh.py` (reuse that file's fake-python helpers and its `remote` fixture style)

- **Bash runs the system-deps script.** Read `install.sh` as text:
  - it contains `bash "$TARGET/scripts/install-system-deps.sh"`;
  - it contains no line matching the regex `^\s*sh "\$TARGET/scripts/install-system-deps.sh"`.
- **3.13 is tried first.** Read `install.sh` as text: it contains
  `for candidate in python3.13 python3.12 python3.11 python3; do`.
- **3.13 is picked.** With fake interpreters on PATH (`python3.13` version-ok
  and recording its argv, `python3` version-ok), the remote install branch
  invokes `python3.13`, not `python3`. Mirror the existing test that has
  `python3.12` refuse and `python3.11` accept.

## Verification commands

- `pytest -q tests/unit/test_full_install_py.py tests/unit/test_install_sh.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_full_install_py.py tests/unit/test_install_sh.py --junitxml="${MINI_ORK_RUN_DIR}/installer-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/installer-junit.xml`

## Success criteria

- The verification command passes. Every pre-existing test is unchanged except
  the one pinned expectation named above.
- `ruff check --select F,E9 scripts/full_install.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
