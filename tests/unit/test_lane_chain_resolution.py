import textwrap

import pytest

from mini_ork.dispatch.llm_dispatch import resolve_lane_family
from mini_ork.dispatch.routing import dispatch_chain


@pytest.fixture(autouse=True)
def _no_run_dir(monkeypatch):
    """pytest inherits ``MINI_ORK_RUN_DIR`` from a wrapping mini-ork launcher;
    ``_effective_lanes`` reads its snapshot ``config/agents.yaml`` first and
    the suite would otherwise pick up this run's lane policy. The launcher
    also leaks ``MINI_ORK_AGENTS`` (overlay YAML from a previous run) which
    the per-user merge overrides the test's temp home with. Clear BOTH so
    every test sees the same no-snapshot / no-overlay baseline.
    """
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)


def _write_agents(tmp_path):
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(textwrap.dedent("""
        lanes:
          implementer: codex
          codex_lens: codex
          kimi_lens: kimi
          opus_lens: opus
    """))
    return str(home)


def test_lens_alias_resolves_to_family(tmp_path, monkeypatch):
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    assert resolve_lane_family("codex_lens") == "codex"
    assert resolve_lane_family("kimi_lens") == "kimi"


def test_plain_and_unknown_pass_through(tmp_path, monkeypatch):
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    assert resolve_lane_family("codex") == "codex"       # plain model name
    assert resolve_lane_family("nonesuch") == "nonesuch"  # unknown alias fails open


def test_missing_agents_yaml_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "nope"))
    assert resolve_lane_family("codex_lens") == "codex_lens"


def test_empty_home_never_resolves_against_cwd(tmp_path, monkeypatch):
    # RATCHET: an empty home/root used to yield the bare "config/agents.yaml",
    # which os.path.isfile resolved against the CWD. With the repo checked out
    # there, that silently adopted the REPO-DEFAULT policy instead of the run's
    # (or none) — so an alias resolved to a family the run never pinned.
    cwd = tmp_path / "cwd"
    (cwd / "config").mkdir(parents=True)
    (cwd / "config" / "agents.yaml").write_text("lanes:\n  codex_lens: codex\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("MINI_ORK_HOME", "")
    monkeypatch.setenv("MINI_ORK_ROOT", "")
    assert resolve_lane_family("codex_lens") == "codex_lens"


def test_chain_lead_is_family_not_alias(tmp_path, monkeypatch):
    # RATCHET: the exact bug — codex_lens must lead the chain with codex,
    # BEFORE the MO_FALLBACK_CODING head (minimax).
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    monkeypatch.delenv("MO_FALLBACK_CODING", raising=False)  # default: minimax,codex,sonnet
    chain = dispatch_chain("implementer", resolve_lane_family("codex_lens"))
    parts = chain.split(",")
    assert parts[0] == "codex", f"chain lead must be codex, got {parts[0]!r} in {chain!r}"
    assert parts.index("codex") < parts.index("minimax")


# ── fallback-tail lane filtering (2026-10-10) ────────────────────────────────
# RATCHET set: a dead tail lane (no providers.yaml entry) must be DROPPED from
# the chain, not walked into a terminal "unknown lane" preflight error that
# masks the lead's real failure. Observed live: quota-dead minimax lead →
# review tail walked to unmapped `sonnet` → run failed naming the wrong lane.

def _write_providers(tmp_path, *lanes):
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "providers.yaml").write_text("providers:\n" + "".join(
        f"  {lane}:\n    kind: openai-chat\n    model: m-{lane}\n"
        f"    api_key_env: K_{lane.upper()}\n" for lane in lanes))
    return str(home)


def test_dead_tail_lane_dropped(tmp_path, monkeypatch, capsys):
    home = _write_providers(tmp_path, "minimax", "codex")  # no sonnet
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    monkeypatch.delenv("MO_FALLBACK_CODING", raising=False)
    chain = dispatch_chain("implementer", "codex", root=home)
    assert chain == "codex,minimax", chain
    assert "sonnet" in capsys.readouterr().err  # the drop is logged, not silent


def test_registered_tail_lane_kept(tmp_path, monkeypatch):
    home = _write_providers(tmp_path, "minimax", "codex", "sonnet")
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    chain = dispatch_chain("implementer", "codex", root=home)
    assert chain == "codex,minimax,sonnet", chain


def test_lead_never_filtered(tmp_path, monkeypatch):
    # The lead is explicit intent — an unregistered lead must stay so preflight
    # fails loud with ITS name, not be silently swallowed by the filter.
    home = _write_providers(tmp_path, "minimax", "codex")
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    chain = dispatch_chain("implementer", "ghost_lead", root=home)
    assert chain.split(",")[0] == "ghost_lead", chain


def test_env_override_tail_also_filtered(tmp_path, monkeypatch):
    home = _write_providers(tmp_path, "minimax")
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    monkeypatch.setenv("MO_FALLBACK_CODING", "ghost,minimax")
    chain = dispatch_chain("implementer", "minimax", root=home)
    assert chain == "minimax", chain  # ghost dropped, minimax deduped into lead


def test_broken_registry_fails_open(tmp_path, monkeypatch):
    bad = tmp_path / "providers.yaml"
    bad.write_text("providers: [not, a, mapping]\n")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(bad))
    monkeypatch.delenv("MO_FALLBACK_REVIEW", raising=False)
    chain = dispatch_chain("reviewer", "opus", root=str(tmp_path))
    # Unreadable registry → no filtering → the dead entry surfaces at preflight.
    assert chain == "opus,kimi,sonnet", chain


def test_empty_registry_fails_open(tmp_path, monkeypatch):
    empty = tmp_path / "providers.yaml"
    empty.write_text("providers: {}\n")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(empty))
    monkeypatch.delenv("MO_FALLBACK_CODING", raising=False)
    chain = dispatch_chain("implementer", "codex", root=str(tmp_path))
    assert chain == "codex,minimax,sonnet", chain
