"""``contributor`` node — open the outbound pull request for a verified patch.

An audit or fix run that stops at "patch committed" hands the commit to the
next stranger who reads the issue. This node closes the loop: once the
publisher has committed the change, the contributor pushes the branch and
opens the PR through :mod:`mini_ork.cli.contribute` (``gh`` + a body file, no
long markdown through a shell string).

Opt-in, and inert without it. The node does nothing unless
``MO_CONTRIBUTE_SLUG`` is set, so adding it to a workflow changes nothing for
runs that operate inside one repo.

Environment contract (all read through ``context_env``):

``MO_CONTRIBUTE_SLUG``
    ``OWNER/NAME`` of the **upstream** repository the PR targets. Presence is
    the opt-in switch.
``MO_CONTRIBUTE_BRANCH``
    Branch to push. Defaults to the target checkout's current branch.
    Refused when that branch is the default branch — a PR from ``main`` to
    ``main`` is a typo, not a contribution.
``MO_CONTRIBUTE_BASE``    base branch (default ``main``)
``MO_CONTRIBUTE_REMOTE``  remote to push to (default ``origin``; a fork-only
                          contributor sets ``fork``)
``MO_CONTRIBUTE_HEAD_OWNER``  fork owner for ``--head owner:branch`` (default:
                          same repo as the base)
``MO_CONTRIBUTE_REPO``    checkout to push from (default: the run's target root)
``MO_CONTRIBUTE_TITLE``   PR title; required when the slug is set
``MO_CONTRIBUTE_BODY_FILE``  body source (default ``<run>/pr-body.md``); when
                          absent the body is derived from ``MO_CONTRIBUTE_ISSUE``
``MO_CONTRIBUTE_ISSUE``   issue number this PR fixes; becomes ``Fixes #N``
``MO_CONTRIBUTE_DRY_RUN``  ``1`` prints the commands instead of posting
``MO_CONTRIBUTE_STRICT``   ``1`` turns a failed contribution into a failing node

Security is the one refusal that is not configurable. A run marked
``MO_SECURITY_FIX`` never opens a public PR: the finding belongs in the private
channel named by the target's ``SECURITY.md``, and publishing a patch for it
discloses the vulnerability. That refusal is deliberate and returns a failing
row.

Failure policy: a posted comment/PR is irreversible, so by default a failed
contribution does **not** fail the node (rc 0, recorded in
``<run>/contribution.json``). Failing here would trigger ``rollback``, which
reverts a patch the publisher already shipped — losing good work to a network
error. ``MO_CONTRIBUTE_STRICT=1`` opts into the failing behaviour.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from mini_ork.cli.contribute import ContributeError, PrRequest, open_pr
from mini_ork.context import context_env

_CONTRIBUTION_FILE = "contribution.json"
_DEFAULT_BODY_FILE = "pr-body.md"


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise ContributeError(
            f"contributor: git {' '.join(args)} failed (rc={out.returncode}): "
            f"{(out.stderr or out.stdout).strip()}",
            3,
        )
    return out.stdout.strip()


def _target_repo(root: str, run_dir: str) -> str:
    configured = context_env("MO_CONTRIBUTE_REPO", "").strip()
    if configured:
        return configured
    try:
        from mini_ork.cli.publisher import load_run_roots  # noqa: PLC0415

        roots = load_run_roots(run_dir) if run_dir else None
        if roots is not None and getattr(roots, "target", ""):
            return roots.target
    except Exception:  # noqa: BLE001 — a missing record falls through to the env form
        pass
    return context_env("MO_TARGET_CWD", "").strip() or root


def _current_branch(repo: str) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _default_branch(repo: str) -> str:
    """The checkout's default branch, from the remote HEAD when it is known."""
    try:
        ref = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        return ref.rsplit("/", 1)[-1]
    except ContributeError:
        return "main"


def _derive_body(issue: str) -> str:
    lines = []
    if issue:
        lines.append(f"Fixes #{issue.lstrip('#')}")
        lines.append("")
    lines.append("Patch produced and gated by mini-ork. The verifier envelopes "
                 "for this run are recorded alongside it in the run directory.")
    return "\n".join(lines) + "\n"


