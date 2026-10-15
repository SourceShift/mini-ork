"""Model diagnoser — an evidence-grounded diagnosis for the *unclassified* runs.

``diagnose`` answers *who owns this failure* with a deterministic rule set and,
when no rule can cite evidence, degrades honestly to ``unclassified`` at
confidence ``0.0`` (see :mod:`mini_ork.recovery.diagnose`). Those runs — 22% of
recent failures, mostly ``lifecycle exited without a verdict`` — then all go to a
human. The operator chose AGGRESSIVE escalation, so a **model** may diagnose
them; the frozen policy (:mod:`mini_ork.recovery.escalation_policy`) admits a
``source: "model"`` diagnosis only above its confidence bar.

This module is that model. Two halves:

  * :func:`build_evidence_pack` — the run's own evidence, assembled into one
    prompt-sized string (≤ :data:`PACK_MAX_CHARS`): the last ``run_events`` rows,
    ``task_runs.notes``, the run's artefacts (``verdict.json``,
    ``publish-abstain.json``, ``repair.json``), the retry-hint summary and the
    failing node's log tail. Every source has its own budget, so a chatty
    lifecycle can never crowd the log tail out, and secret-shaped assignments
    are redacted BEFORE any cut (a cut that kept a value but dropped its
    ``api_key=`` would otherwise slip past the redactor).
  * :func:`diagnose_with_model` — dispatch that pack to one lane and accept the
    reply only if it is grounded: every enum inside ``diagnose``'s own
    vocabulary, at least one evidence item, and **every** excerpt a verbatim
    substring of the run's evidence in the pack (the section headers and the
    rule set's own summary are framing, not evidence, and cannot be cited).
    Anything else returns ``None`` and the rule set's ``unclassified`` result
    stands — a confident lie is worse than "unclassified".

A model never diagnoses its own work: when the diagnoser lane resolves to the
same MODEL as the run's implementer lane the call is refused before it is made,
and so is a run whose implementer lane cannot be read (independence that cannot
be shown is not assumed). The comparison is on the provider entry's ``model``
field (or the lane name when ``model`` is unset) — **never** the provider
``family`` field, which names the transport (``glm``, ``deepseek_flash`` and
``minimax`` are all family ``gateway``); comparing families would silently
disable the diagnoser entirely.

Credentials come from the framework's own secret store through
``providers.dispatch_model`` (``config/vault.example.yaml`` / the environment),
so this module holds no key and writes nothing but its prompt and one
``llm_calls`` row (the spend is governed like every other call). The simple tier
is the same code path with a laptop-local ``config/providers.yaml``: no server,
no daemon. Failure is always closed — an unresolvable lane, a refused dispatch
or an ungrounded reply yields ``None`` (the rule result) rather than a
fabricated verdict.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from mini_ork.context import context_env
# ``dispatch.models`` is stdlib-only (stdlib dataclasses, no web stack), so the
# request envelope is importable here without pulling the dispatch runtime in.
from mini_ork.dispatch.models import DispatchRequest
from mini_ork.recovery import diagnose as dx

# The evidence pack is one prompt — bounded so a pathological run (a 9 MB live
# sidecar) can never blow the context window or the dispatch argv.
PACK_MAX_CHARS = 12_000
_CUT = "…[truncated]"

# The lane a model diagnosis runs on. ``glm`` is a *lane target* (a provider
# entry name in ``config/providers.yaml``), not a model id; it is resolved to a
# model below.
DEFAULT_LANE = "glm"

# ``decide`` runs this inline in the recovery loop, so the call is bounded far
# below a node dispatch's 25-minute default: one JSON reply, no tool use.
DEFAULT_TIMEOUT_S = 180.0
_MAX_TURNS = 3
# The evidence is in the prompt, so the diagnoser needs no tools, servers or
# session. Pinning them matters: the recovery loop runs inside a lifecycle whose
# env may still describe a node — an implementer's ``MO_NODE_ID`` (default grant
# Write/Edit), its MCP servers, a session to ``--resume``. ``native|mcp`` "Read|"
# renders as ``--allowedTools Read --strict-mcp-config``.
_DISPATCH_ENV = {
    "MO_RESOLVED_NODE_TOOLS": "Read|",
    "MO_MCP_SERVERS": "",
    "MO_RESUME_SESSION_ID": "",
}

# The SDE-1 diagnosis contract this module emits: a model result is one more
# ``rule_id`` in the same envelope, distinguishable by ``source == "model"``.
RULE_ID = "M1"
SOURCE_MODEL = dx.SOURCE_MODEL

# A model diagnosis must cite its evidence, not assert it. A sub-20-char excerpt
# ("timeout", "rc=1") is a token, not a citation, and is trivially guessed.
MIN_EXCERPT_CHARS = 20

# Evidence-pack shape: how much of each source the model sees. The budgets sum
# (with headers and the framing sections) to just under PACK_MAX_CHARS, so the
# final cap is a backstop rather than the thing that decides what survives.
_EVENTS_N = 40          # the last N run_events rows
_EVENT_PAYLOAD_CHARS = 200
_LOG_LINES = 80         # the failing node's log tail
_LOG_TAIL_BYTES = 32_768
_RULE_CHARS = 400
_EVENTS_CHARS = 3_600
_NOTES_CHARS = 1_000
_HINT_CHARS = 800
_LOG_CHARS = 3_000
_ARTIFACT_CHARS = {"verdict.json": 1_200, dx.PUBLISH_ABSTAIN_FILE: 800, "repair.json": 800}

# ``api_key`` / ``token`` / ``secret`` / ``password`` assignments, the kickoff's
# redaction contract. The optional quote covers the JSON form a run log often
# carries (``"api_key": "sk-…"``) — the bare regex's ``\s*`` cannot cross the
# quote, so without it a JSON-shaped secret would sail into the prompt. Bearer
# credentials and bare ``sk-`` keys carry no assignment, so they get their own.
_SECRET_RES = (
    re.compile(r"""(?i)(api[_-]?key|token|secret|password)['"]?\s*[:=]\s*\S+"""),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
)
_REDACTED = "<redacted>"

