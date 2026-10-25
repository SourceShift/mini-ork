"""Shared plumbing for this recipe's deterministic verifier nodes.

Same contract as ``recipes/rsi-cycle/verifiers/_common.py``: read artifacts from
``$MINI_ORK_NODE_INPUT_DIR/<input-name>/``, write declared outputs under
``$MINI_ORK_RUN_DIR``, fail by exiting non-zero with one line on stderr.
"""
import glob
import json
import os
import sys
from typing import NoReturn

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ.get("MINI_ORK_NODE_INPUT_DIR", "")


def brief_path() -> str:
    path = os.environ.get("MO_RSI_CRITIC_BRIEF") or os.environ.get("MO_RSI_SUGGEST_BRIEF")
    if not path:
        fail("brief", "MO_RSI_CRITIC_BRIEF is not set; this recipe is dispatched by "
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


def as_list(value) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def served_actors(db: str, run_id: str) -> list[str] | None:
    """Actors that made an LLM call under this run, or None if unmeasurable.

    Read back from `llm_calls` rather than passed in: the alias the engine
    recorded is the one the heterogeneity check has to grade, and a guessed list
    would manufacture `no_calls` problems on a healthy run.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT DISTINCT actor FROM llm_calls WHERE CAST(run_id AS TEXT) = ? "
            "AND actor IS NOT NULL AND actor <> ''",
            (str(run_id),),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return sorted({str(row[0]) for row in rows})


def fail(stage: str, message: str) -> NoReturn:
    sys.stderr.write(f"{stage}: {message}\n")
    raise SystemExit(1)
