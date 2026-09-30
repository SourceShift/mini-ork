"""Per-user lane-config resolution: tracked template + personal overlay.

``config/agents.yaml`` in the repo is TRACKED and acts as the team-default
TEMPLATE. A per-user OVERLAY (``$MINI_ORK_AGENTS`` env path, or
``$MINI_ORK_HOME/config/agents.local.yaml``) is recursively merged over it.
The merged YAML is materialised at
``$MINI_ORK_HOME/config/.agents.effective.yaml`` (atomic, content-changed
only) so every resolver sees ONE coherent file.

Public API::

    from mini_ork.dispatch import agents_config
    p = agents_config.effective_path(home=None)  # always a valid file path

With NO overlay present, ``effective_path()`` returns the tracked template
path unchanged and writes nothing — byte-for-byte behaviour preserved for
every pre-overlay caller.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # PyYAML is optional at runtime in some contexts
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_home(home: str | None) -> str:
    """``home`` arg wins; else ``$MINI_ORK_HOME``; else ``.mini-ork``."""
    if home is not None:
        return home
    return os.environ.get("MINI_ORK_HOME") or ".mini-ork"


def _resolve_root(root: str | None) -> str:
    """``root`` arg wins; else ``$MINI_ORK_ROOT``; else ``.`` (literal)."""
    if root is not None:
        return root
    return os.environ.get("MINI_ORK_ROOT") or "."


def template_path(home: str | None = None, root: str | None = None) -> str:
    """Tracked team-default ``agents.yaml`` path.

    HOME wins when its ``config/agents.yaml`` file exists; otherwise ROOT's.
    Built with ``os.path.join`` (NOT ``pathlib.Path /``) so the literal
    ``./`` prefix is preserved when bash's default root of ``.`` kicks in
    (``config_resolve.py:44-46``).
    """
    h = _resolve_home(home)
    cand = os.path.join(h, "config", "agents.yaml")
    if Path(cand).is_file():
        return cand
    r = _resolve_root(root)
    return os.path.join(r, "config", "agents.yaml")


def personal_path(home: str | None = None) -> str | None:
    """Per-user overlay path, or ``None`` when no overlay is configured.

    Precedence:
      1. ``$MINI_ORK_AGENTS`` if it points at an existing file.
      2. ``<home>/config/agents.local.yaml`` if it exists.
      3. ``None`` — the caller must skip the merge step.
    """
    env_p = os.environ.get("MINI_ORK_AGENTS")
    if env_p:
        if not Path(env_p).is_file():
            # A typo here used to fall back to the team template silently —
            # the user's own lane choice ignored with no sign of it.
            raise ValueError(f"MINI_ORK_AGENTS points at {env_p}, which does not exist")
        return env_p
    h = _resolve_home(home)
    local = os.path.join(h, "config", "agents.local.yaml")
    if Path(local).is_file():
        return local
    return None


# ---------------------------------------------------------------------------
# YAML merge
# ---------------------------------------------------------------------------


def merge(base: dict, over: dict) -> dict:
    """Recursive dict merge: nested mappings merge key-by-key; any non-mapping
    value (scalar, list) in ``over`` REPLACES the base value; a key whose
    ``over`` value is ``None`` is REMOVED from the result. Inputs are not
    mutated; the returned dict is a deep copy.
    """
    if not isinstance(base, dict) or not isinstance(over, dict):
        raise TypeError("merge() requires two dict operands")
    out = copy.deepcopy(base)
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
            continue
        bv = out.get(k)
        if isinstance(bv, dict) and isinstance(v, dict):
            out[k] = merge(bv, v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------------------
# YAML load / dump (malformed → ValueError)
# ---------------------------------------------------------------------------


def _load_yaml(path: str, what: str = "personal agents overlay") -> dict:
    """``yaml.safe_load`` → dict; raise ``ValueError`` naming the file on any
    parse error so the user sees their own mistake instead of silent fallback.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to load agents.yaml files")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise
    except (yaml.YAMLError, OSError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"malformed {what} at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{what} at {path} must be a YAML mapping, got {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# Effective path — the merged materialisation
# ---------------------------------------------------------------------------


_EFFECTIVE_FILENAME = ".agents.effective.yaml"


def overlay_or(default: str | None, home: str | None = None,
               root: str | None = None) -> str | None:
    """``default`` untouched when the user has no overlay, else the merged file.

    For callers whose historical path differs from ``template_path()`` (the
    coalition gate reads the engine root's file; the profile step reads
    ``<home>/config/agents.yaml`` even when absent). Keeps "no overlay ⇒
    byte-for-byte today's behaviour" true for them too.
    """
    if personal_path(home=home) is None:
        return default
    return effective_path(home=home, root=root)


def effective_path(home: str | None = None, root: str | None = None) -> str:
    """Path every consumer should read.

    With no overlay → ``template_path()`` UNCHANGED (no file written).
    With an overlay → ``<home>/config/.agents.effective.yaml`` (atomic write
    only when content changed; leading newline comment names both source
    files so a future reader can tell what they are looking at).
    """
    tmpl = template_path(home=home, root=root)
    over_p = personal_path(home=home)
    if over_p is None:
        return tmpl

    h = _resolve_home(home)
    base = _load_yaml(tmpl, "agents.yaml template") if Path(tmpl).is_file() else {}
    over = _load_yaml(over_p)
    merged = merge(base, over)

    dest = os.path.join(h, "config", _EFFECTIVE_FILENAME)
    body = (
        f"# merged agents.yaml: template={tmpl} overlay={over_p}\n"
        f"# generated by mini_ork.dispatch.agents_config; do not edit by hand.\n"
        + (yaml.safe_dump(merged, sort_keys=True)  # type: ignore[union-attr]
           if yaml is not None else str(merged))
    )

    # Content-changed-only write keeps mtime stable for downstream caches.
    prev = ""
    if Path(dest).is_file():
        try:
            prev = Path(dest).read_text(encoding="utf-8")
        except OSError:
            prev = ""
    if prev == body:
        return dest

    dest_dir = os.path.dirname(dest)
    if dest_dir and not Path(dest_dir).is_dir():
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(
                f"cannot create effective-yaml dir {dest_dir}: {exc}"
            ) from exc

    try:
        fd, tmp = tempfile.mkstemp(prefix=".agents.effective.", dir=dest_dir or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ValueError(
            f"cannot write effective agents.yaml at {dest}: {exc}"
        ) from exc
    return dest