"""Tests for the default cloud placement adopted by a bare ``mini-ork run``.

A run that names neither ``--placement``/``MO_PLACEMENT`` nor
``--env``/``MO_NODE_ENV`` adopts ``MO_DEFAULT_NODE_ENV`` (default ``daytona``)
when — and only when — a profile file for that name exists in the visible
config dirs. Explicit input, off-words and ``MO_REMOTE_NODE=1`` keep today's
local behavior; an existing but invalid default profile fails the run closed.

Everything here is hermetic: temp homes, explicit env mappings, a cleared
process env, no network and no provider credentials.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path

import pytest

import mini_ork
from mini_ork.cli import main as cli_main
from mini_ork.remote import environments as envs

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

# Cleared per test: the placement knobs the kickoff names, plus the two policy
# knobs that would otherwise route the entrypoint through a different
# workspace path on an operator machine.
_CLEARED_KNOBS = (
    "MO_PLACEMENT",
    "MO_NODE_ENV",
    "MO_DEFAULT_NODE_ENV",
    "MO_REMOTE_NODE",
    "MO_WORKSPACE_MODE",
    "MO_REVIEW_BEFORE_MERGE",
)

# Resolves nowhere, so a call that survives placement validation stops at
# "no recipe" (rc 2) instead of spawning subprocesses.
_NO_RECIPE = "zz-spec-no-such-recipe"

_VALID_DAYTONA = "image: x\n"
_SCHEMA_INVALID_DAYTONA = "image: x\ncolour: blue\n"
_SECRET_INVALID_DAYTONA = "env:\n  DAYTONA_API_KEY: abc\n"


def _clear_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _CLEARED_KNOBS:
        monkeypatch.delenv(key, raising=False)


def _make_home(
    tmp_path: Path,
    profiles: dict[str, str] | None = None,
    examples: tuple[str, ...] = (),
) -> Path:
    """A temp ``MINI_ORK_HOME`` with ``config/environments/<name>.yaml``."""
    envdir = tmp_path / "home" / "config" / "environments"
    envdir.mkdir(parents=True)
    for name, text in (profiles or {}).items():
        (envdir / f"{name}.yaml").write_text(text, encoding="utf-8")
    for name in examples:
        (envdir / f"{name}.yaml.example").write_text(
            _VALID_DAYTONA, encoding="utf-8"
        )
    return envdir.parents[1]


def _env(
    home: Path, root: Path | None = None, **extra: str | None
) -> dict[str, str]:
    """An explicit env mapping in the shape ``context_env_snapshot()`` returns."""
    mapping: dict[str, str] = {
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": str(root or home),
    }
    for key, value in extra.items():
        if value is None:
            mapping.pop(key, None)
        else:
            mapping[key] = value
    return mapping


# ── default_node_env ────────────────────────────────────────────────────────


def test_default_node_env_constant_and_exports():
    assert envs.DEFAULT_NODE_ENV == "daytona"
    assert "DEFAULT_NODE_ENV" in envs.__all__
    assert "default_node_env" in envs.__all__


def test_default_node_env_daytona_when_profile_exists(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert envs.default_node_env(_env(home)) == "daytona"


def test_default_node_env_empty_for_empty_home(tmp_path):
    home = _make_home(tmp_path)
    assert envs.default_node_env(_env(home)) == ""


def test_default_node_env_empty_when_only_example_file(tmp_path):
    """``*.yaml.example`` is a template, not a profile: fresh clones stay local."""
    home = _make_home(tmp_path, examples=("daytona",))
    assert envs.default_node_env(_env(home)) == ""


def test_default_node_env_template_dir_counts(tmp_path):
    home = _make_home(tmp_path / "live")
    root = _make_home(tmp_path / "root", profiles={"daytona": _VALID_DAYTONA})
    assert envs.default_node_env(_env(home, root=root)) == "daytona"


@pytest.mark.parametrize(
    "value",
    ["", "0", "false", "no", "off", "none", "local", "LOCAL", "Off", "  off  "],
)
def test_default_node_env_off_words_disable(tmp_path, value):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert envs.default_node_env(_env(home, MO_DEFAULT_NODE_ENV=value)) == ""


def test_default_node_env_strips_the_value(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert (
        envs.default_node_env(_env(home, MO_DEFAULT_NODE_ENV="  daytona  "))
        == "daytona"
    )


def test_default_node_env_custom_name_returned_when_present(tmp_path):
    home = _make_home(tmp_path, profiles={"e2b": _VALID_DAYTONA})
    assert envs.default_node_env(_env(home, MO_DEFAULT_NODE_ENV="e2b")) == "e2b"


def test_default_node_env_custom_name_empty_when_absent(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert envs.default_node_env(_env(home, MO_DEFAULT_NODE_ENV="e2b")) == ""


def test_default_node_env_returns_existing_invalid_profile_anyway(tmp_path):
    """Existence is pre-checked here; ``load_profile`` owns fail-closed validation."""
    home = _make_home(tmp_path, profiles={"daytona": _SCHEMA_INVALID_DAYTONA})
    assert envs.default_node_env(_env(home)) == "daytona"


def test_default_node_env_none_reads_process_environ(tmp_path, monkeypatch):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    _clear_knobs(monkeypatch)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(home))
    assert envs.default_node_env() == "daytona"


# ── _apply_default_placement truth table ────────────────────────────────────


def test_apply_defaults_when_run_named_neither(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert cli_main._apply_default_placement("", "", _env(home)) == (
        "remote",
        "daytona",
        True,
    )


def test_apply_explicit_local_placement_wins(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert cli_main._apply_default_placement("local", "", _env(home)) == (
        "local",
        "",
        False,
    )


def test_apply_explicit_remote_and_env_win(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert cli_main._apply_default_placement("remote", "alpha", _env(home)) == (
        "remote",
        "alpha",
        False,
    )


def test_apply_explicit_env_alone_wins(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    assert cli_main._apply_default_placement("", "alpha", _env(home)) == (
        "",
        "alpha",
        False,
    )


@pytest.mark.parametrize("guard", ["1", " 1 "])
def test_apply_remote_node_guard_skips_default(tmp_path, guard):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    env = _env(home, MO_REMOTE_NODE=guard)
    assert cli_main._apply_default_placement("", "", env) == ("", "", False)


def test_apply_remote_node_other_values_do_not_guard(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    env = _env(home, MO_REMOTE_NODE="0")
    assert cli_main._apply_default_placement("", "", env) == (
        "remote",
        "daytona",
        True,
    )


def test_apply_no_profile_no_adoption(tmp_path):
    home = _make_home(tmp_path)
    assert cli_main._apply_default_placement("", "", _env(home)) == (
        "",
        "",
        False,
    )


def test_apply_off_word_no_adoption(tmp_path):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    env = _env(home, MO_DEFAULT_NODE_ENV="off")
    assert cli_main._apply_default_placement("", "", env) == ("", "", False)


# ── production entrypoint: _run_lifecycle_impl ──────────────────────────────


def _run_lifecycle(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    daytona_yaml: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Call ``_run_lifecycle_impl`` hermetically; return ``(rc, stderr)``."""
    home = _make_home(tmp_path, profiles={"daytona": daytona_yaml})
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# spec kickoff\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(home))
    _clear_knobs(monkeypatch)
    full_argv = list(argv) + [_NO_RECIPE, str(kickoff)]
    watched = ("MO_PLACEMENT", "MO_NODE_ENV", "MO_REMOTE_SESSION_SCOPE")
    before = {k: os.environ.get(k) for k in watched}
    try:
        # A copied context discards contextvar bindings the call publishes,
        # the way a subprocess would; os.environ is restored by the finally.
        ctx = contextvars.copy_context()
        rc = ctx.run(cli_main._run_lifecycle_impl, full_argv, str(REPO_ROOT), {})
    finally:
        for key, prior in before.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    captured = capsys.readouterr()
    return rc, captured.err


