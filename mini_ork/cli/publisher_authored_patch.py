"""Authored-patch resolution and private-index landing for the in-place publisher.

An in-place publish must commit ONLY the change the run itself authored — never
the whole file a peer also edited inside the run's window, and never through the
shared ``.git/index`` (a peer's ``git commit`` would sweep anything staged
there).  This module supplies the three halves of that contract:

* :func:`resolve_authored_patch` — reconstruct the run's own patch from the
  sources that can claim authorship:

  - a recovery **carry patch** (``salvage.patch`` / a ``--carry-patch`` named in
    ``run_profile.json``) that ``restore_carry_patch`` actually APPLIED — proven
    by ``carry-applied.json`` naming it with a matching sha256. A ``salvage.patch``
    that merely sits in the run dir is not a source: the operator never carried
    it, so committing it would publish the unreviewed round-2 work (ide-orca-f2b);
  - a fresh run's implementer ``Write``/``Edit``/``MultiEdit`` calls replayed IN
    ORDER from its session transcript (failed ``is_error`` tool calls are skipped:
    a rejected Edit followed by a successful retry is ordinary, not a peer edit);
  - the implementer's own **emitted unified diff** in ``impl-<node>.log`` — the
    text ``mini_ork.cli.execute.apply_impl_output`` applies with ``git apply``, so
    it is the implementer's patch, not a tree delta;
  - as a last resort, the run's **declared** ``files_changed``
    (``implementer-summary.json``), for the legacy shape that leaves no per-change
    evidence at all AND carries no ``pre-implementer-ref`` baseline.  With a
    baseline present the declared list is a whole-file TREE delta (HEAD →
    working tree), which cannot separate an in-window peer hunk inside a declared
    file, so it is not a source at all; the resolver abstains instead.  It never
    runs once real authorship evidence exists.

  ``framework-edit.diff`` and ``review-diff.patch`` are TREE deltas (``git diff``
  against ``pre-implementer-ref``) and are deliberately NOT sources here: a tree
  delta carries any peer's in-window hunk with it.  When the replay cannot
  reproduce a touched file's final content, a peer edited it between the
  implementer's Read and Write — the resolver abstains ``publish-unattributable``
  rather than guess, and does NOT fall back to the declared files.

* :func:`land_patch` — apply the authored patch through a PRIVATE index parented
  on the CURRENT HEAD and compare-and-swap the branch ref.  The shared
  ``.git/index`` and the working tree are never touched.

* :func:`report_foreign_hunks` — the cross-check: hunks in the working-tree delta
  that the authored patch does not carry are foreign.  They are listed in
  ``<run_dir>/publish-foreign-hunks.txt`` and left untouched.

The two declared abstain reasons are ``publish-unattributable`` (authorship
cannot be established) and ``publish-conflict`` (the patch no longer applies to
HEAD).  Neither ever falls back to a whole-file ``git add`` or a silent
``--3way`` merge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

__all__ = [
    "Abstain",
    "AuthoredPatch",
    "foreign_hunks",
    "land_patch",
    "report_foreign_hunks",
    "resolve_authored_patch",
]

_TOOL_NAMES = frozenset({"Write", "Edit", "MultiEdit"})
_SECTION_RE = re.compile(r"^diff --git ", re.M)
_EMITTED_DIFF_RE = re.compile(r"(^--- .*?)(?=\n```|\Z)", re.S | re.M)


class AuthoredPatch(NamedTuple):
    """A patch proven to come from the run itself, plus where it came from."""

    patch_text: str
    source: str


class Abstain(NamedTuple):
    """A refusal to publish, carrying the reason slug and a human detail."""

    reason: str
    detail: str = ""

    def __bool__(self) -> bool:  # an abstain is a falsy result, never a patch
        return False


class _Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# ─────────────────────────────────────────────────────────────────────────────
# process + path helpers
# ─────────────────────────────────────────────────────────────────────────────


def _b(text: str) -> bytes:
    """Encode a decoded-with-surrogateescape string back to its exact bytes."""
    return text.encode("utf-8", "surrogateescape")


def _git(repo: str, *args: str, index: str | None = None,
         input_bytes: bytes | None = None,
         env_extra: dict | None = None) -> _Result:
    """Run ``git -C repo <args>`` with a private index when ``index`` is given.

    ``GIT_INDEX_FILE`` is popped from the inherited environment first so an
    ambient value can never make an "indexed" call silently hit the shared
    index; the shared ``.git/index`` is only ever read through the default.
    """
    env = dict(os.environ)
    env.pop("GIT_INDEX_FILE", None)
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        input=input_bytes,
        capture_output=True,
        env=env,
        check=False,
    )
    return _Result(proc.returncode,
                   proc.stdout.decode("utf-8", "surrogateescape"),
                   proc.stderr.decode("utf-8", "surrogateescape"))


def _read_text(path: str) -> str:
    """Read a file byte-exactly: ``newline=""`` keeps CRLF as CRLF.

    Universal-newline mode would rewrite ``\r\n`` to ``\n``, so a carry patch for
    a CRLF file would stop applying (spurious ``publish-conflict``) and
    :func:`_verify_against_tree` would compare a normalised working-tree file with
    the byte-exact ``git show`` replay (spurious ``publish-unattributable``).
    """
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pre_impl_ref(run_dir: str) -> str:
    try:
        return _read_text(os.path.join(run_dir, "pre-implementer-ref")).strip()
    except OSError:
        return ""


def _target_repo(run_dir: str) -> str:
    """The repo the run edited, from ``run_profile.json`` (roots.target first)."""
    profile = _read_json(os.path.join(run_dir, "run_profile.json"))
    roots = profile.get("roots")
    if isinstance(roots, dict) and roots.get("target"):
        return str(roots["target"])
    if profile.get("target_repo"):
        return str(profile["target_repo"])
    return os.environ.get("MO_TARGET_CWD", "")


def _scope(run_dir: str) -> list[str]:
    """The run's declared file scope.  Reuses the prompt-injection parser so a
    second scope parser can never disagree with the first (kickoff §1)."""
    try:
        from mini_ork.memory.preferences import scope_paths  # noqa: PLC0415
        return [p for p in scope_paths(run_dir) if isinstance(p, str) and p]
    except Exception:
        return []


def _in_scope(rel: str, scope: list[str]) -> bool:
    """Whether repo-relative ``rel`` is inside the declared ``scope``.

    ``scope == []`` (nothing declared) admits everything. Kickoffs name files
    three ways, and all three count:

    * the repo-relative path (``crates/mini_ork_ui/src/page.rs``);
    * a directory (``crates/mini_ork_ui/src/``): every path under it;
    * a bare file name listed under a directory heading (``page.rs`` in
      "Files in scope (under `crates/mini_ork_ui/src/`)"): matched by its
      trailing path segments, so ``page.rs`` admits ``…/src/page.rs`` and
      ``src/page.rs`` admits ``…/ui/src/page.rs``.
    """
    if not scope:
        return True
    for entry in scope:
        entry = entry.strip().lstrip("./")
        if not entry:
            continue
        if rel == entry:
            return True
        if rel.startswith(entry.rstrip("/") + "/"):
            return True
        if rel.endswith("/" + entry):
            return True
    return False


def _under(path: str, real_dir: str) -> bool:
    """True when ``path`` resolves to ``real_dir`` itself or a child of it."""
    if not real_dir:
        return False
    real = os.path.realpath(path)
    return real == real_dir or real.startswith(real_dir + os.sep)


def _repo_rel(path: str, real_repo: str) -> str | None:
    """Repo-relative POSIX path for ``path``, or None when it escapes the repo.

    This is the strict-child path validation for the authored source: only a
    file proven to live inside the target repo can ever enter the patch.
    """
    if not real_repo:
        return None
    if not os.path.isabs(path):
        path = os.path.join(real_repo, path)
    real = os.path.realpath(path)
    if not real.startswith(real_repo + os.sep):
        return None
    rel = os.path.relpath(real, real_repo).replace(os.sep, "/")
    return rel if rel and not rel.startswith("../") else None


# ─────────────────────────────────────────────────────────────────────────────
# patch text helpers
# ─────────────────────────────────────────────────────────────────────────────


def _patch_sections(patch_text: str) -> list[str]:
    """Split a git patch into per-file sections, keeping the ``diff --git`` line."""
    if not patch_text or not patch_text.strip():
        return []
    parts = _SECTION_RE.split(patch_text)
    if len(parts) > 1:
        return ["diff --git " + part for part in parts[1:]]
    return [patch_text]


def _section_path(section: str) -> str:
    """The repo-relative path a patch section touches (b/ path, else a/ path).

    The scan stops at the first ``@@``: hunk BODIES can hold lines that merely
    look like headers (a deleted ``-- comment`` line in a SQL/Lua/Haskell file
    starts with ``--- ``), and reading one of those as a header would silently
    drop the section from the scope filter.
    """
    plus = minus = ""
    for line in section.splitlines():
        if line.startswith("@@"):
            break
        if line.startswith("+++ "):
            plus = line[4:].strip()
        elif line.startswith("--- "):
            minus = line[4:].strip()
    path = plus if plus and plus != "/dev/null" else minus
    for prefix in ("b/", "a/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return "" if path in ("", "/dev/null") else path


def _paths_in_patch(patch_text: str) -> list[str]:
    out: list[str] = []
    for section in _patch_sections(patch_text):
        path = _section_path(section)
        if path and path not in out:
            out.append(path)
    return out


def _filter_patch_scope(patch_text: str, scope: list[str]) -> str:
    """Keep only the sections whose paths are all inside ``scope``.

    ``scope == []`` means "no declared scope" — the patch is returned whole
    (kickoff §1: do not filter when the run declared nothing).
    """
    if not scope:
        return patch_text
    kept = []
    for section in _patch_sections(patch_text):
        paths = [p for p in (_section_path(section),) if p]
        if paths and all(_in_scope(p, scope) for p in paths):
            kept.append(section.rstrip("\n"))
    return "\n".join(kept) + "\n" if kept else ""


def _unsafe_path(patch_text: str) -> str:
    """First patch path that is absolute or escapes the repo via ``..``."""
    for path in _paths_in_patch(patch_text):
        if os.path.isabs(path) or path == ".." or path.startswith("../") or "/../" in path:
            return path
    return ""


def _change_lines_by_path(patch_text: str) -> dict[str, list[str]]:
    """Map each touched path to its changed lines (``+``/``-`` payloads in order).

    Line level, not hunk level: git merges two changes into one hunk whenever the
    unchanged gap between them is <= 2*context, so a hunk-keyed comparison would
    brand the run's own change foreign the moment a peer edits nearby.  Comparing
    the changed lines isolates the peer's content exactly.
    """
    result: dict[str, list[str]] = {}
    for section in _patch_sections(patch_text):
        path = _section_path(section)
        if not path:
            continue
        changed: list[str] = []
        in_hunk = False
        for line in section.splitlines():
            if line.startswith("@@"):
                in_hunk = True
                continue
            if in_hunk and line[:1] in ("+", "-"):
                changed.append(line)
        if changed:
            result.setdefault(path, []).extend(changed)
    return result


def foreign_hunks(tree_patch: str, authored_patch: str) -> list[str]:
    """The changed lines in ``tree_patch`` that ``authored_patch`` does not carry.

    Pure function (no I/O) so the cross-check is unit-testable on its own.  Each
    result is ``"<path>: <+|-><line>"``; a line the authored patch also changes is
    consumed once, so duplicates on both sides cancel rather than accumulate.
    """
    have = {path: list(lines) for path, lines in _change_lines_by_path(authored_patch).items()}
    out: list[str] = []
    for path, lines in _change_lines_by_path(tree_patch).items():
        authored_here = have.get(path, [])
        for line in lines:
            if line in authored_here:
                authored_here.remove(line)
            else:
                out.append(f"{path}: {line}")
    return out


def report_foreign_hunks(repo: str, run_dir: str, authored_patch: str,
                         pre_impl_ref: str | None = None) -> list[str]:
    """List (and record) the working-tree hunks the authored patch does not carry.

    The tree delta is computed against ``pre-implementer-ref`` through a PRIVATE
    index, so the shared ``.git/index`` is never written.  Results are written to
    ``<run_dir>/publish-foreign-hunks.txt`` when ``run_dir`` is set.
    """
    ref = pre_impl_ref or (_pre_impl_ref(run_dir) if run_dir else "")
    if not ref or not repo:
        return []
    tmp = tempfile.mkdtemp(prefix="mo-foreign-")
    try:
        index = os.path.join(tmp, "index")
        if _git(repo, "read-tree", ref, index=index).returncode != 0:
            return []
        delta = _git(repo, "diff", "--no-color", "--no-ext-diff", ref, index=index)
        if delta.returncode != 0:
            return []
        found = foreign_hunks(delta.stdout, authored_patch)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if run_dir:
        try:
            with open(os.path.join(run_dir, "publish-foreign-hunks.txt"), "w",
                      encoding="utf-8") as fh:
                for line in found:
                    fh.write(line + "\n")
        except OSError:
            pass
    return found


# ─────────────────────────────────────────────────────────────────────────────
# carry patch (a recover)
# ─────────────────────────────────────────────────────────────────────────────


def _carry_patch(run_dir: str) -> tuple[str, str] | None:
    """Resolve + read the carry patch, or None when the run carried none.

    The name comes from ``run_profile.json`` (``carry_patch`` /
    ``recovery.carry_patch``); resolution order (``--carry-patch`` name >
    ``salvage.patch``) is delegated to ``mini_ork.recovery.restore`` so there is
    exactly one carry-patch resolver in the tree.
    """
    profile = _read_json(os.path.join(run_dir, "run_profile.json"))
    cli = ""
    if isinstance(profile.get("carry_patch"), str) and profile["carry_patch"]:
        cli = profile["carry_patch"]
    else:
        recovery = profile.get("recovery")
        if isinstance(recovery, dict) and isinstance(recovery.get("carry_patch"), str):
            cli = recovery["carry_patch"]
    path: str | None = None
    try:
        from mini_ork.recovery.restore import _resolve_patch_path  # noqa: PLC0415
        path = _resolve_patch_path(run_dir, cli or None, None)
    except Exception:
        path = None
    if not path:
        for cand in ([cli] if cli else []) + ["salvage.patch"]:
            candidate = cand if os.path.isabs(cand) else os.path.join(run_dir, cand)
            if os.path.isfile(candidate):
                path = candidate
                break
    if not path or not os.path.isfile(path):
        return None
    try:
        return _read_text(path), os.path.basename(path)
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# transcript replay (a fresh run)
# ─────────────────────────────────────────────────────────────────────────────


def _transcript_candidates(run_dir: str) -> list[str]:
    """Implementer transcript paths, most-specific first.

    The persisted SDK transcript named by ``.sessions/implementer.session`` and
    the raw ``agent-implementer.live.jsonl`` sidecar are the SAME calls in two
    frames, so they are tried in turn (never concatenated — that would replay
    every edit twice).  Only when neither exists do the other run transcripts get
    a look, oldest first.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path and path not in seen and os.path.isfile(path) and os.path.getsize(path) > 0:
            seen.add(path)
            out.append(path)

    sid_file = os.path.join(run_dir, ".sessions", "implementer.session")
    try:
        sid = _read_text(sid_file).strip()
    except OSError:
        sid = ""
    if sid:
        add(os.path.join(run_dir, "sessions", f"{sid}.jsonl"))
    add(os.path.join(run_dir, "agent-implementer.live.jsonl"))
    if not out:
        sessions = os.path.join(run_dir, "sessions")
        try:
            names = sorted(os.listdir(sessions))
        except OSError:
            names = []
        for name in names:
            if name.endswith(".jsonl"):
                add(os.path.join(sessions, name))
    return out


