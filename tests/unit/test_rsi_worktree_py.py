"""Unit tests: the cycle's git surface, with every subprocess faked.

Two outcomes are easy to get wrong and therefore get dedicated tests:

* **``diverged`` is not ``rejected``.** The merge script pushes inside its own
  lock, so a ``--ff-only`` refusal on the local checkout means the commit is
  already on ``origin/main``. Reporting that as a rejection would quarantine
  work that shipped.
* **A conflicted revert keeps its worktree.** The conflict markers are the only
  record of what changed upstream since the merge.

The runner is faked everywhere, so these tests assert *which commands were
built* (``--owns`` per claimed file, a scoped ``MINI_ORK_TEST_CMD``) rather than
whether git works — which is the script's job and is covered by its own suite.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from mini_ork.rsi import worktree as wt_mod
from mini_ork.rsi.worktree import (
    MergeResult,
    Worktree,
    WorktreeConfig,
    WorktreeError,
    apply_patch,
    assert_worktree_root,
    claim_and_create,
    clean,
    merge,
    release,
    revert,
    safe_slug,
    write_quarantine,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mini_ork_worktree.py"


# ── a recording fake runner ────────────────────────────────────────────────


def _contains(argv: list[str], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(tuple(argv[i:i + n]) == needle for i in range(len(argv) - n + 1))


class FakeRunner:
    """Records every call; answers from the first matching rule."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rules: list[tuple[tuple[str, ...], tuple[int, str, str]]] = []
        self.default = (0, "", "")

    def on(self, *needle: str, rc: int = 0, out: str = "", err: str = "") -> "FakeRunner":
        self.rules.append((tuple(needle), (rc, out, err)))
        return self

    def __call__(self, argv, *, cwd=None, env=None, timeout_s=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env or {})})
        for needle, response in self.rules:
            if _contains(argv, needle):
                return response
        return self.default

    def matching(self, *needle: str) -> list[dict]:
        return [c for c in self.calls if _contains(c["argv"], tuple(needle))]


def _cfg(tmp_path: Path, **kw) -> WorktreeConfig:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "mini_ork_worktree.py").write_text("# stub\n", encoding="utf-8")
    base = dict(
        slug="rsi-rsi-abc123",
        files=("mini_ork/learning/ledger.py", "tests/unit/test_ledger.py"),
        worktrees_dir=tmp_path / "worktrees",
        primary=tmp_path,
        test_cmd="python3 -m pytest -q tests/unit/test_ledger.py",
    )
    base.update(kw)
    return WorktreeConfig(**base)


def _wt(cfg: WorktreeConfig) -> Worktree:
    return Worktree(slug=cfg.slug, path=cfg.path, branch=cfg.branch_name)


# ── create ─────────────────────────────────────────────────────────────────


