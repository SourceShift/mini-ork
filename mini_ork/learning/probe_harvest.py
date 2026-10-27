"""Harvest frozen code-fix probes from mini-ork's own mined failures.

The apply gate scores a candidate on a FROZEN probe set. That set is only as
strong as its size: a decision taken on two probes is theater. This module
grows the set from data already in the repo — the mined heldout tasks, their
baseline grades, and the failure diagnoses stored in ``state.db`` — while
keeping the held-out discipline intact:

* a task from the mined ``test`` split is never harvested;
* a salted 25% of the harvested candidates go into a holdout bucket and are
  written with ``holdout: true`` so the apply loop never loads them;
* every probe it writes is a git-oracle probe, which the apply loop ignores
  unless ``MO_APPLY_PROBE_GIT_ORACLE=1``.

``harvest()`` is a library call with no callers yet, and it writes nothing
unless asked (``write=True`` / ``--write``). Everything else is pure input
data plus temp dirs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HARVESTER = "probe_harvest@1"
SUPPORTED_RECIPES = ("code-fix",)
DEFAULT_MAX_NEW = 12
MAX_PER_CLUSTER = 6
HOLDOUT_FRACTION = 0.25
HOLDOUT_SALT = "probe-holdout:"
PROBE_STEM_PREFIX = "probe-h"
MAX_STEM_INDEX = 99
SOLVED = "solved"
UNMEASURED = "unmeasured-failure"
DEFAULT_MANIFEST = "evals/heldout/mined/manifest.json"
DEFAULT_BASELINE = "evals/heldout/results/2026-09-29-baseline-dev.json"

PYTEST_FILE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")

_STEM_RE = re.compile(r"^probe-h(\d{2})$")
_RUN_ID_RE = re.compile(r"^heldout-(mo-[0-9a-f]{10})-")

_FROZEN_PARAGRAPH = (
    "FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from "
    "mini-ork's failure history. Never edit it to fit a candidate. It is "
    "graded by hidden tests, not by this text.\n\n"
)


def is_holdout(fix_sha: str) -> bool:
    """Salted bucket test for the 25% holdout split.

    The salt matters: without it the bucket is the same hash as the mined
    dev/test split, so it selects no dev task at all.
    """
    digest = hashlib.sha256((HOLDOUT_SALT + fix_sha).encode()).hexdigest()
    bucket = int(digest[:8], 16) / 0x100000000
    return bucket < HOLDOUT_FRACTION


def cluster_key(diagnosis: dict | None) -> str:
    """``layer/rule/node`` for a diagnosis, or ``unmeasured-failure``."""
    if diagnosis is None:
        return UNMEASURED
    return "/".join(
        str(diagnosis.get(key) or "-") for key in ("layer", "rule_id", "node")
    )


def mined_candidates(
    manifest: list[dict],
    baseline: dict[str, dict],
    diagnoses: dict[str, dict],
    *,
    baseline_source: str = DEFAULT_BASELINE,
) -> tuple[list[dict], list[dict]]:
    """Split mined manifest tasks into candidates and rejections.

    The first rule that fires gives the reason, so the order is load-bearing.
    """
    kept: list[dict] = []
    rejected: list[dict] = []
    for task in manifest:
        task_id = task["id"]
        test_files = task.get("test_files") or []
        if task.get("split") != "dev":
            rejected.append({"task_id": task_id, "reason": "heldout-test-split"})
            continue
        if task.get("weak_signal"):
            rejected.append({"task_id": task_id, "reason": "weak-signal"})
            continue
        if not test_files or any(
            not PYTEST_FILE.search(path) for path in test_files
        ):
            rejected.append({"task_id": task_id, "reason": "non-pytest-oracle"})
            continue
        if not (task.get("fail_to_pass") or []):
            rejected.append({"task_id": task_id, "reason": "no-fail-to-pass"})
            continue
        entry = baseline.get(task_id)
        passed = entry.get("passed") if isinstance(entry, dict) else None
        if entry is None or not isinstance(passed, bool):
            rejected.append(
                {"task_id": task_id, "reason": "no-baseline-measurement"}
            )
            continue
        diagnosis = diagnoses.get(task_id)
        kept.append(
            {
                "task_id": task_id,
                "source": "heldout-mined",
                "base_sha": task.get("base_sha"),
                "fix_sha": task.get("fix_sha"),
                "test_files": list(test_files),
                "fail_to_pass": list(task.get("fail_to_pass") or []),
                "pass_to_pass": list(task.get("pass_to_pass") or []),
                "src_files": list(task.get("src_files") or []),
                "problem_statement": task.get("problem_statement"),
                "cluster": SOLVED if passed else cluster_key(diagnosis),
                "baseline_outcome": 1.0 if passed else 0.0,
                "baseline_source": baseline_source,
                "holdout": is_holdout(task["fix_sha"]),
                "run_ids": list((diagnosis or {}).get("run_ids", [])),
            }
        )
    return kept, rejected


def select(
    candidates: list[dict],
    *,
    existing_fix_shas: set[str],
    max_new: int = DEFAULT_MAX_NEW,
    max_per_cluster: int = MAX_PER_CLUSTER,
) -> tuple[list[dict], list[dict]]:
    """Admit a balanced, deduplicated subset of the harvested candidates.

    Holdout candidates are admitted uncapped; gate candidates are taken
    round-robin across clusters so one noisy cluster cannot dominate.
    """
    rejected: list[dict] = []
    seen = set(existing_fix_shas)
    survivors: list[dict] = []
    for candidate in candidates:
        fix_sha = candidate.get("fix_sha")
        if fix_sha in seen:
            rejected.append(
                {"task_id": candidate["task_id"], "reason": "duplicate"}
            )
            continue
        seen.add(fix_sha)
        survivors.append(candidate)

    holdout = [c for c in survivors if c["holdout"]]
    gate = [c for c in survivors if not c["holdout"]]

    clusters: dict[str, list[dict]] = {}
    for candidate in gate:
        clusters.setdefault(candidate["cluster"], []).append(candidate)
    order = sorted(key for key in clusters if key != SOLVED)
    if SOLVED in clusters:
        order.append(SOLVED)
    for key in order:
        clusters[key].sort(key=lambda c: c["task_id"])

    taken: dict[str, int] = {key: 0 for key in order}
    cursor: dict[str, int] = {key: 0 for key in order}
    admitted_gate: list[dict] = []
    while len(admitted_gate) < max_new:
        progressed = False
        for key in order:
            if len(admitted_gate) >= max_new:
                break
            if taken[key] >= max_per_cluster:
                continue
            index = cursor[key]
            if index >= len(clusters[key]):
                continue
            admitted_gate.append(clusters[key][index])
            cursor[key] = index + 1
            taken[key] += 1
            progressed = True
        if not progressed:
            break

    admitted_ids = {id(c) for c in admitted_gate}
    for candidate in gate:
        if id(candidate) in admitted_ids:
            continue
        reason = (
            "cluster-cap"
            if taken[candidate["cluster"]] >= max_per_cluster
            else "max-new"
        )
        rejected.append({"task_id": candidate["task_id"], "reason": reason})

    return admitted_gate + holdout, rejected


def existing_fix_shas(probes_dir: str) -> set[str]:
    """The ``fix_sha`` values in ``<probes_dir>/oracles/*.json``."""
    oracle_dir = os.path.join(probes_dir, "oracles")
    if not os.path.isdir(oracle_dir):
        return set()
    shas: set[str] = set()
    for name in sorted(os.listdir(oracle_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(oracle_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("fix_sha"), str):
            shas.add(data["fix_sha"])
    return shas


def next_stems(probes_dir: str, count: int) -> list[str]:
    """The next ``count`` free ``probe-hNN`` stems, one past the highest."""
    highest = 0
    if os.path.isdir(probes_dir):
        for name in os.listdir(probes_dir):
            if not name.endswith(".md"):
                continue
            match = _STEM_RE.match(name[: -len(".md")])
            if match:
                highest = max(highest, int(match.group(1)))
    start = highest + 1
    if start + count - 1 > MAX_STEM_INDEX:
        raise ValueError(
            f"cannot allocate {count} probe stems past probe-h{MAX_STEM_INDEX:02d}"
        )
    return [f"{PROBE_STEM_PREFIX}{index:02d}" for index in range(start, start + count)]


def _scrub(text: str, candidate: dict) -> str:
    needles = [
        candidate["task_id"],
        candidate["fix_sha"],
        candidate["base_sha"],
        candidate["fix_sha"][:10],
        candidate["base_sha"][:10],
        *candidate.get("test_files", []),
    ]
    for needle in sorted({n for n in needles if n}, key=len, reverse=True):
        text = text.replace(needle, "[hidden]")
    return text


def render_probe(
    candidate: dict, *, recipe: str, stem: str, harvested_at: str
) -> tuple[str, dict]:
    """Render a candidate as ``(markdown, oracle)``."""
    frontmatter = "---\noracle: git\n"
    if candidate["holdout"]:
        frontmatter += "holdout: true\n"
    frontmatter += "---\n"

    problem = _scrub(candidate["problem_statement"], candidate)
    files = "".join(f"- {path}\n" for path in candidate["src_files"])

    markdown = (
        frontmatter
        + f"# Frozen probe {stem} — {recipe} harvested task\n\n"
        + _FROZEN_PARAGRAPH
        + f"## Problem\n\n{problem}\n\n"
        + f"## Files in scope\n\n{files}\n"
        + "## Definition of Done\n\n"
        + "Fix the behaviour described above so the repository state at HEAD "
        + "satisfies the spec.\n"
    )

    oracle = {
        "schema": 1,
        "base_sha": candidate["base_sha"],
        "fix_sha": candidate["fix_sha"],
        "test_files": list(candidate["test_files"]),
        "fail_to_pass": list(candidate["fail_to_pass"]),
        "pass_to_pass": list(candidate["pass_to_pass"]),
        "provenance": {
            "source": candidate["source"],
            "task_id": candidate["task_id"],
            "run_ids": list(candidate["run_ids"]),
            "cluster": candidate["cluster"],
            "baseline_outcome": candidate["baseline_outcome"],
            "baseline_source": candidate["baseline_source"],
            "recipe": recipe,
            "harvested_at": harvested_at,
            "harvester": HARVESTER,
        },
    }
    return markdown, oracle


def write_probes(
    admitted: list[dict],
    probes_dir: str,
    *,
    recipe: str,
    harvested_at: str,
) -> list[str]:
    """Write the admitted probes, refusing to overwrite any target."""
    stems = next_stems(probes_dir, len(admitted))
    oracle_dir = os.path.join(probes_dir, "oracles")
    targets = []
    for stem in stems:
        targets.append(os.path.join(probes_dir, f"{stem}.md"))
        targets.append(os.path.join(oracle_dir, f"{stem}.json"))
    for path in targets:
        if os.path.exists(path):
            raise FileExistsError(f"refusing to overwrite existing probe {path}")

    os.makedirs(probes_dir, exist_ok=True)
    os.makedirs(oracle_dir, exist_ok=True)
    for stem, candidate in zip(stems, admitted):
        markdown, oracle = render_probe(
            candidate, recipe=recipe, stem=stem, harvested_at=harvested_at
        )
        with open(os.path.join(probes_dir, f"{stem}.md"), "w", encoding="utf-8") as fh:
            fh.write(markdown)
        with open(os.path.join(oracle_dir, f"{stem}.json"), "w", encoding="utf-8") as fh:
            json.dump(oracle, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return stems


def load_code_fix_inputs(
    root: str, home: str
) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    """Read the mined manifest, the baseline grades, and the failure diagnoses.

    ``state.db`` is opened read-only and ``diagnose`` is always called with
    ``write=False`` — harvesting must never mutate a run's diagnosis.
    """
    with open(os.path.join(root, DEFAULT_MANIFEST), encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(os.path.join(root, DEFAULT_BASELINE), encoding="utf-8") as fh:
        baseline = json.load(fh)

    diagnoses: dict[str, dict] = {}
    state_db = os.path.join(home, "state.db")
    if not os.path.exists(state_db):
        return list(manifest), baseline, diagnoses

    from mini_ork.recovery import diagnose as _dg

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id FROM task_runs WHERE id LIKE 'heldout-mo-%'"
            " AND status = 'failed' ORDER BY created_at, id"
        ).fetchall()
    finally:
        conn.close()

    run_ids_by_task: dict[str, list[str]] = {}
    for (run_id,) in rows:
        match = _RUN_ID_RE.match(run_id)
        if match:
            run_ids_by_task.setdefault(match.group(1), []).append(run_id)

    for task_id, run_ids in run_ids_by_task.items():
        newest_run_id = run_ids[-1]
        result = _dg.diagnose(Path(home), newest_run_id, write=False)
        diagnoses[task_id] = {
            **result,
            "run_id": newest_run_id,
            "run_ids": list(run_ids),
        }
    return list(manifest), baseline, diagnoses


def _count_md(probes_dir: str) -> int:
    if not os.path.isdir(probes_dir):
        return 0
    return sum(1 for name in os.listdir(probes_dir) if name.endswith(".md"))


def _call_validate(validate_fn, candidate: dict) -> tuple[bool, str, dict]:
    """Normalise a ``validate_fn`` result to ``(ok, reason, updated)``.

    The documented signature is ``(candidate) -> (ok, reason, updated)``. A
    two-item ``(ok, updated)`` is also accepted — the rejection reason is then
    empty — so a validator that has nothing to say on the happy path need not
    invent a reason string.
    """
    result = validate_fn(candidate)
    if len(result) == 3:
        ok, reason, updated = result
    elif len(result) == 2:
        ok, updated = result
        reason = ""
    else:  # pragma: no cover - defensive: a validator contract violation
        raise ValueError(
            "validate_fn must return (ok, reason, updated) or (ok, updated)"
        )
    return bool(ok), reason, updated


def harvest(
    recipe: str,
    *,
    root: str,
    home: str,
    max_new: int = DEFAULT_MAX_NEW,
    write: bool = False,
    now: str | None = None,
    validate_fn=None,
) -> dict:
    """Harvest candidates for ``recipe`` and (optionally) write them.

    With ``write=False`` (the default) this is a pure dry run.
    """
    if recipe not in SUPPORTED_RECIPES:
        raise ValueError(f"unsupported recipe {recipe!r}")

    probes_dir = os.path.join(root, "recipes", recipe, "probes")
    manifest, baseline, diagnoses = load_code_fix_inputs(root, home)
    candidates, mined_rejected = mined_candidates(manifest, baseline, diagnoses)
    n_candidates = len(candidates)

    existing = existing_fix_shas(probes_dir)

    # ``rejected`` records what THIS run decided about the candidate pool.
    # Mining rejections (test split, weak signal, …) are the manifest's
    # permanent ineligibility, so they are only surfaced for a plain dry-run
    # audit: once a validation pass or an existing probe set is in play, the
    # caller is asking about this run's verdicts, and repeating tasks that can
    # never change would bury them.
    rejected: list[dict] = list(mined_rejected)

    if validate_fn is not None:
        rejected = []
        survivors = []
        for candidate in candidates:
            ok, reason, updated = _call_validate(validate_fn, candidate)
            if ok:
                survivors.append(updated)
            else:
                rejected.append(
                    {
                        "task_id": candidate["task_id"],
                        "reason": f"validate:{reason}",
                    }
                )
        candidates = survivors
    elif existing and candidates and all(
        candidate.get("fix_sha") in existing for candidate in candidates
    ):
        # A top-up where every mined candidate is already represented by a
        # written sidecar: duplicate-ness is the only decision left.
        rejected = []

    admitted, select_rejected = select(
        candidates,
        existing_fix_shas=existing,
        max_new=max_new,
    )
    rejected.extend(select_rejected)

    n_existing_probes = _count_md(probes_dir)
    stems: list[str | None] = [None] * len(admitted)
    written = False
    if write:
        harvested_at = now or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        stems = list(
            write_probes(
                admitted, probes_dir, recipe=recipe, harvested_at=harvested_at
            )
        )
        written = True

    admitted_report = [
        {
            "task_id": candidate["task_id"],
            "cluster": candidate["cluster"],
            "holdout": candidate["holdout"],
            "baseline_outcome": candidate["baseline_outcome"],
            "stem": stem,
        }
        for candidate, stem in zip(admitted, stems)
    ]
    return {
        "recipe": recipe,
        "harvester": HARVESTER,
        "n_existing_probes": n_existing_probes,
        "n_candidates": n_candidates,
        "admitted": admitted_report,
        "rejected": rejected,
        "n_admitted_gate": sum(1 for c in admitted if not c["holdout"]),
        "n_admitted_holdout": sum(1 for c in admitted if c["holdout"]),
        "diagnoses_available": bool(diagnoses),
        "written": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mini_ork.learning.probe_harvest",
        description="Harvest frozen probes from mini-ork's mined failures.",
    )
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--max-new", type=int, default=DEFAULT_MAX_NEW)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--root", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    root = args.root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = (
        args.home
        or os.environ.get("MINI_ORK_HOME")
        or os.path.join(root, ".mini-ork")
    )

    report = harvest(
        args.recipe,
        root=root,
        home=home,
        max_new=args.max_new,
        write=args.write,
    )
    payload = json.dumps(report)
    print(payload)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
