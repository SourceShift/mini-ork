"""The canvas's workspace surface — files, git, and the bash runtime.

The forked canvas is not only a chat view. Every conversation carries a
workspace panel whose tabs call a small agent-server vocabulary that
``routes/agent_server.py`` never served::

    GET  /api/file/home                    the absolute anchor for rel paths
    GET  /api/file/search_subdirs?path=…   directory picker
    GET  /api/conversations/{id}/workspace/{path}
                                           the static fileserver — file bodies
    POST /api/bash/execute_bash_command    the terminal, AND the file tree
    GET  /api/git/{changes,diff,commits}   the changes/diff/commits tabs
    POST /api/auth/workspace-session       mint the static-asset cookie
    POST /api/skills                       the skills list
    GET  /api/automation/sdk-version       automation version probe

Three of those are load-bearing in a way the names do not advertise.
``execute_bash_command`` is how the Files tab enumerates the working tree —
``use-workspace-files.ts`` sends a ``find`` and parses stdout — so a shim
without it shows an empty file tree, not a missing tab. And the canvas
treats **404 as "this server predates the endpoint"** (see
``agent-server-git-service.api.ts``: callers hide the commits section
rather than erroring) while it treats **405 as a hard failure** it surfaces
as a toast. An unmatched POST used to land on the GET-only SPA catch-all
and so answered 405; ``app.py`` now answers 404 there, which is what lets
the panels that still have no server-side story degrade quietly.

The third is the fileserver route. ``RemoteWorkspace.startWorkspaceSession``
discards the POST body and builds the file URL client-side as
``${host}/api/conversations/{id}/workspace/``, so no answer to the session
POST can suppress it — the Files tab previews a file by fetching that URL, and
an unserved path shows "Failed to read <file>: 404" on every click. Serving it
is what makes the preview work, over the same confined root as the tabs.

**What "workspace" means here.** The canvas assumes an OpenHands
agent-server whose runtime has a home directory and a working tree. mini-ork
has no such daemon: a run dispatches recipe nodes into a *target CWD*
(``MO_TARGET_CWD``, the documented lever for where dispatched agents write).
So that is the workspace root — one anchor, resolved once, shared by the
file, git and bash routes, so the three tabs can never disagree about which
tree they are describing. A serving process with no ``MO_TARGET_CWD``
inherits its own CWD, which is the honest reading of "the tree this server
is operating on".

**Path confinement.** Every path a client sends is resolved and then checked
for containment under the root before it reaches the filesystem or a shell.
``resolve()`` before the check is what makes it real: ``../../etc/passwd``
resolves outside the root and is refused, rather than being pattern-matched
and hoped about. The bash route confines its ``cwd`` the same way — the
command string itself is the operator's own, and is deliberately NOT
sandboxed, for the reason in :func:`execute_bash_command`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .agent_server import require_local_caller

router = APIRouter(tags=["workspace"])

#: The command runner holds a worker thread for the life of the child, so the
#: ceiling is enforced server-side rather than trusted from the client. The
#: canvas asks for 30s by default; a client-supplied value may lower this but
#: never raise it, or a single request could pin a thread indefinitely.
_MAX_TIMEOUT_SECONDS = 120

#: Git output is read into memory before being shaped into JSON. Bounded so a
#: pathological repository cannot turn one request into an OOM.
_GIT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: Content types the fileserver must get right, for the extensions that change
#: how the canvas *renders* rather than how it decodes. `mimetypes.guess_type`
#: consults the host's mime db, which is not the same on every machine — it
#: answers nothing for `.md` on macOS — and behind an `<img>`/`<iframe>` a wrong
#: or missing type is the difference between a rendered preview and a download.
#: The list mirrors `guessMimeType` in `use-workspace-file-content.ts` so the
#: two sides agree; anything not here falls through to the platform db.
_MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
}

#: Opt-out for a deployment that does not want a shell behind the HTTP
#: surface. Unset means enabled, matching ``POST /api/conversations``: that
#: route already lets this same caller launch a run that writes files and
#: executes agent-issued commands, so gating one shell command more tightly
#: than the run launcher would be a distinction without a difference. The
#: control that actually matters is ``require_local_caller``.
_BASH_DISABLED_ENV = "MO_AGENT_BASH_DISABLED"


def _workspace_root() -> Path:
    """The tree the file/git/bash tabs all describe.

    ``MO_TARGET_CWD`` is mini-ork's own lever for "where dispatched agents
    write", so a serving process that has one is describing that tree. Without
    it the server's own CWD is the answer — never ``Path.home()``, which would
    silently widen the surface to the operator's entire home directory.
    """
    raw = os.environ.get("MO_TARGET_CWD")
    candidate = Path(raw).expanduser() if raw and raw.strip() else Path.cwd()
    try:
        return candidate.resolve()
    except OSError:
        return Path.cwd().resolve()


#: Depth of the canvas's virtual working-dir prefix, `workspace/<name>`.
_SANDBOX_PREFIX_DEPTH = 2


def _without_sandbox_prefix(raw: str) -> str | None:
    """Strip the canvas's working-dir prefix, or ``None`` if there is not one.

    The canvas addresses the tree through the *agent-server's* home:
    ``getGitPath`` returns the conversation's ``workspace.working_dir`` (or
    ``workspace/project``) and callers prepend it to a repo-relative path — the
    diff tab asks for ``workspace/project/mini_ork/web/app.py``. mini-ork has no
    such home; its workspace root *is* the repo, so that prefix is the canvas's
    name for the root and has to come off.

    Applied only after the path as given fails to resolve, so a repository that
    really contains a ``workspace/project/`` directory stays addressable by its
    true path. Only relative paths are considered: an absolute
    ``/workspace/project/x`` is a foreign filesystem path, and silently
    rebasing it onto the root would make an escape attempt indistinguishable
    from a legitimate read.
    """
    parts = Path(raw).parts
    if len(parts) > _SANDBOX_PREFIX_DEPTH and parts[0] == "workspace":
        return str(Path(*parts[_SANDBOX_PREFIX_DEPTH:]))
    return None


def _resolve_within(
    root: Path, raw: str | None, *, label: str = "path", missing_ok: bool = True
) -> Path:
    """Resolve a client path and refuse anything outside ``root``.

    An absent or empty value means the root itself, which is what the canvas
    sends for "the working directory" (``GET /api/git/changes?path=workspace``
    and friends routinely name a directory that only exists inside a real
    OpenHands sandbox).

    That last point is why a *missing* path also resolves to the root instead
    of 404ing. The canvas derives the working dir from the conversation and
    sends it verbatim; against mini-ork's target CWD those names do not exist.
    A 404 there would blank the changes panel on a repository that has changes,
    and a wrong-but-empty 200 would do the same thing more quietly. Answering
    about the root — the tree the run actually writes to — is the reading that
    matches what the operator is looking at.

    ``missing_ok=False`` is the other half of that policy, for callers naming a
    *file* rather than a directory: the fileserver must 404 a file that is not
    there, since answering its directory listing would render a folder where
    the user asked for a document.
    """
    if raw is None or not raw.strip():
        return root
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"unresolvable {label}: {raw}") from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise HTTPException(status_code=400, detail=f"{label} escapes the workspace")
    if not resolved.exists():
        without_prefix = _without_sandbox_prefix(raw)
        if without_prefix is not None:
            retry = (root / without_prefix).resolve()
            if (retry == root or retry.is_relative_to(root)) and retry.exists():
                return retry
        if missing_ok:
            return root
        raise HTTPException(status_code=404, detail=f"no such {label} in the workspace: {raw}")
    return resolved


def _relative(root: Path, path: Path) -> str:
    """A path as the canvas wants it: relative to the workspace root."""
    if path == root:
        return "."
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


@router.get("/api/file/home")
def file_home() -> dict[str, Any]:
    """The absolute anchor the canvas resolves relative working dirs against.

    ``favorites``/``locations`` are the sidebar shortcuts of a desktop file
    picker. mini-ork has no such bookmarks, and an invented one would be a
    click that leads nowhere, so both are honestly empty — which is also what
    the canvas renders as "no shortcuts", the same as a fresh agent-server.
    """
    root = _workspace_root()
    return {"home": str(root), "favorites": [], "locations": []}


@router.get("/api/file/search_subdirs")
def search_subdirs(path: str = Query(default="")) -> dict[str, Any]:
    """Immediate subdirectories of ``path``, for the workspace directory picker.

    Non-directories are omitted rather than flagged: the picker only ever
    offers things it can descend into. ``next_page_id`` is always null because
    a single directory listing is returned whole — the canvas treats null as
    "no more pages", which is the truth here.
    """
    root = _workspace_root()
    base = _resolve_within(root, path)
    items: list[dict[str, str]] = []
    if base.is_dir():
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                items.append({"name": child.name, "path": _relative(root, child)})
    return {"path": _relative(root, base), "items": items, "subdirs": items, "next_page_id": None}


@router.get("/api/conversations/{conversation_id}/workspace/{file_path:path}")
def conversation_workspace_file(conversation_id: str, file_path: str) -> Any:
    """Serve one file out of the workspace — the canvas's static fileserver.

    ``RemoteWorkspace.startWorkspaceSession`` builds this URL client-side from
    the host and the conversation id and ignores the session POST's body, so
    this is the ONLY way the Files tab can read a file: a text preview fetches
    it and decodes the bytes, and an image/PDF preview points an ``<img>`` or
    ``<iframe>`` at it. The 404 the unserved path produced was per-file and per
    click, which is why it read as a broken panel rather than a missing feature.

    ``conversation_id`` is accepted and not used: the workspace is one tree
    shared by every conversation (see ``_workspace_root``), so keying it off the
    id would invent a per-conversation sandbox mini-ork does not have and would
    make a conversation that was never registered unable to read its own files.

    A directory (or the empty path) answers a listing rather than 404 —
    ``joinWorkspaceUrl`` documents that the caller may pass no path at all and
    expects the server to fall back. Content type comes from the extension
    because nothing else can know it here, and a wrong type is what turns a
    rendered ``<img>`` into a download.
    """
    import mimetypes

    from fastapi.responses import FileResponse, HTMLResponse

    del conversation_id
    root = _workspace_root()
    target = _resolve_within(root, file_path, label="file", missing_ok=False)
    if not target.is_dir():
        media_type = _MEDIA_TYPES.get(target.suffix.lower())
        if media_type is None:
            media_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(target, media_type=media_type or "application/octet-stream")
    rows = "".join(
        f'<li><a href="{_relative(root, child)}">{child.name}</a></li>'
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower())
    )
    return HTMLResponse(f"<!DOCTYPE html><title>{_relative(root, target)}</title><ul>{rows}</ul>")


@router.post("/api/skills")
def list_skills() -> dict[str, Any]:
    """The workspace skills list.

    mini-ork has no skill marketplace — its recipes under ``recipes/<slug>/``
    are the reusable-unit mechanism, and they are versioned with the repo
    rather than installed into a workspace. An empty list is therefore the
    accurate answer, not a placeholder: the canvas renders it as "no skills
    installed", which is exactly the situation.

    POST rather than GET is the canvas's choice (it sends a body carrying the
    runtime scope); the route is a pure read and ignores it.
    """
    return {"skills": []}


@router.get("/api/automation/sdk-version")
def automation_sdk_version() -> dict[str, Any]:
    """Automation runtime version, reported as the protocol we actually speak.

    This is a capability probe: the automation UI compares it against the
    versions it knows how to drive. Answering with ``AGENT_SERVER_PROTOCOL_VERSION``
    is the honest number — it is the same handshake ``/server_info`` already
    advertises — rather than a padded value that would unlock automations this
    shim cannot run.
    """
    from .agent_server import AGENT_SERVER_PROTOCOL_VERSION

    return {"sdk_version": AGENT_SERVER_PROTOCOL_VERSION, "version": AGENT_SERVER_PROTOCOL_VERSION}


@router.post("/api/auth/workspace-session")
def start_workspace_session() -> dict[str, Any]:
    """Mint the workspace static-asset session.

    The caller (``RemoteWorkspace.startWorkspaceSession``) posts here purely
    for the ``Set-Cookie`` side effect and then builds the file URL itself,
    client-side — the body is discarded. mini-ork serves no workspace static
    fileserver, so there is no cookie to mint and no files to serve at the URL
    the caller will construct.

    So this answers 200 with ``served: false`` and an empty ``base_url``
    rather than a plausible-looking prefix. A 200 here is load-bearing: the
    canvas gates its file-preview ``<iframe>`` on the session query having
    succeeded, and a 405 (what the unserved path used to produce) surfaces as
    an error toast on every conversation the user opens. The empty
    ``base_url`` is the honesty: callers that join a path onto it get nothing
    to render, which is true, instead of a URL that 404s one hop later.
    """
    return {"base_url": "", "served": False, "detail": "mini-ork serves no workspace fileserver"}


@router.delete("/api/auth/workspace-session")
def end_workspace_session() -> Any:
    """Teardown counterpart. 204, since there is no cookie to clear."""
    from fastapi import Response

    return Response(status_code=204)


def _git(root: Path, args: list[str]) -> str:
    """Run a read-only git command in ``root``; raise 409 when there is no repo.

    409 rather than 404 so the canvas's "server too old, hide the section"
    branch does not swallow it — an absent repository is a fact about this
    workspace, not a missing endpoint, and conflating the two would hide a
    broken panel behind a plausible-looking empty one.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail="git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=409, detail="git timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "not a git repository"
        raise HTTPException(status_code=409, detail=detail)
    return proc.stdout[:_GIT_MAX_OUTPUT_BYTES].decode("utf-8", "replace")


