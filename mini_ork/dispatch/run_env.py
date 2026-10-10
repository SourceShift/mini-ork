"""Per-run and per-node environment overrides, declared in a file.

The run-scoped counterpart to ``secrets.local.sh``. Two things a run needs that
a single global store cannot express:

* **A value that differs per run.** A research run against a vendor endpoint
  wants that vendor's token; the next run wants another's. Without this, the
  only way to vary it is a shell launcher exporting variables before
  ``mini-ork run`` — one launcher per recipe, each re-deriving the same set,
  and the values leak into every concurrent run sharing the process tree.
* **A value that differs per node.** Only the collector needs the source API
  token; the reviewer must not have it. Least privilege is a property of the
  node, not of the run.

Shape (``<run_dir>/config/env.yaml``)::

    env:                       # every node in this run
      MO_RESEARCH_MIN_SOURCES: 40
      ARXIV_API_TOKEN: ${ARXIV_API_TOKEN}
    nodes:                     # one node, overrides the run level
      collector:
        ARXIV_API_TOKEN: ${LIBWIT_ARXIV_TOKEN}
      reviewer:
        ARXIV_API_TOKEN: null  # explicitly absent for this node

Values may be ``${NAME}`` / ``${NAME:-fallback}`` references, resolved from the
secret store overlaid with the runtime environment — so a token stays in one
owner-only file and this file stays committable. ``null`` masks a variable: it
is removed rather than inherited, which is how a node opts out of a run-level
or ambient value. Masking that is not explicit is what makes an env overlay
dangerous, so ``declared_keys`` below reports every key the file touches and
the executor clears the rest before each node.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .secrets import SecretStoreError, expand_secret_references


class RunEnvError(ValueError):
    """The run environment file cannot be read or is mistyped."""


# An env value is a string once resolved; ``None`` means "remove this variable".
EnvValue = str | None

# The executor's own per-node variables. A run file that sets one would race the
# publish that carries the node's identity: whichever ran last would win, and
# the node would either lose its id or be told it is a different node. Rejected
# rather than ordered, because a run that needs to override MO_NODE_ID is not
# configured, it is broken.
RESERVED_KEYS = frozenset(
    {
        "MINI_ORK_RUN_ID",
        "MINI_ORK_RUN_DIR",
        "MO_NODE_ID",
        "MO_NODE_TYPE",
        "MO_NODE_ATTEMPT",
        "MO_DISPATCH_CHAIN",
        "MO_TARGET_CWD",
        "MO_RESUME_SESSION_ID",
        "MO_INPUT_HASH",
    }
)


@dataclass(frozen=True)
class RunEnv:
    """A run's declared environment, resolved but not yet published."""

    run_level: dict[str, EnvValue] = field(default_factory=dict)
    nodes: dict[str, dict[str, EnvValue]] = field(default_factory=dict)

    @property
    def declared_keys(self) -> frozenset[str]:
        """Every key the file mentions anywhere — what the node boundary clears.

        The executor masks these before applying one node's own map so a value
        published for node A cannot survive into node B in the serial path,
        where every node shares one process and one ``os.environ``.
        """
        keys = set(self.run_level)
        for overrides in self.nodes.values():
            keys.update(overrides)
        return frozenset(keys)

    @property
    def is_empty(self) -> bool:
        return not self.run_level and not self.nodes

    def for_node(self, node_id: str) -> dict[str, EnvValue]:
        """The effective map for one node: run level, then that node's overlay."""
        merged: dict[str, EnvValue] = dict(self.run_level)
        merged.update(self.nodes.get(node_id, {}))
        return merged

    def node_overrides(self, node_id: str) -> dict[str, EnvValue]:
        """Every declared key, resolved for one node — unmentioned keys masked.

        Returns the complete map a node boundary should publish: this node's
        values where it declares them, the run level elsewhere, ``None`` for a
        key no level in this run supplies.
        """
        effective = self.for_node(node_id)
        return {key: effective.get(key) for key in self.declared_keys}


def run_env_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "config" / "env.yaml"


