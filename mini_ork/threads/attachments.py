"""Thread attachments on disk: one directory per thread under the home.

A file a person attaches to a thread — dragged into the canvas, sent through the
conversation API, or picked in the IDE (``mini-ork threads attach``) — is copied
to ``<home>/attachments/<thread_id>/`` and recorded as a ``MessageRole.FILE``
row whose ``attachments`` holds the stored absolute path. This module is the one
place that decides where a file lands and what it is called, so the HTTP route
and the CLI cannot drift into two layouts. The layout is the one
``mini_ork/acp/agent.py`` already writes for ACP sessions.

Three rules, all enforced here:

* **The name is a leaf.** A client owns the name string, never the path: every
  directory component is dropped, ``.``/``..`` are refused, and the final path
  is re-checked against the thread directory before anything is written.
* **The suffix is not the client's to choose** for a remote upload: it comes
  from the mime type, and a generic mime may only borrow the client's suffix
  when it is a document format :mod:`mini_ork.documents` previews. A local file
  the operator picks keeps its own suffix — the operator is not an adversary.
* **Size is capped** at :data:`MAX_BYTES`, checked before anything is written.

Nothing here touches the thread tables; callers record the rows (see
``threads.service.deliver(attachments=…)``).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

#: The largest attachment stored. The conversation API is an inbound port; an
#: unbounded body is a way to fill the disk.
MAX_BYTES = 25 * 1024 * 1024

#: mime type → stored suffix. The previewable document formats are listed so an
#: office upload lands with a suffix the thread page can preview.
EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/json": ".json",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


class AttachmentError(ValueError):
    """An attachment that cannot be stored: too large, unreadable, or escaping."""


def sanitize_name(name: Any) -> str:
    """A client-supplied filename reduced to a safe leaf name, or ``""``.

    ``../../x`` becomes ``x``; a name that is nothing but traversal becomes
    empty, which makes :func:`target_path` allocate an integer instead. NUL and
    both path separators cannot survive, so the result never names a path.
    """
    raw = str(name or "").strip().replace("\\", "/").replace("\x00", "")
    leaf = raw.rsplit("/", 1)[-1]
    if leaf in ("", ".", ".."):
        return ""
    return leaf[:120]


def extension(mime_type: Any, client_name: str = "") -> str:
    """The stored suffix for a remote upload; ``.bin`` when nothing qualifies.

    The mime type decides. Only when it names nothing (absent, or a generic
    ``application/octet-stream``) may the client's own suffix stand in, and only
    if it is a format :mod:`mini_ork.documents` previews — an allow-list, so a
    client cannot choose ``.sh`` or ``.exe``.
    """
    ext = EXT_BY_MIME.get(str(mime_type or "").lower(), ".bin")
    if ext != ".bin":
        return ext
    from mini_ork.documents import SUPPORTED_FORMATS

    suffix = Path(sanitize_name(client_name)).suffix.lower()
    return suffix if suffix in SUPPORTED_FORMATS else ".bin"


def thread_dir(home: Path | str, thread_id: str) -> Path:
    """``<home>/attachments/<thread_id>``. The id is validated by its caller."""
    return Path(home) / "attachments" / thread_id


def target_path(directory: Path, name: str, ext: str) -> Path:
    """A fresh path in ``directory`` for ``name`` + ``ext`` that overwrites nothing.

    The stem is the sanitized ``name`` when usable, else the next free integer,
    so a second ``0.pdf`` never replaces the first; a taken name gets ``-N``.
    """
    stem = Path(sanitize_name(name)).stem
    if not stem or set(stem) == {"."}:
        used = {int(p.stem) for p in directory.iterdir() if p.stem.isdigit()}
        n = 0
        while n in used:
            n += 1
        stem = str(n)
    target = directory / f"{stem}{ext}"
    n = 0
    while target.exists():
        target = directory / f"{stem}-{n}{ext}"
        n += 1
    if target.resolve().parent != directory.resolve():
        raise AttachmentError(f"attachment path escaped the thread directory: {target}")
    return target


def store_bytes(
    home: Path | str, thread_id: str, data: bytes, *, name: str = "", mime: str = ""
) -> Path:
    """Write an uploaded file under the thread's directory; return its path."""
    if len(data) > MAX_BYTES:
        raise AttachmentError(f"attachment over {MAX_BYTES} bytes")
    directory = thread_dir(home, thread_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = target_path(directory, name, extension(mime, name))
    target.write_bytes(data)
    return target


def store_file(home: Path | str, thread_id: str, source: Path | str) -> Path:
    """Copy a local file the operator picked under the thread's directory.

    The operator chose the file, so it keeps its own suffix (lowercased); the
    size cap and the confinement check still apply.
    """
    src = Path(source).expanduser()
    if not src.is_file():
        raise AttachmentError(f"not a file: {src}")
    size = src.stat().st_size
    if size > MAX_BYTES:
        raise AttachmentError(f"{src.name} is {size} bytes, over the {MAX_BYTES}-byte cap")
    directory = thread_dir(home, thread_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = target_path(directory, src.name, src.suffix.lower() or ".bin")
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, target)
    return target


__all__ = [
    "AttachmentError",
    "EXT_BY_MIME",
    "MAX_BYTES",
    "extension",
    "sanitize_name",
    "store_bytes",
    "store_file",
    "target_path",
    "thread_dir",
]
