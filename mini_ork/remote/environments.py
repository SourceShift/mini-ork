"""Environment-profile loader for the remote-nodes data-plane (remote-nodes-12).

A run selects a NAMED environment. The environment decides which image, with a
setup script CACHED as an image layer; which non-secret env vars to set; which
secret NAMES the run may use (epic 13 resolves values); which network level
applies; the resources. Provisioning progress is reported via ``remote.setup.step``
events so a client can render a live setup checklist.

Lookup order in :func:`load_profile`:

  1. ``$MINI_ORK_HOME/config/environments/<name>.yaml`` (LIVE — operator-edited).
  2. ``$MINI_ORK_ROOT/config/environments/<name>.yaml`` (TEMPLATE — committed).

The two files MERGE PER KEY, mirroring :mod:`mini_ork.remote.nodes` so a live
entry setting only ``resources:`` keeps the template's ``image:`` and
``setup:``. Lists override (operator additions to ``allow_domains`` are an
explicit widening, not an accidental concat). Dicts deep-merge so the
template's defaults (CPU / memory, env vars) keep flowing through. The merged
dict is validated against :mod:`schemas.environment.schema`; an unknown key is
an error (kickoff Requirement 1). Secret-shaped keys (``*_KEY``, ``*_TOKEN``,
``*_SECRET``, ``*PASSWORD*``) raise :class:`LookalikeSecretError` so an
operator who puts ``OPENAI_API_KEY`` under ``env:`` gets a clear message
instead of a silent key in ``argv`` (``kickoff`` line 110: "rejected with a
'put it in secrets' message").

``is_lookalike_secret`` is the helper the test suite imports — exporting it
keeps the loader and tests on the same symbol (the attempt-1 test
``test_environment_lookalike_secret_helper`` failed because the helper was
defined locally; this export handles the lesson).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml


__all__ = [
    "Environment",
    "LookalikeSecretError",
    "is_lookalike_secret",
    "load_profile",
    "resolve_schema_path",
]


# ----- lookalike-secret patterns --------------------------------------------
#
# The kickoff lists four patterns: ``*_KEY``, ``*_TOKEN``, ``*_SECRET``,
# ``*PASSWORD*``. The first three are suffix matches on ``[A-Z0-9_]*`` so a
# key like ``AWS_SECRET_ACCESS_KEY`` matches both ``_SECRET$`` (NO — it ends
# in ``_KEY``) and ``_KEY$`` (yes). The ``*PASSWORD*`` pattern is a CONTAINS
# match (``re.search``), case-sensitive (matches the project's case-folding
# convention elsewhere — see ``mini_ork/verify/catalog.py``).

_RE_SUFFIX_KEY = re.compile(r"^[A-Z0-9_]*_KEY$")
_RE_SUFFIX_TOKEN = re.compile(r"^[A-Z0-9_]*_TOKEN$")
_RE_SUFFIX_SECRET = re.compile(r"^[A-Z0-9_]*_SECRET$")
_RE_CONTAINS_PASSWORD = re.compile(r"PASSWORD")

_LOOKALIKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _RE_SUFFIX_KEY,
    _RE_SUFFIX_TOKEN,
    _RE_SUFFIX_SECRET,
    _RE_CONTAINS_PASSWORD,
)


class LookalikeSecretError(ValueError):
    """An env key matches a secret pattern; route to ``secrets:`` instead.

    The kickoff demands a "put it in secrets" message on rejection — see the
    Acceptance bullet (``OPENAI_API_KEY`` rejected, kickoff line 110). The
    structured ``key`` attribute lets callers/programmatic tests assert on the
    offending name.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            f"env key {key!r} looks like a secret (matches one of "
            "*_KEY, *_TOKEN, *_SECRET, *PASSWORD*); put it in 'secrets' instead"
        )
        self.key = key


def is_lookalike_secret(key: str) -> bool:
    """Return True iff ``key`` matches any of the secret-name patterns.

    Public helper — exported so tests can exercise the predicate directly
    without going through :func:`load_profile` (kickoff
    ``test_environment_lookalike_secret_helper``).
    """
    for pat in _LOOKALIKE_PATTERNS[:3]:
        if pat.match(key):
            return True
    return bool(_LOOKALIKE_PATTERNS[3].search(key))


