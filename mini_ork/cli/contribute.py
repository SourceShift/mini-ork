"""``mini-ork contribute`` — open a pull request or an issue from the CLI.

    mini-ork contribute pr    --repo DIR --slug OWNER/NAME --branch B \\
                              --title T --body-file F [--base main] \\
                              [--remote fork] [--head-owner OWNER] [--draft] [--dry-run]
    mini-ork contribute issue --repo DIR --slug OWNER/NAME \\
                              --title T --body-file F [--dry-run]

Why this exists: an agent that finishes a fix outside mini-ork has to remember
three orderings (stage explicit paths, push to the right remote, target the
right base) and any one of them wrong sends the change somewhere unintended.
This is the deterministic part of an outbound contribution: it pushes the
branch, then hands the body to ``gh`` as a file so no long markdown ever
travels through a shell string.

Two tiers, same shape as the vault:

- **simple** — ``gh auth login`` once on the laptop; no extra config.
- **secure** — send ``GH_TOKEN`` from the environment / secret store; nothing
  is written to disk and no flag carries the credential.

Both fail closed: no ``gh`` on PATH, or an unauthenticated ``gh``, is a clear
error, never a silent no-op.

Exit codes: 0 posted (or dry-run), 2 usage error / missing prerequisite,
3 the remote command failed (its stderr is forwarded).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HELP_TEXT = (
    "Usage:\n"
    "  mini-ork contribute pr    --repo DIR --slug OWNER/NAME --branch B\n"
    "                            --title T --body-file F [--base main]\n"
    "                            [--remote fork] [--head-owner OWNER] [--draft]\n"
    "                            [--dry-run]\n"
    "  mini-ork contribute issue --repo DIR --slug OWNER/NAME\n"
    "                            --title T --body-file F [--dry-run]\n"
    "\n"
    "Open a pull request or an issue through gh, from a body file.\n"
    "--body-file - reads stdin. --dry-run prints the argv instead of running it.\n"
    "\n"
    "Auth: `gh auth login` (simple tier) or GH_TOKEN in the environment\n"
    "(secure tier). Both fail closed when absent.\n"
)


class ContributeError(Exception):
    """A prerequisite is missing or the remote command refused."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class PrRequest:
    repo: Path
    slug: str
    branch: str
    title: str
    body: str
    base: str = "main"
    remote: str = "origin"
    head_owner: str = ""
    draft: bool = False


@dataclass(frozen=True)
class IssueRequest:
    repo: Path
    slug: str
    title: str
    body: str


def _parse_flags(argv: list[str], known: set[str]) -> dict[str, str]:
    """Parse ``--flag value`` / ``--flag`` pairs, rejecting anything unknown."""
    parsed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            raise ContributeError(f"contribute: unexpected argument {token!r}")
        name = token[2:]
        if name not in known:
            raise ContributeError(f"contribute: unknown flag --{name}")
        if name in parsed:
            raise ContributeError(f"contribute: --{name} given twice")
        if name in {"dry-run", "draft"}:
            parsed[name] = "1"
            index += 1
            continue
        if index + 1 >= len(argv):
            raise ContributeError(f"contribute: --{name} needs a value")
        parsed[name] = argv[index + 1]
        index += 2
    return parsed


def _required(flags: dict[str, str], name: str) -> str:
    value = flags.get(name, "").strip()
    if not value:
        raise ContributeError(f"contribute: --{name} is required")
    return value


def _read_body(flags: dict[str, str]) -> str:
    path = _required(flags, "body-file")
    if path == "-":
        body = sys.stdin.read()
    else:
        try:
            body = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ContributeError(f"contribute: cannot read --body-file {path}: {exc}") from exc
    if not body.strip():
        raise ContributeError("contribute: --body-file is empty; gh would reject the body")
    return body


@contextlib.contextmanager
def _body_file(body: str):
    """Stage the body where only this user can read it, and remove it after."""
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", prefix="mini-ork-contribute-",
        delete=False,
    )
    try:
        handle.write(body)
        handle.close()
        os.chmod(handle.name, 0o600)
        yield handle.name
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)


