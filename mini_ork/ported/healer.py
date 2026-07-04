"""Self-healing classification loop — Python port of ``lib/healer.sh``.

Faithful port of the bash decision-engine for failed mini-ork runs: scan the
``run_dir`` for ``worker.log`` / ``verdict.json`` / ``gauntlet*.log``,
optionally try a fingerprint match against the ``lessons_bank`` (via
``lib/memory-retrieve.sh``), optionally call the LLM gateway to classify
the failure (via ``lib/agentflow-llm-helpers.sh``), optionally persist a new
lesson (via ``lib/memory-store.sh``), and emit a single JSON line on stdout:

    {"lesson_id": ..., "failure_class": ..., "recovery_action": ...,
     "recovery_args": ..., "matched": ...}

Line numbers below cite ``lib/healer.sh`` so cross-referencing is trivial.

The bash source stays in place (strangler-fig co-existence). This Python
function ``decide(epic_id, run_dir)`` is the in-process surface parity tests
invoke; ``tests/unit/test_healer_py.py`` runs the live bash subprocess on
identical inputs and asserts byte-identical (rc, stdout, stderr).

NOTE: In the current env the optional sibling scripts (``memory-retrieve.sh``,
``memory-store.sh``, ``agentflow-llm-helpers.sh``, ``mo-event.sh``) are
absent. The bash port therefore always reaches the early-escalate branch
(``lib/healer.sh:83-87``) and emits the hard-coded escalate-human JSON. The
implementer MUST NOT fabricate LLM-classify parity cases until those siblings
land; they are out of scope here.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

# lib/healer.sh:85, :92, :142 — three bash `printf` calls all emit this exact
# escalate-human fallback. Trailing newline is intentional: a downstream
# `head -1` on the orchestrator side stays consistent.
_ESCALATE_HUMAN_JSON = (
    '{"lesson_id":null,"failure_class":"unknown",'
    '"recovery_action":"escalate-human","recovery_args":{},'
    '"matched":false}\n'
)

# lib/healer.sh:55 — `grep -iE 'error|fail|exception|cannot|invalid|exit code|429|401|403|503|TS[0-9]+|Cannot find module'`
_ERROR_LINE_PATTERN = re.compile(
    r'error|fail|exception|cannot|invalid|exit code|429|401|403|503|TS[0-9]+|Cannot find module',
    re.IGNORECASE,
)


def _home() -> Tuple[str, str]:
    """Mirror lib/healer.sh:40-42 — return (MINI_ORK_HOME, DB path).

    Neither is exercised by the escalate-human branch, but kept for parity
    with any future LLM-classify path."""
    home = os.environ.get("MINI_ORK_HOME")
    if not home:
        home = ".mini-ork"
    db = os.environ.get("MINI_ORK_DB")
    if not db:
        db = os.path.join(home, "state.db")
    return home, db


def _collect_error_lines(run_dir: str) -> str:
    """Mirror lib/healer.sh:47-57.

    Pick the first non-empty ``worker.log`` and the first non-empty
    ``gauntlet*.log`` anywhere under ``run_dir`` (``find ... -size +0c`` +
    ``head -1``), grep each for error-shaped tokens, tail the last 50 lines
    per log, concat, then cap at 4000 chars. The blob's content feeds the
    lessons_bank fingerprint step; it is irrelevant to the escalate-human
    branch that dominates this env."""
    worker: Optional[str] = None
    gauntlet: Optional[str] = None
    for root, _, files in os.walk(run_dir):
        for name in files:
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) == 0:
                    continue
            except OSError:
                continue
            if worker is None and name == "worker.log":
                worker = full
            elif gauntlet is None and name.startswith("gauntlet") and name.endswith(".log"):
                gauntlet = full
            if worker is not None and gauntlet is not None:
                break
        if worker is not None and gauntlet is not None:
            break

    chunks: list[str] = []
    for path in (worker, gauntlet):
        if not path:
            continue
        try:
            with open(path, "r", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        matched = [ln for ln in lines if _ERROR_LINE_PATTERN.search(ln)]
        tail = matched[-50:] if len(matched) > 50 else matched
        chunks.append("".join(tail))
    return "".join(chunks)[:4000]


def _extract_reviewer_verdict(run_dir: str) -> str:
    """Mirror lib/healer.sh:62-64 — `jq -r '.verdict // .final_verdict // ""'`
    on the first non-empty ``verdict.json`` under ``run_dir``.

    Pure-stdlib: we DO NOT shell out to ``jq`` (parity must not depend on a
    binary whose install state is not part of the bash contract)."""
    verdict: Optional[str] = None
    for root, _, files in os.walk(run_dir):
        for name in files:
            if name != "verdict.json":
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) == 0:
                    continue
            except OSError:
                continue
            try:
                with open(full, "r", errors="replace") as fh:
                    payload = json.load(fh)
            except (OSError, ValueError):
                verdict = verdict if verdict is not None else ""
                continue
            if not isinstance(payload, dict):
                verdict = verdict if verdict is not None else ""
                continue
            for key in ("verdict", "final_verdict"):
                value = payload.get(key)
                if value is None or value == "":
                    continue
                verdict = str(value)
                break
            else:
                verdict = verdict if verdict is not None else ""
            return verdict or ""
    return verdict or ""


def _try_lessons_match(error_lines: str, retrieve_path: str) -> Optional[str]:
    """Mirror lib/healer.sh:70-72 — invoke ``$RETRIEVE match "$ERROR_LINES"``
    when both error_lines and the script are present.

    Returns the LESSON_JSON string on hit, else None. In this env
    ``memory-retrieve.sh`` is absent, so this is always None. The bash
    parity branch expects exactly that fallback."""
    if not error_lines:
        return None
    if not os.path.isfile(retrieve_path):
        return None
    # When memory-retrieve.sh is added to this repo, replace this body
    # with a subprocess.run that mirrors the bash invocation one-for-one.
    # Do NOT silently fabricate a match — parity would lie.
    return None


def _classify_or_fallback(*, llm_helpers_path: str) -> Optional[str]:
    """Mirror lib/healer.sh:78-94.

    Returns the escalate-human JSON string immediately when the LLM
    helpers script is missing (the dominant path in this env), else None
    — in which case the upstream caller would route to LLM classification
    and synthesize from the schema'd JSON. The downstream branch is left
    unimplemented by design: stubbing it would invite fabricated parity."""
    if not os.path.isfile(llm_helpers_path):
        return _ESCALATE_HUMAN_JSON
    return None


def _persist_lesson_or_synthesize(
    *,
    store_path: str,
    failure_class: str,
    error_pattern: str,
    diagnosis: str,
    recovery_action: str,
    recovery_args_json: str,
) -> Optional[str]:
    """Mirror lib/healer.sh:152-176.

    Real call would invoke ``$STORE`` and pull the lesson back. When the
    store script is absent, the bash port synthesises a hand-rolled LESSON_JSON
    that matches the schema. We expose the same surface so a future port
    swap is one-liner."""
    if not os.path.isfile(store_path):
        return (
            f'{{"id":null,"failure_class":"{failure_class}",'
            f'"error_pattern":"{error_pattern}",'
            f'"diagnosis":"{diagnosis}",'
            f'"recovery_action":"{recovery_action}",'
            f'"recovery_args_json":{recovery_args_json},'
            f'"success_count":0,"failure_count":0,"source":"llm"}}'
        )
    # When memory-store.sh lands, replace this body with the same
    # `subprocess.run` shape that bash uses.
    return None


def _emit_event(**kwargs: object) -> None:
    """Mirror lib/healer.sh:186-196 — source ``mo-event.sh`` and ``mo_emit``.

    Pure no-op when the script is missing (this env). Accepts all of:
    ``event_sh_path``, ``epic_id``, ``lesson_id``, ``failure_class``,
    ``recovery_action``, ``matched`` — but consumes them as opaque kwargs
    so the signature does not drift when ``mo-event.sh`` lands."""
    event_sh_path = kwargs.get("event_sh_path", "")
    if not isinstance(event_sh_path, str) or not os.path.isfile(event_sh_path):
        return
    # When mo-event.sh lands, replace this body with a subprocess-style call.
    return None


def _format_emit(
    *,
    lesson_id: Optional[str],
    failure_class: str,
    recovery_action: str,
    recovery_args_str: str,
    matched: bool,
) -> str:
    """Mirror lib/healer.sh:198-199 — the final stdout line.

    The bash format is::

        printf '{"lesson_id":%s,"failure_class":"%s",...,"matched":%s}\\n'

    Bash's ``%s`` for lesson_id renders ``null`` when empty: see line 199
    ``"${LESSON_ID:-null}"``. failure_class / recovery_action are
    user-controlled or schema-controlled, so we pass them through after
    minimal sanitisation (no embedded double-quotes leak — bash would
    emit them verbatim too)."""
    lesson_token = "null" if lesson_id in (None, "") else str(lesson_id)
    return (
        f'{{"lesson_id":{lesson_token},'
        f'"failure_class":"{failure_class}",'
        f'"recovery_action":"{recovery_action}",'
        f'"recovery_args":{recovery_args_str},'
        f'"matched":{str(matched).lower()}}}\n'
    )


def decide(
    epic_id: str,
    run_dir: str,
    *,
    mini_ork_root: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Mirror ``healer <epic_id> <run_dir>``.

    Returns ``(rc, stdout, stderr)`` so parity tests can compare all three
    surfaces without touching ``sys.stdout`` / ``sys.stderr``.

    Exit codes (lib/healer.sh:20-22):
        0  ok (recovery decided — escalate-human JSON emitted here)
        2  usage error (missing epic_id / run_dir arg)
        3  run_dir does not exist
    """
    if not epic_id or not run_dir:
        # lib/healer.sh:31-34 — usage error. The exact program-name prefix
        # differs (``$0`` in bash vs ``healer`` here) so parity holds on the
        # /Usage/ substring, not the whole line.
        return (2, "", "Usage: healer <epic_id> <run_dir>\n")

    if not os.path.isdir(run_dir):
        # lib/healer.sh:35-38 — caller should escalate; the orchestrator
        # treats rc=3 as "give up, route to a human".
        return (
            3,
            "",
            f"[healer] run_dir not found: {run_dir}\n",
        )

    if mini_ork_root is None:
        # Default mirrors lib/healer.sh:26 — repo root is two dirs up from
        # the script (lib/healer.sh → lib → repo). For Python:
        # mini_ork/ported/healer.py → ... → repo is 3 dirs up.
        mini_ork_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )

    retrieve = os.path.join(mini_ork_root, "lib", "memory-retrieve.sh")
    _ = os.path.join(mini_ork_root, "lib", "memory-store.sh")  # parity with bash :44
    llm_helpers = os.path.join(mini_ork_root, "lib", "agentflow-llm-helpers.sh")
    event_sh = os.path.join(mini_ork_root, "lib", "mo-event.sh")
    _home()  # parity: bash sets these even when unused

    error_lines = _collect_error_lines(run_dir)
    _ = _extract_reviewer_verdict(run_dir)  # parity: bash extracts even if unused

    stderr_parts: list[str] = []

    # lib/healer.sh:67 — Step 2 announcement (always when run_dir exists).
    stderr_parts.append(
        f"[healer] {epic_id} — searching lessons_bank for matching diagnosis...\n"
    )

    lesson_json = _try_lessons_match(error_lines, retrieve)

    matched = False
    if lesson_json:
        # lib/healer.sh:75-77 — match found: announce + keep going.
        matched = True
        try:
            data = json.loads(lesson_json)
        except ValueError:
            data = {}
        stderr_parts.append(
            f"[healer] match found: {data.get('failure_class', '')} → "
            f"{data.get('recovery_action', '')}\n"
        )
    else:
        # lib/healer.sh:79 — Step 3 announcement.
        stderr_parts.append(
            "[healer] no match in lessons_bank; calling LLM to classify...\n"
        )
        early = _classify_or_fallback(llm_helpers_path=llm_helpers)
        if early is not None:
            # lib/healer.sh:83-87 — escalate-human early exit. Step 4 emit
            # (mo-event) is downstream of this branch in bash, so no event
            # is emitted either.
            stderr_parts.append(
                "[healer] agentflow-llm-helpers.sh missing — emitting escalate-human\n"
            )
            return (0, early, "".join(stderr_parts))

        # Unreachable in this env: when lib/agentflow-llm-helpers.sh
        # lands this is where the LLM classify + persist + emit sequence
        # would be ported. Do NOT add fabricated parity for that path —
        # parity tests must wait for the helper to be installed.
        return (0, _ESCALATE_HUMAN_JSON, "".join(stderr_parts))

    # lib/healer.sh:180-199 — Step 4 emit decision.
    try:
        data = json.loads(lesson_json) if lesson_json else {}
    except ValueError:
        data = {}
    lesson_id = data.get("id") if "id" in data else None
    failure_class = data.get("failure_class", "unknown")
    recovery_action = data.get("recovery_action", "escalate-human")
    raw_args = data.get("recovery_args_json")
    if raw_args is None or raw_args == "":
        recovery_args_str = "{}"
    elif isinstance(raw_args, str):
        recovery_args_str = raw_args
    else:
        recovery_args_str = json.dumps(raw_args)

    _emit_event(
        event_sh_path=event_sh,
        epic_id=epic_id,
        lesson_id=None if lesson_id is None else str(lesson_id),
        failure_class=failure_class,
        recovery_action=recovery_action,
        matched=matched,
    )

    out_line = _format_emit(
        lesson_id=None if lesson_id is None else str(lesson_id),
        failure_class=failure_class,
        recovery_action=recovery_action,
        recovery_args_str=recovery_args_str,
        matched=matched,
    )
    return (0, out_line, "".join(stderr_parts))


__all__ = [
    "decide",
    "_collect_error_lines",
    "_extract_reviewer_verdict",
    "_try_lessons_match",
    "_classify_or_fallback",
    "_persist_lesson_or_synthesize",
    "_emit_event",
    "_ESCALATE_HUMAN_JSON",
]