# ----- profile type ---------------------------------------------------------


@dataclass
class Environment:
    """A loaded, validated environment profile.

    Mirrors the schema 1:1; ``name`` is added so callers that hold an
    ``Environment`` know which file it came from. ``to_dict`` is the byte-equal
    representation the loader returns to JSON callers and the
    "default-path byte-identical" test asserts against (``kickoff`` Review
    bar #4: with no shadow override, the loader's output equals the bare
    template).
    """

    name: str
    node: str | None = None
    image: str | None = None
    setup: str = ""
    setup_timeout_s: float = 900.0
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    network: str = "full"
    allow_domains: list[str] = field(default_factory=list)
    resources: dict | None = None

    def to_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.node is not None:
            out["node"] = self.node
        if self.image is not None:
            out["image"] = self.image
        if self.setup:
            out["setup"] = self.setup
        out["setup_timeout_s"] = self.setup_timeout_s
        out["env"] = dict(self.env)
        out["secrets"] = list(self.secrets)
        out["network"] = self.network
        out["allow_domains"] = list(self.allow_domains)
        if self.resources is not None:
            out["resources"] = dict(self.resources)
        return out


# ----- path resolution ------------------------------------------------------


def _config_paths(env: Mapping[str, str]) -> tuple[Path, Path]:
    """Return ``(template_dir, live_dir)`` for the environments directory pair.

    ``MINI_ORK_HOME`` is the live config root; ``MINI_ORK_ROOT`` is the
    engine checkout where the template ships. Falls back to ``os.getcwd()``
    so an out-of-tree import does not crash on path resolution — mirrors
    :func:`mini_ork.remote.nodes._config_paths`.
    """
    home = env.get("MINI_ORK_HOME") or os.getcwd()
    root = env.get("MINI_ORK_ROOT") or os.getcwd()
    return (
        Path(root) / "config" / "environments",
        Path(home) / "config" / "environments",
    )


def resolve_schema_path() -> Path | None:
    """Find ``schemas/environment.schema.json`` next to the repo root.

    Walks up from this file. Returns ``None`` if not found. The ``load_profile``
    caller decides what to do on ``None`` (it raises — see below).
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "schemas" / "environment.schema.json"
        if candidate.is_file():
            return candidate
    return None


# ----- merge + validate -----------------------------------------------------


def _load_yaml(path: Path) -> dict:
    """Load a YAML file; missing or empty files become {} — non-fatal.

    A template-only deployment uses the committed defaults; a live-only
    deployment (no template) uses the operator file verbatim.
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else {}


def _merge_envs(template: dict, live: dict) -> dict:
    """Per-key DEEP merge: a live entry setting only ``resources:`` inherits
    the template's ``image:`` + ``setup:``.

    Why deep and not shallow: see :mod:`mini_ork.remote.nodes` — shallow
    ``update`` would wipe the template's defaults (the same trap
    ``feedback_shadow_providers_yaml_drops_lanes`` already documents). Lists
    OVERRIDE rather than concatenate, so an operator can REPLACE
    ``allow_domains`` without an accidental widening (epic 13 may revisit this
    if ``concat`` becomes the desired semantic).
    """
    if not template:
        return dict(live or {})
    if not live:
        return dict(template)
    out: dict = {}
    for k, v in template.items():
        if k not in live:
            out[k] = v
    for k, v in live.items():
        if k in out:
            tv, lv = out[k], v
            if isinstance(tv, dict) and isinstance(lv, dict):
                out[k] = {**tv, **lv}
            else:
                out[k] = lv
        else:
            out[k] = v
    return out


def _validate_lookalike_secrets(merged: dict) -> None:
    """Raise :class:`LookalikeSecretError` on the first offending env key."""
    env_map = merged.get("env")
    if not isinstance(env_map, dict):
        return
    for key in env_map:
        if isinstance(key, str) and is_lookalike_secret(key):
            raise LookalikeSecretError(key)


