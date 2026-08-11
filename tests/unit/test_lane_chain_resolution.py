import textwrap

from mini_ork.dispatch.llm_dispatch import resolve_lane_family
from mini_ork.dispatch.routing import dispatch_chain


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