def _content_blocks(obj: object):
    """Yield the content blocks of one transcript record, in either frame.

    Handles both frames seen under a run dir: the SDK transcript record
    (``{"type": "assistant", "message": {"content": [...]}}``) and the live
    sidecar wrapper (``{"seq", "stream", "t", "line": "<raw stdout json>"}``).
    """
    if not isinstance(obj, dict):
        return
    inner = obj.get("line")
    if isinstance(inner, str):
        try:
            nested = json.loads(inner)
        except ValueError:
            return
        yield from _content_blocks(nested)
        return
    message = obj.get("message")
    blocks = message.get("content") if isinstance(message, dict) else obj.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict):
                yield block


def _tool_uses_in(obj: object):
    """Yield ``(id, name, input)`` for Write/Edit/MultiEdit blocks in one record."""
    for block in _content_blocks(obj):
        if block.get("type") == "tool_use" and block.get("name") in _TOOL_NAMES:
            yield block.get("id"), block.get("name"), (block.get("input") or {})


def _records_in(path: str):
    """Yield each parsed JSON record of a JSONL transcript (bad lines skipped)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _failed_tool_use_ids(path: str) -> set[str]:
    """Ids of tool calls whose ``tool_result`` came back ``is_error``.

    A rejected call changed nothing, so replaying it would desynchronise the
    replay from the tree: an ordinary "Found 2 matches …" Edit followed by a
    successful retry would look exactly like a peer editing between the Read and
    Write, and abstain ``publish-unattributable`` against an innocent peer.
    """
    failed: set[str] = set()
    for record in _records_in(path):
        for block in _content_blocks(record):
            if block.get("type") == "tool_result" and block.get("is_error") is True:
                tid = block.get("tool_use_id")
                if isinstance(tid, str) and tid:
                    failed.add(tid)
    return failed


def _record_ts(record: object) -> float | None:
    """Epoch seconds of an SDK transcript record's ISO ``timestamp``, or None."""
    raw = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        from datetime import datetime  # noqa: PLC0415
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _collect_edits(path: str, real_repo: str, scope: list[str], run_real: str = "",
                   since: float | None = None):
    """Ordered in-repo, in-scope, non-failed (rel, tool, input) edits from a transcript.

    ``since`` (epoch seconds) keeps only the calls recorded after it: a resumed
    session appends a recover's edits to the transcript that already holds the
    earlier attempt's.

    Edits inside ``run_real`` are dropped: a run dir that lives inside the target
    repo (``<repo>/.mini-ork/runs/<id>``, gitignored) holds the implementer's own
    artifacts, and its ``Write`` of ``impl-<node>.log`` is not a change the run
    authored in the repo.  ``git apply --cached`` ignores ``.gitignore``, so
    without this the publish would commit the run artifact — or, when the runtime
    later rewrites the log, abstain ``publish-unattributable`` and blame a peer.
    """
    failed = _failed_tool_use_ids(path)
    edits = []
    for record in _records_in(path):
        if since is not None:
            # Only calls made after ``since`` (a recover's own attempt). A record
            # without a timestamp cannot be placed, so it is not counted.
            ts = _record_ts(record)
            if ts is None or ts <= since:
                continue
        for tid, name, inp in _tool_uses_in(record):
            if tid is not None and tid in failed:
                continue
            fp = inp.get("file_path")
            if not isinstance(fp, str) or not fp:
                continue
            rel = _repo_rel(fp, real_repo)
            if rel is None:
                continue
            if run_real and _under(os.path.join(real_repo, rel), run_real):
                continue
            if not _in_scope(rel, scope):
                continue
            edits.append((rel, name, inp))
    return edits