def effective_node_overrides(
    run_env: RunEnv,
    node_id: str,
    *,
    node_env: Mapping[str, EnvValue] | None = None,
    workflow_declared: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, EnvValue]:
    """The complete map to publish at one node boundary.

    Precedence, lowest first: the recipe's own ``env:`` block on the node, then
    the run file's ``env:``, then the run file's ``nodes.<id>``. The recipe
    decides *which* agent needs a variable (a committable structural fact); the
    run file supplies the value, and therefore wins.

    Every key any declaration mentions is present in the result, ``None`` where
    this node is not supplied one — so a variable another node declared is
    actively cleared rather than inherited from the ambient environment. That
    clearing is what makes a per-node declaration a guarantee instead of a hope.

    ``environ`` expands the recipe's ``${NAME}`` references. It is passed
    explicitly and applied only to ``node_env``: the run file's own values were
    already expanded when it was parsed, and expanding a resolved value a second
    time would interpret a ``${`` inside a credential as another reference.
    """
    declared = dict(node_env or {})
    if environ is not None:
        for key, value in list(declared.items()):
            if value is None:
                continue
            declared[key] = expand_secret_references(
                value, environ, context=f"node env {key}"
            )
    merged: dict[str, EnvValue] = {}
    keys = set(workflow_declared) | set(run_env.declared_keys) | set(declared)
    for key in keys:
        merged[key] = None
    merged.update(declared)
    merged.update(run_env.run_level)
    merged.update(run_env.nodes.get(node_id, {}))
    return merged


def _coerce_scalar(key: str, value: object, *, where: str) -> EnvValue:
    if value is None:
        return None
    if isinstance(value, bool):
        # YAML ``true``/``false`` would otherwise stringify as "True"/"False",
        # which no child process reads as a flag.
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise RunEnvError(
        f"{where}: {key} must be a string, number, boolean or null, not {type(value).__name__}"
    )


def _parse_mapping(raw: object, *, which: str, environ: Mapping[str, str]) -> dict[str, EnvValue]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RunEnvError(f"{which} must be a mapping of NAME: value, not {type(raw).__name__}")
    parsed: dict[str, EnvValue] = {}
    for key, value in raw.items():
        if isinstance(key, bool):
            # YAML 1.1 reads an unquoted `on:`/`off:`/`yes:`/`no:` key as a
            # boolean, so `ON: 1` would silently become the variable "True".
            raise RunEnvError(
                f"{which}: {key!r} is a YAML boolean, not a variable name; quote it "
                '("ON") if that is the name you want'
            )
        name = str(key)
        where = f"{which}.{name}"
        if name in RESERVED_KEYS:
            raise RunEnvError(
                f"{where}: {name} is owned by the executor and cannot be set here"
            )
        resolved = _coerce_scalar(name, value, where=where)
        if resolved is not None:
            try:
                resolved = expand_secret_references(resolved, environ, context=where)
            except SecretStoreError as exc:
                raise RunEnvError(str(exc)) from exc
        parsed[name] = resolved
    return parsed


def parse_run_env(document: object, environ: Mapping[str, str]) -> RunEnv:
    """Validate and resolve a parsed ``env.yaml`` document."""
    if document is None:
        return RunEnv()
    if not isinstance(document, dict):
        raise RunEnvError("run environment must be a mapping with env: and nodes: sections")
    unknown = sorted(set(document) - {"env", "nodes"})
    if unknown:
        # A typo like ``node:`` would otherwise silently drop every per-node
        # override and run every node with the run-level environment.
        raise RunEnvError(
            f"unknown section(s) {', '.join(unknown)}; expected env: and nodes:"
        )
    run_level = _parse_mapping(document.get("env"), which="env", environ=environ)
    raw_nodes = document.get("nodes")
    if raw_nodes is not None and not isinstance(raw_nodes, dict):
        raise RunEnvError("nodes must be a mapping of node_id: {NAME: value}")
    nodes: dict[str, dict[str, EnvValue]] = {}
    for node_id, overrides in (raw_nodes or {}).items():
        nodes[str(node_id)] = _parse_mapping(
            overrides, which=f"nodes.{node_id}", environ=environ
        )
    return RunEnv(run_level=run_level, nodes=nodes)


def load_run_env(
    run_dir: str | Path, environ: Mapping[str, str] | None = None
) -> RunEnv:
    """Read ``<run_dir>/config/env.yaml``. A missing file is an empty overlay."""
    if not run_dir:
        return RunEnv()
    path = run_env_path(run_dir)
    if not path.is_file():
        return RunEnv()
    if environ is None:
        from .vault import vault_environment

        environ = vault_environment()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunEnvError(f"cannot read run environment {path}: {exc}") from exc
    return parse_run_env(document, environ)
