"""Secure, workflow-aware setup and status checks for provider lenses.

This command is deliberately metadata-only: users can determine exactly which
credentials a recipe needs without loading or printing their values. Values
arrive through hidden terminal input or standard input, then enter the strict
owner-only local store used by the native dispatcher.
"""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

from mini_ork.dispatch.providers import LaneHealth, lane_health, required_secret_envs
from mini_ork.dispatch.secrets import (
    SecretStoreError,
    read_secret_exports,
    secret_store_path,
    write_secret_exports,
)


_USAGE = """Usage:
  mini-ork providers status [--workflow <workflow.yaml>] [<lane> ...]
  mini-ork providers configure [--workflow <workflow.yaml>] [--replace] [--from-stdin] [<lane> ...]

`status` reports only credential presence and source. `configure` reads values
from a hidden terminal prompt, or NAME=value lines on standard input.
"""


def _agents_path(root: str | Path) -> Path:
    home = Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork")
    local = home / "config" / "agents.yaml"
    return local if local.is_file() else Path(root) / "config" / "agents.yaml"


def _load_lane_mapping(root: str | Path) -> Mapping[str, str]:
    path = _agents_path(root)
    if not path.is_file():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid agents configuration {path}: {exc}") from exc
    lanes = document.get("lanes") or {}
    if not isinstance(lanes, Mapping):
        raise ValueError(f"invalid agents configuration {path}: 'lanes' must be a mapping")
    return {str(name): str(model) for name, model in lanes.items() if str(model)}


def workflow_lanes(path: str | Path) -> tuple[str, ...]:
    """Read distinct model_lane values from workflow nodes in declaration order."""
    workflow = Path(path)
    try:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid workflow {workflow}: {exc}") from exc
    nodes = document.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError(f"invalid workflow {workflow}: 'nodes' must be a list")
    lanes: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        lane = str(node.get("model_lane") or "").strip()
        if lane and lane not in lanes:
            lanes.append(lane)
    return tuple(lanes)


def resolve_models(lanes: Iterable[str], *, root: str | Path) -> tuple[tuple[str, str], ...]:
    """Resolve requested/workflow aliases to provider model names."""
    mapping = _load_lane_mapping(root)
    pairs: list[tuple[str, str]] = []
    for lane in lanes:
        name = str(lane).strip()
        if not name:
            continue
        pair = (name, mapping.get(name, name))
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def _parse_args(argv: list[str]) -> tuple[str, str, bool, bool, list[str]]:
    if not argv or argv[0] in {"--help", "-h"}:
        return "help", "", False, False, []
    action = argv.pop(0)
    if action not in {"status", "configure"}:
        raise ValueError(f"unknown providers action: {action}")
    workflow = ""
    replace = False
    from_stdin = False
    lanes: list[str] = []
    while argv:
        item = argv.pop(0)
        if item == "--workflow":
            if not argv:
                raise ValueError("--workflow requires <workflow.yaml>")
            workflow = argv.pop(0)
        elif item.startswith("--workflow="):
            workflow = item.split("=", 1)[1]
        elif item == "--replace":
            replace = True
        elif item == "--from-stdin":
            from_stdin = True
        elif item.startswith("-"):
            raise ValueError(f"unknown providers option: {item}")
        else:
            lanes.append(item)
    if action == "status" and (replace or from_stdin):
        raise ValueError(f"{action} does not accept --replace or --from-stdin")
    return action, workflow, replace, from_stdin, lanes


def _requested_models(lanes: list[str], workflow: str, *, root: str | Path) -> tuple[tuple[str, str], ...]:
    if workflow:
        workflow_path = Path(workflow)
        if not workflow_path.is_file():
            raise ValueError(f"workflow not found: {workflow_path}")
        lanes = [*lanes, *workflow_lanes(workflow_path)]
    if not lanes:
        raise ValueError("provide one or more lanes, or --workflow <workflow.yaml>")
    return resolve_models(lanes, root=root)


