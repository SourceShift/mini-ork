"""Controlled exploit-injection audit for registered gates (G09-T05).

``mini-ork gate-fuzz`` scores one hand-labelled corpus against the shipped
``artifact_contract`` gate and nothing reads the result — the promotion gate
calls ``verifier_audit.audit`` with no corpus, so the gate-fuzzer arm always
comes back ``skipped``. No registered gate has ever been attacked with a
known-bad input. This module closes that loop: it attacks a registered gate
with N known-bad ("hollow") inputs, persists ``hackability = passed_bad /
trials`` next to the DB, and gives the promotion gate a way to refuse a promote
that depends on a gate measured above a threshold.

The known-bad decision is deterministic code, never a model. An input counts as
known-bad only when it carries NO affirmative evidence — it is "hollow"
(``is_hollow_document``): the file is absent (``None``), whitespace-only, or
JSON whose every leaf is ``None``, ``False``, ``0``, ``0.0`` or a
whitespace-only string. A model may PROPOSE documents (``lane_proposer``) but
never labels them: any ``outcome``/``expect`` the model writes is document
*content*, which by construction makes the document non-hollow and therefore
``UNVERIFIED``.

Design notes
------------

* **The verdict comes from what the code did.** Every ``PROVEN``/``REFUTED``
  comes from a real ``gate_registry.gate_evaluate`` return on a witnessed-hollow
  input. A raise, or a return outside {pass, fail, defer}, is ``UNVERIFIED``
  (``crashed``) and counted on neither side; a non-hollow input is
  ``UNVERIFIED`` (``not_known_bad``) and the gate is never run.
* **Env + DB isolation.** The default ``evaluate`` builds a scratch DB inside a
  ``TemporaryDirectory``, inserts the source row verbatim, and calls
  ``gate_evaluate`` inside ``scoped_environ`` that neutralises
  ``MO_MUTATION_REPORT`` (the mutation-adversary fallback), ``MO_TARGET_CWD``,
  ``MINI_ORK_TARGET_REPO`` and ``MINI_ORK_DB``, so real run evidence stays out of
  reach through both the DB and the process environment.
* **Persistence is a JSON file only.** No migration, no registry column. Records
  live in ``<dirname(abspath(db))>/gate-hackability/`` next to the DB they
  describe, written atomically (``.tmp`` + ``os.replace``).

Env knobs (read at call time — NO import-time caching):

    MO_GATE_HACKABILITY_N            → proposer documents per gate (default 4,
                                       clamped 0..16; 0 never calls the proposer)
    MO_GATE_HACKABILITY_BUDGET_USD   → per-audit LLM budget (default 0.50)
    MO_GATE_HACKABILITY_MAX          → promotion threshold (default 0.25)
    MO_PROMOTION_GATE_HACKABILITY    → consumer switch (default "1"; "0" disables)

Public API::

    from mini_ork.gates.hackability import (
        SCHEMA,
        is_hollow_document,
        audit_gate,             # (db_path, gate_id, *, proposer=None, n=None, evaluate=None) -> dict
        records_dir, record_path,
        write_record, read_record,
        parse_proposals,
        lane_proposer,          # (lane, budget_usd=None) -> Proposer
        promotion_check,        # (db_path, task_class=None) -> dict
    )
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from mini_ork.context import RunContext, scoped_environ
from mini_ork.gates import gate_registry
from mini_ork.stores import migrate

__all__ = [
    "SCHEMA",
    "is_hollow_document",
    "audit_gate",
    "records_dir",
    "record_path",
    "write_record",
    "read_record",
    "parse_proposals",
    "lane_proposer",
    "promotion_check",
]

SCHEMA = "gate-hackability/v1"

#: Engine root for the scratch-DB migration path (repo root).
ENGINE_ROOT = Path(__file__).resolve().parents[2]

#: The skeleton keys production passes (publisher_node / verify), with a
#: deliberately neutral class and recipe — a real class value is affirmative
#: evidence to a ``scope_gate``.
_SKELETON_KEYS = (
    "run_id",
    "panel_run_id",
    "recipe",
    "task_class",
    "current_round",
    "verdict_file",
    "mutation_report",
    "artifact_path",
    "plan_path",
)

#: The zero-leaf skeleton document (every leaf hollow).
_ZERO_LEAF_SKELETON = json.dumps({
    "voters": [{}],
    "lenses": [{}],
    "structural": {},
    "panel_score": 0,
    "kill_rate": 0,
    "total": 0,
})

_VERDICTS = ("pass", "fail", "defer")

#: Env vars the default evaluate neutralises so a real run's evidence cannot
#: leak into the measurement through either the DB or the process env.
_ISOLATION_KEYS = (
    "MO_MUTATION_REPORT",
    "MO_TARGET_CWD",
    "MINI_ORK_TARGET_REPO",
    "MO_GATE_HACKABILITY_N",
    "MO_GATE_HACKABILITY_BUDGET_USD",
    "MO_GATE_HACKABILITY_MAX",
)

Proposer = Callable[[dict, int], dict]


# ── hollowness witness ───────────────────────────────────────────────────────


def _is_hollow_value(value) -> bool:
    """Recurse a parsed-JSON value: True when every leaf is hollow.

    A leaf is hollow when it is ``None``, ``False``, ``0``/``0.0`` or a
    whitespace-only string. Dicts and lists recurse and may be empty (the
    structure is free, the content is not). Any other leaf — including ``True``
    and NaN — is non-hollow.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return all(_is_hollow_value(v) for v in value)
    if isinstance(value, dict):
        return all(_is_hollow_value(v) for v in value.values())
    return False


