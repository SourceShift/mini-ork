#!/usr/bin/env python3
"""Shared helpers for the spec-driven-delivery verifiers (not a workflow node).

Each verifier puts its own directory on ``sys.path`` and imports from here.
Stdlib only at import time; ``mini_ork.specdir`` (and with it jsonschema) is
imported lazily through :func:`specdir_module` so a broken engine still yields a
fail-closed JSON verdict instead of a traceback.

Verdict contract (:func:`run_main`): exactly one JSON line on stdout,
``{"pass": bool, "reason": str, ...detail}``, nothing on stderr. Exit 0 pass,
1 semantic fail, 2 malformed or missing required input (:class:`Malformed`)
or an internal error. Python-level stderr written during the body is captured
and reported as ``stderr_tail`` so the executor's evidence file stays one
parseable JSON document.

Probe pass definition, shared by test-validity ("passes on the untouched
tree") and smoke-live ("passes live") through :func:`expect_matches`: a probe
PASSES iff it exits 0 AND its output (stdout with stderr merged) satisfies
``expect``:

* an ``expect`` that only states a zero exit code (``exit 0``, ``exit code
  0``, ``exit status 0``, ``rc=0``, ``returncode 0``; case-insensitive)
  checks the exit code alone;
* any other ``expect`` must match via ``re.search(expect, output, re.M)``;
  when ``expect`` is not a valid regex it must occur as a literal substring;
* an empty ``expect`` never passes.

Probes run as ``bash -c <probe>`` in their own session (process group) with
stdin closed and output captured. On timeout the whole group is SIGKILLed and
reaped, and the result carries ``timed_out: true``; a probe never hangs a
verifier.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import shlex
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROBE_TIMEOUT_S = 120.0
OUTPUT_TAIL_CHARS = 2000


class Malformed(Exception):
    """A required input is missing or unreadable -> verdict exit 2."""

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.detail = detail


# ── verdict emission ──────────────────────────────────────────────────────


def run_main(body) -> int:
    """Run ``body() -> (passed, reason, detail)`` and print its one-line verdict.

    Returns the exit code: 0 iff ``passed is True``, 1 on a semantic fail,
    2 on :class:`Malformed` or any other exception (fail closed).
    """
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            passed, reason, detail = body()
        payload = {**detail, "pass": passed is True, "reason": str(reason)}
        code = 0 if passed is True else 1
    except Malformed as exc:
        payload = {**exc.detail, "pass": False, "reason": f"malformed input: {exc}"}
        code = 2
    except Exception as exc:  # fail closed: any crash is a non-pass verdict
        payload = {"pass": False, "reason": f"internal error: {type(exc).__name__}: {exc}"}
        code = 2
    stray = captured.getvalue()
    if stray:
        payload["stderr_tail"] = stray[-OUTPUT_TAIL_CHARS:]
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()
    return code


# ── environment ───────────────────────────────────────────────────────────


def run_dir() -> Path:
    raw = os.environ.get("MINI_ORK_RUN_DIR", "").strip()
    if not raw:
        raise Malformed("MINI_ORK_RUN_DIR is not set")
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise Malformed(f"MINI_ORK_RUN_DIR is not a directory: {path}")
    return path.resolve()


def _is_engine_root(path: Path) -> bool:
    return (path / "mini_ork" / "specdir").is_dir() and (path / "schemas" / "spec-card.schema.json").is_file()


def engine_root() -> Path:
    """MINI_ORK_ENGINE_ROOT, then MINI_ORK_ROOT, then this file's checkout
    (verifiers -> spec-driven-delivery -> recipes -> root); first one that
    actually holds ``mini_ork/specdir`` and ``schemas/`` wins."""
    candidates = [os.environ.get(v, "").strip() for v in ("MINI_ORK_ENGINE_ROOT", "MINI_ORK_ROOT")]
    paths = [Path(c).expanduser().resolve() for c in candidates if c]
    paths.append(Path(__file__).resolve().parents[3])
    for path in paths:
        if _is_engine_root(path):
            return path
    raise Malformed("engine root not found (need mini_ork/specdir and schemas/)",
                    candidates=[str(p) for p in paths])


def specdir_module(name: str = ""):
    """Import ``mini_ork.specdir[.<name>]`` (the K1 API) from the engine root,
    ahead of any installed copy, and return the module."""
    root = str(engine_root())
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    return importlib.import_module("mini_ork.specdir" + (f".{name}" if name else ""))


def positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise Malformed(f"{name}={raw!r} is not a number") from None
    if value <= 0:
        raise Malformed(f"{name}={raw!r} must be > 0")
    return value


def probe_timeout() -> float:
    return positive_float_env("MO_SDD_SMOKE_TIMEOUT_S", DEFAULT_PROBE_TIMEOUT_S)


def load_dotenv(path: str | os.PathLike[str]) -> dict[str, str]:
    """``KEY=value`` lines; blank lines and ``#`` comments skipped, a leading
    ``export `` and one pair of matching quotes stripped. No interpolation."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise Malformed(f"dotenv file not found: {source}")
    out: dict[str, str] = {}
    for n, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise Malformed(f"{source}:{n}: not a KEY=value line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def probe_env() -> tuple[dict[str, str], list[str]]:
    """Process env with the MO_SDD_SURFACE_ENV dotenv (if set) merged over it.
    Returns the env and the loaded key names (never the values)."""
    env = dict(os.environ)
    surface = os.environ.get("MO_SDD_SURFACE_ENV", "").strip()
    if not surface:
        return env, []
    loaded = load_dotenv(surface)
    env.update(loaded)
    return env, sorted(loaded)


# ── files ─────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def try_load_json(path: str | os.PathLike[str]):
    """``(value, None)`` or ``(None, problem)`` — for per-spec artifacts whose
    absence is a semantic fail rather than malformed verifier input."""
    p = Path(path)
    if not p.is_file():
        return None, f"missing {p.name}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{p.name} is not valid JSON ({exc})"


def load_json(path: str | os.PathLike[str], *, required: bool = True):
    p = Path(path)
    if not p.is_file():
        if required:
            raise Malformed(f"required input missing: {p}")
        return None
    value, problem = try_load_json(p)
    if problem:
        raise Malformed(f"{p}: {problem}")
    return value


def atomic_write_json(path: str | os.PathLike[str], obj) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def is_within(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True iff ``path`` resolves (symlinks followed) inside ``parent``."""
    child, root = os.path.realpath(path), os.path.realpath(parent)
    try:
        return os.path.commonpath([child, root]) == root and child != root
    except ValueError:
        return False


# ── SpecCards ─────────────────────────────────────────────────────────────


def card_files(rd: Path) -> list[Path]:
    return sorted((rd / "spec-cards").glob("*.json"))


def load_cards(rd: Path) -> dict[str, dict]:
    """Every schema-valid SpecCard keyed by spec_id, for the gates that run
    after ratification. No cards, an unreadable card, a schema violation or a
    duplicate spec_id is malformed input (fail closed)."""
    files = card_files(rd)
    if not files:
        raise Malformed(f"no SpecCards in {rd / 'spec-cards'}")
    validate_card = specdir_module().validate_card
    cards: dict[str, dict] = {}
    for path in files:
        card = load_json(path)
        if not isinstance(card, dict):
            raise Malformed(f"{path.name} is not a JSON object")
        errors = validate_card(card)
        if errors:
            raise Malformed(f"{path.name} violates spec-card.schema.json", errors=errors[:10])
        if card["spec_id"] in cards:
            raise Malformed(f"duplicate spec_id {card['spec_id']!r} in {path.name}")
        cards[card["spec_id"]] = card
    return cards


def deliverables_for(card: dict, acceptance_id: str) -> list[str]:
    return [d["id"] for d in card.get("deliverables", []) if acceptance_id in d.get("acceptance_refs", [])]


# ── probes ────────────────────────────────────────────────────────────────


def probe_validity():
    """The core probe rules (``mini_ork.verify.probe_validity``), imported from
    the engine root like :func:`specdir_module`. The pass definition, vacuity
    checks and probe runner live there now so every recipe shares them (I1)."""
    root = str(engine_root())
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    return importlib.import_module("mini_ork.verify.probe_validity")


def run_cmd(argv: list[str], *, timeout: float, env: dict | None = None,
            cwd: str | os.PathLike[str] | None = None) -> dict:
    return probe_validity().run_cmd(argv, timeout=timeout, env=env, cwd=cwd)


def run_probe(probe: str, expect: str, *, timeout: float, env: dict | None = None,
              cwd: str | os.PathLike[str] | None = None) -> dict:
    return probe_validity().run_probe(probe, expect, timeout=timeout, env=env, cwd=cwd)


def is_exit_only(expect: str) -> bool:
    return probe_validity().is_exit_only(expect)


def expect_matches(expect: str, exit_code, output: str) -> bool:
    return probe_validity().expect_matches(expect, exit_code, output)


def is_vacuous_probe(probe) -> bool:
    return probe_validity().is_vacuous_probe(probe)


def vacuous_expect(expect) -> str | None:
    return probe_validity().vacuous_expect(expect)


def shell_template(template: str, values: dict) -> str:
    """Substitute ``{name}`` placeholders in one pass with shell-quoted values
    (a list value becomes space-separated quoted words). Not ``str.format``:
    unknown braces such as ``${VAR}`` or ``{a,b}`` survive untouched, and an
    injected value is never re-scanned for placeholders."""
    quoted = {name: " ".join(shlex.quote(str(v)) for v in value) if isinstance(value, (list, tuple))
              else shlex.quote(str(value)) for name, value in values.items()}
    if not quoted:
        return template
    pattern = re.compile(r"\{(" + "|".join(map(re.escape, quoted)) + r")\}")
    return pattern.sub(lambda m: quoted[m.group(1)], template)
