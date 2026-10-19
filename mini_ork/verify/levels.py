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

# The ONLY declaration point for required levels. `code_fix` proves the
# tree-change quartet through its own instruments. `verified_artifact` proves
# `contract` through its recipe verifier's published evidence (see
# `_contract_from_recipe_verifiers`) — the artifact recipe changes no source
# tree, so its only provable level is the contract its deterministic verifier
# checks (run-1791736854: verdict.json said publish with every level UNVERIFIED
# while evidence/schema.log held a full pass).
REQUIRED_LEVELS = {
    "code_fix": ("applies", "executes", "target", "preserve"),
    "verified_artifact": ("contract",),
}

# Classes whose recipes change a source tree and therefore OWN the
# applies/executes/target/preserve instruments (implementer-summary.json and
# verifier_test.json are both written by code-fix-shaped nodes). For any other
# class an ABSENT instrument is n/a (the level cannot apply to the recipe), not
# UNVERIFIED — abstention is reserved for "the instrument ran and could not
# prove", never for "this recipe never had the instrument".
_CODE_EDIT_CLASSES = {"code_fix"}


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


def _iter_json_objects(path):
    """Every dict parseable from ``path``, bottom-up.

    Evidence files are NOT pure JSON: ``_run_verifier_ref`` merges stdout+stderr,
    so ``verifier_test.json`` starts with ``[test] running: …`` stderr lines, and
    ``verifier_behavioral.json`` is ``indent=2`` multi-line JSON. Scan from the
    last line whose ``lstrip()`` starts with ``{``, trying each such line as the
    whole remainder first and then as that single line, so the LAST blob (the
    verifier's own envelope) is yielded before anything it may contain.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith("{"):
            continue
        for text in ("\n".join(lines[i:]), lines[i]):
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                yield obj


def read_verifier_payload(path, verifier):
    """The LAST JSON object in ``path`` whose ``verifier`` field equals ``verifier``.

    Last, not first: :func:`_iter_json_objects` walks the file bottom-up and
    yields the trailing blob before anything nested inside it, so returning on
    the first match is returning the last writer's verdict — which is the one
    a retry appended last. Returns ``None`` when absent or unparsable.
    """
    for obj in _iter_json_objects(path):
        if obj.get("verifier") == verifier:
            return obj
    return None


def read_single_verifier_payload(path):
    """The one JSON object ``path`` holds, or ``None`` when it holds none or several.

    The counterpart to :func:`read_verifier_payload` for a file whose name
    already says which verifier wrote it (``verifier_<stem>.json`` at the run
    root): only 62 of the recipes' 123 verifier scripts put a ``verifier`` field
    in their payload, so requiring one there reads the other half as "no verdict
    at all". Several distinct objects mean a merged evidence log, whose objects
    are told apart by that field — not one node's verdict, so that is ``None``.

    ``_iter_json_objects`` yields a single-line file's object twice (the whole
    remainder from that line, then the line alone), so the objects are compared
    by value rather than counted, and the scan stops at the first disagreement.
    """
    found = None
    for obj in _iter_json_objects(path):
        if found is None:
            found = obj
        elif obj != found:
            return None
    return found


def _read_json_dict(path):
    """The JSON object at ``path``, or None when absent/unparsable/not a dict."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# Evidence files that are NOT contract instruments: the code-fix test replay
# and the behavioral verifier own their levels; every other verifier_<stem>.json
# at the run root is a recipe verifier's declared output.
_CONTRACT_EVIDENCE_EXCLUDE = {"verifier_test.json", "verifier_behavioral.json"}

# ── Verifier failure provenance (audit 2026-10-11) ───────────────────────────
# A verdict used to be a bare boolean, so "the tool ran and rejected the input"
# and "the tool could not be started" were both `pass: false` — one bad lint call
# (npm refusing to start on a pnpm repo, EBADDEVENGINES) cascaded into 10 failed
# nodes and a rollback of an implementation no tool had actually checked. A
# verifier may now declare WHY it failed:
#   code       the tool ran and rejected the input          → REFUTED
#   toolchain  the tool could not be run at all (spawn failure, a refusal like
#              EBADDEVENGINES, a missing binary)            → UNVERIFIED
#   infra      sandbox/network/OOM                          → UNVERIFIED
# FAIL-CLOSED: an absent or unrecognised value keeps the verdict a plain
# boolean, so every existing verdict keeps today's meaning and only an explicit
# ABSTAINING_KINDS member ever abstains. `infra` ships declared but
# non-abstaining until a verifier can actually detect it.
FAILURE_KINDS = ("code", "toolchain", "infra")
ABSTAINING_KINDS = frozenset({"toolchain"})
# The executor's third node outcome: 0 pass / 1 refuted / 3 abstained. A
# subprocess rc cannot collide — no verifier exits 3, and MO_BEHAV_ABSTAIN_EXIT
# (the canonical verifier's own abstain rc) defaults to 1.
ABSTAIN_RC = 3


