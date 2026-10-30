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
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HARVESTER = "probe_harvest@1"


SUPPORTED_RECIPES = ("code-fix", "framework-edit")
MAX_SRC_FILES = 12
DEFAULT_LANDING_REF = "origin/main"
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
KICKOFF_TEST_RE = re.compile(r"(tests/(?:[\w-]+/)*test_\w+\.py)")
# Files a run dir keeps that ARE (or contain) the change the run made. A probe
# whose problem statement names such a run hands the arm its answer.
ANSWER_ARTIFACTS = (
    "framework-edit.diff", "framework-edit.diff.agent", "review-diff.patch",
    "review-delta.patch", "salvage.patch",
)
# Run-id-shaped tokens: a slug with a run of at least 6 digits in it
# (`<slug>-YYYYMMDDHHMMSS`, `<slug>-YYYYMMDD-HHMMSS`, `run-<epoch>-<pid>`).
_RUN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\d{6,}[A-Za-z0-9_.-]*")
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


def _test_needles(candidate: dict) -> list[str]:
    """Strings that would name the hidden tests in visible probe text.

    Each test id (``<module>::<name>``) contributes itself, its function name and
    its dotted module; a parametrized or class-qualified id (``m::Cls::t[1]``)
    also contributes its bare function name, its class and its module, which
    the whole-id needles alone would leave readable. Each hidden test file
    contributes its path, basename and stem. Over-redaction of a short name is acceptable: the probe text must not
    point the solver at the oracle.
    """
    out: list[str] = []
    for test_id in [*candidate.get("fail_to_pass", []), *candidate.get("pass_to_pass", [])]:
        out.append(test_id)
        module, _, name = test_id.rpartition("::")
        out.extend([module, name])
        out.append(name.split("[", 1)[0])
        out.extend(module.split("::"))
    for path in candidate.get("test_files", []):
        base = path.rsplit("/", 1)[-1]
        out.extend([path, base, base[:-3] if base.endswith(".py") else base])
    return out


def _scrub(text: str, candidate: dict, *, hide_tests: bool = True) -> str:
    needles = [
        candidate["task_id"],
        candidate["fix_sha"],
        candidate["base_sha"],
        candidate["fix_sha"][:10],
        candidate["base_sha"][:10],
    ]
    if hide_tests:
        needles.extend(_test_needles(candidate))
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

    if candidate["source"] == "failed-run":
        # A failed-run candidate IS already a kickoff. Its text is kept
        # verbatim (test paths included — the grader overlays those same
        # paths), so no Problem/Files/DoD scaffolding is synthesized.
        body = _scrub(
            candidate["problem_statement"], candidate, hide_tests=False
        )
        markdown = (
            frontmatter
            + f"# Frozen probe {stem} — {recipe} harvested task\n\n"
            + _FROZEN_PARAGRAPH
            + body
        )
    else:
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


def _git_ok(root: str, *args: str) -> bool:
    """True when ``git -C root <args>`` exits zero (output discarded)."""
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True
    ).returncode == 0


def _git_out(root: str, *args: str) -> str | None:
    """``git -C root <args>`` stdout as text, or ``None`` on a non-zero exit."""
    proc = subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def _git_blob(root: str, spec: str) -> bytes | None:
    """The bytes of ``<spec>`` (``<rev>:<path>``), or ``None`` if it is absent."""
    proc = subprocess.run(
        ["git", "-C", root, "show", spec], capture_output=True
    )
    return proc.stdout if proc.returncode == 0 else None


