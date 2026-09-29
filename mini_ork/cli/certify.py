"""``mini-ork certify`` — does this change actually fix the claimed bug?

Slice C2 entrypoint. The CLI is intentionally thin: it loads the patch,
decides a runtime image, runs the solve-time oracle, writes a certificate,
prints a summary. Every expensive primitive lives in
``mini_ork.certify.{image,oracle,verdict,certificate,llm}``.

Exit codes (CI-gateable):
    0  PROVEN      — the patch fixes the bug AND generalises.
    1  REFUTED     — the patch is wrong (does not apply / probe still fails /
                    invariants don't hold).
    2  UNVERIFIED  — the oracle could not decide (no patch / no runtime /
                    probe couldn't be built / insufficient invariants).
   64  usage error — bad flags, missing --issue, etc.

The module never imports anything that pulls the optional ``verifiers``
runtime layer; the in-process ``judge`` is the only oracle entry point.
That keeps the certifier importable from tests without a heavy cloud SDK.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mini_ork.certify import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Verdict,
    judge,
)
from mini_ork.certify import image as cert_image
from mini_ork.certify import llm as cert_llm
from mini_ork.certify.certificate import build_certificate
from mini_ork.runtime import Crucible, RuntimeSpec


def _build_parser(stdout=None, stderr=None) -> argparse.ArgumentParser:
    """Build the argument parser, redirecting error/help output to ``stdout``/``stderr``.

    argparse's default ``error()`` exits with code 2; the kickoff's contract is
    usage errors → 64. Overriding lets us route help text through the same
    stream the caller controls AND exit with the correct code.
    """
    class _P(argparse.ArgumentParser):
        def __init__(self, **kw):
            self._stdout = stdout or sys.stdout
            self._stderr = stderr or sys.stderr
            super().__init__(prog="mini-ork certify", **kw)

        def error(self, message):
            # argparse routes parse errors here. The kickoff pins these to
            # exit 64 so a CI gate can distinguish "wrong invocation" from
            # "ran but couldn't decide".
            self._stderr.write(f"usage error: {message}\n")
            self.print_help(self._stdout)
            raise SystemExit(64)

        def print_help(self, file=None):
            super().print_help(file or self._stdout)

    p = _P(
        description=(
            "Judge whether a change actually fixes the claimed bug. "
            "Writes a mini-ork.certificate/v1 JSON and prints a summary."
        ),
    )
    p.add_argument("--repo", default=".", help="git repo to certify (default: .)")
    p.add_argument("--base", default="HEAD~1", help="commit before the change (default: HEAD~1)")
    p.add_argument("--head", default="HEAD", help="commit with the change (ignored if --diff)")
    p.add_argument("--diff", default=None, help="use this patch file instead of `git diff base head`")
    issue = p.add_mutually_exclusive_group(required=True)
    issue.add_argument("--issue", default=None, help="what the change claims to fix")
    issue.add_argument("--issue-file", default=None, help="read --issue from this file")
    p.add_argument("--image", default=None, help="prebuilt image; /testbed is the repo at BASE")
    p.add_argument("--workdir", default="/testbed", help="repo location inside the image (default: /testbed)")
    p.add_argument("--mr-n", type=int, default=4, help="metamorphic invariants to generate (default: 4)")
    p.add_argument("--out", default=None, help="certificate path (default: <repo>/.mini-ork/certificates/<id>.json)")
    p.add_argument("--json", action="store_true", help="print the certificate JSON to stdout instead of the summary")
    return p


def _read_issue(args: argparse.Namespace) -> str:
    if args.issue is not None:
        return args.issue
    return Path(args.issue_file).read_text(encoding="utf-8", errors="replace")


def _default_out(repo: Path, cert_id: str) -> Path:
    out_dir = repo / ".mini-ork" / "certificates"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{cert_id}.json"


def _resolve_out(args: argparse.Namespace, repo: Path, cert_id: str) -> Path:
    """``--out`` when supplied, else ``<repo>/.mini-ork/certificates/<id>.json``.

    The empty-patch and runtime-unavailable paths still write the certificate
    even though they short-circuit — ``--out`` applies to ALL outcomes.
    """
    if args.out:
        return Path(args.out).resolve()
    return _default_out(repo, cert_id)


def _resolve_shas(repo: Path, base: str, head: str | None, diff_path: str | None) -> tuple[str, str | None]:
    """Run ``git rev-parse`` for ``base`` (always) and ``head`` (only when no --diff)."""
    def _rev(spec: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", spec],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()

    base_sha = _rev(base)
    head_sha: str | None = None
    if diff_path is None and head is not None:
        head_sha = _rev(head)
    return base_sha, head_sha


def _read_patch(repo: Path, base_sha: str, head: str | None, diff_path: str | None) -> str:
    if diff_path is not None:
        return Path(diff_path).read_text(encoding="utf-8", errors="replace")
    # `git diff --no-color --binary` matches the C1 oracle's patch conventions.
    # `head` is guaranteed non-None here — `_resolve_shas` only leaves it None
    # when a --diff file is supplied, in which case this branch is not taken.
    assert head is not None, "head is None without --diff"
    r = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", "--binary", base_sha, head],
        capture_output=True, text=True,
    )
    return r.stdout


def _origin_url(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def _decide_image(
    *, repo: Path, base_sha: str, image: str | None,
) -> tuple[str | None, str | None]:
    """Resolve which image to run. Returns (tag, unverified_reason).

    Either ``tag`` is non-None and ``unverified_reason`` is None, or vice versa.
    ``tag=None, reason=str`` short-circuits the CLI to UNVERIFIED exit 2.
    """
    if image:
        return image, None
    if not cert_image.is_python_project(repo):
        return None, (
            "unsupported project: v1 certifies Python repos; pass --image"
        )
    if not shutil.which("docker"):
        return None, "runtime unavailable: docker not on PATH"
    try:
        return cert_image.build_image(repo, base_sha), None
    except Exception as e:                                       # noqa: BLE001
        return None, f"runtime unavailable: {e}"


def _summary_lines(cert: dict, write_path: Path) -> list[str]:
    verdict = cert["verdict"]
    reason = cert["reason"]
    change_files = cert["change"]["files"]
    n_files = len(change_files)
    change_short = cert["change"]["sha256"][:8]
    claim = cert["claim"]["summary"]
    if len(claim) > 60:
        claim = claim[:57] + "…"
    cost = cert["cost"]["usd"]
    cost_s = f"${cost:.2f}" if cost is not None else "$0.00"
    dur = int(cert["duration_s"])
    return [
        f"{verdict}  {reason}",
        f"  change  {n_files} file{'s' if n_files != 1 else ''} · sha256 {change_short}   "
        f"claim  \"{claim}\"",
        f"  proof   {write_path}   cost {cost_s} · {dur}s",
    ]


_EXIT = {PROVEN: 0, REFUTED: 1, UNVERIFIED: 2}


def _emit(cert: dict, args: argparse.Namespace, repo: Path, out) -> int:
    """Write the certificate, print the summary (or JSON), return the verdict's exit code.

    Every outcome — including the short-circuits — goes through here, so every run
    leaves a certificate at ``--out`` and CI always gets 0/1/2 from the verdict.
    """
    out_path = _resolve_out(args, repo, cert["id"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(cert, indent=2, sort_keys=True)
    out_path.write_text(body, encoding="utf-8")
    out.write(body + "\n" if args.json else "\n".join(_summary_lines(cert, out_path)) + "\n")
    return _EXIT.get(cert["verdict"], 2)


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = _build_parser(stdout=out, stderr=err)
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()

    # ── issue text ─────────────────────────────────────────────────────────
    try:
        issue_text = _read_issue(args)
    except OSError as e:
        err.write(f"failed to read --issue-file: {e}\n")
        return 64

    # ── shas + patch ──────────────────────────────────────────────────────
    try:
        base_sha, head_sha = _resolve_shas(repo, args.base, args.head, args.diff)
    except subprocess.CalledProcessError as e:
        err.write(f"git rev-parse failed: {e.stderr.strip() or e}\n")
        return 64

    patch_text = _read_patch(repo, base_sha, head_sha, args.diff)

    def certificate(verdict: str, reason: str, *, image: str = "", v: Verdict | None = None,
                    spent: dict | None = None, duration_s: float = 0.0) -> dict:
        return build_certificate(
            verdict=verdict,
            reason=reason,
            repo_path=str(repo),
            remote=_origin_url(repo),
            base_sha=base_sha,
            head_sha=head_sha,
            patch_text=patch_text,
            issue_text=issue_text,
            oracle_method="probe+delta+invariants",
            mr_n=args.mr_n,
            proven_bar=0.667,
            model=os.environ.get("MO_CERTIFY_MODEL", cert_llm.DEFAULT_MODEL),
            image=image,
            poc_plus=v.poc_plus if v else None,
            mr_pass_rate=v.mr_pass_rate if v else None,
            invariants=(v.detail or {}).get("invariants", []) if v else [],
            cost_usd=spent["usd"] if spent else None,
            llm_calls=spent["calls"] if spent else 0,
            duration_s=duration_s,
        )

    # ── empty patch short-circuit (BEFORE we touch the runtime) ───────────
    if not patch_text.strip():
        return _emit(certificate(UNVERIFIED, "no patch"), args, repo, out)

    # ── runtime / image decision ──────────────────────────────────────────
    image_tag, unverified = _decide_image(repo=repo, base_sha=base_sha, image=args.image)
    if unverified is not None:
        return _emit(certificate(UNVERIFIED, unverified), args, repo, out)
    assert image_tag is not None, "image decision should have produced a tag"

    # ── judge ─────────────────────────────────────────────────────────────
    cert_llm.reset_spend()
    t0 = time.monotonic()
    try:
        with Crucible(RuntimeSpec(image=image_tag, workdir=args.workdir)) as c:
            v: Verdict = judge(issue_text, patch_text, runner=c, mr_n=args.mr_n)
    except Exception as e:                                       # noqa: BLE001
        # A harness crash is not a verdict on the patch — abstain, and say why.
        return _emit(certificate(UNVERIFIED, f"certify failed during judge: {e}",
                                 image=image_tag, spent=cert_llm.spent(),
                                 duration_s=time.monotonic() - t0), args, repo, out)

    return _emit(certificate(v.verdict, v.reason, image=image_tag, v=v,
                             spent=cert_llm.spent(), duration_s=time.monotonic() - t0),
                 args, repo, out)

if __name__ == "__main__":
    sys.exit(main())
