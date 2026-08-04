#!/usr/bin/env python3
# verifiers/self-tests-pass.py — run mini-ork's own pytest suite against the
# worktree the implementer patched, from a PRISTINE, HERMETIC sandbox so the
# gate's verdict reflects the patch — not the machine state around it.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR                run directory
#   MINI_ORK_SELF_IMPROVE_WORKTREE  worktree path (set by outer runner)
#   MINI_ORK_SELF_IMPROVE_TEST_CMD  override test command (shlex-split);
#                                   default: `<sys.executable> -m pytest -q`
#
# Output: JSON (stdout) + verifier-result-self-tests-pass.json (sidecar).
# Exit 0 always (caller reads .pass).
#
# HERMETIC SANDBOX — why every piece exists:
#
# History: gate used to `os.chdir(WT)` and inherit the caller's env. On CI that
# was fine (fresh runner, empty .mini-ork). On the dev box the suite showed 6
# machine-dependent failures the patch didn't cause — turning the gate red for
# reasons no implementer could fix. The four root causes we saw and their
# corresponding scrubs:
#
#   1. Framework-tree cwd guard fires when pytest runs from inside the source
#      tree (dispatch tests then divert to `cwd guard failed`, masking the
#      preflight assertion they meant to test). Fix: run pytest from a temp
#      staging dir OUTSIDE the framework tree — the guard sees "not inside" and
#      lets the real assertion run.
#
#   2. A live `.mini-ork/config/agents.yaml` in the operator's home shadows the
#      repo default and can be missing lanes tests reference (e.g. `sonnet`).
#      Fix: do NOT symlink `.mini-ork` into the sandbox — config loader falls
#      back to the worktree's canonical agents.yaml.
#
#   3. `test_no_populated_table_is_dark` reads `Path.cwd()/.mini-ork/state.db`
#      and asserts no populated table is unrouted. On CI the file is absent and
#      the test skips; on a live box the ~22 MB db trips it. Fix: absent
#      `.mini-ork` → `real_db.exists()` is False → test skips cleanly.
#
#   4. `test_empty_cwd_inherits` compares `Path.cwd()` (resolves symlinks) to a
#      subprocess `pwd` (does NOT). A dual-workdir clone (e.g. /Volumes/…
#      symlinked to /Users/…) makes them disagree. Fix: `os.path.realpath` the
#      staging dir before chdir, and place staging under `TMPDIR`'s realpath —
#      both sides see the same string.
#
# Env scrub — reason: `MINI_ORK_*` / `MO_*` from the outer runner would put the
# child pytest back into the shadowed live state we are trying to escape; API
# keys and gateway tokens would let a mis-mocked dispatch fire paid calls under
# the gate. The allowlist is deliberately tiny (PATH, HOME, USER, SHELL, LANG,
# LC_*, TERM, TMPDIR, PYTHONHASHSEED, SSL_CERT_*). `MINI_ORK_DRY_RUN=1` is
# force-set as a belt on the suspenders — even if a dispatch escapes the
# scrubbed env, the DRY_RUN gate short-circuits the paid path.
#
# Vacuous-pass discipline is unchanged: rc==5 or `0 collected` counts as fail.

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
WT_RAW = os.environ.get("MINI_ORK_SELF_IMPROVE_WORKTREE") or os.environ.get(
    "MINI_ORK_ROOT", ""
)
EVIDENCE = os.path.join(RUN_DIR, "verifier-self-tests-pass.log")
ev = open(EVIDENCE, "w")


def _emit(result: dict) -> None:
    ev.close()
    print(json.dumps(result))
    try:
        with open(
            os.path.join(RUN_DIR, "verifier-result-self-tests-pass.json"), "w"
        ) as sc:
            json.dump(result, sc)
    except OSError:
        pass


# ── locate worktree, refuse to run without one ────────────────────────────────

if not WT_RAW or not os.path.isdir(WT_RAW):
    ev.write(f"worktree missing or not a dir: {WT_RAW!r}\n")
    _emit({
        "verifier": "self-tests-pass",
        "pass": False,
        "evidence_path": EVIDENCE,
        "error": "worktree missing",
    })
    sys.exit(0)

