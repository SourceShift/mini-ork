"""Python port of lib/llm-dispatch.sh's WRAPPER layer + pure helpers.

The model-level provider invocation is already ported in ``mini_ork.dispatch``
(prompt-on-stdin, sidecar contract, telemetry). This module ports what remained
in bash: the ``llm_dispatch`` lane-routing shim that bin/mini-ork-{plan,execute,
invoke-prompt} + pre_push_review call, plus the pure classification/backoff/
redaction helpers.

    llm_dispatch(argv, *, dispatch_fn=None) -> int      # --task-class/--node-type/--prompt-text
    mo_llm_dispatch(model, prompt, out_file, timeout_s=1500, max_turns=60) -> int

Every helper (classify_error, provider_for_model, strip_protocol_blocks,
redact_secrets, backoff, throttle/glm-retryable, lane resolution, cost circuit)
is transcribed verbatim from the bash so behaviour byte-matches — including the
bash gateway provider taxonomy (distinct from mini_ork.dispatch's finer one).
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable

from mini_ork.dispatch.predicates import looks_like_json


# ── pure helpers (verbatim regex transcriptions) ──

def classify_error(message: str, rc: str | int = "") -> str:
    text = (message or "").lower()
    if str(rc) in ("6", "7", "28"):
        return "network"
    if re.search(r"missing (api key|wrapper)|api key.*missing|malformed config|config.*missing"
                 r"|cl_[a-z0-9_-]+\.sh missing|no providers\.yaml entry|\$[A-Z0-9_]+ is empty", text):
        return "config"
    if re.search(r"(^|[^0-9])(401|403)([^0-9]|$)|invalid api key|authentication failed"
                 r"|not logged in|unauthorized|forbidden", text):
        return "auth"
    if re.search(r"429", text) and re.search(
            r"monthly|tokens-per-day|billing|quota|insufficient credits|credit limit", text):
        return "quota"
    if re.search(r"(^|[^0-9])(429|503)([^0-9]|$)", text) and re.search(
            r"capacity|concurrent|rate|overload|temporarily unavailable", text):
        return "capacity"
    if re.search(r"(^|[^0-9])400([^0-9]|$)|invalid request|context too long"
                 r"|maximum context|prompt too long", text):
        return "request"
    if re.search(r"(^|[^0-9])422([^0-9]|$)|content filter|safety|moderation", text):
        return "safety"
    if re.search(r"connection refused|could not resolve|dns|timed out|timeout was reached"
                 r"|network is unreachable", text):
        return "network"
    if re.search(r"partial stream|unexpected eof|stream.*(closed|ended|error)"
                 r"|incomplete chunk|chunked encoding", text):
        return "stream"
    if re.search(r"(^|[^0-9])(500|502|504)([^0-9]|$)|internal server error|bad gateway"
                 r"|gateway timeout|provider error", text):
        return "provider"
    return "unknown"


def error_retryable(category: str = "unknown") -> str:
    return "1" if category in ("capacity", "network", "stream", "provider") else "0"


def provider_for_model(model: str) -> str:
    """Bash _mo_llm_provider_for_model taxonomy (llm_calls.provider)."""
    if re.fullmatch(r"codex|gpt-.*|o1.*|o3.*", model):
        return "openai"
    if re.fullmatch(r"gemini.*|.*-gemini-.*", model):
        return "google"
    if re.fullmatch(r"minimax.*|glm.*|kimi.*|deepseek.*", model):
        return "gateway"
    return "anthropic"


def redact_secrets(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"sk-[a-zA-Z]{1,6}-[A-Za-z0-9_-]{8,}", "[REDACTED_KEY]", s)
    s = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED_KEY]", s)
    s = re.sub(r"[Bb]earer\s+[A-Za-z0-9_.+/=-]{8,}", "Bearer [REDACTED]", s)
    s = re.sub(r"(ANTHROPIC_(AUTH_TOKEN|API_KEY)|[A-Z_]+_API_KEY)=[^\s\"']+", r"\1=[REDACTED]", s)
    s = re.sub(r"[a-fA-F0-9]{32,}", "[REDACTED_HEX]", s)
    return s


def strip_protocol_blocks(out_file: str) -> None:
    if not os.path.isfile(out_file):
        return
    try:
        text = open(out_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    if "<z-insight>" not in text:
        return
    cleaned = re.sub(r"<z-insight>.*?</z-insight>", "", text, flags=re.S)
    cleaned = re.sub(r"<z-insight>.*\Z", "", cleaned, flags=re.S)
    if cleaned != text:
        try:
            open(out_file, "w", encoding="utf-8").write(cleaned.rstrip() + "\n")
        except OSError:
            pass


def _int_or(v, d):
    try:
        return int(v)
    except (ValueError, TypeError):
        return d


def backoff_seconds_raw(attempt, max_sleep: int | str = 45, base_s: int | str = 5,
                        *, _jitter=None) -> int:
    max_sleep = _int_or(max_sleep, 45)
    base_s = _int_or(base_s, 5)
    attempt = _int_or(attempt, 1)
    attempt = max(1, min(12, attempt))
    base = 2 ** (attempt - 1) * base_s
    jitter = _jitter if _jitter is not None else _rand_mod4()
    delay = base + jitter
    if delay > max_sleep:
        delay = max_sleep
    if delay < 1:
        delay = 1
    return delay


def _rand_mod4() -> int:
    # bash $RANDOM % 4 — jitter is non-deterministic; isolated for test seams.
    import random
    return random.randint(0, 3)


def backoff_seconds(attempt, *, _jitter=None) -> int:
    return backoff_seconds_raw(attempt, os.environ.get("MO_DISPATCH_RETRY_MAX_SLEEP_S", "45"),
                               os.environ.get("MO_DISPATCH_RETRY_BASE_S", "5"), _jitter=_jitter)



def throttle_retryable(model, message, rc, attempt=1, max_attempts=1) -> bool:
    if not model:
        return False
    attempt = _int_or(attempt, 1)
    max_attempts = _int_or(max_attempts, 1)
    if attempt >= max_attempts:
        return False
    return error_retryable(classify_error(message, rc)) == "1"


def glm_fair_usage_retryable(model, message, attempt=1, max_attempts=1) -> bool:
    if model != "glm":
        return False
    if _int_or(attempt, 1) >= _int_or(max_attempts, 1):
        return False
    return bool(re.search(r"(^|[^0-9])(429|1313)([^0-9]|$)|fair usage policy"
                          r"|usage pattern does not comply", message or "", re.I))


# ── lane resolution + cost circuit + fuse ──

def resolve_lane_model(node_type, root, home) -> str:
    """agents.yaml lanes.<node_type> → lanes.worker → worker_default → sonnet."""
    agents = os.path.join(home, "config", "agents.yaml")
    if not os.path.isfile(agents):
        agents = os.path.join(root, "config", "agents.yaml")
    if not os.path.isfile(agents):
        return "sonnet"
    try:
        import yaml
        d = yaml.safe_load(open(agents)) or {}
        lanes = d.get("lanes", {})
        return lanes.get(node_type) or lanes.get("worker") or lanes.get("worker_default") or "sonnet"
    except Exception:
        return "sonnet"


def resolve_lane_family(lane: str, root: str = "", home: str = "") -> str:
    """Resolve an agents.yaml lane ALIAS (e.g. 'codex_lens', 'decomposer') to its
    family model via lanes.<alias>. A plain model name or unknown alias passes
    through unchanged. Fail-open: any error returns the input lane verbatim.

    Dispatch preflight keys on providers.yaml model names; '*_lens' aliases are
    NOT providers.yaml keys, so an unresolved alias used as the fallback-chain
    lead fails preflight and the MO_FALLBACK_* tail silently decides who serves
    (the codex_lens->minimax bug). Resolving here makes the alias lead with its
    real family model, keeping the tail as a genuine fallback."""
    if not lane:
        return lane
    root = root or os.environ.get("MINI_ORK_ROOT", "")
    home = home or os.environ.get("MINI_ORK_HOME", "")
    agents = os.path.join(home, "config", "agents.yaml")
    if not os.path.isfile(agents):
        agents = os.path.join(root, "config", "agents.yaml")
    if not os.path.isfile(agents):
        return lane
    try:
        import yaml
        d = yaml.safe_load(open(agents)) or {}
        lanes = d.get("lanes", {}) or {}
        return lanes.get(lane) or lane
    except Exception:
        return lane


def cost_circuit_open(db, budget) -> bool:
    if not (db and os.path.isfile(db)):
        return False
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        cutoff = int(time.time()) - 86400
        row = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM task_runs WHERE created_at >= ?",
                          (cutoff,)).fetchone()
        con.close()
        return float(row[0] or 0) >= float(budget)
    except sqlite3.OperationalError:
        return False


def check_lane_fuse(db, lane, category) -> bool:
    """True if the fuse is OPEN (last 3 lane calls all failed same retryable cat)."""
    if not (db and os.path.isfile(db) and lane and category):
        return False
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if not {"status", "feature_name", "error_category", "retryable"} <= cols:
            con.close(); return False
        rows = con.execute(
            "SELECT status, error_category, retryable FROM llm_calls WHERE feature_name LIKE ? "
            "ORDER BY id DESC LIMIT 3", (f"%:{lane}",)).fetchall()
        con.close()
    except Exception:
        return False
    if len(rows) != 3:
        return False
    return all(status == "failed" and err == category and _int_or(retryable, 0) == 1
               for status, err, retryable in rows)


# ── llm_calls row writer (verbatim cost math + schema-adaptive cols) ──

def write_llm_calls_row(db, provider, model_id, tier, feature_name, actor, status,
                        duration_ms, cost_usd, error_message, input_tokens=0, output_tokens=0,
                        metadata_json="{}", cached_input_tokens=0, cache_creation_input_tokens=0,
                        error_category=None, retryable=None):
    if not (db and os.path.isfile(db)):
        return
    in_tok = _int_or(input_tokens, 0)
    out_tok = _int_or(output_tokens, 0)
    cached_in = _int_or(cached_input_tokens, 0)
    cache_create = _int_or(cache_creation_input_tokens, 0)
    uncached_in = max(in_tok - cached_in - cache_create, 0)
    cost_input_uncached = uncached_in * 15.0 / 1_000_000
    cost_input_cached = cached_in * 1.5 / 1_000_000
    cost_cache_write = cache_create * 18.75 / 1_000_000
    import json as _json
    try:
        _md = _json.loads(metadata_json or "{}")
        _sess = _md.get("session_id") if isinstance(_md, dict) else None
    except Exception:
        _sess = None
    con = sqlite3.connect(db, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    insert_cols = ["provider", "model_id", "tier", "feature_name", "actor", "status",
                   "duration_ms", "cost_usd", "error_message", "iter", "run_id", "traceparent",
                   "input_tokens", "output_tokens", "total_tokens", "metadata_json", "session_id"]
    values = [provider, model_id, tier, feature_name, actor,
              status, _int_or(duration_ms, 0), float(cost_usd or 0.0), error_message or None,
              _int_or(os.environ.get("MO_RECURSIVE_ITER"), None),
              os.environ.get("MINI_ORK_RUN_ID") or None, os.environ.get("MO_TRACEPARENT") or None,
              in_tok, out_tok, in_tok + out_tok, metadata_json or "{}", _sess]
    for name, val in (("error_category", error_category), ("retryable", retryable),
                      ("cached_input_tokens", cached_in),
                      ("cache_creation_input_tokens", cache_create),
                      ("cost_input_uncached_usd", cost_input_uncached),
                      ("cost_input_cached_usd", cost_input_cached),
                      ("cost_cache_write_usd", cost_cache_write)):
        if name in cols:
            insert_cols.append(name); values.append(val)
    ph = ",".join("?" for _ in insert_cols)
    con.execute(f"INSERT INTO llm_calls ({', '.join(insert_cols)}) VALUES ({ph})", values)
    con.commit(); con.close()


# ── model-level dispatch (delegates to the ported mini_ork.dispatch core) ──

def mo_llm_dispatch(model, prompt, out_file, timeout_s=1500, max_turns=60, accept=None) -> int:
    """Provider call via mini_ork.dispatch; writes out_file + .cost/.err.log sidecars.
    Returns 0 on success, the provider rc otherwise. (llm_calls is owned by the
    llm_dispatch wrapper, matching the bash split.)

    ``accept`` is an optional structural shape predicate (SE-3 Phase A.2). On the
    multi-lane path it is threaded into ``dispatch_with_fallback`` so a
    wrong-shape lane triggers fallback; on the single-lane path there is nobody
    to fall back to, so a clean-but-wrong-shape result is downgraded to
    ok=False/rc=SHAPE_REJECT_RC here — the same signal, so the caller never
    writes a structurally-broken artifact that the verifier will kill late."""
    from mini_ork.dispatch.models import DispatchRequest
    from mini_ork.dispatch.predicates import SHAPE_REJECT_RC
    from mini_ork.dispatch.providers import dispatch_model, dispatch_with_fallback
    lanes = [m.strip() for m in str(model).split(",") if m.strip()] or [str(model)]
    req = DispatchRequest(model=lanes[0], prompt=prompt, timeout_s=float(timeout_s))
    if len(lanes) > 1:
        res = dispatch_with_fallback(req, lanes, accept=accept)
    else:
        res = dispatch_model(req)
        if accept is not None and res.ok and (res.text or "").strip() and not accept(res):
            from dataclasses import replace
            res = replace(
                res, ok=False, rc=SHAPE_REJECT_RC,
                error="dispatch result failed shape check "
                f"({getattr(accept, '__name__', 'accept')})",
            )
    try:
        open(out_file, "w", encoding="utf-8").write(res.text)
        open(out_file + ".cost", "w", encoding="utf-8").write(f"{res.cost_usd:.6f}")
        open(out_file + ".model", "w", encoding="utf-8").write(res.model or lanes[0])
    except OSError:
        pass
    u = res.usage
    try:
        open(out_file + ".tokens", "w", encoding="utf-8").write(
            f"{u.input_tokens}\t{u.output_tokens}\t{u.cached_input_tokens}\t{u.cache_creation_tokens}")
    except (OSError, AttributeError):
        pass
    if not res.ok and res.error:
        try:
            open(out_file + ".err.log", "w", encoding="utf-8").write(res.error.rstrip() + "\n")
        except OSError:
            pass
    return 0 if res.ok else res.rc


# ── the llm_dispatch lane-routing wrapper ──

# node_type → structural shape predicate (SE-3 Phase A.2). Keyed on the
# --node-type the CLI receives. Deliberately minimal: only nodes whose artifact
# is *known* to be JSON are gated, so prose-emitting nodes (researcher,
# reviewer, reflector, …) are never false-rejected. `planner` is the SE-3
# incident node — a planner lane that returned bad JSON produced no artifact and
# the run died late at the verifier. Add an entry only once a node's JSON output
# contract is confirmed; this is a structural gate, not the semantic
# validate_artifact check. Override with MO_SHAPE_CHECK=0 (kill switch).
_NODE_SHAPE_PREDICATES: dict[str, Callable[[object], bool]] = {
    "planner": looks_like_json,
}


def _shape_predicate_for(node_type: str) -> Callable[[object], bool] | None:
    if os.environ.get("MO_SHAPE_CHECK", "1") == "0":
        return None
    return _NODE_SHAPE_PREDICATES.get(node_type)


def llm_dispatch(argv=None, *, root=None, dispatch_fn=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    # A test/caller-supplied dispatch_fn keeps the historic 5-arg contract and
    # must never receive the A.2 accept= kwarg; only the default dispatcher does.
    _default_dispatch = dispatch_fn is None
    dispatch_fn = dispatch_fn or mo_llm_dispatch

    node_type = prompt_text = out_file = model_override = ""
    timeout_s = _int_or(os.environ.get("MO_NODE_TIMEOUT_S"), 1500)
    max_turns = _int_or(os.environ.get("MO_NODE_MAX_TURNS"), 60)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--task-class":
            _task_class = argv[i + 1]; i += 2  # accepted for CLI compat; currently unused
        elif a == "--node-type":
            node_type = argv[i + 1]; i += 2
        elif a == "--prompt-text":
            prompt_text = argv[i + 1]; i += 2
        elif a == "--out":
            out_file = argv[i + 1]; i += 2
        elif a == "--model":
            model_override = argv[i + 1]; i += 2
        elif a == "--timeout":
            timeout_s = _int_or(argv[i + 1], timeout_s); i += 2
        elif a == "--max-turns":
            max_turns = _int_or(argv[i + 1], max_turns); i += 2
        else:
            i += 1

    # cost circuit breaker
    if cost_circuit_open(db, os.environ.get("MO_DAILY_BUDGET_USD", "50")):
        spent = 0.0
        try:
            con = sqlite3.connect(db)
            spent = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM task_runs "
                                "WHERE created_at >= ?", (int(time.time()) - 86400,)).fetchone()[0]
            con.close()
        except Exception:
            pass
        sys.stderr.write(f"[cost_circuit_open] spent_today=${spent} "
                         f"budget=${os.environ.get('MO_DAILY_BUDGET_USD', '50')} — halting dispatch\n")
        return 42

    # model resolution: override > agents.yaml lane > env default > sonnet
    model = model_override or os.environ.get("MINI_ORK_DEFAULT_MODEL", "sonnet")
    if not model_override and node_type:
        model = resolve_lane_model(node_type, root, home)

    # lane fuse
    if os.environ.get("MO_FUSE_ENABLED", "0") == "1" and os.path.isfile(db):
        for category in ("capacity", "network", "stream", "provider"):
            if check_lane_fuse(db, node_type, category):
                sys.stderr.write(f"[lane_fuse_open] lane={node_type} error_category={category} "
                                 f"consecutive_failures=3 — halting dispatch\n")
                return 43

    tmp_out = ""
    if not out_file:
        import tempfile
        fd, tmp_out = tempfile.mkstemp(prefix="mo-llm-")
        os.close(fd)
        out_file = tmp_out

    max_attempts = _int_or(os.environ.get("MO_DISPATCH_MAX_ATTEMPTS"), 3)
    if max_attempts < 1:
        max_attempts = 1
    if model == "glm" and os.environ.get("MO_GLM_MAX_ATTEMPTS", "").isdigit():
        max_attempts = max(max_attempts, int(os.environ["MO_GLM_MAX_ATTEMPTS"]))

    # A.2 boundary shape check — None unless this node's artifact is known JSON.
    accept_fn = _shape_predicate_for(node_type)

    start_ms = int(time.time() * 1000)
    attempt = 1
    rc = 1
    while True:
        for side in (out_file + ".err.log",):
            try:
                os.remove(side)
            except OSError:
                pass
        if _default_dispatch and accept_fn is not None:
            rc = dispatch_fn(model, prompt_text, out_file, timeout_s, max_turns,
                             accept=accept_fn)
        else:
            rc = dispatch_fn(model, prompt_text, out_file, timeout_s, max_turns)
        if rc == 0:
            break
        probe = ""
        for p in (out_file + ".err.log", out_file):
            try:
                probe += open(p, errors="ignore").read()[-1000:]
            except OSError:
                pass
        probe = probe[-2000:]
        if (glm_fair_usage_retryable(model, probe, attempt, max_attempts)
                or throttle_retryable(model, probe, rc, attempt, max_attempts)):
            sleep_s = backoff_seconds(attempt)
            reason = "fair_usage" if glm_fair_usage_retryable(model, probe, attempt, max_attempts) \
                else classify_error(probe, rc)
            sys.stderr.write(f"[llm_dispatch RETRY model={model} reason={reason} "
                             f"attempt={attempt}/{max_attempts} sleep={sleep_s}s]\n")
            time.sleep(sleep_s)
            attempt += 1
            continue
        break

    selected_model = str(model).split(",", 1)[0].strip()
    try:
        selected_model = open(out_file + ".model", encoding="utf-8").read().strip() or selected_model
    except OSError:
        pass
    provider = provider_for_model(selected_model)
    feature = f"mini-ork:{node_type or 'unknown'}"
    actor = os.environ.get("MO_LANE_ACTOR") or node_type or os.environ.get("USER", "unknown")
    tier = os.environ.get("MO_LANE_TIER", "default")

    if rc == 0:
        duration_ms = int(time.time() * 1000) - start_ms
        _write_duration_ms(duration_ms)
        strip_protocol_blocks(out_file)
        sys.stdout.write(open(out_file, errors="ignore").read())
        cost_usd = "0"
        if os.path.isfile(out_file + ".cost"):
            cost_usd = open(out_file + ".cost").read().strip() or "0"
        in_tok = out_tok = cached_in = cache_create = 0
        if os.path.isfile(out_file + ".tokens"):
            parts = (open(out_file + ".tokens").read().split("\t") + ["0"] * 4)[:4]
            in_tok, out_tok, cached_in, cache_create = (_int_or(p, 0) for p in parts)
        write_llm_calls_row(db, provider, selected_model, tier, feature, actor, "success",
                            duration_ms, cost_usd, "", in_tok, out_tok, "{}", cached_in, cache_create)
        run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
        if os.path.isfile(out_file + ".cost") and run_dir:
            try:
                open(os.path.join(run_dir, ".last-llm-cost"), "w").write(open(out_file + ".cost").read())
                os.remove(out_file + ".cost")
            except OSError:
                pass
        for side in (out_file + ".tokens", out_file + ".model"):
            try:
                os.remove(side)
            except OSError:
                pass
        if tmp_out:
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        return 0

    os.environ["MO_LLM_LAST_RC"] = str(rc)
    _write_duration_ms(0)
    err = ""
    try:
        err = (open(out_file + ".err.log", errors="ignore").read()[-200:])
    except OSError:
        pass
    err = redact_secrets(err)
    write_llm_calls_row(db, provider, selected_model, tier, feature, actor, "failed", 0, 0, err)
    sys.stderr.write(f"[llm_dispatch FAIL model={model} rc={rc}]\n")
    try:
        os.remove(out_file + ".model")
    except OSError:
        pass
    if tmp_out:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
    return rc


def _write_duration_ms(ms):
    rd = os.environ.get("MINI_ORK_RUN_DIR", "")
    if not rd:
        return
    try:
        open(os.path.join(rd, ".last-llm-duration-ms"), "w").write(str(ms))
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(llm_dispatch())