def _base_content(repo: str, base_ref: str, rel: str) -> str | None:
    """The file's content at ``base_ref``, or None when the file is new there."""
    show = _git(repo, "show", f"{base_ref}:{rel}")
    return show.stdout if show.returncode == 0 else None


def _apply_edit(content: str | None, edit: dict) -> str | None:
    """Apply one Write/Edit/MultiEdit payload; None when it cannot be attributed.

    An Edit whose ``old_string`` is absent or non-unique cannot be replayed: the
    file the implementer read is not the file the patch base holds, which is
    exactly the peer-edit-between-Read-and-Write case.  Refuse rather than guess.
    """
    if content is None:
        return None
    edits = edit.get("edits")
    if isinstance(edits, list):  # MultiEdit
        for sub in edits:
            if not isinstance(sub, dict):
                return None
            content = _apply_edit(content, sub)
            if content is None:
                return None
        return content
    old = edit.get("old_string")
    new = edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        return None
    if edit.get("replace_all"):
        return content.replace(old, new) if old in content else None
    if content.count(old) != 1:
        return None
    return content.replace(old, new, 1)


def _replay(repo: str, base_ref: str, edits) -> dict[str, str | None] | Abstain:
    """Replay ``edits`` onto the ``base_ref`` tree, in order."""
    files: dict[str, str | None] = {}
    for rel, name, inp in edits:
        if rel not in files:
            files[rel] = _base_content(repo, base_ref, rel)
        if name == "Write":
            files[rel] = inp.get("content") if isinstance(inp.get("content"), str) else ""
            continue
        replayed = _apply_edit(files[rel], inp)
        if replayed is None:
            return Abstain(
                "publish-unattributable",
                f"{rel}: the implementer's {name} does not replay against "
                f"{base_ref[:12]} — a peer edited the file between its Read and Write",
            )
        files[rel] = replayed
    return files


