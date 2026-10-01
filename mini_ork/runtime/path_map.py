"""PathMap — translate host paths to sandbox paths at the spawn boundary.

D4 (docs/architecture/remote-nodes.md) collapses the four leaky channels
that previously ferried host paths into an isolated CLI:

  1. cwd
  2. argv
  3. env values
  4. prompt text

A single ordered (host_prefix → sandbox_prefix) table rewrites every string
that crosses the boundary. The longest prefix wins, so a host path that
sits under two roots (e.g. ``$HOME/runs`` inside ``$HOME``) is not
double-translated. Prefixes are realpath-normalized so macOS
``/private/var`` vs ``/var`` and symlinked ``/Volumes`` paths map to the
same sandbox equivalent.

The host-to-sandbox table is supplied once per run via :class:`RunRoots`
(``from_roots``) so every node sees the same translations; legacy
``_host_to_container`` callers get ``from_single_root`` so they keep their
single-drive-root shape.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ["PathMap", "UnmappedHostPathError", "DEFAULT_FORBIDDEN"]


class UnmappedHostPathError(ValueError):
    """A host path survived the prefix check.

    Inherits ``ValueError`` so callers that catch the broader type (e.g.
    ``_host_to_container``'s tests, which only match the value's message)
    keep working unchanged.

    Attributes:
        channel: ``"argv"``, ``"env[<key>]"``, or ``"text"`` — which surface
            the leak arrived on.
        value: the offending string.
        forbidden_prefixes: the resolved set checked against; provided so a
            caller can decide whether to extend it or to surface it.
    """

    def __init__(self, channel: str, value: str, forbidden_prefixes: Sequence[str]) -> None:
        self.channel = channel
        self.value = value
        self.forbidden_prefixes = tuple(p for p in forbidden_prefixes if p)
        super().__init__(
            f"host path in {channel!r} survives translation: {value!r} "
            f"(forbidden prefixes: {list(self.forbidden_prefixes)})"
        )


# Default host-path prefixes a sandboxed child must never see. Constructed
# at module load so unit tests can introspect it; $HOME is included only
# when set (CI without $HOME skips it cleanly).
DEFAULT_FORBIDDEN: frozenset[str] = frozenset(
    p for p in (
        os.environ.get("HOME", "") or "",
        "/Users",
        "/Volumes",
        "/private",
        "/home",
    ) if p
)


class PathMap:
    """Ordered (host_prefix → sandbox_prefix) translator.

    Pairs are stored longest-prefix-first so the first match in ``path()``
    is the most specific one. Pure — no I/O, no backend imports — so the
    unit tests can construct one without the workspace session's up/down.
    """

    __slots__ = ("pairs", "agent_home")

    def __init__(
        self,
        pairs: Sequence[tuple[str, str]] | None = None,
        *,
        agent_home: str = "/opt/mini-ork",
    ) -> None:
        # Sort once at construction so lookups stay O(N) instead of O(N log N).
        self.pairs: tuple[tuple[str, str], ...] = tuple(
            sorted(pairs or (), key=lambda kv: len(kv[0]), reverse=True)
        )
        self.agent_home = agent_home

    @classmethod
    def from_roots(cls, roots: Any, *, agent_home: str = "/opt/mini-ork") -> "PathMap":
        """Build a map from a run's :class:`RunRoots`.

        Maps the four D4 roots to their sandbox equivalents:

            target → /workspace/target
            run    → /workspace/run
            home   → /workspace/mo-home   (MINI_ORK_HOME config subset)
            engine → /opt/mini-ork        (MINI_ORK_ROOT, mounted read-only)

        ``/workspace/home`` is the agent's own home (CLI config, transcripts),
        not ``MINI_ORK_HOME``. Equal-length prefixes keep this order, so when
        the target IS the engine root (a mini-ork self-edit) the target wins.
        """
        raw_pairs: list[tuple[str, str]] = []
        for host, sandbox in (
            (getattr(roots, "target", ""), "/workspace/target"),
            (getattr(roots, "run_dir", ""), "/workspace/run"),
            (getattr(roots, "home", ""), "/workspace/mo-home"),
            (getattr(roots, "engine", ""), "/opt/mini-ork"),
        ):
            norm = _normalize_prefix(host)
            if norm:
                raw_pairs.append((norm, sandbox))
        return cls(raw_pairs, agent_home=agent_home)

    @classmethod
    def from_single_root(cls, drive_root: str, mount_path: str) -> "PathMap":
        """Single-root helper for legacy ``_host_to_container`` callers."""
        norm = _normalize_prefix(drive_root)
        if not norm:
            return cls((), agent_home="/opt/mini-ork")
        return cls(((norm, mount_path),), agent_home="/opt/mini-ork")

    def host_roots(self) -> tuple[str, ...]:
        """Host prefixes this map knows about. Used to seed forbidden sets."""
        return tuple(host for host, _ in self.pairs)

    def path(self, p: str) -> str:
        """Map one path. Returns ``p`` unchanged when no prefix matches.

        The caller decides whether an untranslated return is a failure via
        :meth:`assert_no_host_paths`; the legacy single-root caller
        (``_host_to_container``) wraps this with its own check that
        preserves its strict ``outside the drive root`` contract.
        """
        if not isinstance(p, str) or not p:
            return p
        if not p.startswith("/"):
            # Only absolute strings are paths. realpath() resolves "-c",
            # "--print" or an env value like "1" against the CWD, so whenever
            # the process runs inside a mapped root (the executor usually runs
            # in the target repo) plain arguments became "/workspace/target/-c".
            return p
        norm = _try_realpath(p)
        for host_prefix, sandbox_prefix in self.pairs:
            if not host_prefix:
                continue
            if norm == host_prefix:
                return sandbox_prefix
            if norm.startswith(host_prefix + os.sep):
                rel = norm[len(host_prefix) + 1:]
                return sandbox_prefix + "/" + rel.replace(os.sep, "/")
        return p

    def argv(self, items: Sequence[str]) -> list[str]:
        """Apply :meth:`path` to each argv element."""
        return [self.path(str(s)) for s in items]

    def env(self, d: Mapping[str, str]) -> dict[str, str]:
        """Apply :meth:`path` to each env VALUE (keys are not paths)."""
        return {str(k): self.path(str(v)) for k, v in d.items()}

    def text(self, s: str) -> str:
        """Rewrite every occurrence of a host prefix in a prompt-like string.

        Longest-prefix-first so ``/Volumes/foo/Users/bar`` cannot be
        truncated by an earlier ``/Users`` match. Idempotent: re-running on
        already-translated text is a no-op because no host prefix matches a
        ``/workspace/...`` path.
        """
        if not isinstance(s, str) or not s:
            return s
        out = s
        # ``self.pairs`` is already longest-first.
        for host_prefix, sandbox_prefix in self.pairs:
            if host_prefix and host_prefix in out:
                out = out.replace(host_prefix, sandbox_prefix)
        return out

    def json_file(self, src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> Path:
        """Load JSON, recursively translate strings, write ``dst``. Return Path."""
        src_path = Path(src)
        with open(src_path, encoding="utf-8") as fh:
            data = json.load(fh)
        rewritten = self._rewrite_json(data)
        dst_path = Path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dst_path, "w", encoding="utf-8") as fh:
            json.dump(rewritten, fh, indent=2)
        return dst_path

    def _rewrite_json(self, node: Any) -> Any:
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self._rewrite_json(item) for item in node]
        if isinstance(node, dict):
            return {k: self._rewrite_json(v) for k, v in node.items()}
        return node

    def assert_no_host_paths(
        self,
        argv: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        text: str | None = None,
        *,
        forbidden_prefixes: Sequence[str] | None = None,
    ) -> None:
        """Raise :class:`UnmappedHostPathError` on the FIRST leak across all three channels.

        Default forbidden set = :data:`DEFAULT_FORBIDDEN` unioned with this
        map's host roots. The first leak (argv first, then env, then text)
        wins so the error message points at one specific offending value
        and channel.

        Values under a known sandbox root (the map's sandbox prefixes plus
        ``agent_home``) are exempt: every path inside them is sandbox-side,
        so a substring that LOOKS like a host prefix (``/home`` inside
        ``/workspace/home``) is not a leak.
        """
        forbidden = self._resolved_forbidden(forbidden_prefixes)
        sandbox_roots = self._sandbox_roots()
        for item in argv or ():
            self._check_value("argv", item, forbidden, sandbox_roots)
        for key, val in (env or {}).items():
            self._check_value(f"env[{key!r}]", val, forbidden, sandbox_roots)
        if text is not None:
            self._check_value("text", text, forbidden, sandbox_roots)

    def _resolved_forbidden(self, extra: Sequence[str] | None) -> tuple[str, ...]:
        base = set(DEFAULT_FORBIDDEN)
        base.update(self.host_roots())
        if extra:
            base.update(p for p in extra if p)
        return tuple(p for p in base if p)

    def _sandbox_roots(self) -> tuple[str, ...]:
        roots = [self.agent_home] if self.agent_home else []
        roots.extend(sandbox for _, sandbox in self.pairs if sandbox)
        return tuple(roots)

    def _check_value(
        self,
        channel: str,
        value: str,
        forbidden: Sequence[str],
        sandbox_roots: Sequence[str],
    ) -> None:
        if not isinstance(value, str) or not value:
            return
        # Whole value is sandbox-side: every component is sandbox, no host leak.
        if any(value == sr or value.startswith(sr + "/") for sr in sandbox_roots if sr):
            return
        for prefix in forbidden:
            if not prefix:
                continue
            # Strict prefix check: a clean host path. Must precede an
            # embedded match so a host argv like ``/Users/foo/x.py`` trips
            # BEFORE we look at substring noise.
            if value == prefix or value.startswith(prefix + os.sep):
                raise UnmappedHostPathError(channel, value, forbidden)
            # Embedded path token (text prompt with a host reference):
            # match the prefix as a complete path component at any position.
            if self._contains_path_token(value, prefix):
                raise UnmappedHostPathError(channel, value, forbidden)

    @staticmethod
    def _contains_path_token(value: str, prefix: str) -> bool:
        """True if ``prefix`` appears in ``value`` as a complete path component
        (preceded by start of string, ``/``, ``:``, or whitespace — the last
        covers prompt fragments such as "see /Users/foo"; followed by end of
        string, ``/``, ``:``, or whitespace)."""
        idx = 0
        while True:
            idx = value.find(prefix, idx)
            if idx < 0:
                return False
            preceded_ok = idx == 0 or value[idx - 1] in ("/", ":", " ", "\t", "\n")
            end = idx + len(prefix)
            followed_ok = end == len(value) or value[end] in ("/", ":", " ", "\t", "\n")
            if preceded_ok and followed_ok:
                return True
            idx += 1


def _normalize_prefix(prefix: str) -> str:
    """Realpath-normalize a host root. Missing paths fall back to abspath."""
    if not prefix:
        return ""
    try:
        return os.path.realpath(prefix)
    except OSError:
        return os.path.abspath(prefix)


def _try_realpath(p: str) -> str:
    """Realpath if possible; abspath fallback for files that do not exist yet."""
    if not p:
        return p
    try:
        return os.path.realpath(p)
    except OSError:
        return os.path.abspath(p)