# realpath resolves any symlink layer so the child pytest, subprocess pwd,
# and Path.cwd() all see the same string (root cause #4 above).
WT = os.path.realpath(WT_RAW)

# ── build pristine sandbox: staging dir under real TMPDIR ─────────────────────

# Exclude anything that would drag the machine's live state into the run.
# `.mini-ork` and `state.db*` handle the shadow-config / populated-db failures;
# `.git`, `runs`, `tmp`, `.claude` are heavy junk pytest never needs.
EXCLUDE_TOP = {
    ".mini-ork",
    ".git",
    "runs",
    "tmp",
    ".claude",
    "state.db",
    "state.db-shm",
    "state.db-wal",
    "run.log",
    ".venv",
    "venv",
    "node_modules",
    "Volumes",  # never symlink a symlink to another disk
}

tmp_root = os.path.realpath(tempfile.gettempdir())
staging = tempfile.mkdtemp(prefix="mo-gate-", dir=tmp_root)
try:
    for name in os.listdir(WT):
        if name in EXCLUDE_TOP:
            continue
        src = os.path.join(WT, name)
        dst = os.path.join(staging, name)
        # Symlink (not copy) — pytest resolves imports through the symlink;
        # `Path(__file__).resolve()` in tests points back at the real WT source,
        # so tests still measure the patched code, not a stale copy.
        try:
            os.symlink(src, dst)
        except OSError as exc:
            ev.write(f"symlink {name} failed: {exc}\n")

    # ── build pristine env: allowlist + DRY_RUN safety belt ───────────────────

    ENV_KEEP = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG",
        "TERM", "TMPDIR", "TMP", "TEMP",
        "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "PYENV_ROOT", "PYENV_VERSION", "PYENV_SHELL",
    }
    # Explicit credential blocklist — reason: allowlisting handles most, but
    # credentials use fully custom names (ANTHROPIC_API_KEY, OPENAI_API_KEY,
    # MINIMAX_API_KEY, GLM_API_KEY, KIMI_API_KEY, gateway BASE_URLs). The
    # suffix scan is a belt on top of ENV_KEEP: any variable ending in
    # _API_KEY / _TOKEN / _SECRET / _PASSWORD is dropped even if a future
    # ENV_KEEP entry would have kept it. Preserves safety belt (b) in the
    # docstring above.
    def _looks_like_credential(k: str) -> bool:
        return (
            k.endswith("_API_KEY")
            or k.endswith("_TOKEN")
            or k.endswith("_SECRET")
            or k.endswith("_PASSWORD")
        )
    child_env = {
        k: v
        for k, v in os.environ.items()
        if (k in ENV_KEEP or k.startswith("LC_"))
        and not _looks_like_credential(k)
    }
    # NOTE: we do NOT set MINI_ORK_DRY_RUN=1 here.
    # A previous rewrite did, and it silently short-circuited the eval /
    # execute / improve / reflect subcommands (they honor DRY_RUN by
    # emitting "[dry-run] would run…" and skipping the real path), reddening
    # ~11 tests that assert on the live-path output like "=== eval result ===".
    # Safety instead rests on three uncorrelated belts:
    #   (a) the sandbox cwd is outside the framework tree → the cwd guard
    #       lets dispatch tests exercise the real preflight failure paths
    #       they meant to test;
    #   (b) all *_API_KEY / *_TOKEN / *_SECRET vars are stripped, so any
    #       surviving dispatch attempt fails at lane_health() preflight
    #       (missing credential) BEFORE opening a paid connection;
    #   (c) MINI_ORK_HOME points at a sandbox-owned config dir with only the
    #       repo defaults symlinked in (no secrets file, no shadow lanes),
    #       so a lane the operator's shadow drops still resolves via the
    #       committed baseline the tests were written against.

    # ── seed a sandbox-owned MINI_ORK_HOME with the repo default configs ──────
    #
    # Config resolution order:
    #   1. $MINI_ORK_PROVIDERS
    #   2. $MINI_ORK_HOME/config/providers.yaml
    #   3. <repo>/config/providers.yaml
    #
    # An operator's live `.mini-ork/config/providers.yaml` is gitignored and
    # wholly REPLACES the repo default (no merge, first-hit wins). Any test
    # that references a lane the operator's shadow doesn't define (e.g. tests
    # exercising `sonnet` when the shadow lists only glm/kimi/codex/minimax)
    # false-reds even though the patch is fine and CI would pass.
    #
    # Fix: point MINI_ORK_HOME at a sandbox-owned `.mini-ork/` whose
    # `config/` symlinks the WT's repo defaults (config/agents.yaml,
    # config/providers.yaml, etc.). Step-2 lookup now finds the committed
    # defaults; the operator's shadow is bypassed for the gate only. A test's
    # own `env["MINI_ORK_ROOT"]` monkeypatch doesn't override this — HOME and
    # ROOT are distinct env vars.
    sandbox_home = os.path.join(staging, ".mini-ork")
    sandbox_home_config = os.path.join(sandbox_home, "config")
    os.makedirs(sandbox_home_config, exist_ok=True)
    repo_config = os.path.join(WT, "config")
    if os.path.isdir(repo_config):
        for entry in os.listdir(repo_config):
            # The provider credential store REFUSES to load a symlinked
            # secrets file (defense: a symlinked secrets file could be
            # attacker-controlled). Skip any `secrets*` entry — the gate is
            # DRY_RUN=1 so no real key is needed. Include the SecretStoreError
            # trigger name explicitly so a rename ripples through this filter.
            if entry.startswith("secrets") or entry.startswith("secret."):
                continue
            src = os.path.join(repo_config, entry)
            dst = os.path.join(sandbox_home_config, entry)
            try:
                os.symlink(src, dst)
            except OSError:
                pass
    child_env["MINI_ORK_HOME"] = sandbox_home
    # Explicitly clear MINI_ORK_PROVIDERS so step-1 doesn't win with a stale
    # override the operator forgot to unset.
    child_env.pop("MINI_ORK_PROVIDERS", None)

    # ── run pytest, pin sys.executable so PATH shims can't swap interpreters ──

    # DEFAULT pins the running interpreter — the verifier is itself a Python
    # process, so we already know a working pytest lives there. Bare `python3`
    # can resolve to an interpreter on PATH without pytest installed (the
    # exact case that made the earlier "python3 -m pytest -q" gate fail-closed
    # on a dev box with pyenv shims below /usr/local/bin).
    DEFAULT_CMD = f"{shlex.quote(sys.executable)} -m pytest -q"
    override = os.environ.get("MINI_ORK_SELF_IMPROVE_TEST_CMD", "").strip()
    cmd = shlex.split(override or DEFAULT_CMD)

    ev.write(
        "test_cmd={cmd}\nsandbox={s}\nworktree={w}\n"
        "sanitized MINI_ORK_*/MO_*/*_API_KEY/*_TOKEN (allowlist: {keep})\n"
        "===== output =====\n".format(
            cmd=" ".join(cmd),
            s=staging,
            w=WT,
            keep=sorted(ENV_KEEP),
        )
    )
    ev.flush()

    proc = subprocess.run(
        cmd,
        cwd=staging,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = proc.stdout.decode("utf-8", "replace")
    ev.write(out)
    rc = proc.returncode

    # pytest exit codes: 0=all passed, 1=tests failed, 2=interrupted,
    # 3=internal error, 4=usage error, 5=NO TESTS COLLECTED.
    def _count(word: str) -> int:
        m = re.search(rf"(\d+) {word}", out)
        return int(m.group(1)) if m else 0

    n_passed = _count("passed")
    n_failed = _count("failed") + _count("error") + _count("errors")
    collected = n_passed + n_failed

    if rc == 5 or (rc == 0 and collected == 0):
        ev.write("no tests collected — refusing vacuous pass\n")
        passed = 0
    elif rc == 0:
        passed = 1
    else:
        passed = 0

    _emit({
        "verifier": "self-tests-pass",
        "pass": passed == 1,
        "evidence_path": EVIDENCE,
        "test_cmd": " ".join(cmd),
        "sandbox": staging,
        "pytest_rc": rc,
        "tests_collected": collected,
        "tests_passed": n_passed,
        "tests_failed": n_failed,
    })
finally:
    # Best-effort cleanup — symlinks only, no real payload to lose.
    try:
        shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        pass
