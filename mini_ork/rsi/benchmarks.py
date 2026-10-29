"""A benchmark registry and the built-in ``verifier-replay`` benchmark.

The research-driven RSI loop's impact gate (G12) scores a git tree on a fixed
labelled item set, per item, for $0.  A benchmark is three callables plus
metadata: a runner that scores one tree, an item list that says which ids exist
and their stratum, and the file surface the benchmark is sensitive to.

``scripts/verifier_replay.py`` already does the expensive part for the code-fix
test verifier.  The ``verifier-replay`` benchmark wraps it so that:

* the tree under test (base or candidate) supplies the verifier —
  ``import_root`` points the verifier's own ``import mini_ork…`` at that tree,
  not the installed package;
* the hidden-test ground truth is cached once per patch content, because it
  does not depend on the verifier;
* a clean tree's verifier results are cached by tree content;
* items run in parallel.

Standard library only.  The replay harness is imported lazily, so importing this
module (and ``python -m mini_ork.rsi.benchmarks list``) never touches
``scripts/`` unless a benchmark actually runs.  No DB, network or model access.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BenchContext:
    """Everything a runner needs that is not the tree being scored."""

    home: Path
    repo: Path
    workers: int = 4
    timeout_s: int = 900
    python: str = sys.executable
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchResult:
    """One tree's scores, plus the strata and guards that qualify them."""

    benchmark: str
    tree: str | None
    scores: Mapping[str, float]
    strata: Mapping[str, str]
    guards: Mapping[str, float]
    skipped: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-able dict (``skipped`` becomes a list)."""
        return {
            "benchmark": self.benchmark,
            "tree": self.tree,
            "scores": dict(self.scores),
            "strata": dict(self.strata),
            "guards": dict(self.guards),
            "skipped": list(self.skipped),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BenchResult":
        return cls(
            benchmark=d["benchmark"],
            tree=d.get("tree"),
            scores=dict(d.get("scores", {})),
            strata=dict(d.get("strata", {})),
            guards=dict(d.get("guards", {})),
            skipped=tuple(d.get("skipped", ())),
            meta=dict(d.get("meta", {})),
        )

    def subset(self, items) -> "BenchResult":
        """Same result restricted to ``items`` (scores and strata); guards and
        meta are copied as-is."""
        keep = set(items)
        return BenchResult(
            benchmark=self.benchmark,
            tree=self.tree,
            scores={k: v for k, v in self.scores.items() if k in keep},
            strata={k: v for k, v in self.strata.items() if k in keep},
            guards=dict(self.guards),
            skipped=tuple(i for i in self.skipped if i in keep),
            meta=dict(self.meta),
        )


@dataclass(frozen=True, slots=True)
class Benchmark:
    """A named scoring procedure over one git tree."""

    name: str
    runner: Callable[[Path, tuple[str, ...] | None, BenchContext], BenchResult]
    items: Callable[[BenchContext], Mapping[str, str]]
    surface: tuple[str, ...] = ()
    lower_is_better: tuple[str, ...] = ()


# ── registry ────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Benchmark] = {}


def register_benchmark(bench: Benchmark, *, replace: bool = False) -> None:
    """Register ``bench``; a duplicate name without ``replace=True`` raises."""
    if bench.name in _REGISTRY and not replace:
        raise ValueError(f"benchmark {bench.name!r} is already registered")
    _REGISTRY[bench.name] = bench


def get_benchmark(name: str) -> Benchmark:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown benchmark {name!r}") from None


def benchmark_names() -> tuple[str, ...]:
    """Registered names, sorted."""
    return tuple(sorted(_REGISTRY))


# ── tree identity and cache ─────────────────────────────────────────────────


def _git_bytes(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=False, check=False)


def tree_key(root: Path) -> str | None:
    """A content key for ``root``'s tree, or ``None`` when it is not a git tree.

    Covers the HEAD sha, the uncommitted diff against HEAD, and the content of
    every untracked (non-ignored) file, so a worktree edit changes the key."""
    root = Path(root)
    head = _git_bytes(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None
    digest = hashlib.sha256()
    digest.update(head.stdout.strip())
    digest.update(b"\n")
    digest.update(_git_bytes(root, "diff", "HEAD", "--binary").stdout)
    digest.update(b"\n")
    listing = _git_bytes(root, "ls-files", "--others", "--exclude-standard")
    for path in sorted(listing.stdout.decode("utf-8", "replace").splitlines()):
        if not path:
            continue
        try:
            body = (root / path).read_bytes()
            digest.update(f"{path}:{hashlib.sha256(body).hexdigest()}\n".encode())
        except OSError:
            digest.update(f"{path}:unreadable\n".encode())
    return digest.hexdigest()


def _cache_path(ctx: BenchContext, name: str, key: str) -> Path:
    return ctx.home / "rsi" / "bench" / name / f"{key}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically: a unique tmp file in the same dir + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cache_covers(cached: Mapping[str, Any], wanted: tuple[str, ...] | None) -> bool:
    if wanted is None:
        return bool((cached.get("meta") or {}).get("complete"))
    covered = set((cached.get("scores") or {})) | set(cached.get("skipped") or ())
    return all(i in covered for i in wanted)


def _merge_cache(cached: Mapping[str, Any] | None, result: BenchResult,
                 wanted: tuple[str, ...] | None) -> dict[str, Any]:
    base = cached if isinstance(cached, Mapping) else {}
    scores = dict(base.get("scores") or {})
    scores.update(result.scores)
    strata = dict(base.get("strata") or {})
    strata.update(result.strata)
    skipped = list(dict.fromkeys([*(base.get("skipped") or ()), *result.skipped]))
    meta = dict(result.meta)
    meta["complete"] = bool((base.get("meta") or {}).get("complete")) or wanted is None
    return {
        "benchmark": result.benchmark,
        "tree": result.tree,
        "scores": scores,
        "strata": strata,
        "guards": dict(result.guards),
        "skipped": skipped,
        "meta": meta,
    }


def run_benchmark(name: str, root, *, ctx: BenchContext,
                  items=None, use_cache: bool = True) -> BenchResult:
    """Score ``root`` with benchmark ``name``, consulting the result cache."""
    bench = get_benchmark(name)
    root = Path(root)
    wanted = tuple(sorted(set(items))) if items is not None else None
    key = tree_key(root)
    cache_path = _cache_path(ctx, name, key) if key is not None else None

    cached: Mapping[str, Any] | None = None
    corrupt = False
    if cache_path is not None and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("benchmark cache must be a JSON object")
            cached = data
        except (OSError, ValueError):
            corrupt = True

    if use_cache and not corrupt and cached is not None and _cache_covers(cached, wanted):
        result = BenchResult.from_dict(cached)
        return result.subset(wanted) if wanted is not None else result

    # A corrupt cache says nothing about what this tree has been scored on, so
    # rebuild it in full rather than pin a partial result beside unknown data.
    run_wanted = None if (use_cache and corrupt) else wanted
    result = bench.runner(root, run_wanted, ctx)

    if key is not None and cache_path is not None:
        _atomic_write_json(cache_path, _merge_cache(cached, result, run_wanted))

    if corrupt and wanted is not None:
        return result.subset(wanted)
    return result


# ── the built-in verifier-replay benchmark ──────────────────────────────────

VERIFIER_REPLAY = "verifier-replay"
DEFAULT_PATCHES = "evals/heldout/results/2026-09-29-baseline-dev.patches"
DEFAULT_MANIFEST = "evals/heldout/mined/manifest.json"
DEFAULT_SEED = "evals/heldout/results/2026-09-30-replay-testverifier.jsonl"
DEFAULT_VERIFIER = "recipes/code-fix/verifiers/test.py"
VR_SURFACE = ("recipes/code-fix/verifiers", "mini_ork/certify", "mini_ork/verify",
              "mini_ork/gates/suite_adequacy.py", "mini_ork/gates/suite_strengthen.py")
VR_GUARDS = ("false_accept", "false_reject", "no_verdict")

_REPLAY_MODULES: dict[str, Any] = {}


def _replay_module(repo: Path):
    """Import ``scripts/verifier_replay.py`` by file path, cached per repo."""
    path = (Path(repo) / "scripts" / "verifier_replay.py").resolve()
    key = str(path)
    mod = _REPLAY_MODULES.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("mini_ork_verifier_replay", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load verifier_replay from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _REPLAY_MODULES[key] = mod
    return mod


def _resolve(repo: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else repo / p


def _option_paths(ctx: BenchContext) -> tuple[Path, Path, Path]:
    opts = ctx.options
    return (
        _resolve(ctx.repo, opts.get("patches_dir", DEFAULT_PATCHES)),
        _resolve(ctx.repo, opts.get("manifest", DEFAULT_MANIFEST)),
        _resolve(ctx.repo, opts.get("seed_labels", DEFAULT_SEED)),
    )


def _tasks_and_patches(ctx: BenchContext) -> dict[str, tuple[Path, dict]]:
    """item id (the patch stem) -> (patch path, task)."""
    patches_dir, manifest_path, _seed = _option_paths(ctx)
    manifest = json.loads(manifest_path.read_text())
    tasks = {t["id"]: t for t in manifest}
    module = _replay_module(ctx.repo)
    return {Path(p).stem: (Path(p), task)
            for p, task in module.stored_patches(patches_dir, tasks)}


def labels_path(ctx: BenchContext) -> Path:
    return ctx.home / "rsi" / "bench" / VERIFIER_REPLAY / "labels.json"


def _patch_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_labels(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_seed_rows(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def ensure_labels(ctx: BenchContext, ids) -> dict[str, bool]:
    """Return ``{id: hidden_pass}``, caching ground truth on disk.

    Labels are keyed by patch content: an edited patch invalidates its label.
    Unseen ids are seeded from the stored replay run when possible, then graded
    in parallel.  A label whose patch sha no longer matches is re-graded, never
    re-seeded (the seed describes the old patch)."""
    ids = list(ids)
    pairs = _tasks_and_patches(ctx)
    _patches_dir, _manifest, seed_path = _option_paths(ctx)
    path = labels_path(ctx)
    labels = _load_labels(path)

    labelled: dict[str, bool] = {}
    need_grade: list[str] = []
    need_seed: list[str] = []
    for item_id in ids:
        if item_id not in pairs:
            continue
        entry = labels.get(item_id)
        if isinstance(entry, dict):
            if (entry.get("patch_sha256") == _patch_sha(pairs[item_id][0])
                    and isinstance(entry.get("hidden_pass"), bool)):
                labelled[item_id] = entry["hidden_pass"]
            else:
                need_grade.append(item_id)
        else:
            need_seed.append(item_id)

    changed = False
    if need_seed:
        seed_rows = _load_seed_rows(seed_path)
        for item_id in need_seed:
            patch, _task = pairs[item_id]
            sha = _patch_sha(patch)
            row = next((r for r in seed_rows
                        if r.get("run") == f"{item_id}.patch"
                        and isinstance(r.get("hidden_pass"), bool)), None)
            if row is None:
                need_grade.append(item_id)
                continue
            labels[item_id] = {"patch_sha256": sha,
                               "hidden_pass": bool(row["hidden_pass"])}
            labelled[item_id] = labels[item_id]["hidden_pass"]
            changed = True

    if need_grade:
        module = _replay_module(ctx.repo)
        workers = max(1, ctx.workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(module.grade_one, pairs[i][0], pairs[i][1],
                            repo=ctx.repo, python=ctx.python,
                            timeout=ctx.timeout_s): i
                for i in need_grade
            }
            for fut in as_completed(futures):
                item_id = futures[fut]
                try:
                    row = fut.result()
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if not isinstance(row.get("hidden_pass"), bool):
                    continue
                labels[item_id] = {"patch_sha256": _patch_sha(pairs[item_id][0]),
                                   "hidden_pass": row["hidden_pass"]}
                labelled[item_id] = row["hidden_pass"]
                changed = True

    if changed:
        _atomic_write_json(path, labels)
    return {i: labelled[i] for i in ids if i in labelled}


def _vr_items(ctx: BenchContext) -> dict[str, str]:
    pairs = _tasks_and_patches(ctx)
    labels = ensure_labels(ctx, tuple(pairs))
    return {i: ("hard_negative" if not labels[i] else "correct")
            for i in pairs if i in labels}


def _replay_one_with_retry(module, patch: Path, task: dict, *,
                           repo: Path, verifier: Path, python: str,
                           timeout: int, root: Path) -> dict:
    last: Exception | None = None
    for _attempt in range(2):
        try:
            return module.replay_one(
                patch, task, repo=repo, verifier=verifier, python=python,
                timeout=timeout, import_root=root, hidden=False)
        except Exception as exc:  # noqa: BLE001 - retried once, then skipped
            last = exc
    assert last is not None
    raise last


def _vr_run(root, items, ctx: BenchContext) -> BenchResult:
    root = Path(root)
    verifier = root / DEFAULT_VERIFIER
    if not verifier.exists():
        raise FileNotFoundError(str(verifier))

    all_pairs = _tasks_and_patches(ctx)
    skipped: list[str] = []
    if items is not None:
        pairs = {}
        for item_id in items:
            if item_id in all_pairs:
                pairs[item_id] = all_pairs[item_id]
            else:
                skipped.append(item_id)
    else:
        pairs = dict(all_pairs)

    ids = tuple(sorted(pairs))
    labels = ensure_labels(ctx, ids)
    module = _replay_module(ctx.repo)

    rows: dict[str, dict] = {}
    failed: set[str] = set()
    workers = max(1, ctx.workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_replay_one_with_retry, module, pairs[i][0], pairs[i][1],
                        repo=ctx.repo, verifier=verifier, python=ctx.python,
                        timeout=ctx.timeout_s, root=root): i
            for i in ids
        }
        for fut in as_completed(futures):
            item_id = futures[fut]
            try:
                rows[item_id] = fut.result()
            except Exception:  # noqa: BLE001 - a twice-failing item is skipped
                failed.add(item_id)

    scores: dict[str, float] = {}
    strata: dict[str, str] = {}
    reasons: dict[str, str] = {}
    false_accept = false_reject = no_verdict = 0
    for item_id in ids:
        if item_id in failed:
            skipped.append(item_id)
            continue
        row = rows.get(item_id)
        if not isinstance(row, dict) or "skipped" in row:
            skipped.append(item_id)
            continue
        if item_id not in labels:
            skipped.append(item_id)
            continue
        label = labels[item_id]
        strata[item_id] = "hard_negative" if not label else "correct"
        reasons[item_id] = (row.get("verifier_reason") or "")[:200]
        verdict = row.get("verifier_pass")
        if verdict is None:
            scores[item_id] = 0.0
            no_verdict += 1
        elif verdict == label:
            scores[item_id] = 1.0
        else:
            scores[item_id] = 0.0
            if verdict and not label:
                false_accept += 1
            elif not verdict and label:
                false_reject += 1

    accuracy = round(sum(scores.values()) / len(scores), 6) if scores else None
    return BenchResult(
        benchmark=VERIFIER_REPLAY,
        tree=tree_key(root),
        scores=scores,
        strata=strata,
        guards={"false_accept": float(false_accept),
                "false_reject": float(false_reject),
                "no_verdict": float(no_verdict)},
        skipped=tuple(skipped),
        meta={"verifier": DEFAULT_VERIFIER, "n": len(scores),
              "accuracy": accuracy, "reasons": reasons},
    )


register_benchmark(Benchmark(VERIFIER_REPLAY, _vr_run, _vr_items,
                             VR_SURFACE, VR_GUARDS))


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini_ork.rsi.benchmarks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list registered benchmarks")

    run = sub.add_parser("run", help="run one benchmark against a tree")
    run.add_argument("name")
    run.add_argument("--root", required=True)
    run.add_argument("--home", default=None)
    run.add_argument("--repo", default=None)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.command == "list":
        for name in benchmark_names():
            print(f"{name}\t{' '.join(get_benchmark(name).surface)}")
        return 0

    try:
        bench = get_benchmark(args.name)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    root = Path(args.root)
    home = (Path(args.home) if args.home
            else Path(os.environ.get("MINI_ORK_HOME") or (root / ".mini-ork")))
    repo = Path(args.repo) if args.repo else root
    ctx = BenchContext(home=home, repo=repo, workers=args.workers)

    items = None
    if args.limit is not None:
        items = tuple(sorted(bench.items(ctx))[:args.limit])

    result = run_benchmark(args.name, root, ctx=ctx, items=items,
                           use_cache=not args.no_cache)

    scores = result.scores
    summary = {
        "benchmark": result.benchmark,
        "tree": result.tree,
        "n": len(scores),
        "accuracy": round(sum(scores.values()) / len(scores), 6) if scores else None,
        "guards": dict(result.guards),
        "skipped": list(result.skipped),
    }
    print(json.dumps(summary))
    if args.out:
        out = Path(args.out)
        _atomic_write_json(out, result.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