def test_create_claims_every_file_and_verifies_the_root(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeRunner().on("rev-parse", "--show-toplevel", out=str(cfg.path))
    wt = claim_and_create(cfg, runner=fake)

    create = fake.matching("create", cfg.slug)[0]["argv"]
    assert create[-4:] == ["--owns", "mini_ork/learning/ledger.py",
                           "--owns", "tests/unit/test_ledger.py"]
    # the script is invoked through the interpreter, from the primary checkout
    assert create[1] == str(cfg.script)
    assert fake.matching("create", cfg.slug)[0]["cwd"] == str(cfg.primary)
    # and the engine-tree assertion ran
    assert fake.matching("rev-parse", "--show-toplevel")
    assert wt.path == cfg.path and wt.branch == "wt/rsi-rsi-abc123"


def test_create_failure_is_an_error_not_a_decision(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeRunner().on("create", cfg.slug, rc=1, err="path already exists")
    with pytest.raises(WorktreeError, match="path already exists"):
        claim_and_create(cfg, runner=fake)


def test_a_missing_script_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.script.unlink()
    with pytest.raises(WorktreeError, match="worktree script not found"):
        claim_and_create(cfg, runner=FakeRunner())


# ── the engine-tree assertion ──────────────────────────────────────────────


def test_root_assertion_compares_real_paths_not_spellings(tmp_path):
    real = tmp_path / "real" / "wt"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    wt = Worktree(slug="s", path=link, branch="wt/s")

    # git reports the target path while our record holds the symlink: same dir.
    fake = FakeRunner().on("--show-toplevel", out=str(real))
    assert assert_worktree_root(wt, runner=fake) == real


def test_root_assertion_refuses_the_engine_tree(tmp_path):
    wt = Worktree(slug="s", path=tmp_path / "wt", branch="wt/s")
    fake = FakeRunner().on("--show-toplevel", out=str(tmp_path))
    with pytest.raises(WorktreeError, match="outside the worktree"):
        assert_worktree_root(wt, runner=fake)


def test_root_assertion_refuses_an_unresolvable_tree(tmp_path):
    wt = Worktree(slug="s", path=tmp_path / "wt", branch="wt/s")
    fake = FakeRunner().on("--show-toplevel", rc=128, err="not a git repository")
    with pytest.raises(WorktreeError, match="cannot resolve"):
        assert_worktree_root(wt, runner=fake)


# ── apply ──────────────────────────────────────────────────────────────────


def test_apply_goes_through_land_patch_and_passes_an_abstain_through(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    wt = _wt(cfg)
    seen = {}

    def fake_land(repo, patch_text, branch, *, message=""):
        seen.update(repo=repo, patch=patch_text, branch=branch, message=message)
        return wt_mod.Abstain("publish-frozen-path", "config/kernel-frozen.yaml")

    monkeypatch.setattr(wt_mod, "land_patch", fake_land)
    result = apply_patch(wt, "diff --git a/x b/x\n", message="rsi cycle abc")
    assert isinstance(result, wt_mod.Abstain)
    assert result.reason == "publish-frozen-path"
    assert seen["repo"] == str(wt.path)
    assert seen["branch"] == ""          # "whatever HEAD points at", per land_patch
    assert seen["message"] == "rsi cycle abc"


def test_apply_returns_the_new_sha(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(wt_mod, "land_patch", lambda *a, **k: "a" * 40)
    assert apply_patch(_wt(cfg), "diff") == "a" * 40


# ── merge ──────────────────────────────────────────────────────────────────


def _merge_fake(cfg: WorktreeConfig, *, ff_rc: int = 0) -> FakeRunner:
    return (
        FakeRunner()
        .on("remote", "get-url", "origin", out="git@example.com:o/r.git\n")
        .on("merge", cfg.slug)                       # the script verb, rc 0
        .on("rev-parse", "HEAD", out="cafe" * 10 + "\n")
        .on("fetch", "origin", "main")
        .on("merge", "--ff-only", "origin/main", rc=ff_rc, err="not possible to fast-forward")
    )


def test_merge_scopes_the_green_gate_and_refreshes_the_primary(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _merge_fake(cfg)
    result = merge(cfg, runner=fake)

    script_call = fake.matching(str(cfg.script), "merge", cfg.slug)[0]
    assert script_call["env"]["MINI_ORK_TEST_CMD"] == cfg.test_cmd
    assert fake.matching("fetch", "origin", "main")
    assert fake.matching("merge", "--ff-only", "origin/main")
    assert result.status == "merged" and result.landed
    assert result.sha == "cafe" * 10


def test_merge_cleans_the_worktree_after_landing(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _merge_fake(cfg)
    merge(cfg, runner=fake)
    assert fake.matching("clean", cfg.slug)


def test_a_failed_teardown_does_not_downgrade_a_landed_merge(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _merge_fake(cfg).on("clean", cfg.slug, rc=1, err="dirty tree")
    assert merge(cfg, runner=fake).status == "merged"


def test_merge_leaves_the_worktree_alone_when_configured_to(tmp_path):
    cfg = _cfg(tmp_path, clean_after_merge=False)
    fake = _merge_fake(cfg)
    merge(cfg, runner=fake)
    assert not fake.matching("clean", cfg.slug)


def test_a_refused_fast_forward_is_diverged_and_still_landed(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _merge_fake(cfg, ff_rc=1)
    result = merge(cfg, runner=fake)
    assert result.status == "diverged"
    assert result.landed, "the push succeeded before the local ff failed"
    assert result.sha == "cafe" * 10
    assert "reset --hard" in result.detail
    # a diverged primary is not tidied away: the operator needs the tree
    assert not fake.matching("clean", cfg.slug)


def test_a_failed_script_merge_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeRunner().on("remote", "get-url", "origin", out="x\n")
    fake.on("merge", cfg.slug, rc=1, err="green gate failed (pytest)")
    result = merge(cfg, runner=fake)
    assert result.status == "rejected" and not result.landed
    assert "green gate failed" in result.detail
    # rejected before the push: never claims a sha
    assert result.sha == ""
    assert not fake.matching("merge", "--ff-only")


def test_require_remote_refuses_before_running_the_script(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeRunner().on("remote", "get-url", "origin", rc=2, err="No such remote")
    result = merge(cfg, runner=fake)
    assert result.status == "rejected"
    assert "no 'origin'" in result.detail
    assert not fake.matching("merge", cfg.slug), "must not merge without a remote"


def test_an_unresolvable_sha_after_a_successful_push_is_not_merged(tmp_path):
    cfg = _cfg(tmp_path)
    fake = (
        FakeRunner()
        .on("remote", "get-url", "origin", out="x\n")
        .on("merge", cfg.slug)
        .on("rev-parse", "HEAD", rc=128, err="bad object")
    )
    result = merge(cfg, runner=fake)
    assert result.status == "rejected" and not result.landed
    assert "HEAD could not be resolved" in result.detail


def test_merge_result_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        MergeResult("shipped")


# ── clean ──────────────────────────────────────────────────────────────────


def test_clean_failure_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(WorktreeError, match="dirty"):
        clean(cfg, runner=FakeRunner().on("clean", cfg.slug, rc=1, err="dirty"))


def test_release_asks_the_script_for_claims_only(tmp_path):
    """`release` must not become a second spelling of `clean`.

    An operator releasing a quarantine wants the claims gone and the tree kept:
    the branch is evidence, while the claims are what make the next cycle
    reject the same files with `worktree_claim_conflict`.
    """
    cfg = _cfg(tmp_path)
    fake = FakeRunner()
    release(cfg.primary, cfg.slug, runner=fake)
    assert _contains(fake.calls[-1]["argv"], ("release", cfg.slug))
    assert fake.matching("clean") == []


def test_release_does_not_need_a_merge_scope(tmp_path):
    """A release is about claims; a quarantined cycle may record no files."""
    _cfg(tmp_path)                       # only for its script stub
    fake = FakeRunner()
    release(tmp_path, "rsi-c1", runner=fake)
    assert _contains(fake.calls[-1]["argv"], ("release", "rsi-c1"))


def test_a_failed_release_is_an_error(tmp_path):
    _cfg(tmp_path)                       # only for its script stub
    with pytest.raises(WorktreeError, match="release failed"):
        release(tmp_path, "rsi-c1", runner=FakeRunner().on(
            "release", "rsi-c1", rc=1, err="no such slug"))


# ── revert ─────────────────────────────────────────────────────────────────


def test_revert_builds_a_revert_commit_then_merges_through_the_same_path(tmp_path):
    cfg = _cfg(tmp_path)
    fake = (
        FakeRunner()
        .on("rev-parse", "--show-toplevel", out=str(tmp_path / "worktrees" / "revert-rsi-rsi-abc123"))
        .on("revert", "--no-edit")
        .on("remote", "get-url", "origin", out="x\n")
        .on("rev-parse", "HEAD", out="beef" * 10 + "\n")
    )
    result = revert(cfg, "abc123", runner=fake)
    assert result.status == "merged" and result.sha == "beef" * 10
    assert fake.matching("revert", "--no-edit", "abc123")
    # a fresh worktree, claim-first, then the ordinary merge verb
    assert fake.matching("create", "revert-rsi-rsi-abc123", "--owns")
    assert fake.matching(str(cfg.script), "merge", "revert-rsi-rsi-abc123")


def test_a_conflicted_revert_is_rejected_and_keeps_its_worktree(tmp_path):
    cfg = _cfg(tmp_path)
    fake = (
        FakeRunner()
        .on("rev-parse", "--show-toplevel", out=str(tmp_path / "worktrees" / "revert-rsi-rsi-abc123"))
        .on("revert", "--no-edit", rc=1, err="CONFLICT (content): merge conflict in x.py")
    )
    result = revert(cfg, "abc123", runner=fake)
    assert result.status == "rejected" and not result.landed
    assert "worktree is kept" in result.detail
    assert not fake.matching("clean"), "a conflicted revert must not be tidied away"


def test_revert_without_a_sha_is_rejected(tmp_path):
    assert revert(_cfg(tmp_path), "  ").status == "rejected"


# ── quarantine record ──────────────────────────────────────────────────────


def test_quarantine_record_is_written_outside_the_worktree_and_atomically(tmp_path):
    record_dir = tmp_path / "primary" / ".mini-ork" / "rsi" / "quarantine"
    path = write_quarantine({"cycle_id": "c1", "reason": "critic_split"},
                            record_dir=record_dir, cycle_id="c1")
    assert path.parent == record_dir
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "cycle_id": "c1", "reason": "critic_split"}
    assert list(record_dir.glob("*.tmp")) == [], "no temp file left behind"


def test_quarantine_refuses_a_path_walking_cycle_id(tmp_path):
    # Cycle ids are generated by us, so this only fires on an upstream bug —
    # and writing "../../escape.json" is not the way to absorb one.
    with pytest.raises(WorktreeError):
        write_quarantine({}, record_dir=tmp_path, cycle_id="../../escape")
    assert list(tmp_path.rglob("*.json")) == []


# ── config and slugs fail closed ───────────────────────────────────────────


@pytest.mark.parametrize("field,value", [
    ("files", ()),
    ("test_cmd", "   "),
    ("primary", ""),
])
def test_config_refuses_a_cycle_that_cannot_be_gated_or_bounded(tmp_path, field, value):
    with pytest.raises(ValueError):
        _cfg(tmp_path, **{field: value})


def test_config_refuses_a_slug_it_would_have_to_rewrite(tmp_path):
    with pytest.raises(ValueError, match="not already safe"):
        _cfg(tmp_path, slug="rsi cycle 1")
    with pytest.raises(ValueError, match="unusable slug"):
        _cfg(tmp_path, slug="rsi/../etc")


@pytest.mark.parametrize("slug", [".", "..", "a/../b", ""])
def test_safe_slug_refuses_path_walking(slug):
    with pytest.raises(WorktreeError):
        safe_slug(slug)


def test_safe_slug_matches_the_worktree_script():
    spec = importlib.util.spec_from_file_location("mo_worktree_script_slug", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for raw in ("rsi-abc", "rsi cycle 1", "-lead-", "trail-", "a.b_c-d", "a/b"):
        assert safe_slug(raw) == module.sanitize_slug(raw), raw