def _credential_status(
    model: str,
    *,
    root: str | Path,
    stored: Mapping[str, str],
) -> tuple[tuple[str, str, str], LaneHealth]:
    required = required_secret_envs(model, root)
    health_env = {**stored, **os.environ}
    health = lane_health(model, root, environment=health_env)
    if not required:
        return (("-", "not required", ""),), health
    rows: list[tuple[str, str, str]] = []
    for name in required:
        if os.environ.get(name):
            rows.append((name, "configured", "shell"))
        elif stored.get(name):
            rows.append((name, "configured", "local store"))
        else:
            rows.append((name, "missing", ""))
    return tuple(rows), health


def _print_status(models: tuple[tuple[str, str], ...], root: str | Path) -> int:
    store = secret_store_path()
    stored = read_secret_exports(store)
    print("=== mini-ork providers ===")
    print(f"Secret store: {store.resolve()} ({'present' if store.exists() else 'not created'})")
    print("Lane                 Model        Credential             Status")
    exit_code = 0
    for lane, model in models:
        try:
            rows, health = _credential_status(model, root=root, stored=stored)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"{lane:<20} {model:<12} {'-':<22} unavailable: {exc}")
            exit_code = 2
            continue
        for index, (credential, status, source) in enumerate(rows):
            label = lane if index == 0 else ""
            display_model = model if index == 0 else ""
            detail = f"{status} ({source})" if source else status
            print(f"{label:<20} {display_model:<12} {credential:<22} {detail}")
            if status == "missing":
                exit_code = 2
        if not health.ok and not any(status == "missing" for _, status, _ in rows):
            print(f"{'':<20} {'':<12} {'health':<22} {health.reason}")
            exit_code = 2
    return exit_code


def _stdin_updates() -> dict[str, str]:
    updates: dict[str, str] = {}
    for number, line in enumerate(sys.stdin.read().splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"stdin line {number} must be NAME=value")
        name, value = line.split("=", 1)
        updates[name.strip()] = value
    return updates


def _configure(
    models: tuple[tuple[str, str], ...], *, root: str | Path, replace: bool, from_stdin: bool
) -> int:
    store = secret_store_path()
    stored = read_secret_exports(store)
    required: list[str] = []
    for _lane, model in models:
        for name in required_secret_envs(model, root):
            if name not in required:
                required.append(name)
    updates = _stdin_updates() if from_stdin else {}
    missing_input = [name for name in updates if name not in required]
    if missing_input:
        raise ValueError(
            f"stdin contains credentials not needed by selected lanes: {', '.join(missing_input)}"
        )
    if from_stdin and not replace:
        existing_input = [name for name in updates if name in os.environ or name in stored]
        if existing_input:
            raise ValueError(
                "refusing to replace existing credentials without --replace: " + ", ".join(existing_input)
            )
    for name in required:
        if name in os.environ and not replace:
            continue
        if name in stored and not replace:
            continue
        if from_stdin:
            continue
        try:
            value = getpass.getpass(f"{name} (leave blank to skip): ")
        except EOFError as exc:
            raise ValueError("hidden prompt is unavailable; use --from-stdin with NAME=value input") from exc
        if value:
            updates[name] = value
    selected = {name: updates[name] for name in required if updates.get(name)}
    if selected:
        write_secret_exports(selected, store)
        print(f"Configured {len(selected)} credential(s) in {store.resolve()}.")
    else:
        print("No credentials changed.")
    return _print_status(models, root)


def main(argv: list[str] | None = None, *, root: str | Path | None = None) -> int:
    """Run ``mini-ork providers`` without exposing credential material."""
    root_path = Path(root or os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2])
    try:
        action, workflow, replace, from_stdin, lanes = _parse_args(
            list(sys.argv[1:] if argv is None else argv)
        )
        if action == "help":
            sys.stdout.write(_USAGE)
            return 0
        models = _requested_models(lanes, workflow, root=root_path)
        if action == "status":
            return _print_status(models, root_path)
        return _configure(models, root=root_path, replace=replace, from_stdin=from_stdin)
    except (SecretStoreError, ValueError, OSError) as exc:
        sys.stderr.write(f"providers: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