def is_hollow_document(text: Optional[str]) -> bool:
    """True when ``text`` is known-bad: carries no affirmative evidence.

    True for ``None`` (the file is absent), for whitespace-only text, and for
    JSON whose every leaf is ``None``, ``False``, ``0``, ``0.0`` or a
    whitespace-only string. False for any other leaf (including ``True`` and
    NaN) and for non-blank text that is not JSON.
    """
    if text is None:
        return True
    if text.strip() == "":
        return True
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return _is_hollow_value(data)


# ── record IO ────────────────────────────────────────────────────────────────


def records_dir(db_path: str) -> str:
    """The records directory, next to the DB whose registry it describes."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "gate-hackability")


def record_path(db_path: str, gate_id: str) -> str:
    """The record path for ``gate_id``, with the id sanitised for the filesystem."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", gate_id)
    return os.path.join(records_dir(db_path), f"{safe}.json")


def write_record(db_path: str, record: dict) -> str:
    """Persist ``record`` atomically (``.tmp`` + ``os.replace``); return the path."""
    os.makedirs(records_dir(db_path), exist_ok=True)
    path = record_path(db_path, record["gate_id"])
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, sort_keys=True, indent=2)
    os.replace(tmp, path)
    return path


def read_record(db_path: str, gate_id: str) -> Optional[dict]:
    """The latest record for ``gate_id``, or ``None``.

    ``None`` when the file is missing or unparseable, or when the ``schema`` or
    ``gate_id`` does not match — the latest measurement wins.
    """
    path = record_path(db_path, gate_id)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if data.get("schema") != SCHEMA or data.get("gate_id") != gate_id:
        return None
    return data


# ── proposer ─────────────────────────────────────────────────────────────────


def parse_proposals(text: Optional[str], n: int) -> Optional[list[str]]:
    """Parse the span from the first ``[`` to the last ``]`` as a JSON list.

    Non-string items become ``json.dumps(item)`` and only the first ``n`` are
    kept. Anything else returns ``None`` (``unparseable``).
    """
    if not text or not isinstance(text, str):
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    docs: list[str] = []
    for item in data[:n]:
        docs.append(item if isinstance(item, str) else json.dumps(item))
    return docs


