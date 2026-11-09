#!/usr/bin/env python3
"""readme_drift.py — Layers 2a + 2b of the README drift gate, in Python.

Python port of `scripts/readme-drift-gatekeeper.sh`, `readme-drift-panel.sh`
and `readme-drift-providers-doctor.sh` (bash-removal Phase 4) — the same
prompts, thresholds, JSON shapes and exit codes; the three shell twins were
deleted on 2026-11-09 once the pre-push hook, the Makefile and the operator
docs all called this file.

Three layers, one command each:

  gatekeeper  Layer 2a (~$0.005). A single MiniMax call decides whether the
              4-lens panel is worth firing. Most pushes touch typos and links
              that cannot move a claim's truth value.
  doctor      Pre-flight liveness probe. One 1-token call per lens lane; if
              fewer than MO_DRIFT_MIN_RESPONSIVE_LENSES answer cleanly the
              panel cannot produce signal, so the caller skips it instead of
              paying 4 × 90 s of guaranteed-empty calls.
  panel       Layer 2b (~$0.30). Four lens calls in parallel (technical
              accuracy, narrative consistency, comparison fairness, citation
              accuracy), then an opus arbiter that emits the final verdict.

Exit codes:
  gatekeeper  0 panel SHOULD fire · 1 panel can be SKIPPED · 2 invocation error
  doctor      0 panel viable · 1 not viable · 2 invocation error
  panel       0 NO_DRIFT · 1 DRIFT / INDETERMINATE · 2 invocation error

Every layer prints one JSON object on stdout (the gatekeeper prints its
fail-open object on stderr when it cannot decide, exactly like the shell).

Env knobs:
  MO_README                       path to README.md (default: ./README.md)
  MO_SECRETS / MINI_ORK_SECRETS   secret store to load credentials from
  MO_DRIFT_MIN_RESPONSIVE_LENSES  doctor threshold (default: 2)
  MO_DRIFT_PROBE_TIMEOUT_SEC      doctor per-lane timeout (default: 20)
  MO_DRIFT_RUN_DIR                panel output dir (default: /tmp/readme-drift-<ts>-<pid>)

Usage:
  scripts/readme_drift.py gatekeeper
  scripts/readme_drift.py doctor
  scripts/readme_drift.py panel
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANES = (
    ("codex_lens", "codex"),
    ("kimi_lens", "kimi"),
    ("minimax_lens", "minimax"),
    ("glm_lens", "glm"),
)

# The gatekeeper's one provider. Cheap, fast, and a distinct family from the
# panel's aggregate: it shares a family with `minimax_lens`, but as a ROUTER
# rather than a voter that is not a coalition risk — the panel still spans 4.
GATEKEEPER_LANE = "minimax"
GATEKEEPER_TIMEOUT_S = 45
LENS_TIMEOUT_S = 90
ARBITER_LANE = "opus"
ARBITER_TIMEOUT_S = 120


# ── shared plumbing ──────────────────────────────────────────────────────────


def readme_path() -> Path:
    return Path(os.environ.get("MO_README") or "README.md")


def load_secrets() -> None:
    """Fold the local secret store into the environment, guarded.

    The shell sourced `secrets.local.sh`; Python does not execute a local
    shell file, so it reads the same file through the strict reader and only
    fills keys that are absent — an exported value still wins.
    """
    sys.path.insert(0, str(ROOT))
    from mini_ork.dispatch.secrets import SecretStoreError, read_secret_exports

    explicit = os.environ.get("MINI_ORK_SECRETS") or os.environ.get("MO_SECRETS")
    try:
        exports = read_secret_exports(explicit or None)
    except SecretStoreError as exc:
        sys.stderr.write(f"readme-drift: unreadable secret store: {exc}\n")
        raise
    for name, value in exports.items():
        os.environ.setdefault(name, value)


def dispatch(lane: str, prompt: str, timeout_s: float):
    """One lane call. Returns the DispatchResult (`.text`, `.ok`, `.error`)."""
    from mini_ork.dispatch import DispatchRequest, dispatch_model

    return dispatch_model(
        DispatchRequest(model=lane, prompt=prompt, timeout_s=float(timeout_s))
    )


def first_json(raw: str) -> dict | None:
    """The first complete JSON object in `raw`, or None.

    Providers append prose, thinking blocks and `<z-insight>` telemetry after
    the JSON they were asked for, and some fence it. A greedy regex would mash
    the object and the trailing block into one unparseable string, so this
    strips a fence and then walks forward with `raw_decode`, ignoring anything
    past the closing brace.
    """
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    start = raw.find("{")
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def git(*args: str) -> str:
    """Best-effort git call: empty output when the command cannot run."""
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=30, cwd=ROOT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


# ── Layer 2a: the gatekeeper ─────────────────────────────────────────────────


def gatekeeper() -> int:
    """Decide whether the 4-lens panel should fire. 0 = yes, 1 = skip, 2 = error."""
    try:
        load_secrets()
    except Exception:  # noqa: BLE001 — any unreadable store is "can't decide"
        return _gatekeeper_fail_open("secret store unreadable")

    if not os.environ.get("MINIMAX_API_KEY"):
        _write_stderr(
            {
                "verdict": "PANEL_SKIP",
                "reason": "MINIMAX_API_KEY unset — fail-open to keep push moving",
                "cost_estimate_usd": 0,
            }
        )
        return 2

    upstream = "origin/main" if git("rev-parse", "origin/main").strip() else "HEAD~1"
    changed = git("diff", "--name-only", f"{upstream}...HEAD").splitlines()[:50]
    readme_stat = git("diff", "--stat", f"{upstream}...HEAD", "--", str(readme_path())).strip().splitlines()
    roadmap_stat = git("diff", "--stat", f"{upstream}...HEAD", "--", "ROADMAP.md").strip().splitlines()
    structural = sum(
        1
        for path in changed
        if re.match(
            r"^(lib/|bin/|recipes/|db/migrations/|schemas/|install\.sh|examples/)", path
        )
    )

    prompt = (
        "You are a fast router. Given a git diff summary, decide whether a 4-lens "
        "README claim-drift audit should fire.\n\n"
        "The audit costs ~$0.30 and takes ~30 seconds, so it must NOT fire for "
        "trivial changes (typos, formatting, link updates that don't shift a "
        "claim's truth value). It MUST fire when the diff plausibly invalidates a "
        "load-bearing claim in README.md (counts, capabilities, comparisons, "
        "citations, architecture description).\n\n"
        f"Files changed in this push:\n{chr(10).join(changed)}\n\n"
        f"README.md diff stat: {readme_stat[-1] if readme_stat else 'no change'}\n"
        f"ROADMAP.md diff stat: {roadmap_stat[-1] if roadmap_stat else 'no change'}\n"
        f"Structural files changed (lib/ bin/ recipes/ migrations/ schemas/): {structural}\n\n"
        "Respond with exactly two lines:\n"
        "VERDICT: PANEL_NEEDED   (if drift audit should fire)\n"
        "REASON: <one short sentence>\n\n"
        "OR\n\n"
        "VERDICT: PANEL_SKIP\n"
        "REASON: <one short sentence>\n"
    )
    result = dispatch(GATEKEEPER_LANE, prompt, GATEKEEPER_TIMEOUT_S)
    if not result.ok or not (result.text or "").strip():
        reason = f"gatekeeper LLM unreachable ({result.error or 'empty output'}) — fail-open"
        _write_stderr({"verdict": "PANEL_SKIP", "reason": reason, "cost_estimate_usd": 0})
        return 2

    verdict = _line_after(result.text, "VERDICT")
    reason = (_line_after(result.text, "REASON") or "")[:200]
    if verdict == "PANEL_NEEDED":
        emit({"verdict": "PANEL_NEEDED", "reason": reason, "cost_estimate_usd": 0.005})
        return 0
    if verdict == "PANEL_SKIP":
        emit({"verdict": "PANEL_SKIP", "reason": reason, "cost_estimate_usd": 0.005})
        return 1
    _write_stderr(
        {
            "verdict": "PANEL_SKIP",
            "reason": f"gatekeeper output unparseable (got: {verdict[:100]}) — fail-open",
            "cost_estimate_usd": 0.005,
        }
    )
    return 2


def _line_after(text: str, label: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(f"{label}:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _gatekeeper_fail_open(why: str) -> int:
    _write_stderr(
        {"verdict": "PANEL_SKIP", "reason": f"{why} — fail-open", "cost_estimate_usd": 0}
    )
    return 2


def _write_stderr(payload: dict) -> None:
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")


# ── the doctor ───────────────────────────────────────────────────────────────


def doctor() -> int:
    """Probe each lens lane; 0 when enough answer for the panel to mean anything."""
    try:
        load_secrets()
    except Exception:  # noqa: BLE001
        return 2

    threshold = int(os.environ.get("MO_DRIFT_MIN_RESPONSIVE_LENSES") or 2)
    probe_timeout = float(os.environ.get("MO_DRIFT_PROBE_TIMEOUT_SEC") or 20)

    def probe(item: tuple[str, str]) -> tuple[str, dict]:
        lens_name, lane = item
        started = time.monotonic()
        result = dispatch(lane, f"Say {lens_name}_OK only.", probe_timeout)
        wall = int(round(time.monotonic() - started))
        text = (result.text or "").strip()
        note = ""
        if result.ok and text:
            if re.search(r"(error|failed|exception|no stdin data received)", text, re.I):
                note = "rc=0 but stdout contains error string"
                responsive = False
            else:
                responsive = True
        elif _timed_out(result) or wall >= probe_timeout:
            note = f"timeout after {int(probe_timeout)}s"
            responsive = False
        elif result.ok and not text:
            note = "rc=0 but empty stdout (silent fail)"
            responsive = False
        else:
            note = (result.error or "rc!=0").strip()[:200]
            responsive = False
        return lens_name, {
            "responsive": responsive,
            "wall_sec": wall,
            "rc": 0 if result.ok else 1,
            "note": note,
        }

    with ThreadPoolExecutor(max_workers=len(LANES)) as pool:
        probed = dict(pool.map(probe, LANES))

    responsive_count = sum(1 for row in probed.values() if row["responsive"])
    viable = responsive_count >= threshold
    emit(
        {
            "viable": viable,
            "threshold": threshold,
            "responsive_count": responsive_count,
            "providers": probed,
        }
    )
    return 0 if viable else 1


def _timed_out(result) -> bool:
    return bool(re.search(r"time(d)?[ _-]?out", result.error or "", re.I))


# ── Layer 2b: the panel ──────────────────────────────────────────────────────

LENS_PROMPTS = {
    "codex_lens": (
        "Lens role: TECHNICAL ACCURACY. Verify every numerical claim, file path, and "
        "schema enum cited in the README against the repo inventory above. Flag any "
        "count that's wrong, any cited path that doesn't exist, any schema enum that "
        "doesn't match."
    ),
    "kimi_lens": (
        "Lens role: NARRATIVE CONSISTENCY. Read the README as a story. Are the claims "
        "in section N still supported by section M? Does the architecture diagram "
        "match the recipe table? Is the v0.3 status section consistent with the "
        "Roadmap pointer? Flag any internal contradictions or stale narrative arcs."
    ),
    "minimax_lens": (
        "Lens role: COMPARISON-TABLE FAIRNESS. Look at the table comparing mini-ork to "
        "Claude Code / OpenAI Agents SDK / LangGraph dynamic workflows. Is each row "
        "still fair? Has any row become misleading because of changes in those "
        "external tools? Has any row become misleading because of what mini-ork has "
        "shipped or removed? Flag stale comparisons."
    ),
    "glm_lens": (
        "Lens role: CITATION ACCURACY. The README cites 6 arXiv papers (Nasser 2026, "
        "Rajan 2025, Karanam 2025, Zietsman 2026, Shehata 2026, Song 2026). For each "
        "citation, verify the claim being attached to the citation is one the paper "
        "actually supports. Flag any case where the README's gloss of a paper has "
        "drifted from what the paper actually argues."
    ),
}


def inventory(stamp: str) -> str:
    def count(pattern: str, *, dirs: bool = False) -> str:
        path = Path(pattern)
        if dirs:
            return str(len([p for p in path.glob("*") if p.is_dir()]))
        return str(len([p for p in path.glob("*") if p.is_file()]))

    recipes = sorted(p.name for p in Path("recipes").glob("*") if p.is_dir())
    providers = len(re.findall(r"^  [A-Za-z0-9_-]+:$", _read_text(ROOT / "config/providers.yaml"), re.M))
    commits = git("log", "--oneline", "-15").strip().splitlines()
    roadmap = re.findall(r"^### v[0-9].*$", _read_text(ROOT / "ROADMAP.md"), re.M)
    return (
        f"Repo inventory (current state, computed at {stamp}):\n\n"
        f"  lib/*.sh count:                 {count('lib/*.sh')}\n"
        f"  bin/mini-ork-* entrypoint count: {count('bin/mini-ork*')}\n"
        f"  db/migrations/*.sql count:       {count('db/migrations/*.sql')}\n"
        f"  recipes/ subdir count:           {count('recipes/*', dirs=True)}\n"
        f"  provider registry entries:        {providers}\n\n"
        "Recipes shipped:\n"
        + "\n".join(f"  {name}" for name in recipes)
        + "\n\nLatest commits (15 most recent on this branch):\n"
        + "\n".join(f"  {line}" for line in commits)
        + "\n\nROADMAP.md current section header:\n"
        + (f"  {roadmap[-1]}\n" if roadmap else "  \n")
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def panel() -> int:
    """4 lenses in parallel + an arbiter. 0 = NO_DRIFT, 1 = DRIFT/INDETERMINATE."""
    try:
        load_secrets()
    except Exception:  # noqa: BLE001
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(
        os.environ.get("MO_DRIFT_RUN_DIR") or f"/tmp/readme-drift-{stamp}-{os.getpid()}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    common = (
        "You are a README drift auditor for the mini-ork OSS framework. Your job is\n"
        "to verify the load-bearing claims in README.md against the current repo\n"
        "state. You are ONE of FOUR lenses in a heterogeneous-family panel — focus\n"
        "ONLY on your assigned axis; the other lenses cover the rest.\n\n"
        "Output strict JSON on stdout, no prose around it:\n\n"
        "{\n"
        '  "lens":           "<your lens name>",\n'
        '  "verdict":        "NO_DRIFT" | "DRIFT",\n'
        '  "drifted_claims": [\n'
        '    { "claim":         "<exact quote from README>",\n'
        '      "evidence":      "<why it\'s wrong>",\n'
        '      "suggested_fix": "<one-paragraph replacement text>" }\n'
        "  ],\n"
        '  "confidence": 0.0..1.0\n'
        "}\n\n"
        f"Repo inventory:\n{inventory(stamp)}\n"
        f"README.md (the doc being audited):\n\n{_read_text(readme_path())}\n"
    )

    started = time.monotonic()

    def run_lens(item: tuple[str, str]) -> tuple[str, dict]:
        lens_name, lane = item
        result = dispatch(lane, f"{common}\n\n{LENS_PROMPTS[lens_name]}", LENS_TIMEOUT_S)
        parsed = first_json(result.text or "") if result.ok else None
        if parsed is None:
            parsed = {
                "lens": lens_name,
                "verdict": "NO_DRIFT",
                "drifted_claims": [],
                "confidence": 0.0,
                "error": (result.error or "no JSON object in lens output")[:400],
            }
        else:
            parsed.setdefault("lens", lens_name)
            parsed.setdefault("verdict", "NO_DRIFT")
            parsed.setdefault("drifted_claims", [])
            parsed.setdefault("confidence", 0.5)
        (run_dir / f"lens-{lens_name}.json").write_text(json.dumps(parsed), encoding="utf-8")
        return lens_name, parsed

    with ThreadPoolExecutor(max_workers=len(LANES)) as pool:
        lens_results = dict(pool.map(run_lens, LANES))
    parallel_wall = int(round(time.monotonic() - started))

    # A lens counts as responsive when it produced a real verdict — not a
    # parse failure and not confidence 0.0.
    failed = [
        name
        for name, row in lens_results.items()
        if row.get("error") or float(row.get("confidence") or 0.0) == 0.0
    ]
    responsive = len(LANES) - len(failed)

    arbiter_prompt = (
        "You are the arbiter of a 4-lens drift-audit panel. Each lens audited\n"
        "README.md against a different axis and emitted JSON. Synthesize them\n"
        "into ONE final verdict.\n\n"
        "The verdict is THREE-VALUED: NO_DRIFT | DRIFT | INDETERMINATE.\n\n"
        "Rules:\n"
        "- If ALL 4 lenses have confidence == 0.0 OR carry an \"error\" field\n"
        "  (parse failure / provider timeout / empty output) → final\n"
        "  INDETERMINATE. \"Absence of evidence is not evidence of absence\" —\n"
        "  refuse to fabricate NO_DRIFT from a broken pipeline. Capture which\n"
        "  lenses failed in \"notes\".\n"
        "- If ALL 4 lenses say NO_DRIFT AND at least 1 lens has confidence ≥ 0.5\n"
        "  → final NO_DRIFT. (At least one lens must have actually run.)\n"
        "- If 1+ lens says DRIFT with confidence ≥ 0.6 → final DRIFT.\n"
        "- If 1+ lens says DRIFT but with confidence < 0.6 → final NO_DRIFT,\n"
        "  but capture the low-confidence flag in \"notes\".\n"
        "- De-duplicate drifted_claims across lenses (same README quote = same\n"
        "  claim, merge the evidence/suggested_fix into the strongest one).\n\n"
        "Lens outputs (one JSON per line):\n"
        + "\n".join(json.dumps(lens_results[name]) for name, _ in LANES)
        + "\n\nEmit strict JSON on stdout:\n\n"
        "{\n"
        '  "verdict": "NO_DRIFT" | "DRIFT" | "INDETERMINATE",\n'
        '  "lens_verdicts": {\n'
        '    "codex_lens":   "NO_DRIFT" | "DRIFT" | "FAILED",\n'
        '    "kimi_lens":    "NO_DRIFT" | "DRIFT" | "FAILED",\n'
        '    "minimax_lens": "NO_DRIFT" | "DRIFT" | "FAILED",\n'
        '    "glm_lens":     "NO_DRIFT" | "DRIFT" | "FAILED"\n'
        "  },\n"
        '  "responsive_lens_count": <int 0..4 — how many lenses produced non-error output>,\n'
        '  "drifted_claims": [\n'
        '    { "claim": "...", "evidence": "...", "suggested_fix": "...",\n'
        '      "lenses_flagged": ["..."] }\n'
        "  ],\n"
        '  "notes": "<low-confidence flags, split verdicts, OR list of failed lenses>"\n'
        "}\n"
    )

    arbiter_result = dispatch(ARBITER_LANE, arbiter_prompt, ARBITER_TIMEOUT_S)
    arbiter = first_json(arbiter_result.text or "") if arbiter_result.ok else None
    if arbiter is None:
        arbiter = {
            "verdict": "INDETERMINATE",
            "lens_verdicts": {},
            "drifted_claims": [],
            "responsive_lens_count": 0,
            "notes": (
                "arbiter output unparseable — INDETERMINATE (caller decides "
                "fail-open vs block)"
            ),
        }
    arbiter.setdefault("responsive_lens_count", responsive)

    # Post-process safety net (Layer-2b honesty rule): the arbiter can say
    # NO_DRIFT on a panel where nobody had evidence. Absence of evidence is
    # not evidence of absence.
    if responsive == 0:
        arbiter["verdict"] = "INDETERMINATE"
        note = arbiter.get("notes") or ""
        failure_msg = f"all 4 lenses failed (no responsive output): {', '.join(failed)}"
        if failure_msg not in note:
            arbiter["notes"] = (note + " | " if note else "") + failure_msg
    elif arbiter.get("verdict") not in ("NO_DRIFT", "DRIFT", "INDETERMINATE"):
        arbiter["verdict"] = "INDETERMINATE"
        arbiter["notes"] = (
            (arbiter.get("notes") or "")
            + " | arbiter emitted unknown verdict, promoted to INDETERMINATE"
        ).strip(" |")

    (run_dir / "arbiter.json").write_text(json.dumps(arbiter), encoding="utf-8")
    report = run_dir / "report.md"
    report.write_text(_report_markdown(arbiter, stamp, parallel_wall, run_dir), encoding="utf-8")
    total_wall = int(round(time.monotonic() - started))

    emit(
        {
            "verdict": arbiter.get("verdict"),
            "lens_count": 4,
            "drifted_claims": arbiter.get("drifted_claims") or [],
            "notes": arbiter.get("notes") or "",
            "cost_estimate_usd": 0.30,
            "wall_time_sec": total_wall,
            "report_path": str(report),
        }
    )
    return 0 if arbiter.get("verdict") == "NO_DRIFT" else 1


def _report_markdown(arbiter: dict, stamp: str, parallel_wall: int, run_dir: Path) -> str:
    lines = [f"# README drift audit — {stamp}", "", f"**Verdict**: {arbiter.get('verdict')}", ""]
    lines += ["## Per-lens verdicts", ""]
    lines += [
        f"- **{name}**: {verdict}"
        for name, verdict in (arbiter.get("lens_verdicts") or {}).items()
    ]
    lines += ["", "## Drifted claims", ""]
    for claim in arbiter.get("drifted_claims") or []:
        lines += [
            f"### Claim: {claim.get('claim', '')}",
            "",
            f"**Evidence**: {claim.get('evidence', '')}",
            "",
            f"**Suggested fix**: {claim.get('suggested_fix', '')}",
            "",
            f"**Lenses flagged**: {', '.join(claim.get('lenses_flagged') or [])}",
            "",
        ]
    lines += ["## Notes", arbiter.get("notes") or "(none)", "", "## Run metadata"]
    lines += [f"- Parallel-lens wall time: {parallel_wall}s", f"- Total run dir: {run_dir}", ""]
    return "\n".join(lines)


# ── entrypoint ───────────────────────────────────────────────────────────────

_COMMANDS = {"gatekeeper": gatekeeper, "doctor": doctor, "panel": panel}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in _COMMANDS:
        sys.stderr.write("usage: readme_drift.py {gatekeeper|doctor|panel}\n")
        return 2
    return _COMMANDS[args[0]]()


if __name__ == "__main__":
    raise SystemExit(main())
