"""rubric_prescreen — Python port of lib/rubric-prescreen.sh (orchestration layer).

Faithful port of the public surface of ``lib/rubric-prescreen.sh``. The
bash file already lifts three Python heredocs into itself (extracting
JSON, summarizing artifacts, substituting template markers) — this port
collapses those back into a Python module while keeping the bash
control flow (cache lookup → template split → claude subprocess →
extract → cache emit) mirrored as a callable orchestrator.

Co-existence model (strangler-fig): ``lib/rubric-prescreen.sh`` stays
byte-identical. Parity is enforced by
``tests/unit/test_rubric_prescreen_py.py`` (≥6 live-subprocess cases:
each helper called via real ``bash -c 'source lib/rubric-prescreen.sh
&& source lib/cache.sh && fn args'`` against a temp
``db/init.sh``-scaffolded SQLite, then again via the Python port, then
DB-row + stdout diffed). The orchestrators
(``mo_run_rubric_prescreen`` / ``mo_rubric_run_score``) shell out to
non-deterministic subprocesses (claude -p, llm_dispatch); their parity
is structural (same argv, same DB writes) and not covered by the
test suite.

SRP split (SOLID refactor): the pure parsing/scoring helpers live in
``mini_ork/gates/rubric_scoring.py`` and the SQLite cache/DB layer in
``mini_ork/gates/rubric_cache.py``. This module keeps the orchestration
+ lane dispatch + ``RubricPrescreenConfig`` and RE-EXPORTS every moved
public name so existing importers and bash-parity tests are untouched
(behavior is byte-identical — the bash line-reference comments moved
with the code).

Pipeline map (bash → Python; bash line ranges from
``lib/rubric-prescreen.sh`` and ``lib/cache.sh``):

  extract_rubric_json       lines 140-159  → rubric_scoring.extract_rubric_json
  artifact_summary          lines 247-267  → rubric_scoring.artifact_summary
  substitute_template       lines 271-279  → rubric_scoring.substitute_template
  build_parse_error_payload lines 187-191  → rubric_scoring.build_parse_error_payload
  build_panel_verdict       lines 335-339  → rubric_scoring.build_panel_verdict
  fetch_kickoff_path        lines 27-29    → rubric_cache.fetch_kickoff_path
  mo_cache_input_hash       cache.sh 69-75 → rubric_cache.cache_input_hash
  mo_cache_lookup           cache.sh 98-112 → rubric_cache.cache_lookup
  mo_cache_record_hit       cache.sh 115-128 → rubric_cache.cache_record_hit
  mo_cache_emit             cache.sh 135-163 → rubric_cache.cache_emit
  mo_cache_costline_from_log cache.sh 168-177 → rubric_cache.cache_costline_from_log
  mo_run_rubric_prescreen   lines 17-207   → mo_run_rubric_prescreen
  mo_rubric_run_score       lines 229-362  → mo_rubric_run_score
  mo_append_rubric_to_feedback lines 365-383 → mo_append_rubric_to_feedback

Public surface (mirrors the bash signatures exactly where possible):
    extract_rubric_json(text: str) -> Optional[str]
    substitute_template(template: str, kickoff_body: str, diff_summary: str) -> str
    artifact_summary(run_dir: str, max_chars: int = 12000) -> str
    build_parse_error_payload(diag: str = "", log_path: Optional[str] = None) -> dict
    build_panel_verdict(score: int, pass_: bool, task_class: str) -> dict
    fetch_kickoff_path(db_path: str, epic: str, repo_root: str) -> Optional[str]
    cache_input_hash(data: str) -> str
    cache_lookup(db_path: str, stage: str, epic: str, iter: int, input_hash: str) -> str
    cache_record_hit(db_path: str, stage: str, epic: str, iter: int, input_hash: str) -> None
    cache_emit(db_path: str, stage: str, epic: str, iter: int, input_hash: str,
               status: str, output_path: str, log_path: str,
               cost_usd: float, turns: int, duration_ms: int,
               job_id: str = "unknown", prompt_version: str = "v1") -> None
    cache_costline_from_log(log_path: str) -> tuple[float, int, int]
    mo_run_rubric_prescreen(epic, worktree, iter, repo_root, prompts_dir,
                            scripts_dir) -> None
    RubricPrescreenConfig — typed parameter object capturing
        mo_run_rubric_prescreen's env-fallback semantics (M8 ISP refactor).
    mo_rubric_run_score(kickoff_path, run_dir, task_class="generic") -> None
    mo_append_rubric_to_feedback(epic, iter, feedback_path) -> None

Notes on parity:
- The three Python heredocs lifted from bash are byte-equivalent by
  construction (they were Python source files wrapped in bash heredocs).
  The port reproduces them with only the minimum required type hints.
- ``cache_emit`` uses ``secrets.token_hex(16)`` for the uuid (32 hex
  chars, no hyphens). The bash version uses ``uuidgen`` which emits a
  hyphenated UUID. The DB-row diff IGNORES the uuid column (per the
  plan's risk note) — only logical columns (stage, epic_id, iter,
  input_hash, status, output_path, log_path, cost_usd, turns,
  duration_ms, prompt_version) are compared.
- ``substitute_template`` does FIRST-occurrence-only replacement
  (mirrors the bash awk splitter at lines 57-66 which splits on the
  first marker). This is intentionally different from ``str.replace``
  which would substitute every occurrence. The parity test exercises
  the first-only semantics.
- ``cache_costline_from_log`` returns ``(0.0, 0, 0)`` on parse failure
  (bash emits literal "0 0 0" — three space-separated zeros).
- ``mo_run_rubric_prescreen`` / ``mo_rubric_run_score`` are not
  parity-tested: they shell out to ``claude -p`` and ``llm_dispatch``
  which are non-deterministic. The port mirrors the bash control flow
  verbatim including argv construction and DB writes.
- The bash file's own subprocess-on-bash handling
  (``( set -uo pipefail; ... )``) and the ``|| true`` after the
  claude call are preserved: the orchestrator never raises on
  subprocess failure (advisory-only, matches bash semantics).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

# Re-exports (SRP split): pure parsing/scoring lives in rubric_scoring,
# SQLite cache/DB layer in rubric_cache. Names stay importable from
# this module so existing importers and parity tests are untouched.
from mini_ork.gates.rubric_cache import (
    cache_costline_from_log,
    cache_emit,
    cache_input_hash,
    cache_lookup,
    cache_record_hit,
    fetch_kickoff_path,
)
from mini_ork.gates.rubric_scoring import (
    _extract_result_text,
    artifact_summary,
    build_panel_verdict,
    build_parse_error_payload,
    extract_rubric_json,
    substitute_template,
)

__all__ = [
    "RubricPrescreenConfig",
    "extract_rubric_json",
    "substitute_template",
    "artifact_summary",
    "build_parse_error_payload",
    "build_panel_verdict",
    "fetch_kickoff_path",
    "cache_input_hash",
    "cache_lookup",
    "cache_record_hit",
    "cache_emit",
    "cache_costline_from_log",
    "mo_run_rubric_prescreen",
    "mo_rubric_run_score",
    "mo_append_rubric_to_feedback",
]


# ─────────────────────────────────────────────────────────────────────────────
# Free-lane helper (mirrors lib/lane-helpers.sh mo_lane_is_free)
# ─────────────────────────────────────────────────────────────────────────────

_FREE_LANES = frozenset({"glm", "kimi", "minimax"})


def _is_free_lane(lane: str) -> bool:
    return lane in _FREE_LANES


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrators (mirror lib/rubric-prescreen.sh 17-207 + 229-362)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RubricPrescreenConfig:
    """Typed parameter object for ``mo_run_rubric_prescreen`` (ISP, M8).

    Captures in ONE place the env-fallback semantics that previously
    lived inline in the orchestrator (each ``None`` parameter fell back
    to an ``os.environ`` read). ``None`` on a field means "unset" — the
    ``resolve_*`` methods apply the built-in defaults, so the
    resolution precedence is exactly the historical one:

        explicit parameter  >  config field (env var)  >  built-in default

    Env vars read by ``from_env`` (with their historical defaults):
      MINI_ORK_HOME                 (default ".mini-ork", applied lazily
                                     inside resolve_db only)
      MINI_ORK_DB                   (default derived from home + "/state.db")
      MO_RUBRIC_LANE                (default "kimi")
      MO_RUBRIC_BUDGET_USD          (default 0.60)
      MO_RUBRIC_EFFORT              (default "low")
      MO_RUBRIC_MAX_OUTPUT_TOKENS   (default 2000)
      MO_RUBRIC_TIMEOUT_SEC         (default 480)
    """

    mini_ork_home: Optional[str] = None
    mini_ork_db: Optional[str] = None
    lane: Optional[str] = None
    rubric_budget_usd: Optional[float] = None
    rubric_effort: Optional[str] = None
    rubric_max_output_tokens: Optional[int] = None
    rubric_timeout_sec: Optional[int] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RubricPrescreenConfig":
        """Build a config from an env mapping (defaults to ``os.environ``).

        Mirrors the historical inline reads verbatim, including the
        float()/int() coercion semantics: a present-but-malformed
        numeric var raises ValueError here, exactly as the inline
        ``float(os.environ.get(...))`` did when the fallback fired.
        """
        env = os.environ if env is None else env
        return cls(
            mini_ork_home=env.get("MINI_ORK_HOME"),
            mini_ork_db=env.get("MINI_ORK_DB"),
            lane=env.get("MO_RUBRIC_LANE"),
            rubric_budget_usd=(
                float(env["MO_RUBRIC_BUDGET_USD"])
                if "MO_RUBRIC_BUDGET_USD" in env else None
            ),
            rubric_effort=env.get("MO_RUBRIC_EFFORT"),
            rubric_max_output_tokens=(
                int(env["MO_RUBRIC_MAX_OUTPUT_TOKENS"])
                if "MO_RUBRIC_MAX_OUTPUT_TOKENS" in env else None
            ),
            rubric_timeout_sec=(
                int(env["MO_RUBRIC_TIMEOUT_SEC"])
                if "MO_RUBRIC_TIMEOUT_SEC" in env else None
            ),
        )

    def resolve_db(self, mini_ork_home: Optional[str] = None) -> str:
        """Historical MINI_ORK_DB resolution, byte-for-byte.

        Old inline form::

            os.environ.get("MINI_ORK_DB",
                f"{mini_ork_home or os.environ.get('MINI_ORK_HOME', '.mini-ork')}/state.db")

        Note the corner cases preserved here: an env var that is SET
        (even to the empty string) wins over the derived default, and
        the explicit ``mini_ork_home`` parameter wins over the env home
        only when truthy.
        """
        if self.mini_ork_db is not None:
            return self.mini_ork_db
        if mini_ork_home:
            home = mini_ork_home
        elif self.mini_ork_home is not None:
            home = self.mini_ork_home
        else:
            home = ".mini-ork"
        return f"{home}/state.db"

    def resolve_lane(self) -> str:
        return self.lane if self.lane is not None else "kimi"

    def resolve_budget_usd(self) -> float:
        return self.rubric_budget_usd if self.rubric_budget_usd is not None else 0.60

    def resolve_effort(self) -> str:
        return self.rubric_effort if self.rubric_effort is not None else "low"

    def resolve_max_output_tokens(self) -> int:
        return (self.rubric_max_output_tokens
                if self.rubric_max_output_tokens is not None else 2000)

    def resolve_timeout_sec(self) -> int:
        return (self.rubric_timeout_sec
                if self.rubric_timeout_sec is not None else 480)


def mo_run_rubric_prescreen(
    epic: str,
    worktree: str,
    iter: int,
    repo_root: str,
    prompts_dir: str,
    scripts_dir: str,
    mini_ork_home: Optional[str] = None,
    mini_ork_db: Optional[str] = None,
    skip_cache: bool = False,
    lane: Optional[str] = None,
    rubric_budget_usd: Optional[float] = None,
    rubric_effort: Optional[str] = None,
    rubric_max_output_tokens: Optional[int] = None,
    rubric_timeout_sec: Optional[int] = None,
) -> None:
    """Mirror bash ``mo_run_rubric_prescreen`` (lib/rubric-prescreen.sh 17-207).

    Cache lookup → template split → claude subprocess → JSON extract
    → rubric.json write → cache emit. All subprocess calls (claude -p)
    are wrapped in ``(set -uo pipefail; ... ) || true`` semantics:
    failures are advisory-only, never raise.

    Args mirror the bash: ``epic worktree iter`` are positional, the
    rest come from env (caller can pass explicitly here for parity
    with tests that don't source the bash env). Env fallback
    resolution is centralized in ``RubricPrescreenConfig.from_env`` —
    precedence is unchanged: explicit parameter > env var > built-in
    default.
    """
    config = RubricPrescreenConfig.from_env()
    if mini_ork_db is None:
        mini_ork_db = config.resolve_db(mini_ork_home)

    if lane is None:
        lane = config.resolve_lane()

    if rubric_budget_usd is None:
        rubric_budget_usd = config.resolve_budget_usd()
    if rubric_effort is None:
        rubric_effort = config.resolve_effort()
    if rubric_max_output_tokens is None:
        rubric_max_output_tokens = config.resolve_max_output_tokens()
    if rubric_timeout_sec is None:
        rubric_timeout_sec = config.resolve_timeout_sec()

    iter_dir = f"{_run_dir(epic, mini_ork_home, repo_root)}/iter-{iter}"
    prompt_path = f"{iter_dir}/rubric-prompt.md"
    log_path = f"{iter_dir}/rubric.log"
    rubric_path = f"{iter_dir}/rubric.json"
    os.makedirs(iter_dir, exist_ok=True)

    kickoff_abs = fetch_kickoff_path(mini_ork_db, epic, repo_root)
    if not kickoff_abs or not os.path.isfile(kickoff_abs):
        # Match bash's behavior: sqlite3 emits empty on miss, then
        # ``cat $kickoff_abs`` at line 41 fails with a warning but
        # the script continues. Write a parse_error rubric and bail.
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    # Diff summary: file list + per-file +/- LOC. Cheaper than full diff.
    try:
        diff_summary = subprocess.run(
            ["git", "-C", worktree, "diff", "--stat", "main..HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout
        diff_summary = "\n".join(diff_summary.splitlines()[:30])
    except Exception:
        diff_summary = ""

    template_path = f"{prompts_dir}/rubric-prescreen.md"

    # T3: cache lookup. Hash = kickoff_body + diff_summary + template content.
    cached = ""
    cache_hash = ""
    if not skip_cache and os.environ.get("MO_SKIP_CACHE", "0") != "1":
        try:
            kickoff_body = _read_text(kickoff_abs)
            tpl_body = _read_text(template_path) if os.path.isfile(template_path) else ""
            bundle = f"{kickoff_body}\x1e{diff_summary}\x1e{tpl_body}"
            cache_hash = cache_input_hash(bundle)
            cached = cache_lookup(mini_ork_db, "rubric", epic, iter, cache_hash)
        except Exception:
            cached = ""
        if cached and os.path.isfile(cached):
            # Copy to rubric_path + record hit + emit log line.
            with open(cached, "rb") as src, open(rubric_path, "wb") as dst:
                dst.write(src.read())
            cache_record_hit(mini_ork_db, "rubric", epic, iter, cache_hash)
            try:
                with open(rubric_path) as f:
                    meta = json.loads(f.read())
            except (ValueError, OSError):
                meta = {}
            print(
                f"[mini-ork] CACHE HIT: rubric epic={epic} iter={iter} "
                f"pass={meta.get('pass')} score={meta.get('score')}",
                file=__import__("sys").stderr,
            )
            return

    # Build prompt via substitute_template.
    try:
        tpl_text = _read_text(template_path) if os.path.isfile(template_path) else ""
        kickoff_body = _read_text(kickoff_abs)
    except OSError:
        tpl_text, kickoff_body = "", ""
    prompt_text = substitute_template(tpl_text, kickoff_body, diff_summary)
    with open(prompt_path, "w") as f:
        f.write(prompt_text)

    print(
        f"[mini-ork] rubric pre-screen epic={epic} iter={iter} (lane={lane})",
        file=__import__("sys").stderr,
    )

    # Resolve the lane from the canonical provider registry. The rubric prompt
    # uses Claude's JSON-schema CLI contract, while the registry supplies the
    # lane's credentials, endpoint, model, and inherited-environment removals.
    from mini_ork.dispatch.providers import resolve_provider  # noqa: PLC0415
    _lane_root = os.path.dirname(os.path.dirname(scripts_dir.rstrip("/")))
    try:
        provider = resolve_provider(lane, root=_lane_root, environment=os.environ)
    except ValueError:
        print(
            f"[mini-ork] rubric: provider lane is not configured: {lane}",
            file=__import__("sys").stderr,
        )
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    # Subprocess env: source the lane env-script then run claude -p.
    budget_flag: list[str] = []
    if not _is_free_lane(lane):
        budget_flag = ["--max-budget-usd", str(rubric_budget_usd)]

    rubric_schema = (
        '{"type":"object","properties":{"pass":{"type":"boolean"},'
        '"score":{"type":"integer","minimum":0,"maximum":8},'
        '"items":{"type":"array","items":{"type":"object","properties":{'
        '"label":{"type":"string"},"verdict":{"type":"string",'
        '"enum":["PASS","FAIL","SKIP"]},"note":{"type":"string"}'
        '},"required":["label","verdict"]}}},"required":["pass","score","items"]}'
    )

    # Construct the process environment directly from the registry. In
    # particular, unset_env prevents an ambient Anthropic gateway from leaking
    # into the dedicated Opus/Sonnet lanes.
    sub_env = dict(os.environ)
    sub_env["CLAUDE_CODE_EFFORT_LEVEL"] = rubric_effort
    sub_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(rubric_max_output_tokens)
    for key in provider.unset_env:
        sub_env.pop(key, None)
    sub_env.update(provider.env)
    if provider.command[:1] != ("claude",):
        print(
            f"[mini-ork] rubric: lane={lane} does not support Claude rubric scoring",
            file=__import__("sys").stderr,
        )
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", rubric_schema,
        "--dangerously-skip-permissions",
        "--permission-mode", "acceptEdits",
        *budget_flag,
        prompt_text,
    ]

    # Best-effort subprocess: bash uses `( set -uo pipefail; ... ) || true`
    # so a claude failure is advisory-only. Mirror with check=False +
    # swallowing exceptions.
    try:
        with open(log_path, "w") as logf:
            subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                env=sub_env, check=False,
            )
    except (OSError, subprocess.SubprocessError):
        # Fall through to extraction (which will produce parse_error
        # because the log is empty/missing).
        pass

    # Extract JSON. Primary: --output-format json wrapper has model
    # output in .result field. Fallbacks mirror bash lines 126-138.
    result_text = _extract_result_text(log_path)

    extracted = ""
    if result_text:
        extracted = extract_rubric_json(result_text) or ""

    if not extracted:
        # awk fallback (line 163-170).
        try:
            with open(log_path) as f:
                for line in f:
                    if '{' in line and '"pass"' in line and ':' in line:
                        extracted = line.strip()
                        break
        except OSError:
            pass

    if extracted:
        try:
            json.loads(extracted)
            with open(rubric_path, "w") as f:
                f.write(extracted if extracted.endswith("\n") else extracted + "\n")
        except ValueError:
            diag = result_text[-800:] if result_text else ""
            with open(rubric_path, "w") as f:
                json.dump(
                    build_parse_error_payload(diag=diag, log_path=log_path),
                    f,
                )
    else:
        diag = result_text[-800:] if result_text else ""
        with open(rubric_path, "w") as f:
            json.dump(
                build_parse_error_payload(diag=diag, log_path=log_path),
                f,
            )

    try:
        with open(rubric_path) as f:
            meta = json.loads(f.read())
    except (ValueError, OSError):
        meta = {}
    print(
        f"[mini-ork] rubric epic={epic} iter={iter} "
        f"pass={meta.get('pass')} score={meta.get('score')}",
        file=__import__("sys").stderr,
    )

    # T3: emit cache row.
    if not skip_cache and os.environ.get("MO_SKIP_CACHE", "0") != "1":
        try:
            cost, turns, dur = cache_costline_from_log(log_path)
        except Exception:
            cost, turns, dur = 0.0, 0, 0
        try:
            cache_emit(
                mini_ork_db, "rubric", epic, iter, cache_hash, "success",
                rubric_path, log_path, cost, turns, dur,
                job_id=os.environ.get("JOB_ID", "unknown"),
            )
        except sqlite3.IntegrityError:
            pass


def mo_rubric_run_score(
    kickoff_path: str,
    run_dir: str,
    task_class: str = "generic",
    mini_ork_root: Optional[str] = None,
    mini_ork_home: Optional[str] = None,
    mini_ork_db: Optional[str] = None,
) -> None:
    """Mirror bash ``mo_rubric_run_score`` (lib/rubric-prescreen.sh 229-362).

    Run-shaped sibling: takes a kickoff_path + run_dir (no epics table
    needed), dispatches through ``llm_dispatch`` (mirror with a
    subprocess call), extracts JSON, writes ``<run_dir>/rubric.json``
    and ``<run_dir>/panel-verdict.json``, optionally writes an
    ``execution_traces`` row via ``trace_write`` (no-op if not
    available — the bash version checks ``declare -f trace_write``
    and silently skips).

    Advisory-only: never raises on subprocess failure.
    """
    if mini_ork_root is None:
        mini_ork_root = os.environ.get("MINI_ORK_ROOT", ".")
    if mini_ork_db is None:
        mini_ork_db = os.environ.get(
            "MINI_ORK_DB",
            f"{mini_ork_home or os.environ.get('MINI_ORK_HOME', '.mini-ork')}/state.db",
        )

    if not os.path.isfile(kickoff_path):
        print(f"rubric: kickoff not found: {kickoff_path}", file=__import__("sys").stderr)
        return
    if not os.path.isdir(run_dir):
        print(f"rubric: run_dir not found: {run_dir}", file=__import__("sys").stderr)
        return

    template = f"{mini_ork_root}/prompts/rubric-prescreen.md"
    if not os.path.isfile(template):
        print(f"rubric: template missing: {template}", file=__import__("sys").stderr)
        return

    rubric_path = f"{run_dir}/rubric.json"
    verdict_path = f"{run_dir}/panel-verdict.json"

    artifacts = artifact_summary(run_dir)
    if not artifacts:
        artifacts = "(run dir contains no readable artifacts)"

    prompt_text = substitute_template(_read_text(template), _read_text(kickoff_path), artifacts)

    print(f"  rubric: scoring run artifacts (task_class={task_class})", file=__import__("sys").stderr)
    raw = ""
    rc = 0
    # In-process dispatch. The `llm_dispatch` CLI binary was removed in the
    # 2026-07 bash-removal, so shelling out to it raised FileNotFoundError →
    # rc=127 → every rubric score silently degraded to a parse-error payload.
    # Call the native dispatcher directly (mirrors learning/gradient_extractor):
    # on rc==0 it writes the model output to stdout, which we capture here.
    import contextlib
    import io as _io
    from mini_ork.dispatch import llm_dispatch as native_dispatch
    stdout_buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf):
            rc = native_dispatch.llm_dispatch(
                [
                    "--task-class", task_class,
                    "--node-type", "rubric",
                    "--prompt-text", prompt_text,
                ],
                root=mini_ork_root,
            )
        raw = stdout_buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — advisory-only, never raise
        rc = 1
        raw = str(exc)

    if rc != 0:
        print(
            f"  rubric: dispatch failed (rc={rc}): {raw[-300:]}",
            file=__import__("sys").stderr,
        )
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(), f)
        return

    extracted = extract_rubric_json(raw) or ""
    if extracted and "pass" in (json.loads(extracted) if extracted else {}) \
            and "score" in (json.loads(extracted) if extracted else {}):
        with open(rubric_path, "w") as f:
            f.write(extracted if extracted.endswith("\n") else extracted + "\n")
    else:
        diag = raw[-800:] if raw else ""
        with open(rubric_path, "w") as f:
            json.dump(build_parse_error_payload(diag=diag), f)

    try:
        with open(rubric_path) as f:
            meta = json.loads(f.read())
    except (ValueError, OSError):
        meta = {}
    score = meta.get("score", -1)
    passed = bool(meta.get("pass", False))
    print(
        f"  rubric: pass={passed} score={score}/8 → {rubric_path}",
        file=__import__("sys").stderr,
    )

    # Panel verdict for the promotion gate: 0-8 → 0-100.
    if score != -1:
        with open(verdict_path, "w") as f:
            json.dump(
                build_panel_verdict(int(score), passed, task_class),
                f,
            )

    # Learning hook: persist as an execution trace. Mirror bash lines
    # 346-360: if ``trace_write`` is not available, skip silently.
    # The bash version calls ``trace_write <payload> >/dev/null 2>&1
    # || true`` — we mirror with a best-effort subprocess that is
    # allowed to fail without raising.
    trace_id = f"tr-rubric-{int(datetime.now(timezone.utc).timestamp())}"
    status = "success" if passed else "failure"
    try:
        with open(rubric_path) as f:
            rub = json.loads(f.read())
    except (ValueError, OSError):
        rub = {}
    payload = {
        "trace_id": trace_id,
        "task_class": task_class,
        "status": status,
        "final_artifact_ref": rubric_path,
        "verifier_output": rub,
    }
    try:
        subprocess.run(
            ["trace_write", json.dumps(payload)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, env={**os.environ, "MINI_ORK_DB": mini_ork_db},
        )
    except (OSError, subprocess.SubprocessError):
        pass


def mo_append_rubric_to_feedback(
    epic: str,
    iter: int,
    feedback_path: str,
    run_dir: Optional[str] = None,
    mini_ork_home: Optional[str] = None,
) -> None:
    """Mirror bash ``mo_append_rubric_to_feedback`` (lib/rubric-prescreen.sh 365-383).

    Pure file append. Reads ``<run_dir>/iter-<iter>/rubric.json`` (or
    the supplied ``run_dir``), and if ``.pass != true``, appends a
    formatted section to ``feedback_path`` listing only the non-PASS
    items (FAIL + SKIP).

    Pass-through: returns silently if the rubric file is missing or
    the rubric passed.
    """
    if run_dir is None:
        run_dir = _run_dir(epic, mini_ork_home)
    rub = f"{run_dir}/iter-{iter}/rubric.json"
    if not os.path.isfile(rub):
        return
    try:
        with open(rub) as f:
            data = json.loads(f.read())
    except (ValueError, OSError):
        return
    if data.get("pass") is True:
        return
    score = data.get("score", -1)
    items = data.get("items", [])
    lines = [
        "",
        "## Rubric pre-screen (advisory — Phase A.5)",
        "",
        f"Score: {score}/8 (need ≥6 to PASS)",
        "",
    ]
    for it in items:
        verdict = it.get("verdict", "")
        if verdict == "PASS":
            continue
        label = it.get("label", "")
        note = it.get("note", "")
        lines.append(f"- **[{verdict}]** {label} — {note}")
    lines.append("")
    with open(feedback_path, "a") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (not part of __all__; not part of the bash surface)
# ─────────────────────────────────────────────────────────────────────────────

def _run_dir(epic: str, mini_ork_home: Optional[str] = None,
             repo_root: Optional[str] = None) -> str:
    """Mirror bash ``mo_run_dir`` (lib/mo-runner.sh).

    Returns the per-epic run dir. Bash's exact resolution chain
    (T1.0 run-dir-first agents.yaml pattern) is not part of the
    parity contract — the port uses ``<home>/runs/<epic>`` which is
    the canonical location. The bash version is more elaborate (it
    checks $MINI_ORK_HOME, $MO_RUNS_DIR, the repo_root/.mini-ork
    layout); tests pass the resolved dir explicitly via ``run_dir=``
    so the simpler default is sufficient.
    """
    home = mini_ork_home or os.environ.get(
        "MINI_ORK_HOME", repo_root or "."
    )
    return os.path.join(home, "runs", epic)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()
