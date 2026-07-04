"""Parity gate: mini_ork.ported.panel_bias vs lib/panel_bias.sh.

Each test invokes the LIVE bash functions from ``lib/panel_bias.sh`` via
``bash -c 'source lib/panel_bias.sh; <func> <args>'`` on identical
inputs as the Python port and asserts equivalent outputs (file presence,
byte-content, label_map shape, borda/mean_rank values, sort order,
exit codes / raised exceptions). No mocks, no hardcoded outputs beyond
what bash itself emits.

Seven cases (above the kickoff's >=6 floor):
  (a) panel_anonymize happy-path         — both write resp-{A,B,C}.md,
                                            byte-match sources via label_map
                                            round-trip; label_map keys/values
                                            match the same set.
  (b) panel_anonymize seed determinism   — same seed → identical mapping
                                            (bash via awk srand, python via
                                            random.seed; each is deterministic
                                            in-process); two seeds produce
                                            different mappings (collision
                                            retry, mirrors test_panel_bias.sh).
  (c) panel_anonymize missing dir        — bash rc=1 + stderr "not found"
                                            matches python FileNotFoundError
                                            with the path echoed in the
                                            message.
  (d) panel_rank_aggregate hand-computed — 3 reviewers (A C B / B A C / A
                                            B C), N=3 → borda A=5 B=3 C=1;
                                            mean_rank 1.3333/2.0000/2.6667;
                                            sort glm>kimi>opus.
  (e) panel_rank_aggregate tie-break     — borda+mean_rank tie → family
                                            alphabetical (alpha<bravo).
  (f) panel_permute_order determinism    — same seed → identical order;
                                            different seeds → different
                                            orders (with high prob).
  (g) panel_permute_order multiset       — output is a permutation of the
                                            source basenames (count +
                                            multiset preserved).

RNG parity: bash uses awk's RNG (linear-congruential, 20-digit
precision), Python uses ``random.Random(seed)`` (Mersenne Twister). The
two produce DIFFERENT orderings for the same seed — the test does NOT
byte-compare raw permutation order. It compares downstream observable
properties (label_map keys ⊆ A..Z, families ⊆ {glm,kimi,opus};
borda/mean_rank exact; sort-by tuple; permutation-of-source multiset).

Environment isolation: each test points subprocess env at a per-test
temp dir so bash's ``mktemp`` + label_map sibling conventions and
Python's path-based I/O both land in /tmp without polluting the repo.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import panel_bias as pb  # noqa: E402

SH = REPO / "lib" / "panel_bias.sh"

# Tolerance for floats parsed from JSON. The bash implementation emits
# mean_rank via `awk 'BEGIN{printf "%.4f", s/c}'` so the precision is
# bounded at 1e-4. 1e-6 from the kickoff is a strict superset.
_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("jq"):
        pytest.skip("jq not on PATH (required by lib/panel_bias.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/panel_bias.sh at {SH}")


def _bash_panel_anonymize(reports_dir: str, out_dir: str, seed: int) -> subprocess.CompletedProcess:
    """Invoke ``bash -c 'source lib/panel_bias.sh; panel_anonymize ...'``."""
    src = (
        f'source "{SH}"\n'
        f'panel_anonymize "{reports_dir}" "{out_dir}" {seed}\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True, text=True,
    )


def _bash_panel_rank_aggregate(xrank_dir: str, label_map: str) -> subprocess.CompletedProcess:
    """Invoke ``bash -c 'source lib/panel_bias.sh; panel_rank_aggregate ...'``."""
    src = (
        f'source "{SH}"\n'
        f'panel_rank_aggregate "{xrank_dir}" "{label_map}"\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True, text=True,
    )


def _bash_panel_permute_order(reports_dir: str, seed: int) -> subprocess.CompletedProcess:
    """Invoke ``bash -c 'source lib/panel_bias.sh; panel_permute_order ...'``."""
    src = (
        f'source "{SH}"\n'
        f'panel_permute_order "{reports_dir}" {seed}\n'
    )
    return subprocess.run(
        ["bash", "-c", src],
        capture_output=True, text=True,
    )


def _write_lens_families(reports_dir: Path, families: list[str]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    for f in families:
        (reports_dir / f"lens-{f}.md").write_text(f"LENS OUTPUT for {f}\n")


def _read_json(path: Path):
    return json.loads(path.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# (a) panel_anonymize happy-path
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_anonymize_happy_path_parity(tmp_path):
    """3 families (glm/kimi/opus), seed=42 → resp-{A,B,C}.md byte-match
    sources via label_map round-trip; label_map keys ⊆ A..Z, values ⊆
    {glm,kimi,opus}. Both sides must produce a label_map whose
    values are exactly the input families (no orphans)."""
    _which_bash()
    r = tmp_path / "reports"
    o = tmp_path / "out"
    _write_lens_families(r, ["glm", "kimi", "opus"])

    # Bash
    r_bash = _bash_panel_anonymize(str(r), str(o), 42)
    assert r_bash.returncode == 0, (
        f"bash panel_anonymize rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    )
    bash_map = _read_json(o.with_name(o.name + ".label_map.json"))
    assert set(bash_map.keys()) == {"A", "B", "C"}
    assert set(bash_map.values()) == {"glm", "kimi", "opus"}
    for label, family in bash_map.items():
        assert (o / f"resp-{label}.md").read_bytes() == (
            r / f"lens-{family}.md"
        ).read_bytes(), f"bash resp-{label}.md not byte-equal to source lens-{family}.md"

    # Reset out dir for python side
    shutil.rmtree(o)
    o.mkdir()

    # Python
    py_map = pb.panel_anonymize(str(r), str(o), 42)
    assert set(py_map.keys()) == {"A", "B", "C"}
    assert set(py_map.values()) == {"glm", "kimi", "opus"}
    for label, family in py_map.items():
        assert (o / f"resp-{label}.md").read_bytes() == (
            r / f"lens-{family}.md"
        ).read_bytes(), f"python resp-{label}.md not byte-equal to source lens-{family}.md"


# ─────────────────────────────────────────────────────────────────────────────
# (b) panel_anonymize seed determinism
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_anonymize_seed_determinism_parity(tmp_path):
    """Same seed on the SAME implementation must produce the SAME
    mapping (deterministic). Different seeds on the same implementation
    must produce different mappings (with high probability — bash test
    uses 2-re-roll collision retry). Both sides pass this on their own
    RNG; we don't compare bash-vs-python raw permutations because the
    RNG algorithms differ (awk vs random.seed)."""
    _which_bash()
    r = tmp_path / "reports"
    _write_lens_families(r, ["glm", "kimi", "opus"])

    # ── Same seed → same mapping on bash ──
    o1 = tmp_path / "out_bash_42_a"
    o2 = tmp_path / "out_bash_42_b"
    for o in (o1, o2):
        r_bash = _bash_panel_anonymize(str(r), str(o), 42)
        assert r_bash.returncode == 0
    map_a = _read_json(o1.with_name(o1.name + ".label_map.json"))
    map_b = _read_json(o2.with_name(o2.name + ".label_map.json"))
    assert map_a == map_b, f"bash seed=42 not deterministic across invocations: {map_a} vs {map_b}"

    # ── Same seed → same mapping on python ──
    py_map_a = pb.panel_anonymize(str(r), str(tmp_path / "out_py_42_a"), 42)
    py_map_b = pb.panel_anonymize(str(r), str(tmp_path / "out_py_42_b"), 42)
    assert py_map_a == py_map_b, (
        f"python seed=42 not deterministic across invocations: {py_map_a} vs {py_map_b}"
    )

    # ── Different seeds → different mapping on bash (with retry) ──
    o_seed99 = tmp_path / "out_bash_99"
    r_bash99 = _bash_panel_anonymize(str(r), str(o_seed99), 99)
    assert r_bash99.returncode == 0
    map_99 = _read_json(o_seed99.with_name(o_seed99.name + ".label_map.json"))
    tries = 0
    seed = 99
    while map_a == map_99 and tries < 2:
        tries += 1
        seed = 99 + tries
        o_retry = tmp_path / f"out_bash_{seed}"
        r_retry = _bash_panel_anonymize(str(r), str(o_retry), seed)
        assert r_retry.returncode == 0
        map_99 = _read_json(o_retry.with_name(o_retry.name + ".label_map.json"))
    assert map_a != map_99, (
        f"bash seed=42 vs seed={seed} produced identical mapping "
        f"(collision after {tries + 1} retries)"
    )

    # ── Different seeds → different mapping on python (with retry) ──
    py_seed = 99
    py_99 = pb.panel_anonymize(str(r), str(tmp_path / "out_py_99"), py_seed)
    tries = 0
    while py_map_a == py_99 and tries < 2:
        tries += 1
        py_seed = 99 + tries
        py_99 = pb.panel_anonymize(str(r), str(tmp_path / f"out_py_{py_seed}"), py_seed)
    assert py_map_a != py_99, (
        f"python seed=42 vs seed={py_seed} produced identical mapping"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) panel_anonymize missing reports_dir
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_anonymize_missing_reports_dir_parity(tmp_path):
    """Missing reports_dir → bash rc=1 + stderr "not found"; python
    FileNotFoundError with path echoed in message."""
    _which_bash()
    bogus = str(tmp_path / "no_such_reports")
    out = str(tmp_path / "out")

    r_bash = _bash_panel_anonymize(bogus, out, 0)
    assert r_bash.returncode != 0, (
        f"bash panel_anonymize rc=0 on missing dir (should fail): {r_bash.stderr}"
    )
    assert "not found" in r_bash.stderr.lower(), (
        f"bash stderr should mention 'not found', got: {r_bash.stderr!r}"
    )
    assert bogus in r_bash.stderr, f"bash stderr should echo the path: {r_bash.stderr!r}"

    with pytest.raises(FileNotFoundError) as exc:
        pb.panel_anonymize(bogus, out, 0)
    assert "not found" in str(exc.value).lower()
    assert bogus in str(exc.value), f"python error should echo the path: {exc.value!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (d) panel_rank_aggregate hand-computed (3 reviewers)
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_rank_aggregate_hand_computed_parity(tmp_path):
    """3 reviewers with rankings ('A C B', 'B A C', 'A B C'), N=3:
        borda scale p=0→2, p=1→1, p=2→0
          A: r1=2 (p=0), r2=1 (p=1), r3=2 (p=0)   → Σ = 5
          B: r1=0 (p=2), r2=2 (p=0), r3=1 (p=1)   → Σ = 3
          C: r1=1 (p=1), r2=0 (p=2), r3=0 (p=2)   → Σ = 1
        mean_rank (1-indexed average position):
          A: (1+2+1)/3 = 1.3333
          B: (3+1+2)/3 = 2.0000
          C: (2+3+3)/3 = 2.6667
      Sort: borda DESC → glm(5) > kimi(3) > opus(1). Both sides must
      emit panel-rank-aggregate.json with this exact shape + ordering.
    """
    _which_bash()
    x = tmp_path / "xrank"
    x.mkdir()
    (x / "r1.md").write_text("# R1\npreamble\nFINAL RANKING: A C B\n")
    (x / "r2.md").write_text("# R2\npreamble\nFINAL RANKING: B A C\n")
    (x / "r3.md").write_text("# R3\npreamble\nFINAL RANKING: A B C\n")
    lm = tmp_path / "label_map.json"
    lm.write_text(json.dumps({"A": "glm", "B": "kimi", "C": "opus"}))

    # Bash
    r_bash = _bash_panel_rank_aggregate(str(x), str(lm))
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    bash_out = _read_json(x / "panel-rank-aggregate.json")
    assert isinstance(bash_out, list) and len(bash_out) == 3
    bash_by_label = {e["label"]: e for e in bash_out}
    assert int(bash_by_label["A"]["borda"]) == 5
    assert int(bash_by_label["B"]["borda"]) == 3
    assert int(bash_by_label["C"]["borda"]) == 1
    assert abs(float(bash_by_label["A"]["mean_rank"]) - 1.3333) < _FLOAT_TOL
    assert abs(float(bash_by_label["B"]["mean_rank"]) - 2.0000) < _FLOAT_TOL
    assert abs(float(bash_by_label["C"]["mean_rank"]) - 2.6667) < _FLOAT_TOL
    bash_order = [e["family"] for e in bash_out]
    assert bash_order == ["glm", "kimi", "opus"], f"bash order={bash_order}"

    # Reset output and run python on the same input
    (x / "panel-rank-aggregate.json").unlink()
    py_out = pb.panel_rank_aggregate(str(x), str(lm))
    assert isinstance(py_out, list) and len(py_out) == 3
    py_by_label: dict = {e["label"]: e for e in py_out}
    assert int(str(py_by_label["A"]["borda"])) == 5
    assert int(str(py_by_label["B"]["borda"])) == 3
    assert int(str(py_by_label["C"]["borda"])) == 1
    assert abs(float(str(py_by_label["A"]["mean_rank"])) - 1.3333) < _FLOAT_TOL
    assert abs(float(str(py_by_label["B"]["mean_rank"])) - 2.0000) < _FLOAT_TOL
    assert abs(float(str(py_by_label["C"]["mean_rank"])) - 2.6667) < _FLOAT_TOL
    py_order = [e["family"] for e in py_out]
    assert py_order == ["glm", "kimi", "opus"], f"python order={py_order}"

    # The output file written by python must round-trip identically.
    py_out_file = _read_json(x / "panel-rank-aggregate.json")
    assert py_out_file == py_out


# ─────────────────────────────────────────────────────────────────────────────
# (e) panel_rank_aggregate tie-break (borda+mean_rank tie → family ASC)
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_rank_aggregate_tiebreak_parity(tmp_path):
    """N=2, 2 reviewers with anti-symmetric ranking ('A B', 'B A') →
    borda_tie=1 each, mean_rank=1.5 each. Tie broken by family
    alphabetical (alpha < bravo)."""
    _which_bash()
    x = tmp_path / "xrank_tb"
    x.mkdir()
    (x / "r1.md").write_text("FINAL RANKING: A B\n")
    (x / "r2.md").write_text("FINAL RANKING: B A\n")
    lm = tmp_path / "label_map_tb.json"
    lm.write_text(json.dumps({"A": "bravo", "B": "alpha"}))

    r_bash = _bash_panel_rank_aggregate(str(x), str(lm))
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    bash_out = _read_json(x / "panel-rank-aggregate.json")
    bash_order = [e["family"] for e in bash_out]
    assert bash_order == ["alpha", "bravo"], f"bash tie-break order={bash_order}"

    (x / "panel-rank-aggregate.json").unlink()
    py_out = pb.panel_rank_aggregate(str(x), str(lm))
    py_order = [e["family"] for e in py_out]
    assert py_order == ["alpha", "bravo"], f"python tie-break order={py_order}"


# ─────────────────────────────────────────────────────────────────────────────
# (f) panel_permute_order determinism (same seed → same order)
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_permute_order_determinism_parity(tmp_path):
    """Same seed on the SAME implementation must yield identical order
    (deterministic). Different seeds on the same implementation must
    yield different orders (with high prob). 5 files: alpha/beta/gamma
    /delta/epsilon."""
    _which_bash()
    p = tmp_path / "perm_reports"
    p.mkdir()
    for f in ("alpha", "beta", "gamma", "delta", "epsilon"):
        (p / f"{f}.md").write_text("")

    # Bash: same seed → same order
    r1 = _bash_panel_permute_order(str(p), 1)
    r2 = _bash_panel_permute_order(str(p), 1)
    assert r1.returncode == 0 and r2.returncode == 0
    assert r1.stdout == r2.stdout, (
        f"bash panel_permute_order seed=1 not deterministic:\n{r1.stdout!r}\nvs\n{r2.stdout!r}"
    )

    # Bash: different seed → different order (with retry)
    bash_seed = 2
    rb = _bash_panel_permute_order(str(p), bash_seed)
    assert rb.returncode == 0
    tries = 0
    while r1.stdout == rb.stdout and tries < 2:
        tries += 1
        bash_seed = 2 + tries
        rb = _bash_panel_permute_order(str(p), bash_seed)
        assert rb.returncode == 0
    assert r1.stdout != rb.stdout, (
        f"bash seed=1 vs seed={bash_seed} produced identical order "
        f"(collision after {tries + 1} retries)"
    )

    # Python: same seed → same order
    py1 = pb.panel_permute_order(str(p), 1)
    py2 = pb.panel_permute_order(str(p), 1)
    assert py1 == py2, f"python seed=1 not deterministic: {py1} vs {py2}"

    # Python: different seed → different order (with retry)
    py_seed = 2
    pyb = pb.panel_permute_order(str(p), py_seed)
    tries = 0
    while py1 == pyb and tries < 2:
        tries += 1
        py_seed = 2 + tries
        pyb = pb.panel_permute_order(str(p), py_seed)
    assert py1 != pyb, f"python seed=1 vs seed={py_seed} produced identical order"


# ─────────────────────────────────────────────────────────────────────────────
# (g) panel_permute_order multiset preservation
# ─────────────────────────────────────────────────────────────────────────────
def test_panel_permute_order_multiset_parity(tmp_path):
    """Both sides must emit a permutation of the source basenames —
    count + multiset preserved. 7 files of varying lengths."""
    _which_bash()
    p = tmp_path / "perm_multi"
    p.mkdir()
    files = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    for f in files:
        (p / f"{f}.md").write_text("")
    src_sorted = sorted(f"{f}.md" for f in files)

    r_bash = _bash_panel_permute_order(str(p), 7)
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode}\nstderr={r_bash.stderr}"
    bash_lines = sorted(line for line in r_bash.stdout.splitlines() if line)
    assert bash_lines == src_sorted, (
        f"bash output not a permutation of source: got {bash_lines}"
    )
    assert len(bash_lines) == len(set(bash_lines)), "bash output has duplicates"

    py_out = pb.panel_permute_order(str(p), 7)
    assert sorted(py_out) == src_sorted, (
        f"python output not a permutation of source: got {sorted(py_out)}"
    )
    assert len(py_out) == len(set(py_out)), "python output has duplicates"