def _verify_against_tree(repo: str, files: dict[str, str | None]) -> Abstain | None:
    """Abstain unless every replayed file still matches the working tree."""
    for rel, content in files.items():
        try:
            actual: str | None = _read_text(os.path.join(repo, rel))
        except FileNotFoundError:
            actual = None
        except OSError:
            return Abstain("publish-unattributable",
                           f"{rel}: cannot read the working-tree file")
        if actual != content:
            return Abstain(
                "publish-unattributable",
                f"{rel}: the transcript replay does not reproduce the final content "
                "— a peer edited it inside the run's window",
            )
    return None


def _base_mode(repo: str, base_ref: str, rel: str) -> str:
    tree = _git(repo, "ls-tree", base_ref, "--", rel)
    if tree.returncode == 0 and tree.stdout.strip():
        mode = tree.stdout.split()[0]
        if mode in ("100644", "100755", "120000"):
            return mode
    return "100644"


def _build_patch(repo: str, base_ref: str, files: dict[str, str | None]):
    """A git patch (base_ref tree → replayed tree) built through a private index.

    Blob/tree plumbing is written to the object store (harmless); no ref and no
    index outside the temp dir is touched.
    """
    tmp = tempfile.mkdtemp(prefix="mo-authored-")
    try:
        idx_base = os.path.join(tmp, "index.base")
        idx_new = os.path.join(tmp, "index.new")
        for index in (idx_base, idx_new):
            read = _git(repo, "read-tree", base_ref, index=index)
            if read.returncode != 0:
                return Abstain("publish-unattributable",
                               f"read-tree {base_ref[:12]} failed: {read.stderr.strip()}")
        for rel, content in sorted(files.items()):
            if content is None:
                _git(repo, "update-index", "--force-remove", "--", rel, index=idx_new)
                continue
            blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=_b(content))
            if blob.returncode != 0:
                return Abstain("publish-unattributable", f"hash-object {rel} failed")
            mode = _base_mode(repo, base_ref, rel)
            update = _git(repo, "update-index", "--add", "--cacheinfo",
                          f"{mode},{blob.stdout.strip()},{rel}", index=idx_new)
            if update.returncode != 0:
                return Abstain("publish-unattributable",
                               f"update-index {rel} failed: {update.stderr.strip()}")
        tree_base = _git(repo, "write-tree", index=idx_base)
        tree_new = _git(repo, "write-tree", index=idx_new)
        if tree_base.returncode != 0 or tree_new.returncode != 0:
            return Abstain("publish-unattributable", "write-tree failed")
        diff = _git(repo, "diff-tree", "-p", "-r", "--no-color", "--no-ext-diff",
                    tree_base.stdout.strip(), tree_new.stdout.strip())
        if diff.returncode != 0:
            return Abstain("publish-unattributable",
                           f"diff-tree failed: {diff.stderr.strip()}")
        return diff.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _replay_edits(repo: str, base_ref: str, edits):
    """Replay ``edits`` and prove the result against the tree; the patch, or why not."""
    replayed = _replay(repo, base_ref, edits)
    if isinstance(replayed, Abstain):
        return replayed
    mismatch = _verify_against_tree(repo, replayed)
    if mismatch is not None:
        return mismatch
    patch = _build_patch(repo, base_ref, replayed)
    if isinstance(patch, Abstain):
        return patch
    if not patch.strip():
        return Abstain("publish-no-authored-patch",
                       "the implementer's replay produced no change")
    return AuthoredPatch(patch, "transcript-replay")