@router.get("/api/git/changes")
def git_changes(path: str = Query(default="")) -> list[dict[str, str]]:
    """Working-tree changes, in the canvas's ``{path, status}`` vocabulary.

    ``AgentServerGitService`` maps the server's statuses through
    ``mapAnyGitStatusToClientStatus``, which is why the wire values are the
    agent-server's ``UPDATED``/``ADDED``/``DELETED`` rather than git's letters.
    Renames arrive as ``R  old -> new``; the destination is what the diff and
    the editor address, so that is the path reported.
    """
    root = _workspace_root()
    target = _resolve_within(root, path)
    out = _git(target, ["status", "--porcelain", "-z", "--untracked-files=all"])
    # -z separates entries with NUL and, for renames, puts the two paths in
    # consecutive fields — parsing on newlines would corrupt any path with a
    # space in it, which is common enough to matter.
    fields = [f for f in out.split("\0") if f]
    changes: list[dict[str, str]] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            if i < len(fields):
                name = fields[i]
                i += 1
        if code.strip() == "??":
            status = "ADDED"
        elif "D" in code:
            status = "DELETED"
        elif "A" in code:
            status = "ADDED"
        else:
            status = "UPDATED"
        changes.append({"path": name, "status": status})
    return changes


@router.get("/api/git/diff")
def git_diff(path: str = Query(default="")) -> dict[str, str]:
    """Before/after text for one file, as the canvas's split diff editor wants.

    ``git diff`` speaks in hunks; the editor wants two whole documents. So the
    two sides are reconstructed from HEAD and the working tree — the same
    ``original``/``modified`` pair the canvas's own mock returns, and the shape
    ``use-workspace-file-content`` feeds into the diff view.
    """
    root = _workspace_root()
    target = _resolve_within(root, path)
    if target == root or not target.is_file():
        rel = _relative(root, target)
        raise HTTPException(status_code=400, detail=f"not a file in the workspace: {rel}")
    rel = _relative(root, target)
    if rel == ".":
        raise HTTPException(status_code=400, detail="a directory has no diff")
    try:
        original = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            capture_output=True,
            timeout=20,
        ).stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        original = ""
    # A file git has never seen (added, or untracked) has no HEAD side; an
    # empty original is the correct before-image for "this did not exist".
    modified = target.read_text(encoding="utf-8", errors="replace")
    return {"original": original, "modified": modified}