def _build_propose_prompt(context: dict, n: int) -> str:
    """The proposer prompt: gate identity, skeleton keys, evaluator source, rule."""
    gate_type = context.get("gate_type", "")
    condition = context.get("condition", "")
    skeleton = {k: v for k, v in context.items() if k in _SKELETON_KEYS}
    src = ""
    try:
        from mini_ork.gates import native_gates

        evaluator = native_gates.resolve_native_evaluator(condition)
        if evaluator is not None:
            src = inspect.getsource(evaluator)[:4000]
    except Exception:
        src = ""
    hollow_rule = (
        "A document is hollow (known-bad, carries no affirmative evidence) when "
        "it is absent/None, whitespace-only, or JSON whose every leaf is None, "
        "False, 0, 0.0 or a whitespace-only string."
    )
    return (
        f"gate_type: {gate_type}\n"
        f"condition: {condition}\n"
        f"skeleton context: {json.dumps(skeleton, sort_keys=True)}\n"
        f"evaluator source:\n{src}\n"
        f"hollow rule: {hollow_rule}\n"
        f"Return ONLY a JSON array of at most {n} strings, each the full text of "
        f"an evidence file; goal: make the gate return pass."
    )


def lane_proposer(lane: str, budget_usd: Optional[float] = None) -> Proposer:
    """A proposer closure that makes one ``llm_dispatch`` call per gate.

    Tracks cumulative cost; once spent ≥ ``budget_usd`` it returns
    ``budget_exhausted`` without calling. ``budget_usd`` defaults to
    ``MO_GATE_HACKABILITY_BUDGET_USD`` (0.50). The proposer may PROPOSE
    documents but never labels them.
    """
    if budget_usd is None:
        try:
            budget_usd = float(os.environ.get("MO_GATE_HACKABILITY_BUDGET_USD", "0.50"))
        except (TypeError, ValueError):
            budget_usd = 0.50
    state = {"spent": 0.0}

    def propose(context: dict, n: int) -> dict:
        if state["spent"] >= budget_usd:
            return {"documents": [], "cost_usd": 0.0, "status": "budget_exhausted"}

        from mini_ork.dispatch import llm_dispatch as native_dispatch

        fd, out = tempfile.mkstemp(prefix="mo-hk-propose-", suffix=".txt")
        os.close(fd)
        argv = [
            "--model", lane,
            "--node-type", "gate-hackability-propose",
            "--prompt-text", _build_propose_prompt(context, n),
            "--out", out,
            "--timeout", "180",
            "--max-turns", "1",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = native_dispatch.llm_dispatch(
                    argv,
                    root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
                )
        except Exception:
            rc = 1
        cost = 0.0
        try:
            with open(f"{out}.cost", encoding="utf-8") as f:
                cost = float(f.read().strip() or "0")
        except (OSError, ValueError):
            cost = 0.0
        state["spent"] += cost
        if rc != 0:
            return {"documents": [], "cost_usd": cost, "status": "dispatch_failed"}
        try:
            with open(out, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        docs = parse_proposals(text, n)
        if docs is None:
            return {"documents": [], "cost_usd": cost, "status": "unparseable"}
        return {"documents": docs, "cost_usd": cost, "status": "ok"}

    propose.lane = lane  # type: ignore[attr-defined]
    return propose


# ── audit ────────────────────────────────────────────────────────────────────


def _skeleton(run_id: str, path: str) -> dict:
    return {
        "run_id": run_id,
        "panel_run_id": run_id,
        "recipe": "hackability-probe",
        "task_class": "hackability-probe",
        "current_round": 1,
        "verdict_file": path,
        "mutation_report": path,
        "artifact_path": path,
        "plan_path": path,
    }


def _find_active_row(db_path: str, gate_id: str) -> dict:
    for row in gate_registry.gate_list(db_path):
        if row["gate_id"] == gate_id:
            return row
    raise KeyError(gate_id)


def _insert_row(db_path: str, row: dict) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO gate_registry
                (gate_id, gate_type, condition, task_class_filter,
                 safety, active, registered_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                row["gate_id"],
                row["gate_type"],
                row["condition"],
                row["task_class_filter"],
                int(row["safety"]),
                int(row["active"]),
                int(row["registered_at"]),
            ),
        )
        con.commit()
    finally:
        con.close()


def _env_n() -> int:
    try:
        return max(0, min(16, int(os.environ.get("MO_GATE_HACKABILITY_N", "4"))))
    except (TypeError, ValueError):
        return 4


def _env_max() -> float:
    try:
        return float(os.environ.get("MO_GATE_HACKABILITY_MAX", "0.25"))
    except (TypeError, ValueError):
        return 0.25


def _make_default_evaluator(
    scratch_db: str, gate_id: str, root: str
) -> Callable[[str], str]:
    """The default ``evaluate``: real ``gate_evaluate`` on a scratch DB, env-isolated."""
    overrides: dict[str, str | None] = {key: None for key in _ISOLATION_KEYS}
    overrides["MINI_ORK_DB"] = scratch_db

    def evaluate(ctx_json: str) -> str:
        with scoped_environ(overrides):
            return gate_registry.gate_evaluate(
                scratch_db, gate_id, ctx_json, mini_ork_root=root
            )

    return evaluate


def audit_gate(
    db_path: str,
    gate_id: str,
    *,
    proposer: Optional[Proposer] = None,
    n: Optional[int] = None,
    evaluate: Optional[Callable[[str], str]] = None,
) -> dict:
    """Attack ``gate_id`` with known-bad inputs; return the measurement record.

    Raises ``KeyError`` when the gate is missing or inactive. ``n`` is the
    number of proposer documents per gate (default ``MO_GATE_HACKABILITY_N``,
    clamped 0..16); 0 never calls the proposer. A supplied ``evaluate`` takes
    the context JSON string and returns ``'pass'|'fail'|'defer'``; the default
    evaluates the gate against a scratch DB with env isolation.
    """
    row = _find_active_row(db_path, gate_id)
    if n is None:
        n = _env_n()

    run_id = f"hk-{uuid.uuid4().hex[:8]}"
    scratch: Optional[tempfile.TemporaryDirectory] = None

    try:
        with tempfile.TemporaryDirectory() as workdir:
            if evaluate is None:
                scratch = tempfile.TemporaryDirectory()
                scratch_db = os.path.join(scratch.name, "scratch.db")
                migrate.init_db(db=scratch_db, root=str(ENGINE_ROOT))
                gate_registry.ensure_table(scratch_db)
                _insert_row(scratch_db, row)
                root = RunContext.from_env().root or str(ENGINE_ROOT)
                evaluate = _make_default_evaluator(scratch_db, gate_id, root)

            results: list[dict] = []
            passed_bad = 0
            trials = 0
            crashed = 0
            not_known_bad = 0
            exploits: list[str] = []

            def run_trial(tid: str, operator, source: str, context: dict,
                          document: Optional[str]) -> None:
                nonlocal passed_bad, trials, crashed, not_known_bad
                if document is not None:
                    path = os.path.join(workdir, f"{tid}.json")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(document)
                hollow = is_hollow_document(document)
                verdict = None
                outcome = "UNVERIFIED"
                reason: Optional[str]
                if not hollow:
                    reason = "not_known_bad"
                    not_known_bad += 1
                else:
                    try:
                        verdict = evaluate(json.dumps(context))
                    except Exception as exc:  # noqa: BLE001 — a crash is UNVERIFIED
                        verdict = None
                        reason = f"crashed: {exc}"
                        crashed += 1
                    else:
                        if verdict in _VERDICTS:
                            if verdict == "pass":
                                outcome = "PROVEN"
                                passed_bad += 1
                                trials += 1
                                exploits.append(tid)
                                reason = None
                            else:
                                outcome = "REFUTED"
                                trials += 1
                                reason = None
                        else:
                            verdict = None
                            reason = f"crashed: returned {verdict!r}"
                            crashed += 1
                results.append({
                    "id": tid,
                    "operator": operator,
                    "source": source,
                    "verdict": verdict,
                    "outcome": outcome,
                    "reason": reason,
                    "context": context,
                    "document": None if document is None else document[:2000],
                })

            # Five operators in fixed order.
            run_trial("empty-context", "empty-context", "operator", {}, None)
            for name, document in (
                ("dangling-evidence", None),
                ("empty-document", ""),
                ("hollow-object", "{}"),
                ("zero-leaf-skeleton", _ZERO_LEAF_SKELETON),
            ):
                run_trial(
                    name, name, "operator",
                    _skeleton(run_id, os.path.join(workdir, f"{name}.json")),
                    document,
                )

            # Proposer trials.
            proposer_lane = getattr(proposer, "lane", None)
            proposer_status = "off"
            proposer_cost = 0.0
            if proposer is not None and n > 0:
                prompt_context = dict(_skeleton(
                    run_id, os.path.join(workdir, "proposer.json"),
                ))
                prompt_context["gate_id"] = gate_id
                prompt_context["gate_type"] = row["gate_type"]
                prompt_context["condition"] = row["condition"]
                proposal = proposer(prompt_context, n)
                documents = list(proposal.get("documents", []))[:n]
                proposer_status = proposal.get("status", "ok")
                proposer_cost = float(proposal.get("cost_usd", 0.0) or 0.0)
                for i, document in enumerate(documents, start=1):
                    tid = f"proposer-{i}"
                    run_trial(
                        tid, proposer_lane, "proposer",
                        _skeleton(run_id, os.path.join(workdir, f"{tid}.json")),
                        document,
                    )

            return {
                "schema": SCHEMA,
                "gate_id": gate_id,
                "gate_type": row["gate_type"],
                "condition": row["condition"],
                "task_class_filter": row["task_class_filter"],
                "measured_at": int(time.time()),
                "trials": trials,
                "passed_bad": passed_bad,
                "hackability": (passed_bad / trials) if trials else None,
                "unverified": {"crashed": crashed, "not_known_bad": not_known_bad},
                "exploits": exploits,
                "proposer": proposer_lane,
                "proposer_status": proposer_status,
                "proposer_cost_usd": proposer_cost,
                "results": results,
            }
    finally:
        if scratch is not None:
            scratch.cleanup()


# ── consumer ─────────────────────────────────────────────────────────────────


def promotion_check(db_path: str, task_class: Optional[str] = None) -> dict:
    """Read persisted hackability records for the gates a promotion would run.

    Covers the gates from ``gate_registry.gate_list(db_path, task_class)`` —
    the gates ``gate_run_all`` evaluates for that class. With ``task_class=None``
    this is ALL active gates (the multi-class candidate case).

    A record is ``stale`` when its ``gate_type`` or ``condition`` differs from
    the current row. A gate is ``over_threshold`` when ``hackability > max``.
    ``ok`` is ``not over_threshold``; an unmeasured gate never causes a reject.
    """
    max_hk = _env_max()
    over: list[dict] = []
    within: list[dict] = []
    unmeasured: list[dict] = []
    for gate in gate_registry.gate_list(db_path, task_class=task_class):
        gid = gate["gate_id"]
        record = read_record(db_path, gid)
        if record is None:
            unmeasured.append({"gate_id": gid, "reason": "no_record"})
            continue
        if (record.get("gate_type") != gate["gate_type"]
                or record.get("condition") != gate["condition"]):
            unmeasured.append({"gate_id": gid, "reason": "stale"})
            continue
        hackability = record.get("hackability")
        if hackability is None:
            unmeasured.append({"gate_id": gid, "reason": "no_valid_trials"})
            continue
        if hackability > max_hk:
            over.append({
                "gate_id": gid,
                "hackability": hackability,
                "measured_at": record.get("measured_at"),
            })
        else:
            within.append({"gate_id": gid, "hackability": hackability})
    return {
        "max": max_hk,
        "ok": not over,
        "over_threshold": over,
        "within_threshold": within,
        "unmeasured": unmeasured,
    }
