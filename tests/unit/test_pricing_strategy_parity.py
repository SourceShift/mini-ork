"""Parity gate: ``mini_ork.dispatch.pricing_strategy.lookup`` vs ``bash lib/pricing_strategy.sh``.

For each fixture we seed a self-contained ``pricing.yaml`` under ``tmp_path``,
invoke the LIVE bash function via subprocess (no mocking, exactly as the
production runtime would), then call the Python port against
``yaml.safe_load`` of the SAME file and compare the resulting stdout
strings byte-for-byte.

Why stdout-only: the bash function emits ``"0"`` plus a stderr warning on
invalid kind / unknown provider / unknown model / parse error. The port
deliberately strips stderr (caller-visible parity is the stdout byte);
this gate therefore checks stdout strings only, matching the kickoff
contract ("floats within 1e-6, strings exact" — interpreted as "strings
exact, since output is a string").

Strangler-fig co-existence is preserved: ``lib/pricing_strategy.sh`` is
byte-identical before and after this test exists. The test only WRITES
to its ``tmp_path`` (its own pricing.yaml) and READS from
``lib/pricing_strategy.sh`` (verified by ``git diff --stat`` in the
verifier step).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from mini_ork.dispatch.pricing_strategy import lookup

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_PRICING = REPO_ROOT / "lib" / "pricing_strategy.sh"


def _run_bash(yaml_path: Path, provider: str, model: str, kind: str) -> str:
    """Shell out to bash and invoke ``pricing_lookup <p> <m> <k>`` against the live file.

    The parent shell's ``MO_PRICING_YAML`` is popped first so bash does not
    silently re-read the repo's ``.mini-ork/config/pricing.yaml`` instead of
    our fixture file. The fixture path is then exported via the subprocess
    env. ``cwd=REPO_ROOT`` keeps ``lib/...`` resolution stable.
    """
    env = os.environ.copy()
    env.pop("MO_PRICING_YAML", None)
    env.pop("MINI_ORK_HOME", None)
    env["MO_PRICING_YAML"] = str(yaml_path)

    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_PRICING}" && pricing_lookup "{provider}" "{model}" "{kind}"'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    # Strip the trailing newline — bash's ``print("3.0")`` emits "3.0\n".
    # The Python port returns the bare string, so we align both sides.
    return proc.stdout.strip()


def _run_python(yaml_path: Path, provider: str, model: str, kind: str) -> str:
    """In-process port: parse the same yaml the bash heredoc sees, then look up."""
    with open(yaml_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return lookup(data, provider, model, kind)


def _assert_parity(yaml_path: Path, provider: str, model: str, kind: str, label: str) -> None:
    bash_out = _run_bash(yaml_path, provider, model, kind)
    py_out = _run_python(yaml_path, provider, model, kind)
    assert bash_out == py_out, (
        f"parity drift [{label}]: bash={bash_out!r} py={py_out!r} "
        f"(provider={provider!r} model={model!r} kind={kind!r})"
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────
# Each entry yields the yaml body + the lookup triple. Written as
# standalone functions so the yaml content stays close to its assertions
# and the parametrize ids read like a spec.

def _yaml_anthropic() -> str:
    return (
        "pricing:\n"
        "  anthropic:\n"
        "    claude-sonnet-4-6:\n"
        "      input:       3.00\n"
        "      output:     15.00\n"
        "      cache_read:  0.30\n"
        "      cache_write: 3.75\n"
        "  openai:\n"
        "    gpt-5:\n"
        "      input:       2.50\n"
        "      output:     10.00\n"
    )


def _yaml_custom_decimal() -> str:
    return (
        "pricing:\n"
        "  custom:\n"
        "    special-model:\n"
        "      input:  10.50\n"
        "      output: 99.99\n"
    )


def _yaml_int_rates() -> str:
    return (
        "pricing:\n"
        "  anthropic:\n"
        "    claude-haiku-4-5:\n"
        "      input:  5\n"
        "      output: 25\n"
    )


def _yaml_missing_cache_column() -> str:
    """gpt-5 has no cache_read/cache_write — those keys must silently miss."""
    return (
        "pricing:\n"
        "  openai:\n"
        "    gpt-5:\n"
        "      input:  2.50\n"
        "      output: 10.00\n"
    )


def _yaml_minimal() -> str:
    return "pricing:\n  anthropic:\n    claude-sonnet-4-6:\n      input: 3.00\n"


FIXTURES = [
    # (id, yaml_body, provider, model, kind)
    ("f01_hit_input",            _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "input"),
    ("f02_hit_cache_read",       _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "cache_read"),
    ("f03_hit_output",           _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "output"),
    ("f04_miss_provider",        _yaml_anthropic(),         "unknown",   "claude-sonnet-4-6", "input"),
    ("f05_miss_model",           _yaml_anthropic(),         "anthropic", "gpt-5",             "input"),
    ("f06_miss_cache_write",     _yaml_missing_cache_column(), "openai", "gpt-5",            "cache_write"),
    ("f07_unknown_kind",         _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "bogus_kind"),
    ("f08_custom_decimal",       _yaml_custom_decimal(),    "custom",    "special-model",     "input"),
    ("f09_int_rate",             _yaml_int_rates(),         "anthropic", "claude-haiku-4-5",  "input"),
    ("f10_minimal",              _yaml_minimal(),           "anthropic", "claude-sonnet-4-6", "input"),
]


@pytest.mark.parametrize(
    "yaml_body,provider,model,kind",
    [(f[1], f[2], f[3], f[4]) for f in FIXTURES],
    ids=[f[0] for f in FIXTURES],
)
def test_pricing_lookup_matches_bash(tmp_path, yaml_body, provider, model, kind):
    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text(yaml_body, encoding="utf-8")

    label = f"{provider}/{model}/{kind}"
    _assert_parity(yaml_path, provider, model, kind, label)


def test_precondition_bash_returns_non_zero_for_hit(tmp_path):
    """Precondition gate: if bash ever returns '0' for a known hit (e.g. PyYAML
    missing in the test env), every other fixture would silently 'pass' on the
    wrong grounds. Lock the positive case FIRST so a broken environment
    surfaces as a fixture failure, not a vacuous parity 'pass'.
    """
    yaml_path = tmp_path / "pricing.yaml"
    yaml_path.write_text(_yaml_anthropic(), encoding="utf-8")

    bash_out = _run_bash(yaml_path, "anthropic", "claude-sonnet-4-6", "input")
    assert bash_out == "3.0", (
        f"precondition: bash must return '3.0' for known input rate; got {bash_out!r}. "
        f"PyYAML or python3 likely missing in test env — every fixture would now "
        f"silently match '0' and the gate would be meaningless."
    )


def test_smoke_import_and_lookup_no_io():
    """Pure-path smoke: import the module, call lookup against an in-memory
    dict — no env, no file, no subprocess. Confirms the port works
    in-process and exercises the ALLOWED branch.
    """
    data = {
        "pricing": {
            "anthropic": {
                "claude-sonnet-4-6": {
                    "input": 3.00,
                    "cache_read": 0.30,
                }
            }
        }
    }
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "input") == "3.0"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "cache_read") == "0.3"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "cache_write") == "0"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "notakind") == "0"
    assert lookup(data, "unknown", "claude-sonnet-4-6", "input") == "0"
    assert lookup(data, "anthropic", "unknown", "input") == "0"
    assert lookup(None, "anthropic", "claude-sonnet-4-6", "input") == "0"
    assert lookup({"pricing": "not a dict"}, "anthropic", "claude-sonnet-4-6", "input") == "0"