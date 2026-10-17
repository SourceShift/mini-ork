"""Lane-identity provenance for the run profile (LF-R7b).

``gen_profile`` used to store only lane ALIASES, so a profile reader could not
tell which model family a role actually got without resolving ``providers.yaml``
by hand. This covers the two additions:

* ``agents_config.lane_families`` — pure alias → ``{lane, model, family}``;
* ``agents_config.lane_family_warnings`` + the ``effective_path()`` advisory —
  one stderr line per family claimed by more than one ``*_lens`` alias;
* ``gen_profile`` recording both alongside the untouched ``provider_policy.lanes``.

Hermetic: temp dirs, a pinned providers registry via ``MINI_ORK_PROVIDERS``
(first in the loader's precedence), and temp agents/template trees — never the
operator's ``.mini-ork/config/agents.local.yaml``, never the live DB.
"""

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mini_ork.dispatch import agents_config
from mini_ork.verify import panel_heterogeneity

# The live overlay's lens shape: six aliases onto three lanes, three families.
LENS_OVERLAY = {
    "opus_lens": "glm",
    "glm_lens": "glm",
    "kimi_lens": "minimax",
    "minimax_lens": "minimax",
    "codex_lens": "deepseek_flash",
    "deepseek_lens": "deepseek_flash",
}

TAIL_ZAI = (
    "all resolve to family 'zai' (lane glm) — a panel using them is not "
    "family-heterogeneous"
)
TAIL_MINIMAX = (
    "all resolve to family 'minimax' (lane minimax) — a panel using them is "
    "not family-heterogeneous"
)
TAIL_DEEPSEEK = (
    "all resolve to family 'deepseek' (lane deepseek_flash) — a panel using "
    "them is not family-heterogeneous"
)

# glm -> glm-4.7 -> zai; minimax -> MiniMax-M2 -> minimax;
# deepseek_flash -> deepseek-chat-v3 -> deepseek.
REGISTRY_YAML = (
    "providers:\n"
    "  glm: {model: glm-4.7}\n"
    "  minimax: {model: MiniMax-M2}\n"
    "  deepseek_flash: {model: deepseek-chat-v3}\n"
)