def failure_kind(obj):
    """``obj['failure_kind']`` when it is a recognised kind, else ``""`` (fail-closed)."""
    if not isinstance(obj, dict):
        return ""
    kind = obj.get("failure_kind")
    return kind if kind in FAILURE_KINDS else ""


def abstain_kind(obj):
    """The abstention ``obj`` declares, else ``""``.

    Only an :data:`ABSTAINING_KINDS` member abstains, and never on a verdict that
    already passed: a contradictory ``{"pass": true, "failure_kind": "toolchain"}``
    stays a pass rather than becoming an abstention.
    """
    kind = failure_kind(obj)
    if kind and kind in ABSTAINING_KINDS and obj.get("pass") is not True:
        return kind
    return ""


def _read_verifier_evidence(path):
    """The last JSON object in ``path`` shaped like a recipe verifier's envelope.

    Recipe verifier evidence (e.g. verified-artifact's schema.py) is published
    as merged stdout+stderr followed by a pretty-printed
    ``{"pass": bool, "checks": [{"id", "pass", "gating", ...}, ...]}`` envelope.
    Scan bottom-up like :func:`read_verifier_payload`; the envelope's shape
    (bool ``pass`` + list ``checks``) distinguishes it from the inner check
    objects, which fail the guard.
    """
    for obj in _iter_json_objects(path):
        if isinstance(obj.get("pass"), bool) and isinstance(obj.get("checks"), list):
            return obj
    return None


def abstained_kinds(run_dir):
    """Sorted distinct abstention kinds across a run's verifier evidence.

    The field only ever appears on a verifier's own envelope, so the bottom-up
    scan takes the last dict carrying a recognised ``failure_kind`` per file —
    no shape guard, which matters because the recipe verifiers that shell out
    (e.g. tier1-compile-typecheck) publish ``pass`` without a ``checks`` list and
    would fail :func:`_read_verifier_evidence`'s envelope guard.

    Falls back to the run-level ``verdict.json`` / ``run-verdict.json``
    ``failure_kinds`` stamp: under remote placement the local copy of a
    verifier's evidence is best-effort, so the stamp is the second witness.
    """
    if not run_dir:
        return []
    kinds = set()
    try:
        names = sorted(
            name for name in os.listdir(run_dir)
            if name.startswith("verifier_") and name.endswith(".json")
        )
    except OSError:
        names = []
    for name in names:
        for obj in _iter_json_objects(os.path.join(run_dir, name)):
            kind = abstain_kind(obj)
            if kind:
                kinds.add(kind)
                break  # the file's own envelope; nothing deeper can declare it
    if not kinds and not names:
        # Only when there is no local verifier evidence at all. With evidence
        # present but silent, the files are authoritative — falling back here
        # would resurrect a previous attempt's stamp in a reused run dir and
        # withhold a run whose runner has since been fixed.
        for stamp in ("verdict.json", "run-verdict.json"):
            data = _read_json_dict(os.path.join(run_dir, stamp))
            for kind in (data or {}).get("failure_kinds") or []:
                if kind in FAILURE_KINDS:
                    kinds.add(str(kind))
    return sorted(kinds)