def _repo_dir(flags: dict[str, str]) -> Path:
    raw = flags.get("repo", ".")
    repo = Path(raw).expanduser()
    if not repo.is_dir():
        raise ContributeError(f"contribute: --repo {raw} is not a directory")
    if not (repo / ".git").exists():
        raise ContributeError(f"contribute: --repo {raw} is not a git checkout")
    return repo


def _require_gh() -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise ContributeError(
            "contribute: gh is not on PATH; install GitHub CLI "
            "(https://cli.github.com) and run `gh auth login`, or set GH_TOKEN"
        )
    return gh


def _run(argv: list[str], *, dry_run: bool) -> str:
    """Run one command, returning its stdout. A non-zero exit is an error."""
    printable = " ".join(argv)
    if dry_run:
        print(f"[contribute] dry-run: {printable}")
        return ""
    try:
        proc = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContributeError(f"contribute: {argv[0]} could not run: {exc}", 3) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ContributeError(
            f"contribute: {argv[0]} {argv[1] if len(argv) > 1 else ''} failed "
            f"(rc={proc.returncode}): {detail}",
            3,
        )
    return proc.stdout.strip()


_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


def _check_auth(gh: str, *, dry_run: bool) -> None:
    """Fail closed when nothing can authenticate the call.

    An environment token *is* the credential, so the local `gh auth status`
    probe is skipped for it: a laptop with a stale keyring entry but a valid
    GH_TOKEN must still publish.
    """
    if any(os.environ.get(name) for name in _TOKEN_ENV_VARS):
        return
    _run([gh, "auth", "status"], dry_run=dry_run)


def open_pr(req: PrRequest, *, dry_run: bool = False) -> str:
    gh = _require_gh()
    _check_auth(gh, dry_run=dry_run)
    _run(
        ["git", "-C", str(req.repo), "push", "--set-upstream", req.remote, req.branch],
        dry_run=dry_run,
    )
    with _body_file(req.body) as body_path:
        argv = [gh, "pr", "create", "--repo", req.slug, "--title", req.title,
                "--body-file", body_path, "--base", req.base,
                "--head", f"{req.head_owner}:{req.branch}" if req.head_owner else req.branch]
        if req.draft:
            argv.append("--draft")
        return _run(argv, dry_run=dry_run)


def open_issue(req: IssueRequest, *, dry_run: bool = False) -> str:
    gh = _require_gh()
    _check_auth(gh, dry_run=dry_run)
    with _body_file(req.body) as body_path:
        argv = [gh, "issue", "create", "--repo", req.slug, "--title", req.title,
                "--body-file", body_path]
        return _run(argv, dry_run=dry_run)


_PR_FLAGS = {"repo", "slug", "branch", "title", "body-file", "base", "remote",
             "head-owner", "draft", "dry-run"}
_ISSUE_FLAGS = {"repo", "slug", "title", "body-file", "dry-run"}


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if not rest or rest[0] in {"-h", "--help", "help"}:
        print(HELP_TEXT, file=sys.stdout if rest else sys.stderr)
        return 0 if rest else 2
    verb, tail = rest[0], rest[1:]
    try:
        if verb == "pr":
            flags = _parse_flags(tail, _PR_FLAGS)
            request = PrRequest(
                repo=_repo_dir(flags),
                slug=_required(flags, "slug"),
                branch=_required(flags, "branch"),
                title=_required(flags, "title"),
                body=_read_body(flags),
                base=flags.get("base", "main"),
                remote=flags.get("remote", "origin"),
                head_owner=flags.get("head-owner", ""),
                draft="draft" in flags,
            )
            url = open_pr(request, dry_run="dry-run" in flags)
        elif verb == "issue":
            flags = _parse_flags(tail, _ISSUE_FLAGS)
            request = IssueRequest(
                repo=_repo_dir(flags),
                slug=_required(flags, "slug"),
                title=_required(flags, "title"),
                body=_read_body(flags),
            )
            url = open_issue(request, dry_run="dry-run" in flags)
        else:
            raise ContributeError(f"contribute: unknown verb {verb!r} (pr|issue)")
    except ContributeError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    if url:
        print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
