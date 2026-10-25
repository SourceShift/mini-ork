"""Shared plumbing for this recipe's deterministic verifier nodes.

A verifier reads the artifacts its edges delivered under
``$MINI_ORK_NODE_INPUT_DIR/<input-name>/`` and writes its declared outputs under
``$MINI_ORK_RUN_DIR``. It fails by exiting non-zero with a one-line reason on
stderr — the engine routes that to the ``rollback`` node.
"""
import glob
import json
import os
import sys
from typing import NoReturn

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ.get("MINI_ORK_NODE_INPUT_DIR", "")


def brief_path() -> str:
    """The cycle writes the machine-readable brief and points every node at it.

    It is shipped by environment variable rather than as an artifact because the
    run directory does not exist yet when the cycle builds it — the engine creates
    it. This is the one input that arrives from outside the DAG.
    """
    path = os.environ.get("MO_RSI_SUGGEST_BRIEF") or os.environ.get("MO_RSI_CRITIC_BRIEF")
    if not path:
        fail("brief", "MO_RSI_SUGGEST_BRIEF is not set; this recipe is dispatched by "
                      "`mini-ork rsi-cycle`, never by hand")
    return path


def input_file(name: str, filename: str) -> str:
    path = os.path.join(INPUT_DIR, name, filename)
    if not os.path.isfile(path):
        fail(name, f"missing input artifact {path}")
    return path


def input_many(name: str, suffix: str) -> list[str]:
    folder = os.path.join(INPUT_DIR, name)
    if not os.path.isdir(folder):
        return []
    return sorted(p for p in glob.glob(os.path.join(folder, "**", "*" + suffix), recursive=True)
                  if os.path.isfile(p))


def read_json(path: str, what: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        fail(what, f"{path} does not exist")
    except ValueError as exc:
        fail(what, f"{path} is not JSON: {exc}")
    if not isinstance(payload, dict):
        fail(what, f"{path} must be a JSON object, got {type(payload).__name__}")
    return payload


def write_json(name: str, payload: object, what: str) -> str:
    path = os.path.join(RUN_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"verifier": what, "pass": True, "artifact": name}))
    return path


def first(payload: dict, *keys: str, default=None):
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def as_list(value) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def fail(stage: str, message: str) -> NoReturn:
    sys.stderr.write(f"{stage}: {message}\n")
    raise SystemExit(1)
