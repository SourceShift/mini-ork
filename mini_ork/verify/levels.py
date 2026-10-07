"""Correctness level vector for run verdicts and the publish gate (G08-T05).

Correctness levels are empirically NON-NESTED: passing a shallow level does not
imply a deeper one. This module derives a fixed five-level vector from evidence
a run already writes — never from any LLM output — so a run can be published
only when every REQUIRED level is PROVEN:

    applies   — the implementer actually changed the target tree.
    executes  — the test verifier reached a test outcome (post_rc in {0, 1}).
    target    — a green suite PROVED it exercises the change (replay overlap).
    preserve  — no regression against a green base.
    contract  — a behavioral verifier PROVED the contract (unrequired today).

Each level is scored independently and is three-valued (PROVEN / REFUTED /
UNVERIFIED), mirroring :class:`mini_ork.verify.behavioral.BehavioralVerdict`
and :class:`mini_ork.runtime.engine.ExecOutcome`: PROVEN only from the
deterministic evidence it names, REFUTED from a demonstrated failure, and
UNVERIFIED (abstention — never a pass) otherwise. ``contract`` maps to
``"n/a"`` when no behavioral verifier produced a file, and ``target`` maps
to ``"n/a"`` when the replay instrument does not apply to the test runner
(a non-pytest command) — an ``n/a`` level does not block publish.

Opt-out knob ``MO_LEVEL_VECTOR`` (DEFAULT ON; ``"0"`` disables). The knob is
read ONLY by :func:`enabled`; :func:`derive_levels` / :func:`level_report`
never read it. Only ``"1"`` (or unset) enables — ``"0"``, ``"true"`` and any
other value are OFF.

Import-time contract: stdlib + ``mini_ork.context.context_env`` + the three
status constants. No ``subprocess``, no ``mini_ork.cli`` / ``mini_ork.dispatch``,
no LLM, no network — importing this module never pulls a dispatch seam.
"""
from __future__ import annotations

import json
import os

from mini_ork.context import context_env
from mini_ork.verify.behavioral import PROVEN, REFUTED, UNVERIFIED

LEVELS = ("applies", "executes", "target", "preserve", "contract")
NA = "n/a"
VALUES = (PROVEN, REFUTED, UNVERIFIED, NA)

# The ONLY declaration point for required levels. `contract` stays unrequired
# until a contract producer is wired into the code-fix workflow.
REQUIRED_LEVELS = {"code_fix": ("applies", "executes", "target", "preserve")}


def required_levels(task_class):
    """The levels a task class must PROVE before publish; ``()`` for any other
    class (the gate passes; the vector is still recorded)."""
    return REQUIRED_LEVELS.get(task_class, ())


def enabled(environ=None):
    """True only when ``MO_LEVEL_VECTOR == "1"`` (DEFAULT ON; ``"0"`` disables).

    Reads ``environ`` when given, else ``context_env("MO_LEVEL_VECTOR", "1")``.
    """
    if environ is not None:
        return environ.get("MO_LEVEL_VECTOR", "1") == "1"
    return context_env("MO_LEVEL_VECTOR", "1") == "1"