def _session_contributions(run_dir: str, real_repo: str, scope: list[str]):
    """Persisted session transcripts that edited the repo, oldest first.

    Only sessions with at least one in-repo, in-scope Write/Edit contribute, so a
    lens or reviewer transcript (which only reads) never joins the chain.
    """
    sessions = os.path.join(run_dir, "sessions")
    try:
        names = sorted(os.listdir(sessions))
    except OSError:
        return []
    run_real = os.path.realpath(run_dir)
    found = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(sessions, name)
        edits = _collect_edits(path, real_repo, scope, run_real)
        if edits:
            found.append((path, edits))
    try:
        found.sort(key=lambda item: os.path.getmtime(item[0]))
    except OSError:
        pass
    return found


def _resolve_from_transcripts(run_dir: str, repo: str, base_ref: str,
                              scope: list[str]):
    """Try each transcript until one reconciles, then their ordered chain.

    Returns ``(result, evidence)``.  ``evidence`` is True when a transcript held
    implementer Write/Edit calls for this repo — whether or not they reconciled.
    The caller must abstain rather than fall back once there is evidence to judge:
    a transcript that does not reproduce the tree IS the peer-edit diagnosis.
    """
    real_repo = os.path.realpath(repo)
    run_real = os.path.realpath(run_dir)
    last: Abstain | None = None
    evidence = False
    for path in _transcript_candidates(run_dir):
        edits = _collect_edits(path, real_repo, scope, run_real)
        if not edits:
            continue
        evidence = True
        outcome = _replay_edits(repo, base_ref, edits)
        if isinstance(outcome, AuthoredPatch):
            return outcome, evidence
        last = outcome
    # A revise round dispatches a FRESH session, so its transcript holds only that
    # round's edits — and those were made on top of the previous round's, which the
    # run's base does not contain, so replaying it alone can never reconcile for a
    # file an earlier round created.  Replay every session the run produced that
    # edited the repo, oldest first: still only the implementer's own calls (in the
    # order it made them), and the tree comparison proves the chain.
    contributions = _session_contributions(run_dir, real_repo, scope)
    if len(contributions) > 1:
        chained = [edit for _, edits in contributions for edit in edits]
        evidence = True
        outcome = _replay_edits(repo, base_ref, chained)
        if isinstance(outcome, AuthoredPatch):
            return outcome, evidence
        last = outcome
    if last is None:
        last = Abstain(
            "publish-no-authored-patch",
            "no implementer Write/Edit calls for this repo in the run's transcripts",
        )
    return last, evidence


# ─────────────────────────────────────────────────────────────────────────────
# emitted diff (a text/codex lane) and declared files (last resort)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_unified_diff(text: str) -> str:
    """The unified diff the implementer emitted in its log, or ``""``.

    Same extraction ``mini_ork.cli.execute.apply_impl_output`` uses before it
    applies the text with ``git apply``, so the authored patch is exactly the
    change the executor already put on the tree.
    """
    if not text:
        return ""
    if not (re.search(r"^--- (a/|/dev/null)", text, re.M)
            and re.search(r"^\+\+\+ b/", text, re.M)):
        return ""
    match = _EMITTED_DIFF_RE.search(text)
    return match.group(1) if match else ""


def _impl_logs(run_dir: str) -> list[str]:
    """Implementer output logs, newest first (the summary names the exact one)."""
    out: list[str] = []
    summary = _read_json(os.path.join(run_dir, "implementer-summary.json"))
    named = summary.get("implementation_log")
    if isinstance(named, str) and named and os.path.isfile(named):
        out.append(named)
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        names = []
    for name in names:
        if not (name.startswith("impl-") and name.endswith(".log")):
            continue
        path = os.path.join(run_dir, name)
        if path not in out and os.path.isfile(path):
            out.append(path)
    try:
        return sorted(out, key=os.path.getmtime, reverse=True)
    except OSError:
        return out


def _resolve_from_emitted_diff(run_dir: str, repo: str, scope: list[str]):
    """The implementer's own emitted unified diff, or None when no log has one.

    Text/codex lanes print a diff instead of calling Write/Edit, and the executor
    applies that exact text (:func:`_extract_unified_diff`).  It is the
    implementer's own patch — never a tree delta — so no replay is needed.
    """
    real_repo = os.path.realpath(repo)
    for path in _impl_logs(run_dir):
        try:
            text = _read_text(path)
        except OSError:
            continue
        diff = _extract_unified_diff(text)
        if not diff:
            continue
        # Safety before scope: a path that escapes the repo is a hard signal and
        # deserves its own reason, not a silent drop by the scope filter.
        unsafe = _unsafe_path(diff)
        if unsafe:
            return Abstain("publish-unattributable",
                           f"the implementer log {os.path.basename(path)} touches a path "
                           f"outside the repo: {unsafe}")
        if scope:
            diff = _filter_patch_scope(diff, scope)
            if not diff.strip():
                continue
        for rel in _paths_in_patch(diff):
            if _repo_rel(rel, real_repo) is None:
                return Abstain("publish-unattributable",
                               f"the implementer log {os.path.basename(path)} touches "
                               f"{rel}, which is not inside the target repo")
        return AuthoredPatch(diff, f"emitted-diff:{os.path.basename(path)}")
    return None


