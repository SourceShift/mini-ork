"""The dispatch-time lane resolvers honour the per-user agents overlay.

``resolve_lane_family`` (fallback-chain lead, execute_handlers) and
``resolve_lane_model`` (llm_dispatch model pick) used to read
``<home>/config/agents.yaml`` directly, so ``agents.local.yaml`` /
``$MINI_ORK_AGENTS`` never reached the dispatch chain: a user who overlaid
``codex_lens: minimax`` still dispatched to codex.
"""
import os
import textwrap

import pytest

from mini_ork.dispatch.llm_dispatch import resolve_lane_family, resolve_lane_model


@pytest.fixture(autouse=True)
def _no_run_dir(monkeypatch):
    """A pytest process started inside a mini-ork run inherits
    ``MINI_ORK_RUN_DIR`` from the launcher; ``_effective_lanes`` then reads
    the run-snapshot ``config/agents.yaml`` instead of the home's policy,
    flipping lane aliases under the suite. The launcher also leaks
    ``MINI_ORK_AGENTS`` (overlay YAML from a previous run), which the
    per-user merge then overrides the test's temp home. Clear BOTH so every
    test sees the same no-snapshot / no-overlay baseline.
    """
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

TEMPLATE = """
    lanes:
      implementer: codex
      codex_lens: codex
      opus_lens: opus
      worker: codex
"""


def _home(tmp_path, overlay=None):
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(textwrap.dedent(TEMPLATE))
    if overlay is not None:
        (home / "config" / "agents.local.yaml").write_text(textwrap.dedent(overlay))
    return str(home)


def test_overlay_wins_for_lane_family(tmp_path):
    home = _home(tmp_path, overlay="""
        lanes:
          codex_lens: minimax
    """)
    assert resolve_lane_family("codex_lens", root=home, home=home) == "minimax"
    # keys the overlay does not mention keep the template value
    assert resolve_lane_family("opus_lens", root=home, home=home) == "opus"


def test_overlay_wins_for_lane_model(tmp_path):
    home = _home(tmp_path, overlay="""
        lanes:
          implementer: minimax
    """)
    assert resolve_lane_model("implementer", home, home) == "minimax"
    assert resolve_lane_model("reviewer", home, home) == "codex"  # falls to lanes.worker


def test_no_overlay_is_template_parity(tmp_path):
    home = _home(tmp_path)
    assert resolve_lane_family("codex_lens", root=home, home=home) == "codex"
    assert resolve_lane_model("implementer", home, home) == "codex"


def test_env_overlay_path_is_honoured(tmp_path, monkeypatch):
    home = _home(tmp_path)
    over = tmp_path / "mine.yaml"
    over.write_text("lanes:\n  codex_lens: glm\n")
    monkeypatch.setenv("MINI_ORK_AGENTS", str(over))
    assert resolve_lane_family("codex_lens", root=home, home=home) == "glm"


def test_env_overlay_pointing_nowhere_raises(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("MINI_ORK_AGENTS", str(tmp_path / "typo.yaml"))
    with pytest.raises(ValueError, match="typo.yaml"):
        resolve_lane_family("codex_lens", root=home, home=home)


def test_malformed_overlay_raises_naming_the_file(tmp_path):
    home = _home(tmp_path, overlay="lanes: [unclosed\n")
    with pytest.raises(ValueError, match="agents.local.yaml"):
        resolve_lane_family("codex_lens", root=home, home=home)


def test_resolution_writes_no_file(tmp_path):
    home = _home(tmp_path, overlay="lanes:\n  codex_lens: minimax\n")
    resolve_lane_family("codex_lens", root=home, home=home)
    resolve_lane_model("implementer", home, home)
    assert not os.path.exists(os.path.join(home, "config", ".agents.effective.yaml"))


def test_empty_home_ignores_cwd_overlay(tmp_path, monkeypatch):
    # The clean-miss ratchet extends to the overlay: an empty home must not
    # pick up a CWD-relative config/agents.local.yaml either.
    cwd = tmp_path / "cwd"
    (cwd / "config").mkdir(parents=True)
    (cwd / "config" / "agents.yaml").write_text("lanes:\n  codex_lens: codex\n")
    (cwd / "config" / "agents.local.yaml").write_text("lanes:\n  codex_lens: minimax\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("MINI_ORK_HOME", "")
    monkeypatch.setenv("MINI_ORK_ROOT", "")
    assert resolve_lane_family("codex_lens", root="", home="") == "codex_lens"


def test_overlay_without_template_still_resolves(tmp_path):
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.local.yaml").write_text("lanes:\n  codex_lens: minimax\n")
    assert resolve_lane_family("codex_lens", root=str(tmp_path / "nope"), home=str(home)) == "minimax"