def validate_candidate(
    candidate: dict, *, root: str, timeout: float = 600.0
) -> tuple[bool, str, dict]:
    """Re-check a candidate as fail-on-base, pass-on-fix in the local repo.

    A probe mined months ago under another interpreter is not evidence today:
    every candidate is re-graded here, and only a real fail-to-pass flip is
    accepted. Any tree this creates is removed in a ``finally``.
    """
    from mini_ork.learning import probe_scorer

    # The revisions reach ``git`` as arguments: anything but a hex SHA (a
    # ``-``-prefixed string would parse as an option) is refused up front.
    for key in ("base_sha", "fix_sha"):
        if not _SHA_RE.fullmatch(str(candidate.get(key) or "")):
            return False, "unsafe-revision", candidate

    trees: list[str] = []
    try:
        try:
            base = probe_scorer.materialize_git_tree(
                candidate["base_sha"], root=root
            )
        except RuntimeError:
            return False, "archive-failed", candidate
        trees.append(base)

        for path in candidate["test_files"]:
            # A test path is written into the scratch tree: it must stay there.
            parts = Path(path).parts
            if os.path.isabs(path) or not parts or ".." in parts:
                return False, "unsafe-test-path", candidate
            blob = _git_blob(root, f"{candidate['fix_sha']}:{path}")
            if blob is None:
                return False, "fix-missing-test", candidate
            target = os.path.join(base, path)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(blob)

        base_report = probe_scorer.run_pytest_junit(
            base, candidate["test_files"], timeout=timeout
        )
        if base_report is None:
            return False, "base-no-report", candidate

        try:
            fix = probe_scorer.materialize_git_tree(
                candidate["fix_sha"], root=root
            )
        except RuntimeError:
            return False, "archive-failed", candidate
        trees.append(fix)

        fix_report = probe_scorer.run_pytest_junit(
            fix, candidate["test_files"], timeout=timeout
        )
        if fix_report is None:
            return False, "fix-no-report", candidate

        broken = sorted(
            test_id
            for test_id, outcome in fix_report.items()
            if outcome in ("failed", "error")
        )
        if broken:
            return False, f"fix-not-green:{len(broken)}", candidate

        fail_to_pass = sorted(
            test_id
            for test_id, outcome in fix_report.items()
            if outcome == "passed" and base_report.get(test_id) != "passed"
        )
        if not fail_to_pass:
            return False, "no-fail-to-pass", candidate

        pass_to_pass = sorted(
            test_id
            for test_id, outcome in fix_report.items()
            if outcome == "passed" and base_report.get(test_id) == "passed"
        )
        return True, "", {
            **candidate,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
        }
    finally:
        for tree in trees:
            shutil.rmtree(tree, ignore_errors=True)


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML ``---`` block; text without one is untouched."""
    match = re.match(r"---\n.*?\n---\n", text, re.S)
    return text[match.end():] if match else text


def answer_pointers(text: str, home: str) -> list[str]:
    """Run ids named in ``text`` whose ``<home>/runs/<id>/`` holds an answer artifact.

    Sorted and de-duplicated. A token that is not an existing run dir is ignored;
    a run dir with none of :data:`ANSWER_ARTIFACTS` is not a pointer.
    """
    found: set[str] = set()
    for raw in _RUN_TOKEN.findall(text or ""):
        token = raw.rstrip(".-_")
        if token in found:
            continue
        run_dir = os.path.join(home, "runs", token)
        if os.path.isdir(run_dir) and any(
            os.path.exists(os.path.join(run_dir, name))
            for name in ANSWER_ARTIFACTS
        ):
            found.add(token)
    return sorted(found)


def failed_run_candidates(
    home: str, root: str, *, landing_ref: str = DEFAULT_LANDING_REF
) -> tuple[list[dict], list[dict]]:
    """Candidates from failed framework-edit runs that landed a named test.

    A run's pre-impl fixture names the base it started from; the kickoff names
    the test file its spec-test author was to write. When that file landed on
    ``landing_ref``, the landing commit's first parent is the base and the
    landing commit itself is the fix — a ready-made git oracle. Runs sharing a
    landing commit merge into one candidate. The first rule that fires gives the
    rejection reason, so the order is load-bearing (``no-pre-impl-fixture``,
    ``baseline-unreachable``, ``layer:<name>``, ``kickoff-unrecoverable``,
    ``no-test-named``, ``test-not-landed``, ``no-parent``,
    ``landing-too-broad``, then ``answer-pointer``).
    """
    if landing_ref.startswith("-"):
        raise ValueError(f"landing_ref must be a ref, not an option: {landing_ref!r}")
    state_db = os.path.join(home, "state.db")
    if not os.path.exists(state_db):
        return [], []

    # Local import so a monkeypatched ``diagnose`` is seen at call time.
    from mini_ork.recovery import diagnose as _dg

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, kickoff_path, created_at FROM task_runs"
            " WHERE recipe = 'framework-edit' AND status = 'failed'"
            " ORDER BY created_at, id"
        ).fetchall()
    finally:
        conn.close()

    candidates: list[dict] = []
    by_fix_sha: dict[str, dict] = {}
    rejections: list[dict] = []

    for run_id, kickoff_path, _created_at in rows:
        def reject(reason: str) -> None:
            rejections.append({"task_id": run_id, "reason": reason})

        manifest_path = os.path.join(
            home, "runs", run_id, "pre-impl-fixture", "MANIFEST.json"
        )
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            reject("no-pre-impl-fixture")
            continue
        if not isinstance(manifest, dict):
            reject("no-pre-impl-fixture")
            continue

        baseline_ref = manifest.get("baseline_ref")
        if (
            not isinstance(baseline_ref, str)
            or not baseline_ref
            or baseline_ref.startswith("-")
            or not _git_ok(root, "cat-file", "-e", f"{baseline_ref}^{{commit}}")
        ):
            reject("baseline-unreachable")
            continue

        diagnosis = _dg.diagnose(Path(home), run_id, write=False)
        if diagnosis.get("layer") != "change":
            reject(f"layer:{diagnosis.get('layer')}")
            continue

        kickoff_group = None
        match = re.search(
            r"(?:^|/)(kickoffs/.*\.md)$", kickoff_path or ""
        )
        if match:
            kickoff_group = match.group(1)

        text = None
        if kickoff_path and os.path.isfile(kickoff_path):
            try:
                with open(kickoff_path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = None
        elif kickoff_group:
            blob = _git_blob(root, f"{baseline_ref}:{kickoff_group}")
            if blob is not None:
                text = blob.decode("utf-8", "replace")
        if text is None:
            reject("kickoff-unrecoverable")
            continue

        named_tests = sorted(set(KICKOFF_TEST_RE.findall(text)))
        if not named_tests:
            reject("no-test-named")
            continue

        landed: list[tuple[str, str]] = []
        for test_path in named_tests:
            if _git_ok(root, "cat-file", "-e", f"{baseline_ref}:{test_path}"):
                continue
            if not _git_ok(root, "cat-file", "-e", f"{landing_ref}:{test_path}"):
                continue
            log = _git_out(
                root, "log", "--diff-filter=A", "--format=%H",
                landing_ref, "--", test_path,
            )
            adds = [line for line in (log or "").splitlines() if line.strip()]
            if adds:
                # ``git log`` is newest-first; the landing commit is the
                # OLDEST add, i.e. the last line.
                landed.append((test_path, adds[-1]))
        if not landed:
            reject("test-not-landed")
            continue

        fix_sha = landed[0][1]
        test_files = sorted(
            test_path for test_path, add in landed if add == fix_sha
        )

        base_text = _git_out(root, "rev-parse", f"{fix_sha}^1")
        if base_text is None:
            reject("no-parent")
            continue
        base_sha = base_text.strip()

        diff = _git_out(root, "diff", "--name-only", base_sha, fix_sha) or ""
        src_files = sorted(
            path for path in diff.splitlines()
            if path and not re.search(r"(^|/)tests/", path)
        )
        if len(src_files) > MAX_SRC_FILES:
            reject("landing-too-broad")
            continue

        # The kickoff that LANDED wins: the run's on-disk copy may predate the
        # edit that shipped with the fix.
        if kickoff_group:
            blob = _git_blob(root, f"{fix_sha}:{kickoff_group}")
            if blob is not None:
                text = blob.decode("utf-8", "replace")
        problem_statement = _strip_frontmatter(text)

        pointers = answer_pointers(problem_statement, home)
        if pointers:
            reject("answer-pointer")
            continue

        candidate = {
            "task_id": f"fe-{fix_sha[:10]}",
            "source": "failed-run",
            "base_sha": base_sha,
            "fix_sha": fix_sha,
            "test_files": test_files,
            "src_files": src_files,
            "problem_statement": problem_statement,
            "cluster": cluster_key(diagnosis),
            "baseline_outcome": 0.0,
            "baseline_source": "task_runs.status=failed",
            "holdout": is_holdout(fix_sha),
            "run_ids": [run_id],
            "fail_to_pass": [],
            "pass_to_pass": [],
        }

        if fix_sha in by_fix_sha:
            merged = by_fix_sha[fix_sha]
            merged["run_ids"] = sorted(set(merged["run_ids"]) | {run_id})
            merged["cluster"] = candidate["cluster"]
            merged["problem_statement"] = candidate["problem_statement"]
        else:
            by_fix_sha[fix_sha] = candidate
            candidates.append(candidate)

    return candidates, rejections


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
    landing_ref: str = DEFAULT_LANDING_REF,
) -> dict:
    """Harvest candidates for ``recipe`` and (optionally) write them.

    With ``write=False`` (the default) this is a pure dry run.
    """
    if recipe not in SUPPORTED_RECIPES:
        raise ValueError(f"unsupported recipe {recipe!r}")

    probes_dir = os.path.join(root, "recipes", recipe, "probes")
    if recipe == "framework-edit":
        candidates, mined_rejected = failed_run_candidates(
            home, root, landing_ref=landing_ref
        )
        diagnoses_available = os.path.exists(os.path.join(home, "state.db"))
    else:
        manifest, baseline, diagnoses = load_code_fix_inputs(root, home)
        candidates, mined_rejected = mined_candidates(
            manifest, baseline, diagnoses
        )
        diagnoses_available = bool(diagnoses)
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

    # A probe is graded by its fail_to_pass list. A candidate without one (a
    # framework-edit candidate that was never validated) has no oracle and
    # would grade any solver as solved, so it is never admitted.
    no_oracle = [c for c in candidates if not c.get("fail_to_pass")]
    if no_oracle:
        rejected.extend(
            {"task_id": c["task_id"], "reason": "no-oracle"} for c in no_oracle
        )
        candidates = [c for c in candidates if c.get("fail_to_pass")]

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
        "diagnoses_available": diagnoses_available,
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
    parser.add_argument("--landing-ref", default=DEFAULT_LANDING_REF)
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument("--grade-timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    root = args.root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = (
        args.home
        or os.environ.get("MINI_ORK_HOME")
        or os.path.join(root, ".mini-ork")
    )

    validate_fn = None
    if not args.no_validate:
        validate_fn = lambda candidate: validate_candidate(  # noqa: E731
            candidate, root=root, timeout=args.grade_timeout
        )

    report = harvest(
        args.recipe,
        root=root,
        home=home,
        max_new=args.max_new,
        write=args.write,
        landing_ref=args.landing_ref,
        validate_fn=validate_fn,
    )
    payload = json.dumps(report)
    print(payload)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