def _contract_from_recipe_verifiers(run_dir):
    """``(PROVEN|REFUTED|UNVERIFIED|None, reason)`` from verifier_<stem>.json
    evidence at the run root. ``None`` = no such file (the caller decides n/a
    vs UNVERIFIED from the task class). Any demonstrated failure (envelope
    ``pass:false`` or a failed gating check) REFUTES; an envelope that declares
    it could not run its tool (:func:`abstain_kind`) and an unparsable evidence
    file with no refutation are both UNVERIFIED (abstain — never a pass)."""
    if not run_dir:
        return None, ""
    try:
        names = sorted(
            name for name in os.listdir(run_dir)
            if name.startswith("verifier_") and name.endswith(".json")
            and name not in _CONTRACT_EVIDENCE_EXCLUDE
        )
    except OSError:
        return None, ""
    verdicts = {}
    for name in names:
        obj = _read_verifier_evidence(os.path.join(run_dir, name))
        if obj is None:
            verdicts[name] = "unparsable"
            continue
        if abstain_kind(obj):
            # The tool could not run, so its checks say nothing about the code:
            # an abstention outranks a failed gating check. Never "proven".
            verdicts[name] = "abstained"
            continue
        if str(obj.get("status") or "").strip().lower() == "unverified":
            # A second way an envelope declares the same thing, and the one the
            # rest of the engine already reads: `eval_judge._verifier_passed`,
            # `execute_handlers` and `run_flow` all treat `status: unverified`
            # as "this instrument could not judge", not as a pass. Reading it as
            # one here minted a PROVEN contract level out of a skip — the
            # verifier's own payload says it examined nothing.
            verdicts[name] = "abstained"
            continue
        gating_failed = any(
            isinstance(check, dict)
            and check.get("gating") is True
            and check.get("pass") is not True
            for check in obj["checks"]
        )
        verdicts[name] = "refuted" if (obj["pass"] is False or gating_failed) else "proven"
    if not verdicts:
        return None, ""
    if "refuted" in verdicts.values():
        bad = ", ".join(n for n, v in verdicts.items() if v == "refuted")
        return REFUTED, f"recipe verifier evidence REFUTED ({bad})"
    if "abstained" in verdicts.values():
        bad = ", ".join(n for n, v in verdicts.items() if v == "abstained")
        return UNVERIFIED, f"recipe verifier could not run ({bad})"
    if "unparsable" in verdicts.values():
        bad = ", ".join(n for n, v in verdicts.items() if v == "unparsable")
        return UNVERIFIED, f"recipe verifier evidence unparsable ({bad})"
    return PROVEN, "recipe verifier evidence PROVEN (" + ", ".join(verdicts) + ")"