def _declared_files(run_dir: str) -> list[str]:
    """The ``files_changed`` list the run's own summary declares."""
    summary = _read_json(os.path.join(run_dir, "implementer-summary.json"))
    declared = summary.get("files_changed")
    if not isinstance(declared, list):
        return []
    return [f for f in declared if isinstance(f, str) and f]


def _resolve_from_declared_files(run_dir: str, repo: str, scope: list[str], base: str):
    """Last resort: the run's declared files, taken whole from the working tree.

    Used ONLY when the run left no authorship evidence at all (no carry patch, no
    transcript Write/Edit, no emitted diff) yet declared ``files_changed`` — the
    M1 empty-outputs in-place path.  It is a whole-file TREE delta (``base`` →
    working tree), so it cannot separate a peer's in-window hunk inside a declared
    file.  The CALLER must therefore only reach here for a run with NO
    ``pre-implementer-ref`` baseline (the legacy shape); when a baseline exists the
    tree delta is foreign and the resolver abstains instead (kickoff [c:0]).  The
    publisher still logs the declared-files source as a warning.  Returns None when
    nothing is declared.

    ``base`` is the CURRENT HEAD: ``pre-implementer-ref`` (when the legacy run
    happened to carry one) is a ``git stash create`` snapshot of the already-dirty
    tree, so a diff from it to the (dirty + implementer) tree is empty and would
    abstain "nothing to commit".
    """
    declared = _declared_files(run_dir)
    if not declared:
        return None
    real_repo = os.path.realpath(repo)
    files: dict[str, str | None] = {}
    for raw in declared:
        rel = _repo_rel(raw, real_repo)
        if rel is None:
            continue  # outside the target repo — never committed
        if not _in_scope(rel, scope):
            continue
        try:
            files[rel] = _read_text(os.path.join(real_repo, rel))
        except OSError:
            files[rel] = None  # deleted by the implementer
    if not files:
        return Abstain("publish-unattributable",
                       "implementer-summary.json declares files_changed, but none is inside "
                       "the target repo and the run's declared scope")
    patch = _build_patch(repo, base, files)
    if isinstance(patch, Abstain):
        return patch
    if not patch.strip():
        return Abstain("publish-no-authored-patch",
                       "the declared files_changed matches the patch base — nothing to commit")
    return AuthoredPatch(patch, "declared-files:implementer-summary.json")


# ─────────────────────────────────────────────────────────────────────────────
# public API
# ─────────────────────────────────────────────────────────────────────────────


def _edits_since(run_dir: str, real_repo: str, scope: list[str], since: float):
    """The implementer's in-scope edits recorded after ``since``, oldest session first."""
    sessions = os.path.join(run_dir, "sessions")
    try:
        names = sorted(os.listdir(sessions))
    except OSError:
        return []
    run_real = os.path.realpath(run_dir)
    found = []
    for name in names:
        path = os.path.join(sessions, name)
        if not name.endswith(".jsonl"):
            continue
        try:
            if os.path.getmtime(path) <= since:
                continue
        except OSError:
            continue
        edits = _collect_edits(path, real_repo, scope, run_real, since=since)
        if edits:
            found.append((os.path.getmtime(path), edits))
    found.sort(key=lambda item: item[0])
    return [edit for _, edits in found for edit in edits]


def _attempt_started(run_dir: str, fallback: float) -> float:
    """When the run's LATEST implementer attempt started (epoch seconds).

    A recover can be stopped and relaunched: an abandoned attempt's edits sit in
    the same resumed transcript but were discarded from the tree, so only the
    calls after the latest ``node_start`` of the implementer belong to the
    change being published. Falls back to ``fallback`` when the run's
    ``run_events`` cannot be read.
    """
    home = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    run_id = os.path.basename(os.path.abspath(run_dir))
    try:
        from mini_ork.web.db import db_for  # noqa: PLC0415
        rows = db_for(Path(home)).rows(
            "SELECT payload_json, created_at FROM run_events WHERE run_id = ? "
            "AND event_type = 'node_start' ORDER BY created_at DESC, rowid DESC",
            (run_id,))
    except Exception:  # noqa: BLE001 — no DB: the carry file's time is the floor
        return fallback
    for row in rows or []:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and (payload.get("node_type") == "implementer"
                                          or payload.get("node_id") == "implementer"):
            try:
                return max(float(row.get("created_at") or 0), fallback)
            except (TypeError, ValueError):
                return fallback
    return fallback


def _carry_applied(run_dir: str, name: str, text: str) -> bool:
    """Whether the run dir's carry patch was actually APPLIED to the tree.

    ``mini_ork.recovery.restore.restore_carry_patch`` writes
    ``carry-applied.json`` (the patch's basename + the sha256 of its bytes) when
    it lands a carry. A ``salvage.patch`` that merely sits in the run dir — the
    work the operator never carried — must not be published, so it is a source
    ONLY when this marker names it and the sha matches (the ide-orca-f2b
    incident: the publisher committed the unreviewed round-2 salvage).
    """
    record = _read_json(os.path.join(run_dir, "carry-applied.json"))
    if not record:
        return False
    if str(record.get("patch") or "") != name:
        return False
    expected = str(record.get("sha256") or "")
    if not expected:
        return False
    try:
        digest = hashlib.sha256(_b(text)).hexdigest()
    except Exception:  # noqa: BLE001 — an unencodable patch is not a proven carry
        return False
    return digest == expected