# The fields a model reply must carry, and the schema block the prompt shows it.
_REPLY_FIELDS = ("layer", "stage", "node", "routes_to", "confidence", "summary", "evidence")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── evidence pack ────────────────────────────────────────────────────────────


def _redact(text: str) -> str:
    """Replace secret-shaped assignments with ``<redacted>``."""
    out = str(text or "")
    for pattern in _SECRET_RES:
        out = pattern.sub(_REDACTED, out)
    return out


def _head(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` chars, keeping the start (a JSON artefact)."""
    return text if len(text) <= limit else text[:limit - len(_CUT)] + _CUT


def _tail(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` chars, keeping the NEWEST end (a log, notes)."""
    return text if len(text) <= limit else _CUT + text[-(limit - len(_CUT)):]


def _tail_lines(path: Path, n: int) -> str:
    """The last ``n`` lines of ``path``, read from a bounded tail (never whole).

    A tail that starts mid-file drops its first, partial line: it may begin
    inside a secret whose ``api_key=`` fell before the window, and a value
    without its key is invisible to the redactor.
    """
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - _LOG_TAIL_BYTES)
            fh.seek(start)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = data.splitlines()
    if start and lines:
        lines = lines[1:]
    return "\n".join(lines[-n:])


def _newest_first(paths: list[Path]) -> list[Path]:
    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(paths, key=mtime, reverse=True)


def _log_tail(run_dir: Path, node: str | None) -> tuple[str, str]:
    """``(file name, last _LOG_LINES lines)`` of the failing node's log.

    Read here rather than through ``retry_hint._tail_log``, whose summary-
    shaped ``[:2000]`` cut would *drop the newest lines* — the ones that say why
    the run stopped. Candidate order is ``retry_hint``'s (``impl-<node>.log`` →
    ``agent-<node>.live.jsonl`` → ``execute.log``); when none of those has text
    the run's own ``impl-*.log`` / ``agent-*.live.jsonl`` files stand in, newest
    first — this run dir only, never a sibling run's.
    """
    run_dir = Path(run_dir)
    candidates: list[Path] = []
    if node:
        candidates += [run_dir / f"impl-{node}.log",
                       run_dir / f"agent-{node}.live.jsonl",
                       run_dir / "execute.log"]
    for pattern in ("impl-*.log", "agent-*.live.jsonl"):
        candidates += _newest_first(list(run_dir.glob(pattern)))
    for path in candidates:
        text = _tail_lines(path, _LOG_LINES)
        if text.strip():
            return path.name, text
    return "", ""


def _artifact(run_dir: Path, name: str) -> str:
    data = dx._read_json(Path(run_dir) / name)
    return "" if data is None else json.dumps(data, sort_keys=True)


def _events_block(events: list[dict[str, Any]] | None) -> str:
    """The last :data:`_EVENTS_N` rows, newest kept when over budget."""
    lines = [
        f"{ev.get('event_type') or '?'} "
        f"{_head(_redact(str(ev.get('payload_json') or '')), _EVENT_PAYLOAD_CHARS)}"
        for ev in (events or [])[-_EVENTS_N:]
    ]
    while lines and sum(len(line) + 1 for line in lines) > _EVENTS_CHARS:
        lines.pop(0)
    return "\n".join(lines)


def _hint_block(home: Path, run_id: str) -> str:
    hint = dx._hint(home, run_id)
    if not isinstance(hint, dict):
        return ""
    bits = [
        f"failed_node={hint.get('failed_node')}",
        f"strategy={hint.get('strategy')}",
        f"retryable={hint.get('retryable')}",
    ]
    summary = str(hint.get("summary") or "")
    if summary:
        bits.append(f"summary={summary}")
    return "\n".join(bits)


def _sections(home: Path, run_id: str,
              rule_diag: Mapping[str, Any] | None) -> list[tuple[str, str, bool]]:
    """``(title, body, citable)`` per non-empty pack section, redacted + cut.

    The two framing sections (the run id and the rule set's own verdict) orient
    the model but are not *evidence*: a reply citing them has quoted the
    framework back at itself, so they are marked non-citable.
    """
    run_dir = dx._run_dir(home, run_id)
    diag = rule_diag if isinstance(rule_diag, Mapping) else {}
    events = dx._lifecycle(home, run_id)
    node, _reason = dx._failing(events)
    log_name, log_text = _log_tail(run_dir, node)

    sections: list[tuple[str, str, bool]] = [
        ("RUN", f"run_id={run_id} failing_node={node or 'unknown'}", False),
        ("RULE DIAGNOSIS", _head(_redact(
            f"layer={diag.get('layer')} stage={diag.get('stage')} "
            f"node={diag.get('node')} rule_id={diag.get('rule_id')} "
            f"summary={diag.get('summary')}"
        ), _RULE_CHARS), False),
        (f"RUN EVENTS (last {_EVENTS_N})", _events_block(events), True),
        ("RUN NOTES", _tail(_redact(dx._notes(home, run_id)), _NOTES_CHARS), True),
    ]
    for name, limit in _ARTIFACT_CHARS.items():
        sections.append((name, _head(_redact(_artifact(run_dir, name)), limit), True))
    sections.append(("RETRY HINT", _head(_redact(_hint_block(home, run_id)), _HINT_CHARS), True))
    sections.append((f"NODE LOG ({log_name or 'none'}, last {_LOG_LINES} lines)",
                     _tail(_redact(log_text), _LOG_CHARS), True))
    return [(title, body, citable) for title, body, citable in sections
            if str(body or "").strip()]


def _assemble(sections: list[tuple[str, str, bool]]) -> str:
    pack = "\n".join(f"=== {title} ===\n{body}" for title, body, _citable in sections)
    return _head(pack, PACK_MAX_CHARS)


def build_evidence_pack(home: Path, run_id: str,
                        rule_diag: Mapping[str, Any] | None = None) -> str:
    """The run's own evidence as one string (≤ :data:`PACK_MAX_CHARS`).

    Never raises: every source is read fail-soft, because a broken home must
    degrade to "less evidence" (the reply then fails to ground and is discarded)
    rather than to an exception in the recovery loop.
    """
    return _assemble(_sections(Path(home), str(run_id), rule_diag))


# ── lane / self-diagnosis resolution ─────────────────────────────────────────


def _lanes(home: Path, run_id: str) -> dict[str, str]:
    """``run_profile.json`` ``provider_policy.lanes`` — role → provider entry."""
    profile = dx._read_json(dx._run_dir(home, run_id) / "run_profile.json")
    policy = profile.get("provider_policy") if isinstance(profile, dict) else None
    lanes = policy.get("lanes") if isinstance(policy, Mapping) else None
    if not isinstance(lanes, Mapping):
        return {}
    return {str(k): str(v) for k, v in lanes.items() if v}


def _registry() -> Mapping[str, Any]:
    """The provider registry, resolved the way the dispatcher resolves it.

    Reused rather than re-implemented so the diagnoser and the dispatcher can
    never disagree about which entry a lane names (``$MINI_ORK_PROVIDERS`` →
    ``$MINI_ORK_HOME/config/providers.yaml`` → the repo's). An unreadable or
    absent registry degrades to ``{}`` — lane names then stand in for models,
    which still refuses the exact-match self-diagnosis below.
    """
    try:
        from mini_ork.dispatch import providers
        registry = providers._load_providers_registry()
        return registry if isinstance(registry, Mapping) else {}
    except Exception:  # noqa: BLE001 — a broken registry is "no models known"
        return {}


def _model_of(entry_name: str, registry: Mapping[str, Any]) -> str:
    """The MODEL a lane resolves to: the entry's ``model``, else the lane name.

    Deliberately not the ``family`` field — see the module docstring.
    """
    entry = registry.get(entry_name)
    if isinstance(entry, Mapping):
        return str(entry.get("model") or entry_name)
    return str(entry_name)


def _independence(home: Path, run_id: str, lane: str) -> tuple[str, str]:
    """``(provider entry to dispatch, refusal note)`` for the diagnoser ``lane``.

    The note is empty when the diagnoser may run. It is set when the run's
    implementer lane is unknown (no ``provider_policy.lanes.implementer`` in
    ``run_profile.json``) or resolves to the same MODEL as ``lane``. A lane
    named after a role (``reviewer``) resolves through the same ``lanes`` map,
    and the resolved entry is what gets dispatched — the guard checks exactly
    the model that would answer.
    """
    lanes = _lanes(home, run_id)
    entry = lanes.get(lane) or lane
    implementer = lanes.get("implementer") or ""
    if not implementer:
        return entry, ("the run's implementer lane is unknown (no provider_policy.lanes"
                       ".implementer in run_profile.json); a model never diagnoses "
                       "its own work, so independence must be shown, not assumed")
    registry = _registry()
    model = _model_of(entry, registry)
    if model == _model_of(implementer, registry):
        return entry, (f"lane {lane!r} resolves to the implementer's own model "
                       f"{model!r}; a model never diagnoses its own work")
    return entry, ""


def _timeout_s() -> float:
    try:
        value = float(context_env("MO_DIAGNOSER_TIMEOUT_S", "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


# ── prompt + reply validation ────────────────────────────────────────────────


def _prompt(run_id: str, pack: str) -> str:
    layers = ", ".join(sorted(dx.LAYERS - {dx.LAYER_UNCLASSIFIED}))
    stages = ", ".join(sorted(dx.STAGES))
    routes = ", ".join(sorted(dx.ROUTES))
    return (
        "You diagnose failed mini-ork runs. A deterministic rule set could not "
        f"classify run {run_id}, so the owner is unknown and the run is going to a\n"
        "human. Read the evidence below and name who owns the failure.\n\n"
        "Answer with ONE JSON object and nothing else — no prose, no code fence.\n"
        f"Fields: {', '.join(_REPLY_FIELDS)}.\n"
        f"  layer ∈ {{{layers}}}\n"
        f"  stage ∈ {{{stages}}}\n"
        f"  routes_to ∈ {{{routes}}}\n"
        "  confidence is your own 0.0-1.0 belief that this diagnosis is right.\n"
        "  evidence is a NON-EMPTY list of {kind, ref, excerpt}. Every excerpt\n"
        "  MUST be at least 20 characters copied VERBATIM from ONE section body of\n"
        "  the evidence pack below (not a === header ===, not the RUN or RULE\n"
        "  DIAGNOSIS sections); an excerpt that is not a literal substring of the\n"
        "  run's evidence invalidates the whole answer.\n\n"
        "=== EVIDENCE PACK ===\n"
        f"{pack}\n"
        "=== END EVIDENCE PACK ===\n"
    )


def _loads(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_reply(text: str) -> dict[str, Any] | None:
    """The reply's JSON object — bare, fenced, or wrapped in prose."""
    raw = str(text or "").strip()
    if not raw:
        return None
    obj = _loads(raw)
    if obj is not None:
        return obj
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    return _loads(raw[start:end + 1])


def _validate(obj: Mapping[str, Any], pack: str,
              citable: list[str]) -> tuple[dict[str, Any] | None, str]:
    """Validate one model reply against the contract and the evidence.

    ``(result, note)`` — ``result`` is ``None`` (and ``note`` says why) on any
    failure. Every check is a hard gate: a model diagnosis is only worth having
    if it cannot be a confident guess. An excerpt must sit in the pack the model
    actually saw AND inside one citable section body — so neither a header, the
    rule set's own summary, nor text the final cap cut away can stand in for
    evidence.
    """
    layer = str(obj.get("layer") or "")
    if layer not in dx.LAYERS:
        return None, f"layer {layer!r} outside the diagnosis vocabulary"
    if layer == dx.LAYER_UNCLASSIFIED:
        return None, "the model returned unclassified (no diagnosis was made)"

    stage = str(obj.get("stage") or "")
    if stage not in dx.STAGES:
        return None, f"stage {stage!r} outside {sorted(dx.STAGES)}"

    routes_to = str(obj.get("routes_to") or "")
    if routes_to not in dx.ROUTES:
        return None, f"routes_to {routes_to!r} outside the routes vocabulary"

    raw_confidence = obj.get("confidence")
    if isinstance(raw_confidence, bool):
        return None, f"confidence {raw_confidence!r} is not a number"
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None, f"confidence {raw_confidence!r} is not a number"
    if not 0.0 <= confidence <= 1.0:
        return None, f"confidence {confidence} outside [0, 1]"

    raw_evidence = obj.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        return None, "the model cited no evidence"

    evidence: list[dict[str, str]] = []
    for i, item in enumerate(raw_evidence):
        if not isinstance(item, Mapping):
            return None, f"evidence[{i}] is not an object"
        excerpt = str(item.get("excerpt") or "")
        if len(excerpt) < MIN_EXCERPT_CHARS:
            return None, f"evidence[{i}] excerpt is shorter than {MIN_EXCERPT_CHARS} chars"
        if excerpt not in pack or not any(excerpt in body for body in citable):
            return None, f"evidence[{i}] excerpt is not verbatim in the run's evidence"
        evidence.append(dx._ev(item.get("kind") or "", item.get("ref") or "", excerpt))

    node = obj.get("node")
    return {
        "layer": layer,
        "stage": stage,
        "routes_to": routes_to,
        "confidence": confidence,
        "evidence": evidence,
        "node": str(node) if node else None,
        "summary": str(obj.get("summary") or ""),
    }, ""


def _record_call(home: Path, run_id: str, result: Any) -> None:
    """One ``llm_calls`` row for the diagnoser's call — best effort.

    The diagnoser spends money inside the recovery loop; the ledger is how that
    spend stays visible to the same cost governance as every node call.
    """
    try:
        from mini_ork.dispatch.models import DispatchResult
        if not isinstance(result, DispatchResult):
            return
        from mini_ork.dispatch.providers import provider_for_model
        from mini_ork.dispatch.telemetry import persist_call
        persist_call(Path(home) / "state.db", result,
                     provider=provider_for_model(result.model),
                     feature_name="model_diagnoser", run_id=run_id)
    except Exception:  # noqa: BLE001 — telemetry must never break a diagnosis
        pass


# ── the diagnoser ────────────────────────────────────────────────────────────


def diagnose_with_model_verbose(
    home: Path, run_id: str, rule_diag: Mapping[str, Any] | None = None,
    *, dispatch: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """``(diagnosis_or_None, note)`` — the diagnoser's full result.

    The note says why a call was skipped or a reply discarded (empty on success).
    :func:`diagnose_with_model` is the ``dict | None`` view of this.
    """
    home = Path(home)
    run_id = str(run_id)
    diag = rule_diag if isinstance(rule_diag, Mapping) else {}
    if str(diag.get("layer") or "") != dx.LAYER_UNCLASSIFIED:
        return None, "the rule set classified this run; no model diagnosis needed"

    lane = context_env("MO_DIAGNOSER_LANE", DEFAULT_LANE) or DEFAULT_LANE
    entry, refusal = _independence(home, run_id, lane)
    if refusal:
        return None, refusal

    sections = _sections(home, run_id, diag)
    pack = _assemble(sections)
    citable = [body for _title, body, cite in sections if cite]
    if dispatch is None:
        from mini_ork.dispatch.providers import dispatch_model
        dispatch = dispatch_model
    try:
        # An empty scratch cwd: the call reads nothing from disk, so it is given
        # no tree to touch (and never trips the framework-tree cwd guard).
        with tempfile.TemporaryDirectory(prefix="mo-diagnoser-") as scratch:
            result = dispatch(DispatchRequest(
                model=entry, prompt=_prompt(run_id, pack), timeout_s=_timeout_s(),
                max_turns=_MAX_TURNS, cwd=scratch,
                env=dict(_DISPATCH_ENV),
            ))
    except Exception as exc:  # noqa: BLE001 — a failed call is "no diagnosis"
        return None, f"dispatch failed ({type(exc).__name__}: {exc})"
    _record_call(home, run_id, result)

    if not getattr(result, "ok", False):
        return None, f"dispatch was not ok: {getattr(result, 'error', '') or 'unknown'}"

    try:
        cost_usd = float(getattr(result, "cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost_usd = 0.0

    parsed = _parse_reply(getattr(result, "text", ""))
    if parsed is None:
        return None, "the reply was not a JSON object"

    validated, note = _validate(parsed, pack, citable)
    if validated is None:
        return None, note

    return {
        "version": dx.DIAGNOSIS_VERSION,
        "run_id": run_id,
        "rule_id": RULE_ID,
        "layer": validated["layer"],
        "stage": validated["stage"],
        "node": validated["node"],
        "source": SOURCE_MODEL,
        "confidence": validated["confidence"],
        "evidence": validated["evidence"],
        "routes_to": validated["routes_to"],
        "summary": validated["summary"],
        "computed_at": _now_iso(),
        "model_lane": lane,
        "cost_usd": cost_usd,
    }, ""


def diagnose_with_model(
    home: Path, run_id: str, rule_diag: Mapping[str, Any] | None = None,
    *, dispatch: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """An evidence-grounded model diagnosis for an unclassified run, or ``None``.

    ``None`` is the answer for every reason a model diagnosis is not admissible:
    the rule set already classified the run, the lane would be (or cannot be
    shown not to be) diagnosing its own work, the dispatch failed, or the reply
    was not grounded in the run's evidence. Callers keep their rule result —
    never an exception, never a guess.
    """
    return diagnose_with_model_verbose(home, run_id, rule_diag, dispatch=dispatch)[0]