def read_verifier_payload(path, verifier):
    """The LAST JSON object in ``path`` whose ``verifier`` field equals ``verifier``.

    The evidence file is NOT pure JSON: ``_run_verifier_ref`` merges stdout+stderr,
    so ``verifier_test.json`` starts with ``[test] running: …`` stderr lines, and
    ``verifier_behavioral.json`` is ``indent=2`` multi-line JSON. Scan bottom-up
    for the last line whose ``lstrip()`` starts with ``{``, then try the whole
    remainder and the single line. Returns ``None`` when absent or unparsable.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith("{"):
            continue
        for text in ("\n".join(lines[i:]), lines[i]):
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("verifier") == verifier:
                return obj
    return None


def _read_json_dict(path):
    """The JSON object at ``path``, or None when absent/unparsable/not a dict."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def derive_levels(run_dir):
    """Derive the five-level vector ``(vector, reasons)`` from run evidence.

    Inputs (all named deterministic producers):
      S = <run_dir>/implementer-summary.json   (git-derived, _write_implementer_summary)
      T = read_verifier_payload(<run_dir>/verifier_test.json, "test")
      B = read_verifier_payload(<run_dir>/verifier_behavioral.json, "behavioral")

    First match wins per level; anything not listed is UNVERIFIED. Never raises
    and never reads a reviewer/panel/eval/rubric file. Each reason is prefixed
    by its source file name.
    """
    run_dir = run_dir or ""
    S = _read_json_dict(os.path.join(run_dir, "implementer-summary.json"))
    T = read_verifier_payload(os.path.join(run_dir, "verifier_test.json"), "test") or {}
    B = read_verifier_payload(os.path.join(run_dir, "verifier_behavioral.json"), "behavioral")

    vector = dict.fromkeys(LEVELS, UNVERIFIED)
    reasons = dict.fromkeys(LEVELS, "")

    # applies ← implementer-summary.json (status + files_changed). `implemented`
    # with an empty/absent files_changed is NOT proven: the git derivation can
    # fall back to `implemented` with no files when it could not derive a list.
    status = (S or {}).get("status")
    files_changed = (S or {}).get("files_changed")
    changed = files_changed if isinstance(files_changed, list) else None
    if status == "implemented" and changed and all(
        isinstance(entry, str) for entry in changed
    ):
        vector["applies"] = PROVEN
        reasons["applies"] = (
            f"implementer-summary.json: implemented, files_changed={len(changed)}"
        )
    elif status == "no_changes":
        vector["applies"] = REFUTED
        reasons["applies"] = "implementer-summary.json: no_changes"
    else:
        reasons["applies"] = "implementer-summary.json: missing or empty files_changed"

    # Derived values shared by executes / target / preserve.
    post_rc = T.get("post_rc")
    rc = post_rc if isinstance(post_rc, int) else None
    base_green = str(T.get("base_rc", "")) == "0"
    replay = T.get("replay")
    ov = (
        replay.get("overlap")
        if isinstance(replay, dict) and isinstance(replay.get("overlap"), list)
        else None
    )
    ru = T.get("replay_unverified") is True
    au = T.get("adequacy_unverified") is True

    # executes — the runner reached a test outcome.
    if rc in (0, 1):
        vector["executes"] = PROVEN
        reasons["executes"] = f"verifier_test.json: post_rc={rc}"
    elif isinstance(rc, int) and base_green:
        vector["executes"] = REFUTED
        reasons["executes"] = f"verifier_test.json: post_rc={rc} on green base"
    else:
        reasons["executes"] = "verifier_test.json: no post_rc"

    # target — a green suite PROVED it exercises the change (replay overlap).
    # When the replay instrument does not apply to the test runner (a
    # non-pytest command), a green suite cannot be proven by replay; record
    # n/a (publish does not block) rather than UNVERIFIED.
    if T.get("replay_applicable") is False and rc == 0:
        vector["target"] = NA
        reasons["target"] = "verifier_test.json: replay instrument n/a for this test runner"
    elif not (ru or au) and rc == 0 and T.get("pass") is True and ov:
        vector["target"] = PROVEN
        reasons["target"] = "verifier_test.json: pass with replay overlap"
    elif not (ru or au) and rc == 0 and T.get("pass") is False and ov == []:
        vector["target"] = REFUTED
        reasons["target"] = "verifier_test.json: pass=false, no overlap"
    else:
        reasons["target"] = "verifier_test.json: target not proven"

    # preserve — no regression against a green base. `au` downgrades PROVEN: the
    # suite is the only witness for "no regression" and was measured unable to
    # detect faults in the changed files. `ru` does not affect preserve (rc==0
    # already shows every test passes).
    if not au and rc == 0:
        vector["preserve"] = PROVEN
        reasons["preserve"] = "verifier_test.json: post_rc=0"
    elif rc not in (None, 0) and base_green:
        vector["preserve"] = REFUTED
        reasons["preserve"] = f"verifier_test.json: post_rc={rc} on green base (regression)"
    else:
        reasons["preserve"] = "verifier_test.json: preserve not proven"

    # contract ← verifier_behavioral.json. Absent file → n/a; present but
    # unparsable or any other status → UNVERIFIED.
    b_path = os.path.join(run_dir, "verifier_behavioral.json")
    if B is None:
        if os.path.isfile(b_path):
            reasons["contract"] = "verifier_behavioral.json: unparsable"
        else:
            vector["contract"] = NA
            reasons["contract"] = "verifier_behavioral.json: absent"
    elif B.get("status") == PROVEN:
        vector["contract"] = PROVEN
        reasons["contract"] = "verifier_behavioral.json: PROVEN"
    elif B.get("status") == REFUTED:
        vector["contract"] = REFUTED
        reasons["contract"] = "verifier_behavioral.json: REFUTED"
    else:
        reasons["contract"] = f"verifier_behavioral.json: status={B.get('status')!r}"

    return vector, reasons


def publish_decision(vector, *, required):
    """``"refute"`` if any required level is REFUTED; else ``"abstain"`` if any
    required level is UNVERIFIED or missing; else ``"publish"``. A level that
    is ``"n/a"`` (the instrument does not apply to this run) does not block —
    it is skipped. Empty ``required`` → ``"publish"``."""
    required = tuple(required)
    if not required:
        return "publish"
    for level in required:
        if vector.get(level, UNVERIFIED) == REFUTED:
            return "refute"
    for level in required:
        if vector.get(level, UNVERIFIED) not in (PROVEN, NA):
            return "abstain"
    return "publish"


def all_levels_ok(vector, *, required):
    """True when every required level is PROVEN (``publish_decision == "publish"``)."""
    return publish_decision(vector, required=required) == "publish"


def level_report(run_dir, task_class):
    """The run-level report stamped into verdict.json and read by the publisher.

    Returns exactly these keys, in order: ``levels``, ``levels_reasons``,
    ``levels_required``, ``levels_ok``, ``levels_decision``.
    """
    vector, reasons = derive_levels(run_dir)
    required = list(required_levels(task_class))
    return {
        "levels": vector,
        "levels_reasons": reasons,
        "levels_required": required,
        "levels_ok": all_levels_ok(vector, required=required),
        "levels_decision": publish_decision(vector, required=required),
    }
