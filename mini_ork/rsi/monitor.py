"""The post-merge canary and the research-refresh launcher.

Two jobs that make the research-driven RSI loop safe to leave running
unattended, both deliberately separate from the cycle state machine:

* **The canary.** Score ``main`` on a benchmark whenever its tree changes, keep
  the score history, and halt technique adoption when the score has declined
  more than ``monitor.drop`` below its best-ever value for
  ``monitor.consecutive`` canaries in a row. The repo rule is *fix forward*: a
  decline queues a ``regression`` candidate and writes a halt marker. It never
  reverts ``main``.

* **Research refresh.** When the newest technique review for a domain is older
  than ``research.refresh_days``, launch that domain's review recipe detached,
  at most once a day, so the loop keeps reading new papers.

No model and no network access lives here. The canary shells out to ``git`` (to
materialise the tree it scores) and to the benchmark runner; the refresh
launcher spawns the recipe and records that it did. Everything else is files and
a clock in.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import benchmarks, impact, impact_state, research
from .candidates import Candidate

__all__ = [
    "HALT_MARKER",
    "adoption_halted",
    "bench_domain",
    "canary_tree",
    "halt_path",
    "maybe_refresh",
    "regression_candidate",
    "remove_canary_tree",
    "review_age_days",
    "run_canary",
]

#: The file name of the durable adoption halt under ``<home>/rsi/``.
HALT_MARKER = "ADOPTION_HALTED"

#: How long a research refresh is suppressed after one launches, in seconds.
_REFRESH_COOLDOWN_S = 86400

#: The run-id prefix the default review glob (``runs/verify-*``) matches.
_RUN_ID_PREFIX = "verify-rrl-"


# --------------------------------------------------------------------------
# The adoption halt marker
# --------------------------------------------------------------------------


def halt_path(home: str | Path) -> Path:
    """Where the adoption-halt marker lives."""
    return Path(home) / "rsi" / HALT_MARKER


def adoption_halted(home: str | Path) -> dict[str, Any] | None:
    """The marker's JSON, or ``None`` when absent.

    An unparseable marker still means "halted": the whole point of the file is
    that a corrupt read must fail closed, never be mistaken for "clear".
    """
    path = halt_path(home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reason": "unreadable marker"}
    if not isinstance(payload, dict):
        return {"reason": "unreadable marker"}
    return payload


# --------------------------------------------------------------------------
# Which domain a benchmark belongs to
# --------------------------------------------------------------------------


def bench_domain(bench: str) -> str:
    """The domain of the first spec whose benchmark is ``bench``.

    Falls back to ``verification`` — the domain whose benchmark the shipped
    loop uses — so a regression candidate always lands in a real domain.
    """
    for spec in research.DEFAULT_SPECS.values():
        if spec.benchmark == bench:
            return spec.domain
    return "verification"


def _spec_for_bench(bench: str):
    for spec in research.DEFAULT_SPECS.values():
        if spec.benchmark == bench:
            return spec
    return None


# --------------------------------------------------------------------------
# The canary tree
# --------------------------------------------------------------------------


def canary_tree(root: str | Path, home: str | Path, sha: str) -> Path:
    """A detached worktree at ``sha`` under the RSI home, created on demand."""
    tree = Path(home) / "rsi" / f"canary-{sha[:12]}"
    if not tree.exists():
        tree.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(tree), sha],
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"git worktree add {tree} {sha} failed: {detail}")
    return tree


def remove_canary_tree(root: str | Path, path: str | Path) -> None:
    """Remove a canary worktree; errors are ignored (cleanup must not mask a result)."""
    subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
        capture_output=True, text=True, check=False)


# --------------------------------------------------------------------------
# Scoring main
# --------------------------------------------------------------------------


def _head_sha(root: str | Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git rev-parse HEAD in {root} failed: {detail}")
    return proc.stdout.strip()


def _score(result: benchmarks.BenchResult) -> float | None:
    """The result's accuracy, else the mean of its per-item scores."""
    accuracy = result.meta.get("accuracy")
    if accuracy is not None:
        return float(accuracy)
    values = [float(value) for value in result.scores.values()]
    if not values:
        return None
    return sum(values) / len(values)


def run_canary(
    root: str | Path,
    home: str | Path,
    cfg,
    bench: str,
    *,
    now: int,
) -> dict[str, Any]:
    """Score ``main`` on ``bench``, record it, and ratchet the history.

    A regressed canary writes the adoption-halt marker and returns
    ``status="regressed"``; a later canary at or above the peak clears the
    marker and returns ``status="recovered"``. Nothing here reverts ``main``.
    """
    root = Path(root)
    home = Path(home)
    sha = _head_sha(root)
    tree = canary_tree(root, home, sha)
    try:
        ctx = benchmarks.BenchContext(
            home=home, repo=root,
            workers=cfg.impact.workers, timeout_s=cfg.impact.timeout_s)
        result = benchmarks.run_benchmark(bench, tree, ctx=ctx)
    finally:
        remove_canary_tree(root, tree)

    score = _score(result)
    if score is None:
        return {"status": "error", "bench": bench, "sha": sha, "reason": "no scores"}

    state = impact_state.load_state(home)
    if bench not in state.get("benchmarks", {}):
        items = benchmarks.get_benchmark(bench).items(ctx)
        impact_state.bench_state(
            state, bench, impact_state.corpus_hash(items))
    impact_state.record_canary(state, bench, sha=sha, score=score, at=now)
    impact_state.save_state(home, state)

    verdict = impact.ratchet(
        impact_state.canary_scores(state, bench),
        drop=cfg.monitor.drop, consecutive=cfg.monitor.consecutive)
    history = state["benchmarks"][bench]["canary"]
    peak_sha = (history[verdict["peak_index"]]["sha"]
                if verdict["peak_index"] is not None else None)
    outcome = {
        "status": "ok",
        "bench": bench,
        "sha": sha,
        "score": score,
        **verdict,
        "peak_sha": peak_sha,
    }

    if verdict["revert"]:
        marker = halt_path(home)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "bench": bench,
            "peak": verdict["peak"],
            "peak_sha": peak_sha,
            "latest": score,
            "latest_sha": sha,
            "declines": verdict["declines"],
            "at": now,
        }), encoding="utf-8")
        outcome["status"] = "regressed"
        return outcome

    halted = adoption_halted(home)
    if (halted is not None and halted.get("bench") == bench
            and score >= verdict["peak"] - cfg.monitor.drop):
        try:
            halt_path(home).unlink()
        except OSError:
            pass
        outcome["status"] = "recovered"
    return outcome


