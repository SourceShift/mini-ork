#!/usr/bin/env python3
# verifiers/kickoff-contract.py — execute the contract the kickoff declared.
#
# A framework-edit run must fail when it does not deliver what its kickoff
# declared. Today it can pass without it: `overlay_plan`
# (mini_ork/planning/recipe_plan.py) replaces the plan's outputs with the
# recipe's own contract, so a kickoff's "Required artifacts" never reach
# plan.json, and nothing runs its "Self-application measurement" — the
# implementer is trusted to. Four SDE runs on 2026-10-13/14 each declared an
# artifact (`diagnosis-replay.json`, `kernel-refusal.json`) plus a measurement
# command, produced neither, and still ended `verdict: pass`.
#
# This verifier closes that gap for the two sections a kickoff can DECLARE, and
# nothing else: existing kickoffs with neither section keep passing unchanged.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by the native execute runtime
#   MO_TARGET_CWD    — the run's TARGET repo/worktree (the repo under test);
#                      preferred over MINI_ORK_ROOT, which names the ENGINE
#   MINI_ORK_ROOT    — engine repo root (fallback when MO_TARGET_CWD is unset)
#   MINI_ORK_HOME    — passed through to measure commands
#   MO_KICKOFF_MEASURE_TIMEOUT_S — per-measure-command timeout (default 900; a
#                      present-but-malformed value fails the measure closed)
#
# A measure block is operator prose this verifier EXECUTES, so the command runs
# under a scrubbed env (`mini_ork.verify.test_env.scrubbed_test_env`: provider
# credentials and mini-ork state pointers removed, `MINI_ORK_RUN_DIR` and
# `MINI_ORK_HOME` added back) — secrets never reach the shell or the log.
#
# Output: JSON to stdout; an evidence log and `verifier_kickoff-contract.json`
# in the run dir. Exit 0 on pass, 1 on fail. This verifier never writes or
# changes `verdict.json` — the verifier nodes that own it are the only writers.

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys

# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH,
# but walk up to the engine root anyway so a hand-run or a caller that forgets
# it still imports `mini_ork.context_v2` for the pre-run kickoff snapshot.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)

NAME = "kickoff-contract"
JSON_NAME = f"verifier_{NAME}.json"
EVIDENCE_NAME = f"verifier-{NAME}.log"
CHECKS_TSV_NAME = f"verifier-{NAME}.checks.tsv"

# A kickoff whose FIRST line carries this marker was opened automatically from a
# raw log excerpt. A log line must never become an executed command, so such a
# kickoff declares no contract this verifier may enforce.
ORIGIN_MARKER = "<!-- mini-ork:origin=auto-triage -->"

# Headings that switch the contract on. Both are prefix matches (any suffix is
# allowed) so "## Required artifacts — declare EXACTLY these" is recognised. A
# kickoff with NEITHER heading is untouched by this verifier.
MEASURE_HEADING = re.compile(r"^(#{2,4})[ \t]*Self-application measurement", re.IGNORECASE)
ARTIFACTS_HEADING = re.compile(r"^(#{2,4})[ \t]*Required artifacts", re.IGNORECASE)

# "```measure" — the info string must be exactly `measure`. Prose that merely
# shows a command in backticks carries no fence and is never executed.
_FENCE = re.compile(r"^\s*```(.*)$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")

RUN_PREFIX = "${MINI_ORK_RUN_DIR}/"


# ── kickoff source ──────────────────────────────────────────────────────────