def _carry_applied_at(run_dir: str) -> float:
    """``carry-applied.json``'s ``applied_at`` (epoch seconds), else 0.0."""
    record = _read_json(os.path.join(run_dir, "carry-applied.json"))
    try:
        return float(record.get("applied_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rolled_back(run_dir: str) -> bool:
    """True when the run's rollback reset the tree to the base."""
    return os.path.isfile(os.path.join(run_dir, "rolled-back.json"))


def _rolled_back_at(run_dir: str) -> float:
    """``rolled-back.json``'s mtime (epoch seconds), else 0.0 — the epoch.

    Used as the floor when the run's ``node_start`` events cannot be read: an
    edit recorded before the rollback belongs to a discarded attempt.
    """
    try:
        return os.path.getmtime(os.path.join(run_dir, "rolled-back.json"))
    except OSError:
        return 0.0


def _carry_tree(repo: str, base_ref: str, carry_text: str) -> str | Abstain:
    """The tree of ``base_ref`` with the carry patch applied (private index)."""
    tmp = tempfile.mkdtemp(prefix="mo-carry-")
    index = os.path.join(tmp, "index")
    try:
        if _git(repo, "read-tree", base_ref, index=index).returncode != 0:
            return Abstain("publish-unattributable", f"read-tree {base_ref[:12]} failed")
        applied = _git(repo, "apply", "--cached", "-", index=index,
                       input_bytes=_b(carry_text))
        if applied.returncode != 0:
            return Abstain("publish-unattributable",
                           f"the carry patch does not apply to {base_ref[:12]}: "
                           f"{applied.stderr.strip()}")
        tree = _git(repo, "write-tree", index=index)
        if tree.returncode != 0:
            return Abstain("publish-unattributable", "write-tree of the carry tree failed")
        return tree.stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _compose_carry_and_edits(repo: str, base_ref: str, carry_text: str,
                             carry_name: str, edits) -> AuthoredPatch | Abstain:
    """A recover's authored patch: the carry patch, then the revived implementer's
    own edits replayed on top of it — proven against the working tree, and
    expressed against ``base_ref`` like every other authored patch."""
    tree = _carry_tree(repo, base_ref, carry_text)
    if isinstance(tree, Abstain):
        return tree
    replayed = _replay(repo, tree, edits)
    if isinstance(replayed, Abstain):
        return replayed
    files = dict(replayed)
    for rel in _paths_in_patch(carry_text):
        if rel not in files:
            files[rel] = _base_content(repo, tree, rel)
    mismatch = _verify_against_tree(repo, files)
    if mismatch is not None:
        return mismatch
    patch = _build_patch(repo, base_ref, files)
    if isinstance(patch, Abstain):
        return patch
    if not patch.strip():
        return Abstain("publish-no-authored-patch", "the carry patch and edits cancel out")
    return AuthoredPatch(patch, f"carry-patch:{carry_name}+transcript-replay")


def resolve_authored_patch(run_dir: str, repo: str | None = None) -> AuthoredPatch | Abstain:
    """The run's AUTHORED patch, or an :class:`Abstain` when it cannot be proven.

    Precedence: a recovery carry patch, a fresh run's transcript replay, the
    implementer's emitted diff, then its declared ``files_changed``.  The result is
    scoped to the run's declared files (``scope_paths``) and every path in it is
    validated to live inside the target repo.  ``repo`` is the publisher's already
    resolved target — pass it so the resolver cannot diverge from the repo the
    patch is landed in.
    """
    if not run_dir:
        return Abstain("publish-unattributable", "no run_dir — authorship cannot be established")
    run_dir = os.path.abspath(run_dir)
    scope = _scope(run_dir)

    carried = _carry_patch(run_dir)
    if carried is not None:
        text, name = carried
        # A carry patch counts as authored ONLY when the restore actually
        # APPLIED it (``carry-applied.json`` names it and its sha matches). A
        # ``salvage.patch`` that merely sits in the run dir is not a source —
        # committing it would publish the unreviewed salvage (ide-orca-f2b).
        if _carry_applied(run_dir, name, text):
            if scope:
                text = _filter_patch_scope(text, scope)
            if not text.strip():
                return Abstain("publish-no-authored-patch",
                               f"carry patch {name} has nothing inside the run's declared scope")
            unsafe = _unsafe_path(text)
            if unsafe:
                return Abstain("publish-unattributable",
                               f"carry patch {name} touches a path outside the repo: {unsafe}")
            # A revived implementer keeps working on top of the carry patch. Its
            # own later edits are part of the run's authored change: committing
            # the carry patch alone would publish less than the reviewer approved
            # and leave the rest as "foreign" hunks.
            carry_repo = repo or _target_repo(run_dir)
            base_ref = _pre_impl_ref(run_dir)
            carry_path = os.path.join(run_dir, name)
            if carry_repo and os.path.isdir(carry_repo) and base_ref and os.path.isfile(carry_path):
                since = _attempt_started(run_dir, _carry_applied_at(run_dir))
                later = _edits_since(run_dir, os.path.realpath(carry_repo), scope, since)
                if later:
                    return _compose_carry_and_edits(carry_repo, base_ref, text, name, later)
            return AuthoredPatch(text, f"carry-patch:{name}")
        # The carry was NOT applied: fall through. The rolled-back rule below
        # (or the normal replay chain) decides authorship from the tree.

    repo = repo or _target_repo(run_dir)
    if not repo or not os.path.isdir(repo):
        return Abstain("publish-unattributable", "no target repo in run_profile.json")
    base_ref = _pre_impl_ref(run_dir)

    # A rolled-back run with no applied carry: the rollback reset the tree to
    # the base before the LATEST implementer attempt, so only the edits recorded
    # after that attempt started belong to the change being published. Replaying
    # the whole transcript would resurrect an earlier, discarded attempt's edits
    # — never fall back to those sessions.
    if base_ref and _rolled_back(run_dir):
        since = _attempt_started(run_dir, _rolled_back_at(run_dir))
        later = _edits_since(run_dir, os.path.realpath(repo), scope, since)
        if later:
            return _replay_edits(repo, base_ref, later)
        return Abstain(
            "publish-no-authored-patch",
            "the run rolled back and no implementer edits after its latest attempt "
            "reconcile against the base",
        )

    # 1) the implementer's own Write/Edit calls, replayed in order.
    if base_ref:
        replayed, evidence = _resolve_from_transcripts(run_dir, repo, base_ref, scope)
        if isinstance(replayed, AuthoredPatch):
            return replayed
        if evidence:
            # Evidence existed and did not reconcile: a peer's in-window edit.
            # Falling back here would commit exactly the change we must not.
            return replayed

    # 2) the implementer's emitted diff (text/codex lanes print instead of editing).
    emitted = _resolve_from_emitted_diff(run_dir, repo, scope)
    if emitted is not None:
        return emitted

    # 3) declared files — the legacy shape only, and only with NO baseline.  With a
    #    pre-implementer-ref present the declared list is a whole-file TREE delta
    #    (HEAD → working tree) that cannot separate a peer's in-window hunk inside a
    #    declared file; taking it would commit exactly the change we must not
    #    (kickoff [c:0]: a tree delta is not a source, not even as a fallback).
    if not base_ref:
        declared = _resolve_from_declared_files(
            run_dir, repo, scope, _git(repo, "rev-parse", "HEAD").stdout.strip())
        if declared is not None:
            return declared
        return Abstain("publish-no-authored-patch",
                       "no implementer Write/Edit calls, no emitted implementer diff and no "
                       "declared files_changed for this repo in the run's artifacts")

    return Abstain("publish-no-authored-patch",
                   "the run carries a pre-implementer-ref baseline but no carry patch, no "
                   "implementer Write/Edit replay and no emitted implementer diff that "
                   "reconciles — the declared files_changed is a tree delta, not a source")


def land_patch(repo: str, patch_text: str, branch: str, *, message: str = "") -> str | Abstain:
    """Commit ``patch_text`` onto ``branch`` through a private index. Returns the sha.

    The parent is the CURRENT HEAD, never ``pre-implementer-ref``: a tree built
    from the latter and committed onto HEAD would orphan or revert every commit
    that landed in between.  ``branch`` empty means "the branch HEAD points at".

    On a clean ``git apply --cached --check`` the patch is applied to the private
    index, written to a tree, committed with ``-p HEAD``, and the branch ref is
    compare-and-swapped (``update-ref <ref> <new> <old>``).  A patch that no
    longer applies, or a ref that moved under the CAS, abstains
    ``publish-conflict`` with HEAD unchanged.  There is deliberately no ``--3way``
    auto-merge and no ``git add`` fallback.

    After the CAS the shared ``.git/index`` entries for the PUBLISHED paths
    (and only those) are reset to the new HEAD (``git reset -q -- <paths>``).
    Left alone, they would still hold the pre-publish blobs: ``git status``
    would show the publish as a staged revert, and a peer's next
    ``git add other && git commit`` would commit that revert. Every other index
    entry, and the whole working tree, is left as it was; a peer's staged hunks
    inside a published file are unstaged, never lost (they stay in the tree).
    """
    if not isinstance(patch_text, str) or not patch_text.strip():
        return Abstain("publish-empty", "the authored patch is empty")
    if not repo or not os.path.isdir(repo):
        return Abstain("publish-conflict", f"target repo is not a directory: {repo!r}")

    branch = (branch or "").strip()
    if not branch:
        symbolic = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
    if not branch:
        return Abstain("publish-conflict", "HEAD is detached — no branch ref to move")

    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        return Abstain("publish-conflict", "cannot resolve HEAD")
    old = head.stdout.strip()

    tmp = tempfile.mkdtemp(prefix="mo-land-")
    index = os.path.join(tmp, "index")
    try:
        read = _git(repo, "read-tree", old, index=index)
        if read.returncode != 0:
            return Abstain("publish-conflict", f"read-tree HEAD failed: {read.stderr.strip()}")
        patch_bytes = _b(patch_text)
        check = _git(repo, "apply", "--cached", "--check", "-", index=index,
                     input_bytes=patch_bytes)
        if check.returncode != 0:
            return Abstain("publish-conflict",
                           f"the authored patch does not apply to HEAD {old[:12]}: "
                           f"{check.stderr.strip()}")
        applied = _git(repo, "apply", "--cached", "-", index=index, input_bytes=patch_bytes)
        if applied.returncode != 0:
            return Abstain("publish-conflict",
                           f"git apply --cached failed: {applied.stderr.strip()}")
        tree = _git(repo, "write-tree", index=index)
        if tree.returncode != 0:
            return Abstain("publish-conflict", f"write-tree failed: {tree.stderr.strip()}")
        ident = {"GIT_AUTHOR_NAME": "mini-ork", "GIT_AUTHOR_EMAIL": "mini-ork@local",
                 "GIT_COMMITTER_NAME": "mini-ork", "GIT_COMMITTER_EMAIL": "mini-ork@local"}
        commit = _git(repo, "commit-tree", tree.stdout.strip(), "-p", old,
                      "-m", message or "mini-ork: in-place publish (authored patch)",
                      env_extra=ident)
        if commit.returncode != 0:
            return Abstain("publish-conflict", f"commit-tree failed: {commit.stderr.strip()}")
        new = commit.stdout.strip()
        moved = _git(repo, "update-ref", f"refs/heads/{branch}", new, old)
        if moved.returncode != 0:
            return Abstain("publish-conflict",
                           f"HEAD moved under the publisher (expected {old[:12]}): "
                           f"{moved.stderr.strip()}")
        paths = _paths_in_patch(patch_text)
        if paths:
            # Best-effort: the commit has landed either way; a failed refresh
            # only leaves the stale entries this step exists to clear.
            _git(repo, "reset", "-q", "--", *paths)
        return new
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