# --------------------------------------------------------------------------
# The fix-forward candidate a halt queues
# --------------------------------------------------------------------------


def regression_candidate(halt: Mapping[str, Any], *, now: int) -> Candidate:
    """The fix-forward ``regression`` candidate a canary halt queues."""
    bench = str(halt["bench"])
    peak = halt.get("peak")
    latest = halt.get("latest")
    peak_sha = halt.get("peak_sha")
    latest_sha = halt.get("latest_sha")
    declines = halt.get("declines")
    spec = _spec_for_bench(bench)
    short_peak = str(peak_sha)[:12]
    short_latest = str(latest_sha)[:12]
    return Candidate.build(
        domain=bench_domain(bench),
        source=f"monitor:{bench}",
        kind="regression",
        title=f"{bench} canary fell below its best-ever score",
        evidence_key=f"canary:{bench}:{short_peak}",
        files=spec.files if spec is not None else (),
        detail=f"peak {peak} at {short_peak}, now {latest} at {short_latest}",
        fix_hint=(
            f"main's {bench} score fell from {peak} (at {short_peak}) to "
            f"{latest} (at {short_latest}) over {declines} consecutive canaries. "
            f"Find the change since {short_peak} that caused it and restore the "
            "score. Fix forward; do not revert main."),
        success_metric=f"{bench}: sealed paired gain (G12)",
        success_command=f"bench:{bench}",
        evidence=dict(halt),
        now=now,
    )


# --------------------------------------------------------------------------
# The research refresh
# --------------------------------------------------------------------------


def review_age_days(home: str | Path, spec: research.DomainSpec, *, now: int) -> int | None:
    """Whole days since the domain's newest review, or ``None`` when none exists."""
    path = research.latest_review(Path(home), spec.review_glob)
    if path is None:
        return None
    return max(0, (int(now) - int(path.stat().st_mtime)) // 86400)


def _refresh_record_path(home: Path, domain: str) -> Path:
    return home / "rsi" / f"research-refresh-{domain}.json"


def _launched_within_a_day(home: Path, domain: str, now: int) -> bool:
    path = _refresh_record_path(home, domain)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        last = int(record["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return now - last < _REFRESH_COOLDOWN_S


def _launch_review(root: Path, home: Path, spec: research.DomainSpec, now: int) -> dict[str, Any]:
    """Spawn one detached review recipe and record the launch."""
    stamp = datetime.fromtimestamp(int(now)).strftime("%Y%m%d-%H%M%S")
    run_id = f"{_RUN_ID_PREFIX}{stamp}"
    rsi_dir = home / "rsi"
    rsi_dir.mkdir(parents=True, exist_ok=True)
    log = rsi_dir / f"research-refresh-{spec.domain}-{stamp}.log"
    launch = root / "recipes" / spec.recipe / "launch.sh"
    handle = open(log, "ab")
    try:
        proc = subprocess.Popen(
            ["bash", str(launch), run_id],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "MINI_ORK_HOME": str(home)},
        )
    finally:
        handle.close()
    _refresh_record_path(home, spec.domain).write_text(
        json.dumps({"at": int(now), "run_id": run_id, "log": str(log),
                    "pid": proc.pid}),
        encoding="utf-8")
    return {"run_id": run_id, "log": str(log), "pid": proc.pid}


def _result(domain: str, action: str, reason: str, age_days: int | None,
            log: str | None) -> dict[str, Any]:
    return {"domain": domain, "action": action, "reason": reason,
            "age_days": age_days, "log": log}


def maybe_refresh(
    root: str | Path,
    home: str | Path,
    cfg,
    *,
    now: int,
    force: bool = False,
    domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Launch stale domains' review recipes, at most one launch per day each.

    ``force`` ignores both the age gate and the once-a-day cooldown — the
    operator asked for it explicitly. A domain that has no ``DEFAULT_SPECS``
    entry, or whose entry names no recipe, is not touched.
    """
    root = Path(root)
    home = Path(home)
    chosen = set(domains) if domains is not None else None
    results: list[dict[str, Any]] = []
    for domain in cfg.research.domains:
        if chosen is not None and domain not in chosen:
            continue
        spec = research.DEFAULT_SPECS.get(domain)
        if spec is None or not spec.recipe:
            continue
        age = review_age_days(home, spec, now=now)
        if not force:
            if not cfg.research.auto_refresh:
                results.append(_result(domain, "skipped", "auto_refresh is off",
                                       age, None))
                continue
            if age is not None and age < cfg.research.refresh_days:
                results.append(_result(domain, "skipped", "review is current",
                                       age, None))
                continue
            if _launched_within_a_day(home, domain, now):
                results.append(_result(domain, "skipped",
                                       "launched within the last day", age, None))
                continue
        launched = _launch_review(root, home, spec, now)
        results.append(_result(domain, "launched", "launched", age,
                               launched["log"]))
    return results
