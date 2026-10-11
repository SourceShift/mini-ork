# One-line install: `curl -fsSL …/install.sh | sh` works without a clone

## Goal

Today installing needs `git clone` + `cd` + `make install`. `install.sh` at the repo root
only works from inside a checkout (it execs `$(dirname $0)/bin/mini-ork install`). Make
the same file work when piped from the network:

```sh
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
```

## Files in scope

- `install.sh` — the installer.
- `tests/unit/test_install_sh.py` — NEW, its tests.

No other file changes.

## Mechanism (exact spec)

POSIX `sh` only (must run under dash and macOS `/bin/sh`); `set -eu`.

1. **Checkout mode (unchanged behaviour).** Resolve the script directory as today. If
   `"$DIR/bin/mini-ork"` and `"$DIR/scripts/full_install.py"` both exist, keep today's
   behaviour exactly: `exec python3 "$DIR/bin/mini-ork" install "$@"`.
2. **Remote mode** (anything else, e.g. piped into `sh`, where `$0` is `sh`):
   - Pick Python: `$PYTHON` if set; else the first of `python3.12`, `python3.11`,
     `python3` on PATH whose `-c 'import sys; sys.exit(sys.version_info < (3, 11))'`
     succeeds. None → print `mini-ork needs Python 3.11 or newer (found: <versions>)` to
     stderr, exit 1.
   - Require `git` on PATH, else print `mini-ork install needs git` and exit 1.
   - Target: `${MINI_ORK_INSTALL_DIR:-$HOME/.local/share/mini-ork}`. Repo URL:
     `${MINI_ORK_REPO_URL:-https://github.com/SourceShift/mini-ork.git}`. Ref:
     `${MINI_ORK_REF:-main}`.
   - Target missing or empty → `git clone --depth 1 --branch "$REF" "$URL" "$TARGET"`.
   - Target is a git checkout whose `scripts/full_install.py` exists → update it:
     `git -C "$TARGET" fetch --depth 1 origin "$REF"` then
     `git -C "$TARGET" merge --ff-only FETCH_HEAD`. If that fails, print
     `could not fast-forward <target>; local changes? leaving it as is` and continue with
     the existing checkout (do not abort, do not reset).
   - Target exists, non-empty, and is not such a checkout → print
     `<target> exists and is not a mini-ork checkout; set MINI_ORK_INSTALL_DIR` and exit 1.
     Never delete or overwrite it.
   - System deps: if `${INSTALL_SYSTEM_DEPS:-1}` is `1` and
     `"$TARGET/scripts/install-system-deps.sh"` exists → `sh` it (same as `make install`).
   - Then `cd "$TARGET" && exec "$PY" scripts/full_install.py "$@"` (arguments pass through).
3. Before the exec in remote mode, print one line to stderr:
   `mini-ork: installing into <target> (python: <py>)`.
4. Keep the existing header comment; add two lines documenting the one-liner and the
   `MINI_ORK_INSTALL_DIR` / `MINI_ORK_REF` / `INSTALL_SYSTEM_DEPS` knobs.

## Tests (`tests/unit/test_install_sh.py`)

Run `install.sh` with `subprocess.run(["sh", <copy>], ...)` where `<copy>` is the script
copied ALONE into a temp dir (that is remote mode). Put fake `git` and `python3.11` shell
scripts first on PATH (keep `/bin:/usr/bin` after them so `sh`, `mkdir`, `cat` work).
The fake `git clone` creates the target with `scripts/full_install.py` (any content) and
`scripts/install-system-deps.sh` (writes a marker file); the fake python records its argv
to a file and exits 0. Hide real `python3.12`/`python3` by making the fake `python3.12`
exit 1 for the version check. Cases:

- fresh install: clone called with the default URL + `--depth 1 --branch main`; deps
  marker written; python called with `scripts/full_install.py --foo` when run with `--foo`.
- `MINI_ORK_REF=v0.9.0` → clone uses `--branch v0.9.0`.
- re-run on an existing checkout → `fetch` + `merge --ff-only`, no clone.
- failing `merge --ff-only` → still runs full_install, prints the "could not fast-forward" line.
- target exists with an unrelated file → exit 1, message names `MINI_ORK_INSTALL_DIR`, file untouched.
- `INSTALL_SYSTEM_DEPS=0` → no deps marker.
- no acceptable python (all fakes fail the version check, `PYTHON` unset) → exit 1 with the "needs Python 3.11" message.
- checkout mode: copy `install.sh` into a temp dir that also has `bin/mini-ork` (a fake
  that records argv) and `scripts/full_install.py`, put a fake `python3` first on PATH
  that execs its first argument → `bin/mini-ork install` is what runs.
- `sh -n install.sh` passes; if `dash` is on PATH, `dash -n install.sh` passes (skip otherwise).

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_install_sh.py` passes.
- `ruff check tests/unit/test_install_sh.py` clean.
- Diff touches only the two files in scope.