@router.get("/api/git/commits")
def git_commits(
    path: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Recent commits on the current branch, newest first.

    Field names are the agent-server's snake_case (``short_sha``, not
    ``shortSha``) — the client renames on its side, and renaming here too would
    make the two conventions drift. ``has_more`` is derived from asking for one
    commit past the limit, so it is a fact rather than a guess.
    """
    root = _workspace_root()
    target = _resolve_within(root, path)
    out = _git(
        target,
        ["log", f"--max-count={limit + 1}", "--date=iso-strict", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1e"],
    )
    commits: list[dict[str, str]] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short_sha, subject, author, timestamp = parts
        commits.append(
            {
                "sha": sha,
                "short_sha": short_sha,
                "subject": subject,
                "author": author,
                "timestamp": timestamp,
            }
        )
    has_more = len(commits) > limit
    return {"commits": commits[:limit], "has_more": has_more}


@router.post(
    "/api/bash/execute_bash_command",
    dependencies=[Depends(require_local_caller)],
)
def execute_bash_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one shell command in the workspace and return its captured output.

    This is the canvas's runtime primitive and it is called for far more than
    the terminal: the Files tab enumerates the working tree by sending a
    ``find`` here and parsing stdout. Without it the file tree renders empty
    rather than absent, which is why it is implemented rather than declined.

    **The command is deliberately not sandboxed.** mini-ork's whole premise is
    dispatching agents that edit files and run tools; the operator who started
    this server can already do all of that through ``POST /api/conversations``,
    and a shell filter here would be security theatre that breaks the real
    ``find``/``git``/``ls`` invocations the panels depend on. What IS enforced
    is who may call it (``require_local_caller`` — a foreign page cannot, since
    a cross-origin simple POST skips preflight and the side effect would
    otherwise land), where it runs (``cwd`` confined to the workspace root), and
    for how long (see ``_MAX_TIMEOUT_SECONDS``). ``MO_AGENT_BASH_DISABLED=1``
    turns the route off entirely for a deployment that wants that.

    Response shape is the SDK's ``{exit_code, stdout, stderr}``. A nonzero exit
    is a normal result, not an error: ``find`` exits 1 on a permission error
    and the canvas still wants the partial listing.
    """
    if os.environ.get(_BASH_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes"):
        raise HTTPException(status_code=403, detail="bash execution disabled by the operator")

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=400, detail="command is required")

    root = _workspace_root()
    cwd = _resolve_within(root, payload.get("cwd") if isinstance(payload.get("cwd"), str) else None, label="cwd")
    if not cwd.is_dir():
        cwd = root

    try:
        requested = float(payload.get("timeout") or 30)
    except (TypeError, ValueError):
        requested = 30.0
    timeout = max(1.0, min(requested, float(_MAX_TIMEOUT_SECONDS)))

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout is reported as an exit code, not an HTTP error: the canvas
        # renders stderr inline, and a raised exception would arrive as a
        # generic request failure with the partial output discarded.
        stdout = (exc.stdout or b"").decode("utf-8", "replace")
        return {
            "exit_code": 124,
            "stdout": stdout,
            "stderr": f"timed out after {timeout:g}s",
        }
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }
