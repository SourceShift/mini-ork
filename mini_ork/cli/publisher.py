"""Publisher node: artifact-contract delivery + git commit (extracted from cli/execute.py).

Owns the two blocking pre-publish gates (oracle safety gates, panel-verdict
approval for recursive-validate-impl), the source_artifact -> outputs[] copy,
and the strict-child-path implementer commit. Re-exported from
mini_ork.cli.execute for backward compatibility.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from mini_ork.context import apply_env_overrides


def set_status(db, run_id, new_status):  # late binding — avoids the execute<->publisher cycle
    from mini_ork.cli.execute import set_status as _impl  # noqa: PLC0415
    return _impl(db, run_id, new_status)

def _envsubst(s):
    """B2-C: envsubst-equivalent — expand $VAR / ${VAR} from the environment, BLANKING
    unset vars (os.path.expandvars leaves them literal, which commits garbage
    ${VAR}-in-path files, the OSS-leak-garbage class the bash guard prevents)."""
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                  lambda m: os.environ.get(m.group(1) or m.group(2), ''), s)



def _publisher_try_commit_files(root, target_repo, run_dir, review_file, verdict_env,
                                recipe, node_desc, run_id):
    """Port of bash `_publisher_try_commit_files` (embedded python, :824-949). Commit
    the implementer's in-place edits on reviewer APPROVE. Strict-child path validation
    (the OSS-leak guard — only `git add --` files proven inside the target repo, never
    `-A`). Returns True on commit, False on skip."""
    def log(msg):
        print(msg, file=sys.stderr, flush=True)
    summary_path = os.path.join(run_dir, "implementer-summary.json") if run_dir else ""
    verdict = ""
    candidates = []
    if run_dir:
        for name in ("panel-verdict.json", "review-verdict.json"):
            p = os.path.join(run_dir, name)
            if os.path.isfile(p):
                candidates.append(p)
    if review_file and os.path.isfile(review_file):
        candidates.append(review_file)
    for p in candidates:
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("pass") is True or str(data.get("verdict", "")).strip().lower() in {"approve", "approved", "pass"}:
            verdict = "approve"
            break
    if verdict != "approve" and review_file and root:
        try:
            out = subprocess.check_output(
                ["python3", os.path.join(root, "lib", "extract_verdict.py"), review_file],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip().lower()
            if out in {"approve", "approved", "pass"}:
                verdict = "approve"
        except Exception:
            pass
    if verdict != "approve" and (verdict_env or "").strip().lower() in {"approve", "approved", "pass"}:
        verdict = "approve"
    if verdict != "approve":
        disp = verdict or (verdict_env or "").strip() or "<none>"
        log(f"  [skip-publish] reviewer verdict (resolved: '{disp}') is not APPROVE — no commit")
        return False
    files = []
    if summary_path and os.path.isfile(summary_path):
        try:
            data = json.load(open(summary_path, encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files_changed"), list):
                files = [e for e in data["files_changed"] if isinstance(e, str) and e]
        except Exception:
            pass
    if not files:
        log(f"  [skip-publish] no files_changed in {summary_path or '<unset>'} — no commit")
        return False
    if not target_repo:
        log("  [skip-publish] no target_repo resolved (MO_TARGET_CWD empty and git toplevel failed)")
        return False
    real_root = os.path.realpath(target_repo)
    valid = []
    for raw in files:
        ap = raw if os.path.isabs(raw) else os.path.abspath(raw)
        if not os.path.exists(ap):
            log(f"  [reject-publish] file does not exist: {raw}")
            continue
        real = os.path.realpath(ap)
        if real != real_root and not real.startswith(real_root + os.sep):
            log(f"  [reject-publish] file escapes target repo toplevel: {raw} -> {real} not under {real_root}")
            continue
        valid.append(real)
    if not valid:
        log(f"  [skip-publish] no valid files inside target_repo={real_root} — no commit")
        return False
    try:
        subprocess.run(["git", "add", "--", *valid], cwd=target_repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        msg = f"mini-ork({recipe}): {node_desc} [run {run_id}]"
        subprocess.run(["git", "-c", "user.email=mini-ork@local", "-c", "user.name=mini-ork",
                        "commit", "-q", "-m", msg], cwd=target_repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sha = subprocess.check_output(["git", "-C", target_repo, "rev-parse", "HEAD"]).decode("utf-8", "replace").strip()
        log(f"  [publish] committed {len(valid)} file(s): {sha}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"  [skip-publish] git add/commit failed in {target_repo}: rc={e.returncode}")
        return False


def publisher_node(root, run_dir, db, run_id, recipe, task_class, review_file="", verdict_env=""):
    """Port of bash publisher branch (:2909-3200). The panel found the port stubbed
    this to `set_status('published')` — this restores the two BLOCKING gates + delivery:
      1. oracle gates (block on safety_violation);
      2. recursive-validate-impl panel-verdict approval gate;
      3. artifact-contract copy source_artifact→outputs + git commit, OR the M1
         empty-outputs in-place implementer commit.
    Returns (rc, finish_reason). Template ${VAR} paths resolved via expandvars
    (envsubst-equivalent)."""
    # ── oracle gates: fire once pre-publish; only a definitive safety_violation blocks
    if os.environ.get("MO_ORACLE_GATES_AUTO", "1") == "1" and run_id and db:
        verdict_file = os.path.join(run_dir, "panel-verdict.json")
        ctx = json.dumps({"panel_run_id": run_id, "recipe": recipe or "unknown",
                          "task_class": task_class or "generic",
                          "verdict_file": verdict_file, "current_round": 1})
        try:
            from mini_ork.gates import gate_bootstrap, gate_registry
            gate_bootstrap.bootstrap_oracle_gates(db=db, root=root)
            result = gate_registry.gate_run_all(
                db,
                task_class or "generic",
                ctx,
                mini_ork_root=root,
            )
            sv = result.get("safety_violation", False)
            if sv is True or str(sv) == "True":
                print("  [BLOCK] oracle-gates: safety_violation — publish refused (COALITION_ABORT or equivalent)")
                return 1, "safety_violation"
            print("  [ok] oracle-gates: pre-publish pass")
        except Exception:
            pass
    # ── recursive-validate-impl requires an approved panel verdict
    if recipe == "recursive-validate-impl":
        pvf = os.path.join(run_dir, "panel-verdict.json")
        if not (os.path.isfile(pvf) and os.path.getsize(pvf) > 0):
            print(f"  [BLOCK] publisher: missing panel verdict at {pvf}", file=sys.stderr)
            return 1, "verdict_fail"
        try:
            data = json.load(open(pvf, encoding="utf-8"))
            ok = data.get("pass") is True or str(data.get("verdict", "")).strip().lower() in {"approve", "approved", "pass"}
        except Exception:
            ok = False
        if not ok:
            print("  [BLOCK] publisher: panel verdict is not approved — publish refused", file=sys.stderr)
            return 1, "verdict_fail"
    # ── artifact contract
    contract = os.path.join(root, "recipes", recipe, "artifact_contract.yaml") if recipe else ""
    if not contract or not os.path.isfile(contract):
        print(f"  [warn] publisher: no artifact_contract.yaml at {contract} — skipping", file=sys.stderr)
        return 0, "done"
    src_name, outputs = "synthesis.md", []
    try:
        import yaml  # noqa: PLC0415
        d = yaml.safe_load(open(contract, encoding="utf-8")) or {}
        if isinstance(d, dict):
            src_name = d.get("source_artifact") or "synthesis.md"
            for o in (d.get("outputs") or []):
                if isinstance(o, dict) and o.get("path"):
                    outputs.append(o["path"])
                elif isinstance(o, str):
                    outputs.append(o)
    except Exception:
        pass
    if not outputs:
        # M1 empty-outputs: commit the implementer's in-place edits (code-fix recipes)
        target_repo = os.environ.get("MO_TARGET_CWD", "")
        if not target_repo:
            try:
                target_repo = subprocess.check_output(
                    ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL).decode().strip()
            except Exception:
                target_repo = root or "."
        print("  [warn] publisher: artifact_contract.yaml has no outputs[] — skipping publish", file=sys.stderr)
        _publisher_try_commit_files(root, target_repo, run_dir, review_file, verdict_env,
                                    recipe or "code-fix", os.environ.get("MINI_ORK_NODE_DESC", "implementer"),
                                    run_id or "local")
        set_status(db, run_id, "published")
        return 0, "done"
    # ── copy source_artifact → outputs[] + git-commit each.
    # B2-C: recipe-creator meta-recipe references ${MINI_ORK_DERIVED_RECIPE_NAME}; read
    # it from chosen/recipe_name and export BEFORE resolving templates (bash :3075-3079).
    if not os.environ.get("MINI_ORK_DERIVED_RECIPE_NAME"):
        chosen = os.path.join(run_dir, "chosen", "recipe_name")
        if os.path.isfile(chosen):
            try:
                apply_env_overrides({
                    "MINI_ORK_DERIVED_RECIPE_NAME": "".join(open(chosen).read().split())})
            except OSError:
                pass
    src = os.path.join(run_dir, _envsubst(src_name))
    src_is_dir = os.path.isdir(src)
    if not src_is_dir and not os.path.isfile(src):
        print(f"  [warn] publisher: expected source artifact missing: {src}", file=sys.stderr)
        return 1, "error"
    # B1: track copy/commit failures like bash (:3111-3184). A failed copy, or a
    # copy-OK-but-commit-failed with a dirty tree, is a real failure — return 1 and
    # do NOT mark 'published'. A commit that fails with nothing-to-commit (dst already
    # matches) is an OK no-op. Was: swallowed failures + unconditional 'published'.
    published_count = 0
    failed_count = 0
    run_root = os.path.realpath(run_dir)
    for out in outputs:
        out = _envsubst(out)
        dst = os.path.join(root, out)
        dst_real = os.path.realpath(dst)
        run_local = dst_real == run_root or dst_real.startswith(run_root + os.sep)

        # Composite/propose-only recipes publish several heterogeneous files in
        # MINI_ORK_RUN_DIR (for example a diff plus JSON ledger and verdict).
        # Those files are already in their canonical destination: copying the
        # single source_artifact over every output corrupts the artifact set.
        # Preserve each run-local output byte-for-byte and fail closed when the
        # producer omitted one.
        if run_local:
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                print(f"  [ok] publisher: preserved run-local artifact {out}")
                published_count += 1
            elif os.path.isdir(dst) and os.listdir(dst):
                print(f"  [ok] publisher: preserved run-local artifact {out}")
                published_count += 1
            else:
                print(f"  [fail] publisher: missing run-local artifact {out}",
                      file=sys.stderr)
                failed_count += 1
            continue
        copied = False
        try:
            if src_is_dir:
                dst_norm = dst.rstrip("/")
                os.makedirs(dst_norm, exist_ok=True)
                copied = subprocess.run(["cp", "-R", src + "/.", dst_norm + "/"],
                                        capture_output=True).returncode == 0
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(src, dst)
                copied = True
        except Exception as e:
            print(f"  [fail] publisher: cp failed for {out}: {e}", file=sys.stderr)
        if not copied:
            print(f"  [fail] publisher: cp failed for {out}", file=sys.stderr)
            failed_count += 1
            continue
        subprocess.run(["git", "add", out], cwd=root, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commit = subprocess.run(
            ["git", "-c", "user.email=mini-ork@local", "-c", "user.name=mini-ork",
             "commit", "-q", "-m",
             f"audit({recipe or 'unknown'}): publish synthesis from {run_id}\n\n"
             f"Run: {run_id}\nRecipe: {recipe or 'unknown'}\nOutput: {out}\n"
             "Dispatched by mini-ork-execute publisher node (D-037 v0.2-pt9)."],
            cwd=root, capture_output=True)
        if commit.returncode == 0:
            print(f"  [ok] publisher: published {out} (committed)")
            published_count += 1
        else:
            # nothing-to-commit (dst unchanged) is an OK no-op, not a failure.
            status = subprocess.run(["git", "status", "--porcelain", out], cwd=root,
                                    capture_output=True, text=True)
            if not status.stdout.strip():
                print(f"  [ok] publisher: {out} unchanged from prior cycle (no-op commit)")
                published_count += 1
            else:
                print(f"  [warn] publisher: copy OK but commit failed for {out}", file=sys.stderr)
                failed_count += 1
    if failed_count > 0:
        print(f"  [fail] publisher: {failed_count} of {published_count + failed_count} outputs failed",
              file=sys.stderr)
        return 1, "error"
    print(f"  [ok] publisher: {published_count} artifact(s) published")
    set_status(db, run_id, "published")
    return 0, "done"


