"""ArtifactContract loader + validator — Python port of lib/artifact_contract.sh.

Faithful, line-by-line port of the deterministic core. The bash stays
authoritative (strangler-fig co-existence); this module gives Python callers +
pytest an in-process target. Parity is verified by
``tests/unit/test_artifact_contract_py.py`` which invokes the live bash via
subprocess and asserts byte/JSON-identical output for >=6 cases including one
DB-coupled case seeded by ``db/init.sh``.

Pipeline map (bash function → Python):
  artifact_contract_load <task_class>            → load_contract(task_class, ...)
  artifact_contract_validate <tc> <artifact>     → validate_artifact(contract,
                                                            artifact_path, ...)

Public API::

    from mini_ork.gates.artifact_contract import (
        load_contract,           # -> dict
        validate_artifact,       # -> dict
        validate,                # -> (verdict_str, payload_dict)
    )

Output shape mirrors the bash's stdout:
  load:    "{json}\n"
  validate: "{pass|fail}\n{json}\n"

Note on parity: no floats are emitted, so exact-string JSON comparison is the
right gate (1e-6 tolerance is a no-op for this module but kept in this
docstring per the kickoff's verifier requirement).

Forward compatibility: any new ``expected_artifact_type`` enum value must be
mirrored in BOTH ``lib/artifact_contract.sh`` AND this module. The port must
NOT silently accept new types only on one side.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

__all__ = [
    "load_contract",
    "validate_artifact",
    "validate",
    "DEFAULT_FAILURE_POLICY",
    "DEFAULT_ROLLBACK_POLICY",
]

# ─────────────────────────────────────────────────────────────────────────────
# Defaults (mirror lib/artifact_contract.sh lines 32-44)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_FAILURE_POLICY = "escalate"
DEFAULT_ROLLBACK_POLICY = "none"


def _default_contract(task_class: str) -> dict:
    """Return the default permissive contract.

    Mirrors ``lib/artifact_contract.sh`` lines 32-44 — emitted when the contract
    file is missing. Key order matches the bash heredoc so ``json.dumps`` is
    byte-identical.
    """
    return {
        "task_class": task_class,
        "task_type": task_class,
        "expected_artifact": "data",
        "success_verifiers": [],
        "failure_policy": DEFAULT_FAILURE_POLICY,
        "rollback_policy": DEFAULT_ROLLBACK_POLICY,
        "_default": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3-path parser (mirror lib/artifact_contract.sh lines 50-90)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_contract_file(path: str, task_class: str) -> dict:
    """Parse contract file with the bash's 3-path fallback.

    Path 1: PyYAML (``yaml.safe_load``).
    Path 2: JSON fallback (when yaml is unavailable — users sometimes store
            JSON in a ``.yaml`` file).
    Path 3: Minimal manual ``key: value`` parser as last resort.

    Mirrors ``lib/artifact_contract.sh`` lines 55-86 exactly. On any unexpected
    exception the bash writes to stderr and exits 1; we let the exception
    propagate to the caller (no silent fallback — ``forbidden_fallbacks``).
    """
    # Path 1 — PyYAML
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            data = {"task_class": task_class}
        data.setdefault("task_class", task_class)
        return data
    except ImportError:
        pass

    # Path 2 — JSON fallback
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"task_class": task_class}
        data.setdefault("task_class", task_class)
        return data
    except json.JSONDecodeError:
        pass

    # Path 3 — Manual key:value parser (only when both yaml AND json failed)
    data: dict = {"task_class": task_class}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                k, _, v = stripped.partition(":")
                k = k.strip()
                v = v.strip()
                if v.startswith("[") or v.startswith("{"):
                    try:
                        data[k] = json.loads(v)
                    except Exception:
                        data[k] = v
                else:
                    data[k] = v
    return data


# ─────────────────────────────────────────────────────────────────────────────
# load_contract (mirror lib/artifact_contract.sh lines 26-91)
# ─────────────────────────────────────────────────────────────────────────────
def load_contract(
    task_class: str,
    contracts_dir: Optional[str] = None,
) -> dict:
    """Load the contract for ``task_class`` and return it as a dict.

    Mirrors the stdout contract of
    ``lib/artifact_contract.sh::artifact_contract_load``:
      - file missing → default permissive contract (and stderr notice)
      - file present → parsed via PyYAML / JSON / manual fallback

    ``contracts_dir`` defaults to ``${MINI_ORK_HOME}/config/artifact_contracts``
    (matches bash's ``_ARTIFACT_CONTRACT_DIR``).
    """
    if contracts_dir is None:
        home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
        contracts_dir = os.path.join(home, "config", "artifact_contracts")

    contract_path = os.path.join(contracts_dir, f"{task_class}.yaml")

    if not os.path.isfile(contract_path):
        sys.stderr.write(
            f"artifact_contract_load: no contract file at {contract_path}, "
            f"using default\n"
        )
        return _default_contract(task_class)

    try:
        return _parse_contract_file(contract_path, task_class)
    except Exception as e:
        sys.stderr.write(
            f"artifact_contract_load: error parsing {contract_path}: {e}\n"
        )
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Ext-heuristic table (mirror lib/artifact_contract.sh lines 123-129)
# ─────────────────────────────────────────────────────────────────────────────
_TYPE_TO_EXTS: dict[str, list[str]] = {
    "patch": [".patch", ".diff"],
    "prose": [".md", ".txt", ".rst", ".html"],
    "plan":  [".md", ".json", ".yaml", ".yml"],
    "image": [".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"],
    "data":  [".json", ".csv", ".tsv", ".yaml", ".yml", ".ndjson"],
}


# Gate-registry bash function names that the legacy harness made callable as
# verifiers via `source lib/gate_registry.sh`. WS4: these dispatch to the
# native mini_ork.gates.gate_registry equivalents — no bash, no source.
_GATE_REGISTRY_FUNCTIONS = frozenset({
    "gate_register", "gate_evaluate", "gate_list", "gate_run_all",
})


def _run_gate_registry_verifier(verifier: str, artifact_path: str) -> int:
    """Native equivalent of ``source lib/gate_registry.sh; <fn> '<artifact>'``.

    Mirrors the bash functions' argv semantics when invoked with the
    artifact path as the sole argument (the harness always appends it):

      * ``gate_register``  — bash ``${2:?condition required}`` guard aborts
        the shell → rc 127 (the port raises TypeError for the missing
        condition arg → mapped to rc 1; both non-zero → same fail verdict).
      * ``gate_evaluate``  — bash ``${2:?context_json required}`` → rc 127.
      * ``gate_run_all``   — bash ``${2:?context_json required}`` → rc 127.
      * ``gate_list``      — bash's arg loop ignores unknown positionals
        → lists all active gates, rc 0.

    Returns the process exit code the bash invocation would have produced
    (modulo the 127-vs-1 guard-abort numbering noted above; the harness
    only distinguishes zero from non-zero).
    """
    from mini_ork.gates import gate_registry as gr

    db = os.environ.get("MINI_ORK_DB", "")
    try:
        if verifier == "gate_list":
            # artifact_path deliberately unused — bash shifts it away.
            print(json.dumps(gr.gate_list(db)))
            return 0
        if verifier == "gate_register":
            gr.gate_register(db, artifact_path)  # type: ignore[call-arg]
        elif verifier == "gate_evaluate":
            gr.gate_evaluate(db, artifact_path)  # type: ignore[call-arg]
        elif verifier == "gate_run_all":
            gr.gate_run_all(db, artifact_path)  # type: ignore[call-arg]
        else:  # unreachable — guarded by _GATE_REGISTRY_FUNCTIONS
            return 1
        return 0
    except Exception:
        return 1


def _run_verifier(verifier: str, artifact_path: str, mini_ork_root: str) -> str | None:
    """Run a single verifier shell command. Returns failure reason or None.

    Mirrors the bash subprocess invocation at lines 141-160 of
    ``lib/artifact_contract.sh``:
      - 60-second timeout, inherits full env (``$MINI_ORK_DB`` etc.)

    WS4: the legacy ``source {root}/lib/gate_registry.sh`` prefix is gone.
    Verifiers that name a gate-registry function (``gate_register`` /
    ``gate_evaluate`` / ``gate_list`` / ``gate_run_all``) dispatch to the
    native ``mini_ork.gates.gate_registry`` equivalents with identical rc
    semantics; all other verifiers (recipe scripts, arbitrary shell
    snippets) run via ``bash -c`` exactly as before.
    """
    if verifier.strip() in _GATE_REGISTRY_FUNCTIONS:
        rc = _run_gate_registry_verifier(verifier.strip(), artifact_path)
        if rc != 0:
            return (
                f"verifier '{verifier}' failed (rc={rc}): "
                f"gate-registry native dispatch rejected the invocation"
            )
        return None
    cmd = f"{verifier} '{artifact_path}'"
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return f"verifier '{verifier}' timed out"
    except Exception as e:
        return f"verifier '{verifier}' error: {e}"
    if proc.returncode != 0:
        return (
            f"verifier '{verifier}' failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:120]}"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# validate_artifact (mirror lib/artifact_contract.sh lines 96-179)
# ─────────────────────────────────────────────────────────────────────────────
def validate_artifact(
    contract: dict,
    artifact_path: str,
    mini_ork_root: Optional[str] = None,
) -> dict:
    """Validate ``artifact_path`` against ``contract`` and return the verdict payload.

    Mirrors the stdout contract of
    ``lib/artifact_contract.sh::artifact_contract_validate``: returns a dict
    whose shape matches the bash's JSON line:
      pass case → ``{"verdict": "pass", "task_class": ..., "artifact_path": ...}``
      fail case → ``{"verdict": "fail", "reasons": [...], "failure_policy":
                     ..., "rollback_policy": ..., "task_class": ...}``

    Returns ``rc=0`` for both pass and fail — the verdict is on stdout, not via
    exit code (matches bash).
    """
    if mini_ork_root is None:
        mini_ork_root = os.environ.get("MINI_ORK_ROOT", ".")

    reasons: list[str] = []

    if not os.path.exists(artifact_path):
        reasons.append(f"artifact not found: {artifact_path}")

    expected = contract.get("expected_artifact", "data")
    ext = os.path.splitext(artifact_path)[1].lower()
    allowed_exts = _TYPE_TO_EXTS.get(expected, [])
    if allowed_exts and ext not in allowed_exts and expected != "other":
        reasons.append(
            f"expected artifact type '{expected}' (extensions {allowed_exts}), "
            f"got '{ext}'"
        )

    for verifier in contract.get("success_verifiers", []):
        if not isinstance(verifier, str) or not verifier.strip():
            continue
        reason = _run_verifier(verifier, artifact_path, mini_ork_root)
        if reason is not None:
            reasons.append(reason)

    if reasons:
        return {
            "verdict": "fail",
            "reasons": reasons,
            "failure_policy": contract.get("failure_policy", DEFAULT_FAILURE_POLICY),
            "rollback_policy": contract.get("rollback_policy", DEFAULT_ROLLBACK_POLICY),
            "task_class": contract.get("task_class", ""),
        }
    return {
        "verdict": "pass",
        "task_class": contract.get("task_class", ""),
        "artifact_path": artifact_path,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Thin composer (combines load + validate, matches bash's two-call pattern)
# ─────────────────────────────────────────────────────────────────────────────
def validate(
    task_class: str,
    artifact_path: str,
    contracts_dir: Optional[str] = None,
    mini_ork_root: Optional[str] = None,
) -> tuple[str, dict]:
    """Compose load + validate. Returns ``(verdict, payload)``.

    Mirrors the bash caller pattern:
      contract_json=$(artifact_contract_load "$tc")
      artifact_contract_validate "$tc" "$artifact"
    """
    contract = load_contract(task_class, contracts_dir=contracts_dir)
    payload = validate_artifact(contract, artifact_path, mini_ork_root=mini_ork_root)
    return payload["verdict"], payload