@pytest.fixture
def pinned_registry(tmp_path, monkeypatch):
    """Point every lane resolution at a temp registry, isolated from the
    operator's home and the repo copy."""
    reg = tmp_path / "spec-providers.yaml"
    reg.write_text(REGISTRY_YAML, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    return reg


def _corrupt_registry(tmp_path, monkeypatch):
    """A registry file the loader itself rejects ('providers' not a mapping)."""
    bad = tmp_path / "broken-providers.yaml"
    bad.write_text("providers: [not, a, mapping]\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(bad))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    return bad


def _make_home(tmp_path, template_lanes, overlay_lanes=None, name="home"):
    """A temp ``MINI_ORK_HOME``-shaped tree: template + optional overlay."""
    home = tmp_path / name
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "agents.yaml").write_text(
        yaml.safe_dump({"lanes": template_lanes}, sort_keys=True), encoding="utf-8")
    if overlay_lanes is not None:
        (cfg / "agents.local.yaml").write_text(
            yaml.safe_dump({"lanes": overlay_lanes}, sort_keys=True), encoding="utf-8")
    return home


def _gen_profile_tree(tmp_path, lanes):
    """Temp kickoff + temp agents file + minimal recipe tree for gen_profile."""
    root = tmp_path / "proot"
    recipe = root / "recipes" / "spectest"
    recipe.mkdir(parents=True, exist_ok=True)
    (recipe / "task_class.yaml").write_text("{}\n", encoding="utf-8")
    (recipe / "artifact_contract.yaml").write_text("outputs: []\n", encoding="utf-8")
    kickoff = root / "kickoff.md"
    kickoff.write_text(
        "# Spec test kickoff\n\n"
        "## Verification commands\n\n"
        "```bash\necho ok\n```\n",
        encoding="utf-8")
    agents = root / "agents.yaml"
    agents.write_text(yaml.safe_dump({"lanes": lanes}, sort_keys=True), encoding="utf-8")
    profile = root / "run_profile.json"
    return kickoff, root, agents, profile


# ---------------------------------------------------------------------------
# lane_families
# ---------------------------------------------------------------------------


def test_lane_families_resolves_alias_to_lane_model_family(pinned_registry):
    fams = agents_config.lane_families({"lanes": dict(LENS_OVERLAY)})
    assert set(fams) == set(LENS_OVERLAY)
    for alias, lane in LENS_OVERLAY.items():
        entry = fams[alias]
        assert set(entry) == {"lane", "model", "family"}, entry
        assert entry["lane"] == lane
        assert entry["model"] == panel_heterogeneity.lane_model(lane)
        assert entry["family"] == panel_heterogeneity.lane_family(lane)
    # The overlay's whole point: names disagree with families.
    assert fams["opus_lens"]["family"] == fams["glm_lens"]["family"] == "zai"
    assert fams["kimi_lens"]["family"] == fams["minimax_lens"]["family"] == "minimax"
    assert fams["codex_lens"]["family"] == fams["deepseek_lens"]["family"] == "deepseek"


def test_lane_families_skips_non_string_and_empty_values(pinned_registry):
    fams = agents_config.lane_families({"lanes": {
        "opus_lens": None,
        "kimi_lens": "",
        "num_lens": 7,
        "blank_lens": "glm",
    }})
    assert set(fams) == {"blank_lens"}
    assert fams["blank_lens"]["lane"] == "glm"


def test_lane_families_unknown_lane_is_own_family(pinned_registry):
    fams = agents_config.lane_families({"lanes": {"probe_lens": "mystery_api"}})
    assert fams["probe_lens"] == {
        "lane": "mystery_api",
        "model": "mystery_api",
        "family": "mystery_api",
    }


def test_lane_families_never_raises_on_corrupt_registry(tmp_path, monkeypatch):
    _corrupt_registry(tmp_path, monkeypatch)
    fams = agents_config.lane_families({"lanes": {"probe_lens": "glm"}})
    assert isinstance(fams, dict)
    # The fallback is the lane name as its own family, not an exception.
    assert fams["probe_lens"] == {
        "lane": "glm", "model": "glm", "family": "glm"}


def test_lane_families_tolerates_missing_or_odd_lanes():
    assert agents_config.lane_families({}) == {}
    assert agents_config.lane_families({"lanes": None}) == {}
    assert agents_config.lane_families({"lanes": ["glm"]}) == {}


# ---------------------------------------------------------------------------
# lane_family_warnings
# ---------------------------------------------------------------------------


def test_lane_family_warnings_three_sorted_lines_for_the_real_overlay(pinned_registry):
    warnings = agents_config.lane_family_warnings({"lanes": dict(LENS_OVERLAY)})
    assert warnings == [
        f"lane aliases {', '.join(sorted(['codex_lens', 'deepseek_lens']))} "
        f"{TAIL_DEEPSEEK}",
        f"lane aliases {', '.join(sorted(['glm_lens', 'opus_lens']))} {TAIL_ZAI}",
        f"lane aliases {', '.join(sorted(['kimi_lens', 'minimax_lens']))} "
        f"{TAIL_MINIMAX}",
    ]
    assert warnings == sorted(warnings)


def test_lane_family_warnings_distinct_families_is_empty(pinned_registry):
    agents = {"lanes": {"a_lens": "glm", "b_lens": "minimax",
                        "c_lens": "deepseek_flash"}}
    assert agents_config.lane_family_warnings(agents) == []


def test_lane_family_warnings_skipped_values_produce_no_line(pinned_registry):
    assert agents_config.lane_family_warnings(
        {"lanes": {"opus_lens": None, "glm_lens": None}}) == []
    assert agents_config.lane_family_warnings(
        {"lanes": {"opus_lens": "", "kimi_lens": "minimax"}}) == []


def test_lane_family_warnings_only_counts_lens_aliases(pinned_registry):
    # Ordinary role aliases sharing a lane are not a judge panel; the sweep is
    # restricted to ``*_lens`` so the real overlay yields exactly three lines.
    agents = {"lanes": {
        "implementer": "minimax", "worker": "minimax", "verifier": "minimax",
        "opus_lens": "glm", "glm_lens": "glm",
    }}
    warnings = agents_config.lane_family_warnings(agents)
    assert len(warnings) == 1
    assert TAIL_ZAI in warnings[0]


def test_lane_family_warnings_never_raises_on_corrupt_registry(tmp_path, monkeypatch):
    _corrupt_registry(tmp_path, monkeypatch)
    warnings = agents_config.lane_family_warnings({"lanes": dict(LENS_OVERLAY)})
    assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# effective_path advisory
# ---------------------------------------------------------------------------


def _effective(home, monkeypatch, warn_env=None):
    if warn_env is None:
        monkeypatch.delenv("MO_LANE_FAMILY_WARN", raising=False)
    else:
        monkeypatch.setenv("MO_LANE_FAMILY_WARN", warn_env)
    return agents_config.effective_path(home=str(home))


def test_effective_path_warns_by_default(capsys, tmp_path, monkeypatch, pinned_registry):
    home = _make_home(tmp_path, template_lanes={}, overlay_lanes={
        "opus_lens": "glm", "glm_lens": "glm"}, name="home-default")
    returned = _effective(home, monkeypatch)
    captured = capsys.readouterr()
    assert returned == os.path.join(str(home), "config", ".agents.effective.yaml")
    assert captured.err.count("[agents_config] ") == 1
    assert "[agents_config] " not in captured.out
    assert TAIL_ZAI in captured.err


def test_effective_path_silent_with_switch_at_0(capsys, tmp_path, monkeypatch, pinned_registry):
    home = _make_home(tmp_path, template_lanes={}, overlay_lanes={
        "opus_lens": "glm", "glm_lens": "glm"}, name="home-zero")
    returned = _effective(home, monkeypatch, warn_env="0")
    captured = capsys.readouterr()
    assert returned == os.path.join(str(home), "config", ".agents.effective.yaml")
    assert "[agents_config]" not in captured.err


def test_effective_path_no_overlay_branch_warns_too(capsys, tmp_path, monkeypatch,
                                                    pinned_registry):
    home = _make_home(tmp_path, template_lanes={
        "opus_lens": "glm", "glm_lens": "glm"}, overlay_lanes=None,
        name="home-nooverlay")
    monkeypatch.delenv("MO_LANE_FAMILY_WARN", raising=False)
    returned = agents_config.effective_path(home=str(home))
    captured = capsys.readouterr()
    assert returned == os.path.join(str(home), "config", "agents.yaml")
    assert not (home / "config" / ".agents.effective.yaml").exists()
    assert TAIL_ZAI in captured.err


def test_effective_path_three_lines_and_merge_unblocked(capsys, tmp_path, monkeypatch,
                                                         pinned_registry):
    home = _make_home(tmp_path, template_lanes={}, overlay_lanes=dict(LENS_OVERLAY),
                      name="home-real")
    agents_config.effective_path(home=str(home))
    captured = capsys.readouterr()
    assert captured.err.count("[agents_config] ") == 3
    assert TAIL_ZAI in captured.err
    assert TAIL_MINIMAX in captured.err
    assert TAIL_DEEPSEEK in captured.err
    body = (home / "config" / ".agents.effective.yaml").read_text(encoding="utf-8")
    for alias in LENS_OVERLAY:
        assert alias in body


def test_effective_path_never_raises_on_corrupt_registry(capsys, tmp_path, monkeypatch):
    _corrupt_registry(tmp_path, monkeypatch)
    home = _make_home(tmp_path, template_lanes={}, overlay_lanes=dict(LENS_OVERLAY),
                      name="home-corrupt")
    returned = agents_config.effective_path(home=str(home))
    assert returned == os.path.join(str(home), "config", ".agents.effective.yaml")
    body = (home / "config" / ".agents.effective.yaml").read_text(encoding="utf-8")
    for alias in LENS_OVERLAY:
        assert alias in body, "the warning must never block the merge"
    capsys.readouterr()


# ---------------------------------------------------------------------------
# gen_profile
# ---------------------------------------------------------------------------


def test_gen_profile_records_families_and_warnings(tmp_path, monkeypatch,
                                                   pinned_registry):
    from mini_ork.cli import main as cli_main

    kickoff, root, agents, profile = _gen_profile_tree(tmp_path, dict(LENS_OVERLAY))
    data = cli_main.gen_profile(str(kickoff), str(root), "spectest",
                                "framework_edit", str(profile), str(agents),
                                recipe_base=str(root))
    policy = data["provider_policy"]
    # lanes stay exactly as configured.
    assert policy["lanes"] == LENS_OVERLAY
    assert policy["lanes"] == yaml.safe_load(agents.read_text(encoding="utf-8"))["lanes"]
    # the resolution and the advisory rides alongside.
    assert policy["lane_families"] == agents_config.lane_families(
        {"lanes": dict(LENS_OVERLAY)})
    assert policy["lane_family_warnings"] == agents_config.lane_family_warnings(
        {"lanes": dict(LENS_OVERLAY)})
    assert len(policy["lane_family_warnings"]) == 3
    assert policy["lane_families"]["opus_lens"]["family"] == "zai"
    on_disk = json.loads(profile.read_text(encoding="utf-8"))
    assert on_disk["provider_policy"]["lanes"] == LENS_OVERLAY
    assert on_disk["provider_policy"]["lane_families"] == policy["lane_families"]
    assert (on_disk["provider_policy"]["lane_family_warnings"]
            == policy["lane_family_warnings"])


def test_gen_profile_docstring_says_configured_not_served():
    from mini_ork.cli import main as cli_main

    doc = inspect.getdoc(cli_main.gen_profile) or ""
    assert "CONFIGURED" in doc
    assert "llm_calls.model_id" in doc
    assert "traces.agent_version_id" in doc


def test_gen_profile_or_reuse_does_not_mutate_existing_profile(
        tmp_path, monkeypatch, pinned_registry):
    from mini_ork.cli import main as cli_main

    kickoff, root, agents, profile = _gen_profile_tree(tmp_path, dict(LENS_OVERLAY))
    existing = {
        "schema_version": "1.0",
        "profile_status": "ready",
        "answers": {"Which files are in scope?": "mini_ork/dispatch/agents_config.py"},
        "provider_policy": {
            "source": str(agents),
            "lanes": dict(LENS_OVERLAY),
            "env": {"MINI_ORK_PROVIDER_POLICY": str(agents)},
        },
    }
    profile.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
    before = profile.read_bytes()
    out = cli_main._gen_profile_or_reuse(str(kickoff), str(root), "spectest",
                                         "framework_edit", str(profile),
                                         str(agents), str(root))
    assert profile.read_bytes() == before
    assert "lane_families" not in out["provider_policy"]
    assert "lane_family_warnings" not in out["provider_policy"]


# ---------------------------------------------------------------------------
# Non-regression pins
# ---------------------------------------------------------------------------


def test_lazy_import_keeps_verify_off_dispatch_import_path():
    """A module-level import would put ``verify`` on ``dispatch``'s path."""
    repo_root = Path(agents_config.__file__).resolve().parents[2]
    code = (
        "import sys\n"
        "import mini_ork.dispatch.agents_config\n"
        "print(any(m == 'mini_ork.verify' or m.startswith('mini_ork.verify.') "
        "for m in sys.modules))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(repo_root),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"

    tree = ast.parse(Path(agents_config.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert "panel_heterogeneity" not in name
            assert not name.startswith("mini_ork.verify")


def test_merge_and_effective_filename_unchanged(pinned_registry):
    base = {"lanes": {"implementer": "minimax", "opus_lens": "glm"}}
    over = {"lanes": {"opus_lens": "glm", "glm_lens": "glm"}}
    merged = agents_config.merge(dict(base), dict(over))
    assert merged == {"lanes": {
        "implementer": "minimax", "opus_lens": "glm", "glm_lens": "glm"}}
    assert base == {"lanes": {"implementer": "minimax", "opus_lens": "glm"}}