@pytest.mark.parametrize(
    "daytona_yaml",
    [_SCHEMA_INVALID_DAYTONA, _SECRET_INVALID_DAYTONA],
    ids=["schema-invalid", "lookalike-secret"],
)
def test_invalid_default_profile_fails_closed_rc2(
    tmp_path, monkeypatch, capsys, daytona_yaml
):
    rc, err = _run_lifecycle([], monkeypatch, tmp_path, daytona_yaml, capsys)
    assert rc == 2
    assert "MO_DEFAULT_NODE_ENV" in err
    assert "is invalid" in err
    assert "daytona" in err
    # The defaulted branch uses its own wording, not the explicit-profile one.
    assert "--env:" not in err
    assert os.environ.get("MO_PLACEMENT") is None
    assert os.environ.get("MO_REMOTE_SESSION_SCOPE") is None


def test_explicit_env_invalid_keeps_old_wording(tmp_path, monkeypatch, capsys):
    rc, err = _run_lifecycle(
        ["--placement", "remote", "--env", "daytona"],
        monkeypatch,
        tmp_path,
        _SCHEMA_INVALID_DAYTONA,
        capsys,
    )
    assert rc == 2
    assert "--env: environment profile 'daytona' is invalid" in err
    assert "(MO_DEFAULT_NODE_ENV)" not in err


def test_valid_default_publishes_remote_placement(tmp_path, monkeypatch, capsys):
    home = _make_home(tmp_path, profiles={"daytona": _VALID_DAYTONA})
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# spec kickoff\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(home))
    _clear_knobs(monkeypatch)
    watched = ("MO_PLACEMENT", "MO_NODE_ENV", "MO_REMOTE_SESSION_SCOPE")
    before = {k: os.environ.get(k) for k in watched}
    try:
        ctx = contextvars.copy_context()
        # rc 2 is the later "no recipe" refusal; the point is what the adopted
        # default published before the run got that far.
        rc = ctx.run(
            cli_main._run_lifecycle_impl,
            [_NO_RECIPE, str(kickoff)],
            str(REPO_ROOT),
            {},
        )
        assert rc == 2
        assert os.environ.get("MO_PLACEMENT") == "remote"
        assert os.environ.get("MO_NODE_ENV") == "daytona"
        assert os.environ.get("MO_REMOTE_SESSION_SCOPE") == "lifecycle"
    finally:
        for key, prior in before.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    capsys.readouterr()