def _load_pack(run_dir: str) -> dict:
    """The run's pre-run context-pack v2 (``{}`` when absent or unreadable).

    Prefer the engine's parser; fall back to a direct ``json.load`` of the pack
    so a caller whose ``mini_ork`` is not importable STILL reaches the snapshot
    — otherwise the snapshot-wins guarantee would quietly degrade to reading the
    (editable) live kickoff file."""
    try:
        from mini_ork import context_v2
        return context_v2.load_pack(run_dir) or {}
    except Exception:  # noqa: BLE001 — no module: read the file ourselves
        pass
    try:
        with open(os.path.join(run_dir, "context-pack.v2.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _profile_kickoff_path(run_dir: str) -> str:
    try:
        with open(os.path.join(run_dir, "run_profile.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    path = data.get("kickoff_path")
    return path if isinstance(path, str) and path.strip() else ""


def kickoff_source(run_dir: str) -> tuple[str, str]:
    """``(text, source)`` — the contract the run RECEIVED, most authoritative first.

    The pre-run snapshot is the contract as the run was dispatched; editing the
    live kickoff afterwards cannot weaken it, which is the whole point. Only a
    pack with no snapshot falls back to the file ``run_profile.json`` records.
    """
    snapshot = _load_pack(run_dir).get("kickoff_snapshot")
    if isinstance(snapshot, str) and snapshot.strip():
        return snapshot, "snapshot"
    path = _profile_kickoff_path(run_dir)
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read(), "profile-file"
        except OSError:
            pass
    return "", "none"


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0] if text else ""


def is_auto_triage(text: str) -> bool:
    """``startswith`` on the FIRST line, never a substring test: a human
    kickoff that merely quotes the marker further down must still read as
    human. A leading BOM is tolerated as in the file read."""
    return _first_line(text).lstrip("\ufeff").strip().startswith(ORIGIN_MARKER)


# ── section parsing ─────────────────────────────────────────────────────────

def _region_end(lines: list[str], start: int, level: int) -> int:
    """Index one past the section body: the next heading of the SAME or higher
    level (i.e. that many hashes or fewer), skipping fenced blocks so a ``#``
    comment line inside a ```measure fence cannot end the section early."""
    i = start
    fenced = False
    terminator = re.compile(r"^#{1,%d}[ \t]" % level)
    while i < len(lines):
        if re.match(r"^\s*```", lines[i]):
            fenced = not fenced
            i += 1
            continue
        if not fenced and terminator.match(lines[i]):
            break
        i += 1
    return i


def _section_regions(text: str, heading_re: re.Pattern[str]) -> list[list[str]]:
    """Every region under a heading matching ``heading_re`` (usually one)."""
    lines = text.split("\n")
    regions: list[list[str]] = []
    i = 0
    while i < len(lines):
        m = heading_re.match(lines[i])
        if m:
            level = len(m.group(1))
            j = _region_end(lines, i + 1, level)
            regions.append(lines[i + 1:j])
            i = j
        else:
            i += 1
    return regions


def has_measure_fence(text: str) -> bool:
    for region in _section_regions(text, MEASURE_HEADING):
        for line in region:
            m = _FENCE.match(line)
            if m and m.group(1).strip() == "measure":
                return True
    return False


def measure_commands(text: str) -> list[str]:
    """Every command in a ```measure fence under a measurement heading.

    One non-empty, non-``#`` line is one command. Inline backtick commands in
    prose are never collected — measure blocks are opt-in."""
    commands: list[str] = []
    for region in _section_regions(text, MEASURE_HEADING):
        i = 0
        while i < len(region):
            m = _FENCE.match(region[i])
            if m and m.group(1).strip() == "measure":
                i += 1
                while i < len(region) and not _FENCE_CLOSE.match(region[i]):
                    line = region[i].strip()
                    if line and not line.startswith("#"):
                        commands.append(line)
                    i += 1
                i += 1  # step past the closing fence
            else:
                i += 1
    return commands


def has_artifacts_section(text: str) -> bool:
    return bool(_section_regions(text, ARTIFACTS_HEADING))


def _escapes_run(token: str, run_dir: str) -> bool:
    """True when an expanded token resolves OUTSIDE the run dir.

    The verifier's visibility limit is run-local, so a declared
    ``${MINI_ORK_RUN_DIR}/../elsewhere`` is refused rather than accepted as a
    path the run cannot vouch for."""
    expanded = os.path.normpath(token.replace("${MINI_ORK_RUN_DIR}", run_dir))
    root = os.path.realpath(run_dir)
    try:
        return os.path.commonpath([root, os.path.realpath(expanded)]) != root
    except ValueError:  # mixed absolute/relative — cannot happen for an abs run dir
        return True


def required_artifacts(text: str, run_dir: str) -> tuple[list[str], list[str]]:
    """``(paths, escaped_tokens)`` — the run-local artifacts a kickoff declares.

    Only tokens prefixed ``${MINI_ORK_RUN_DIR}/`` are collected; any other path
    is outside the run and therefore outside this verifier's visibility. A token
    that expands OUT of the run dir is returned in ``escaped_tokens`` so the
    caller can fail it loudly instead of ignoring the declaration."""
    found: list[str] = []
    escaped: list[str] = []
    for region in _section_regions(text, ARTIFACTS_HEADING):
        for line in region:
            if not re.match(r"^\s*[-*+]\s", line):
                continue
            for quoted in re.findall(r"`([^`\n]+)`", line):
                for token in quoted.split():
                    if not token.startswith(RUN_PREFIX):
                        continue
                    if _escapes_run(token, run_dir):
                        if token not in escaped:
                            escaped.append(token)
                        continue
                    path = os.path.normpath(token.replace("${MINI_ORK_RUN_DIR}", run_dir))
                    if path not in found:
                        found.append(path)
    return found, escaped


# ── measures ────────────────────────────────────────────────────────────────

def measure_cwd() -> str:
    """The measure working directory: the TARGET tree, never the engine."""
    for key in ("MO_TARGET_CWD", "MINI_ORK_TARGET_REPO"):
        val = os.environ.get(key)
        if val and os.path.isdir(val):
            return val
    root = os.environ.get("MINI_ORK_ROOT")
    if root and os.path.isdir(root):
        return root
    return os.getcwd()


def measure_timeout() -> tuple[int | None, str]:
    """``(timeout_s, error)``. Unset/empty takes the 900 s default; a value that
    is PRESENT but not a positive integer fails closed — a malformed knob is a
    clear error, never a silent fallback (CLAUDE.md's fail-closed rule)."""
    raw = str(os.environ.get("MO_KICKOFF_MEASURE_TIMEOUT_S", "")).strip()
    if not raw:
        return 900, ""
    try:
        value = int(raw)
    except ValueError:
        return None, f"MO_KICKOFF_MEASURE_TIMEOUT_S is not an integer: {raw!r}"
    if value <= 0:
        return None, f"MO_KICKOFF_MEASURE_TIMEOUT_S must be a positive integer: {raw!r}"
    return value, ""


# test.py:33-43's inline fallback, for a verifier copied into a fixture without
# the rest of `mini_ork` on PYTHONPATH — kept byte-compatible with the module it
# stands in for so a measure never sees a different env depending on the import.
_SCRUB_NAMES = frozenset({
    "MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME",
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS",
    "MO_TARGET_CWD",
})


def _fallback_scrubbed_env() -> dict:
    env = dict(os.environ)
    for key in list(env):
        if (key in _SCRUB_NAMES
                or key.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY"))
                or key.startswith("ANTHROPIC_") or key in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
            env.pop(key)
    return env


def measure_env(run_dir: str) -> dict:
    """The env a measure command runs under.

    A measure block is operator-written prose that the run then EXECUTES, so it
    gets the same treatment as a target-repo test suite: provider credentials
    are scrubbed in, never out (they must not reach the command, and must not
    reach the evidence log). ``scrubbed_test_env`` drops ``MINI_ORK_RUN_DIR``
    and ``MINI_ORK_HOME`` along with them, so the two a measure legitimately
    needs are added back."""
    try:
        from mini_ork.verify.test_env import scrubbed_test_env
        env = dict(scrubbed_test_env())
    except Exception:  # noqa: BLE001 — verifier copied without mini_ork
        env = _fallback_scrubbed_env()
    env["MINI_ORK_RUN_DIR"] = run_dir
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        env["MINI_ORK_HOME"] = home
    # Pin the interpreter exactly as test.py does: a measure command that says
    # bare `python3` must resolve the interpreter running this verifier, not the
    # host's (3.9 raised a `|` TypeError in sde-3b1 and cost a 2580 s revise
    # round). The inherited PATH is preserved after the pin.
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


def _kill_group(proc) -> None:
    """SIGKILL the measure's whole process group. ``bash -c`` forks for a
    compound command, so killing only the direct child can leave a grandchild
    running past the timeout."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 — reap best-effort; the group is already killed
        pass


def run_measures(commands: list[str], run_dir: str, cwd: str, log=None) -> list[dict]:
    env = measure_env(run_dir)
    timeout, timeout_err = measure_timeout()
    out = log if log is not None else subprocess.DEVNULL
    results: list[dict] = []
    for cmd in commands:
        if timeout_err:
            results.append({"command": cmd, "rc": None, "timed_out": False, "error": timeout_err})
            continue
        # stdout is a FILE, never a PIPE: a grandchild that outlives the timeout
        # cannot hold a pipe open and hang the reap the way communicate() would.
        try:
            proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=env,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            results.append({"command": cmd, "rc": None, "timed_out": False, "error": str(exc)})
            continue
        try:
            rc = proc.wait(timeout=timeout)
            results.append({"command": cmd, "rc": rc, "timed_out": False})
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            results.append({"command": cmd, "rc": None, "timed_out": True, "timeout_s": timeout})
    return results


# ── the contract check ──────────────────────────────────────────────────────

def _artifact_ok(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def evaluate(run_dir: str, *, measure: bool = True, log=None) -> dict:
    """Run the kickoff contract for one run dir. Pure: writes nothing."""
    text, source = kickoff_source(run_dir)
    result: dict = {
        "kickoff_source": source,
        "measures": [],
        "artifacts": {},
        "missing": [],
        "escaped": [],
        "note": None,
    }

    if is_auto_triage(text):
        result["note"] = "auto-triage kickoff: no operator contract"
        result["pass"] = True
        return result

    has_measure = has_measure_fence(text)
    has_artifacts = has_artifacts_section(text)
    if not has_measure and not has_artifacts:
        result["note"] = "no kickoff contract declared"
        result["pass"] = True
        return result

    commands = measure_commands(text)
    if measure:
        result["measures"] = run_measures(commands, run_dir, measure_cwd(), log=log)
    else:
        # --no-measure: the commands are listed but never executed, so nothing
        # about them is judged (a skip is not a pass AND not a failure).
        result["measures"] = [{"command": c, "skipped": True, "pass": True} for c in commands]
    for entry in result["measures"]:
        entry.setdefault("pass", bool(not entry.get("timed_out") and entry.get("rc") == 0))
    measures_ok = all(m["pass"] for m in result["measures"])

    found, escaped = required_artifacts(text, run_dir)
    for path in found:
        result["artifacts"][path] = _artifact_ok(path)
    # A declaration that points outside the run is not ignored: it fails, and it
    # is named separately so the evidence says WHY it could not be satisfied.
    for token in escaped:
        result["artifacts"][token] = False
    result["escaped"] = escaped
    result["missing"] = [p for p, ok in result["artifacts"].items() if not ok]
    result["pass"] = measures_ok and not result["missing"]
    return result


# ── replay self-check ───────────────────────────────────────────────────────

# The four kickoffs the gap was found on. `overlay_plan` dropped their declared
# artifacts, so each run ended `verdict: pass` with the artifact absent. The
# expected matrix is the regression this verifier exists to catch: exit 0 iff
# the three pass-1/SDE-2 runs FAIL and sde-1-diagnose-20261014-000929 PASSES.
REPLAY_EXPECTATIONS = (
    ("sde-1-diagnose-20261013-232707", False),
    ("sde-2-kernel-20261013-232708", False),
    ("sde-2-kernel-20261014-000931", False),
    ("sde-1-diagnose-20261014-000929", True),
)


def replay_check(runs_dir: str, log=None) -> int:
    """Read-only replay across finished run dirs: never executes a measure."""
    matrix: dict[str, dict] = {}
    ok = True
    for run_id, expect_pass in REPLAY_EXPECTATIONS:
        run_dir = os.path.join(runs_dir, run_id)
        if not os.path.isdir(run_dir):
            sys.stderr.write(f"kickoff-contract: expected run dir absent: {run_dir}\n")
            ok = False
            continue
        result = evaluate(run_dir, measure=False)
        matrix[run_id] = {"pass": bool(result["pass"]), "missing": result["missing"]}
        if bool(result["pass"]) != expect_pass:
            ok = False
    sys.stdout.write(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
    return 0 if ok else 1


# ── entry point ─────────────────────────────────────────────────────────────

def _checks(result: dict) -> list[dict]:
    """The check rows, in the sibling envelope shape (``{name, expected,
    actual, pass, gating}``). ``verifier_kickoff-contract.json`` is read back by
    ``mini_ork.verify.levels._contract_from_recipe_verifiers``, which needs a
    bool ``pass`` plus a list ``checks`` or the whole run's contract level flips
    to UNVERIFIED — so this list is load-bearing, not decoration."""
    if result.get("note"):
        return [{"name": "kickoff-contract",
                 "expected": "a kickoff contract is declared",
                 "actual": result["note"],
                 "pass": True, "gating": False}]
    checks = []
    for i, entry in enumerate(result["measures"], 1):
        if entry.get("skipped"):
            actual = "skipped (--no-measure)"
        elif entry.get("timed_out"):
            actual = f"timed out after {entry.get('timeout_s')}s"
        else:
            actual = f"rc={entry.get('rc')}"
        checks.append({"name": f"measure-{i}",
                       "expected": "the kickoff's measure command exits 0",
                       "actual": f"{entry['command']} → {actual}",
                       "pass": bool(entry.get("pass")), "gating": True})
    escaped = result.get("escaped") or []
    plain = [p for p in result["missing"] if p not in escaped]
    parts = []
    if plain:
        parts.append("missing: " + ", ".join(plain))
    if escaped:
        parts.append("outside the run dir (refused): " + ", ".join(escaped))
    checks.append({
        "name": "required-artifacts",
        "expected": "every declared run-local artifact exists and is non-empty",
        "actual": "; ".join(parts) or "all present",
        "pass": not result["missing"], "gating": True})
    return checks


def _normal(run_dir: str, *, measure: bool) -> int:
    evidence = os.path.join(run_dir, EVIDENCE_NAME)
    json_path = os.path.join(run_dir, JSON_NAME)
    tsv_path = os.path.join(run_dir, CHECKS_TSV_NAME)
    with open(evidence, "w", encoding="utf-8") as log:
        log.write(f"# {NAME} evidence — run {run_dir}\n")
        result = evaluate(run_dir, measure=measure, log=log)
        log.write(f"kickoff source: {result['kickoff_source']}\n")
        if result.get("note"):
            log.write(f"note: {result['note']}\n")
        for entry in result["measures"]:
            log.write(f"measure: {entry}\n")
        for path, good in result["artifacts"].items():
            log.write(f"artifact: {'ok' if good else 'MISSING'} {path}\n")
        for token in result.get("escaped") or []:
            log.write(f"refused: {token} resolves outside the run dir\n")
        log.write(f"pass: {bool(result['pass'])}\n")

    checks = _checks(result)
    failed = [c["name"] for c in checks if not c["pass"] and c["gating"]]
    reasons = [f"{c['name']} failed: {c['actual']}" for c in checks
               if not c["pass"] and c["gating"]]
    with open(tsv_path, "w", encoding="utf-8") as tsv:
        for check in checks:
            tsv.write(f"{check['name']}\t{check['actual']}\t{'true' if check['pass'] else 'false'}\n")

    payload: dict = {
        "verifier": NAME,
        "pass": bool(result["pass"]),
        "verdict": "pass" if result["pass"] else "fail",
        "evidence_path": evidence,
        "measures": result["measures"],
        "artifacts": result["artifacts"],
        "missing": result["missing"],
        "escaped": result.get("escaped") or [],
        "kickoff_source": result["kickoff_source"],
        "checks": checks,
        "checks_run": [c["name"] for c in checks],
        "failed_checks": failed,
        "reasons": reasons,
        # `diagnose.py` reads a single `reason`/`summary` string for the failure
        # detail; `reasons` alone would leave a diagnosis without one.
        "reason": "; ".join(reasons) or ("all checks passed" if result["pass"] else ""),
    }
    if result.get("note"):
        payload["note"] = result["note"]
    # The executor copies its captured stdout over verifier_kickoff-contract.json
    # (`execute_handlers._handle_verifier`), so stdout — not this file — is what
    # the engine reads back. Serialize ONCE and use that same text for both, so
    # the two copies cannot drift. Writing the file too keeps the verifier
    # self-contained for hand-runs and for the unit tests.
    blob = json.dumps(payload, indent=2)
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(blob + "\n")
    sys.stdout.write(blob + "\n")
    return 0 if payload["pass"] else 1


def main(argv: list[str]) -> int:
    if "--replay-check" in argv:
        idx = argv.index("--replay-check")
        runs_dir = argv[idx + 1] if idx + 1 < len(argv) else ""
        if not runs_dir:
            sys.stderr.write("kickoff-contract: --replay-check needs a runs directory\n")
            return 1
        return replay_check(runs_dir)

    run_dir = (os.environ.get("MINI_ORK_RUN_DIR") or "").strip()
    if not run_dir:
        sys.stderr.write("kickoff-contract: MINI_ORK_RUN_DIR is not set\n")
        return 1
    return _normal(run_dir, measure="--no-measure" not in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