def _validate_against_schema(merged: dict) -> None:
    """Validate ``merged`` against the JSON Schema; raise on violation.

    Kickoff Requirement 1: "Validate against the schema; an unknown key is
    an error." We import jsonschema lazily so a test environment without
    the dep can still import this module (the schema is a strict contract,
    not a soft upgrade). The schema file MUST be present in the engine
    checkout — if it isn't, raise.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "jsonschema is required to validate environment profiles; "
            "install mini_ork with its declared dependencies"
        ) from exc
    schema_file = resolve_schema_path()
    if schema_file is None:
        raise FileNotFoundError(
            "schemas/environment.schema.json not found in any parent of "
            f"{Path(__file__).resolve()}; cannot validate environment profile"
        )
    try:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to read {schema_file}: {exc}") from exc
    try:
        jsonschema.validate(instance=merged, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"environment profile failed schema validation: {exc.message}"
        ) from exc


def _to_environment(name: str, merged: dict) -> Environment:
    """Coerce a merged YAML dict into the typed dataclass."""
    setup_timeout_raw = merged.get("setup_timeout_s")
    setup_timeout_s = float(setup_timeout_raw) if setup_timeout_raw is not None else 900.0
    env_map_raw = merged.get("env") or {}
    env_map: dict[str, str] = (
        {str(k): str(v) for k, v in env_map_raw.items()}
        if isinstance(env_map_raw, dict) else {}
    )
    secrets_raw = merged.get("secrets") or []
    secrets: list[str] = [str(s) for s in secrets_raw] if isinstance(secrets_raw, list) else []
    allow_domains_raw = merged.get("allow_domains") or []
    allow_domains: list[str] = (
        [str(d) for d in allow_domains_raw] if isinstance(allow_domains_raw, list) else []
    )
    resources_raw = merged.get("resources")
    resources = dict(resources_raw) if isinstance(resources_raw, dict) else None
    network = merged.get("network") or "full"
    if network not in ("full", "allowlist"):
        network = "full"
    return Environment(
        name=name,
        node=merged.get("node"),
        image=merged.get("image"),
        setup=str(merged.get("setup") or ""),
        setup_timeout_s=setup_timeout_s,
        env=env_map,
        secrets=secrets,
        network=network,
        allow_domains=allow_domains,
        resources=resources,
    )


def list_profiles(*, env: Mapping[str, str] | None = None) -> list[str]:
    """Names of the environment profiles visible to this run (template + live
    dirs; ``*.yaml.example`` files are not profiles)."""
    src = os.environ if env is None else env
    names: set[str] = set()
    for d in _config_paths(src):
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.yaml") if p.is_file())
    return sorted(names)


def load_profile(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    skip_schema_validation: bool = False,
) -> Environment:
    """Load and merge the ``<name>`` environment profile pair.

    Args:
      name: the profile filename stem (e.g. ``default``).
      env: env mapping to read ``MINI_ORK_HOME`` / ``MINI_ORK_ROOT`` from.
        Defaults to ``os.environ`` so production callers don't have to thread
        it through. Tests pass a stub mapping.
      skip_schema_validation: when True, skip the JSON-Schema check. The
        loader for ``default``-from-test worktrees uses this when the schema
        isn't on disk (a debug convenience; production paths always
        validate).

    Raises:
      FileNotFoundError: neither the template nor the live file exists.
      LookalikeSecretError: an ``env:`` key matches a secret pattern.
      ValueError: the merged dict fails schema validation.
    """
    src = os.environ if env is None else env
    template_dir, live_dir = _config_paths(src)
    template = _load_yaml(template_dir / f"{name}.yaml")
    live = _load_yaml(live_dir / f"{name}.yaml")
    if not template and not live:
        raise FileNotFoundError(
            f"environment profile {name!r} not found in "
            f"{template_dir} or {live_dir}; ship {name}.yaml under one of them"
        )
    merged = _merge_envs(template, live)
    _validate_lookalike_secrets(merged)
    if not skip_schema_validation:
        _validate_against_schema(merged)
    return _to_environment(name, merged)


def list_profile_names(
    *, env: Mapping[str, str] | None = None
) -> list[str]:
    """Return the sorted union of profile names from both dirs.

    A diagnostic helper, not used by the runtime hot path; useful for the
    ``/api/agent-profiles`` placeholder that this epic gives real data to
    serve later (kickoff line 32-33).
    """
    src = os.environ if env is None else env
    template_dir, live_dir = _config_paths(src)
    names: set[str] = set()
    for d in (template_dir, live_dir):
        if d.is_dir():
            for p in d.glob("*.yaml"):
                names.add(p.stem)
    return sorted(names)