def derive_levels(run_dir, task_class=None):
    """Derive the five-level vector ``(vector, reasons)`` from run evidence.

    Inputs (all named deterministic producers):
      S = <run_dir>/implementer-summary.json   (git-derived, _write_implementer_summary)
      T = read_verifier_payload(<run_dir>/verifier_test.json, "test")
      B = read_verifier_payload(<run_dir>/verifier_behavioral.json, "behavioral")
      R = verifier_<stem>.json envelopes at the run root (recipe verifiers)

    ``task_class`` (optional; the verdict path always passes it) selects the
    instruments a recipe OWNS: for a class outside ``_CODE_EDIT_CLASSES`` an
    ABSENT S or T is n/a ("this recipe never had the instrument"), while a
    present-but-unproven instrument still abstains. Without ``task_class``
    (legacy callers, tests) every absent instrument stays UNVERIFIED.

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

    # Instrument ownership. `has_test` gates the n/a short-circuit: a present
    # verifier_test.json is scored normally whatever the class.
    has_summary = S is not None
    has_test = os.path.isfile(os.path.join(run_dir, "verifier_test.json"))
    owns_tree_instruments = (
        task_class is not None and task_class in _CODE_EDIT_CLASSES
    )

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
    elif not has_summary and not owns_tree_instruments and task_class is not None:
        vector["applies"] = NA
        reasons["applies"] = f"implementer-summary.json: instrument n/a for task_class={task_class}"
    else:
        reasons["applies"] = "implementer-summary.json: missing or empty files_changed"

    # The applies n/a verdict above means the whole tree-change quartet is
    # outside this recipe; without test evidence the remaining three levels
    # are n/a together rather than three copies of the same abstention.
    tree_levels_na = (
        task_class is not None
        and not owns_tree_instruments
        and not has_summary
        and not has_test
    )

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
    # An adapter-less runner (a wrapper script, `make test`, `go test ./...`)
    # cannot name overlapping tests, so it proves the delta at suite grain:
    # the whole command flipped red→green with tests running on both sides.
    opaque_proof = (
        isinstance(replay, dict)
        and replay.get("proven_by") == "exit-code-delta"
    )

    # executes — the runner reached a test outcome.
    if tree_levels_na:
        vector["executes"] = NA
        reasons["executes"] = f"verifier_test.json: instrument n/a for task_class={task_class}"
    elif rc in (0, 1):
        vector["executes"] = PROVEN
        reasons["executes"] = f"verifier_test.json: post_rc={rc}"
    elif isinstance(rc, int) and base_green:
        vector["executes"] = REFUTED
        reasons["executes"] = f"verifier_test.json: post_rc={rc} on green base"
    else:
        reasons["executes"] = "verifier_test.json: no post_rc"

    # target — a green suite PROVED it exercises the change (replay overlap),
    # or — for an adapter-less runner — the whole suite flipped red→green with
    # tests running on both sides (opaque exit-code delta). When the replay
    # instrument truly does not apply (no adapter, and the output carries no
    # test-run marker), record n/a (publish does not block) rather than
    # UNVERIFIED.
    if tree_levels_na:
        vector["target"] = NA
        reasons["target"] = f"verifier_test.json: instrument n/a for task_class={task_class}"
    elif T.get("replay_applicable") is False and rc == 0:
        vector["target"] = NA
        reasons["target"] = "verifier_test.json: replay instrument n/a for this test runner"
    elif not (ru or au) and rc == 0 and T.get("pass") is True and (ov or opaque_proof):
        vector["target"] = PROVEN
        reasons["target"] = (
            "verifier_test.json: pass with replay overlap" if ov
            else "verifier_test.json: pass with opaque exit-code delta"
        )
    elif not (ru or au) and rc == 0 and T.get("pass") is False and ov == []:
        vector["target"] = REFUTED
        reasons["target"] = "verifier_test.json: pass=false, no overlap"
    else:
        reasons["target"] = "verifier_test.json: target not proven"

    # preserve — no regression against a green base. `au` downgrades PROVEN: the
    # suite is the only witness for "no regression" and was measured unable to
    # detect faults in the changed files. `ru` does not affect preserve (rc==0
    # already shows every test passes).
    if tree_levels_na:
        vector["preserve"] = NA
        reasons["preserve"] = f"verifier_test.json: instrument n/a for task_class={task_class}"
    elif not au and rc == 0:
        vector["preserve"] = PROVEN
        reasons["preserve"] = "verifier_test.json: post_rc=0"
    elif rc not in (None, 0) and base_green:
        vector["preserve"] = REFUTED
        reasons["preserve"] = f"verifier_test.json: post_rc={rc} on green base (regression)"
    else:
        reasons["preserve"] = "verifier_test.json: preserve not proven"

    # contract ← verifier_behavioral.json, else the recipe's own contract
    # verifier evidence (verifier_<stem>.json envelopes at the run root). With
    # neither instrument present, a task class that REQUIRES contract records
    # UNVERIFIED (a required level may never ride an absent instrument into
    # publish); every other class records n/a.
    b_path = os.path.join(run_dir, "verifier_behavioral.json")
    if B is not None and B.get("status") == PROVEN:
        vector["contract"] = PROVEN
        reasons["contract"] = "verifier_behavioral.json: PROVEN"
    elif B is not None and B.get("status") == REFUTED:
        vector["contract"] = REFUTED
        reasons["contract"] = "verifier_behavioral.json: REFUTED"
    elif B is not None:
        reasons["contract"] = f"verifier_behavioral.json: status={B.get('status')!r}"
    elif os.path.isfile(b_path):
        reasons["contract"] = "verifier_behavioral.json: unparsable"
    else:
        status, reason = _contract_from_recipe_verifiers(run_dir)
        if status is PROVEN:
            vector["contract"] = PROVEN
            reasons["contract"] = reason
        elif status is REFUTED:
            vector["contract"] = REFUTED
            reasons["contract"] = reason
        elif status is UNVERIFIED:
            reasons["contract"] = reason
        elif "contract" in required_levels(task_class or ""):
            reasons["contract"] = (
                "no contract-verifier evidence (required for task_class="
                f"{task_class})"
            )
        else:
            vector["contract"] = NA
            reasons["contract"] = "verifier_behavioral.json: absent"

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
    vector, reasons = derive_levels(run_dir, task_class)
    required = list(required_levels(task_class))
    return {
        "levels": vector,
        "levels_reasons": reasons,
        "levels_required": required,
        "levels_ok": all_levels_ok(vector, required=required),
        "levels_decision": publish_decision(vector, required=required),
    }