def _read_body(run_dir: str, issue: str) -> str:
    configured = context_env("MO_CONTRIBUTE_BODY_FILE", "").strip()
    if configured:
        path = configured
    elif run_dir:
        path = os.path.join(run_dir, _DEFAULT_BODY_FILE)
    else:
        path = ""
    if path and os.path.isfile(path):
        body = open(path, encoding="utf-8").read()
        if configured and not body.strip():
            raise ContributeError(f"contributor: body file {path} is empty")
        if body.strip():
            return body
    if not issue:
        raise ContributeError(
            "contributor: no PR body. Write one to "
            f"{os.path.join(run_dir, _DEFAULT_BODY_FILE) if run_dir else _DEFAULT_BODY_FILE}, "
            "set MO_CONTRIBUTE_BODY_FILE, or set MO_CONTRIBUTE_ISSUE"
        )
    return _derive_body(issue)


def _record(run_dir: str, payload: dict) -> None:
    if not run_dir:
        return
    try:
        with open(os.path.join(run_dir, _CONTRIBUTION_FILE), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError:
        pass


def contributor_node(root: str, run_dir: str, db, run_id: str) -> tuple[int, str]:
    """Open the outbound PR for this run. Returns ``(rc, finish_reason)``."""
    slug = context_env("MO_CONTRIBUTE_SLUG", "").strip()
    if not slug:
        print("  [skip] contributor: MO_CONTRIBUTE_SLUG unset — no outbound PR")
        return 0, "skipped"

    if _truthy(context_env("MO_SECURITY_FIX", "")):
        message = (
            "contributor: refusal — this run is marked MO_SECURITY_FIX. A security "
            "finding must go to the private channel named in the target's SECURITY.md, "
            "never a public issue or PR; publishing this patch discloses it."
        )
        print(f"  [BLOCK] {message}", flush=True)
        _record(run_dir, {"status": "refused", "reason": "security_fix",
                          "slug": slug, "detail": message})
        return 1, "security_channel_required"

    # A withheld publish has nothing to contribute. The publisher records the
    # abstention and exits 0, so without this read the contributor would push a
    # branch whose change the gates explicitly refused to ship.
    if run_dir:
        from mini_ork.cli.publisher import _PUBLISH_ABSTAIN_FILE  # noqa: PLC0415

        if os.path.isfile(os.path.join(run_dir, _PUBLISH_ABSTAIN_FILE)):
            print("  [skip] contributor: the publisher withheld this run — no PR")
            return 0, "publish_abstained"

    strict = _truthy(context_env("MO_CONTRIBUTE_STRICT", ""))
    try:
        title = context_env("MO_CONTRIBUTE_TITLE", "").strip()
        if not title:
            raise ContributeError(
                "contributor: MO_CONTRIBUTE_TITLE is required when "
                "MO_CONTRIBUTE_SLUG is set"
            )
        repo = _target_repo(root, run_dir)
        branch = context_env("MO_CONTRIBUTE_BRANCH", "").strip() or _current_branch(repo)
        default_branch = _default_branch(repo)
        if branch in {"main", "master", default_branch}:
            raise ContributeError(
                f"contributor: refusing to open a PR from {branch!r}, the default "
                "branch — the work must live on a feature branch "
                "(set MO_CONTRIBUTE_BRANCH or check out the run's branch)"
            )
        issue = context_env("MO_CONTRIBUTE_ISSUE", "").strip()
        request = PrRequest(
            repo=Path(repo),
            slug=slug,
            branch=branch,
            title=title,
            body=_read_body(run_dir, issue),
            base=context_env("MO_CONTRIBUTE_BASE", "main").strip() or "main",
            remote=context_env("MO_CONTRIBUTE_REMOTE", "origin").strip() or "origin",
            head_owner=context_env("MO_CONTRIBUTE_HEAD_OWNER", "").strip(),
            draft=_truthy(context_env("MO_CONTRIBUTE_DRAFT", "")),
        )
        dry_run = _truthy(context_env("MO_CONTRIBUTE_DRY_RUN", ""))
        url = open_pr(request, dry_run=dry_run)
    except ContributeError as exc:
        print(f"  [warn] contributor: {exc}", flush=True)
        _record(run_dir, {"status": "failed", "error": str(exc), "slug": slug})
        if db and run_id:
            try:
                from mini_ork.cli.publisher import _note  # noqa: PLC0415

                _note(db, run_id, f"contributor: outbound PR failed ({exc})")
            except Exception:  # noqa: BLE001 — a note must never fail the node
                pass
        return (1, "contribute_failed") if strict else (0, "contribution_failed")

    _record(run_dir, {
        "status": "dry_run" if dry_run else "posted",
        "url": url,
        "slug": slug,
        "branch": request.branch,
        "base": request.base,
        "remote": request.remote,
        "head_owner": request.head_owner,
        "issue": issue,
    })
    if dry_run:
        print("  [ok] contributor: dry-run — nothing posted")
    else:
        print(f"  [ok] contributor: {url}")
    return 0, "done"
