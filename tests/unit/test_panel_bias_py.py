"""Standalone unit tests for ``mini_ork.ported.panel_bias``.

Replaces the bash-parity gate as part of the bash→Python migration: the
Python port is now the sole implementation, so its coverage no longer runs
``lib/panel_bias.sh`` in a subprocess (via ``bash -c 'source ...'``) — it
asserts the port's behaviour directly. These pin the deterministic contract
adversarial panel review depends on: anonymize (lens-*.md → resp-<LABEL>.md
+ sibling label_map.json), rank_aggregate (Borda + mean_rank + tie-break
sort), and permute_order (seed-deterministic shuffle), independent of any
bash oracle. All filesystem I/O is isolated to pytest's ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from mini_ork.ported.panel_bias import (
    _LABELS,
    _iter_rankings,
    _list_lens_files,
    _shuffle_lines,
    panel_anonymize,
    panel_permute_order,
    panel_rank_aggregate,
)


def _write_lens_families(reports_dir, families: list[str]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    for f in families:
        (reports_dir / f"lens-{f}.md").write_text(f"LENS OUTPUT for {f}\n")


class TestLabelsConstant:
    def test_is_26_uppercase_letters_a_to_z(self):
        # Guards the label alphabet against silent drift (mirrors bash
        # _PB_LABELS=(A B C ... Z)).
        assert _LABELS == tuple(chr(ord("A") + i) for i in range(26))
        assert len(_LABELS) == 26
        assert _LABELS[0] == "A"
        assert _LABELS[-1] == "Z"


class TestListLensFiles:
    def test_globs_lens_prefixed_md_files_sorted(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["opus", "glm", "kimi"])
        assert _list_lens_files(str(r)) == ["lens-glm.md", "lens-kimi.md", "lens-opus.md"]

    def test_ignores_non_matching_files(self, tmp_path):
        r = tmp_path / "reports"
        r.mkdir()
        (r / "lens-glm.md").write_text("x")
        (r / "notlens.md").write_text("x")
        (r / "lens-glm.txt").write_text("x")
        (r / "readme.md").write_text("x")
        assert _list_lens_files(str(r)) == ["lens-glm.md"]

    def test_ignores_directories_matching_the_pattern(self, tmp_path):
        r = tmp_path / "reports"
        r.mkdir()
        (r / "lens-glm.md").write_text("x")
        (r / "lens-dir.md").mkdir()  # matches the glob pattern but is a dir
        assert _list_lens_files(str(r)) == ["lens-glm.md"]

    def test_missing_dir_raises_file_not_found(self, tmp_path):
        bogus = str(tmp_path / "nope")
        with pytest.raises(FileNotFoundError) as exc:
            _list_lens_files(bogus)
        assert bogus in str(exc.value)

    def test_empty_dir_returns_empty_list(self, tmp_path):
        r = tmp_path / "reports"
        r.mkdir()
        assert _list_lens_files(str(r)) == []


class TestShuffleLines:
    def test_same_seed_is_deterministic(self):
        lines = ["a", "b", "c", "d", "e"]
        assert _shuffle_lines(lines, 7) == _shuffle_lines(lines, 7)

    def test_different_seeds_can_differ(self):
        lines = ["a", "b", "c", "d", "e"]
        out1 = _shuffle_lines(lines, 1)
        out2 = _shuffle_lines(lines, 2)
        assert out1 != out2  # not a hard guarantee in general, true for this input/seed pair

    def test_output_is_a_permutation_of_input(self):
        lines = ["alpha", "bravo", "charlie", "delta"]
        out = _shuffle_lines(lines, 42)
        assert sorted(out) == sorted(lines)
        assert len(out) == len(lines)

    def test_empty_input_is_empty_output(self):
        assert _shuffle_lines([], 0) == []

    def test_does_not_mutate_input_list(self):
        lines = ["a", "b", "c"]
        original = list(lines)
        _shuffle_lines(lines, 3)
        assert lines == original


class TestIterRankings:
    def test_extracts_final_ranking_tokens(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("# Reviewer 1\npreamble\nFINAL RANKING: A C B\n")
        out = _iter_rankings(str(x))
        assert out == [["A", "C", "B"]]

    def test_takes_first_matching_line_only(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text(
            "FINAL RANKING: A B\nnotes\nFINAL RANKING: B A\n"
        )
        out = _iter_rankings(str(x))
        assert out == [["A", "B"]]

    def test_skips_files_without_the_marker(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("FINAL RANKING: A B\n")
        (x / "r2.md").write_text("no marker here\n")
        out = _iter_rankings(str(x))
        assert out == [["A", "B"]]

    def test_no_matching_files_raises_value_error(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("nothing relevant\n")
        with pytest.raises(ValueError, match="FINAL RANKING"):
            _iter_rankings(str(x))

    def test_missing_dir_raises_file_not_found(self, tmp_path):
        bogus = str(tmp_path / "nope")
        with pytest.raises(FileNotFoundError):
            _iter_rankings(bogus)

    def test_ignores_subdirectories(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("FINAL RANKING: A B\n")
        (x / "subdir").mkdir()
        (x / "subdir" / "r2.md").write_text("FINAL RANKING: B A\n")
        out = _iter_rankings(str(x))
        assert out == [["A", "B"]]


class TestPanelAnonymize:
    def test_happy_path_byte_matches_source_via_label_map(self, tmp_path):
        r = tmp_path / "reports"
        o = tmp_path / "out"
        _write_lens_families(r, ["glm", "kimi", "opus"])

        label_map = panel_anonymize(str(r), str(o), 42)

        assert set(label_map.keys()) == {"A", "B", "C"}
        assert set(label_map.values()) == {"glm", "kimi", "opus"}
        for label, family in label_map.items():
            assert (o / f"resp-{label}.md").read_bytes() == (
                r / f"lens-{family}.md"
            ).read_bytes()

    def test_label_map_written_as_sibling_not_inside_out_dir(self, tmp_path):
        r = tmp_path / "reports"
        o = tmp_path / "out"
        _write_lens_families(r, ["glm", "kimi"])

        panel_anonymize(str(r), str(o), 0)

        sibling = o.with_name(o.name + ".label_map.json")
        assert sibling.is_file()
        assert not (o / "label_map.json").exists()

    def test_returned_dict_matches_written_file(self, tmp_path):
        r = tmp_path / "reports"
        o = tmp_path / "out"
        _write_lens_families(r, ["glm", "kimi"])

        returned = panel_anonymize(str(r), str(o), 5)
        on_disk = json.loads(o.with_name(o.name + ".label_map.json").read_text())
        assert returned == on_disk

    def test_same_seed_is_deterministic_across_invocations(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm", "kimi", "opus"])

        map_a = panel_anonymize(str(r), str(tmp_path / "out_a"), 42)
        map_b = panel_anonymize(str(r), str(tmp_path / "out_b"), 42)
        assert map_a == map_b

    def test_different_seeds_can_produce_different_mappings(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm", "kimi", "opus"])

        map_42 = panel_anonymize(str(r), str(tmp_path / "out_42"), 42)
        map_99 = panel_anonymize(str(r), str(tmp_path / "out_99"), 99)
        assert map_42 != map_99

    def test_default_seed_is_zero(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm", "kimi"])

        explicit = panel_anonymize(str(r), str(tmp_path / "out_explicit"), 0)
        default = panel_anonymize(str(r), str(tmp_path / "out_default"))
        assert explicit == default

    def test_missing_reports_dir_raises_with_path_in_message(self, tmp_path):
        bogus = str(tmp_path / "no_such_reports")
        out = str(tmp_path / "out")
        with pytest.raises(FileNotFoundError) as exc:
            panel_anonymize(bogus, out, 0)
        assert "not found" in str(exc.value).lower()
        assert bogus in str(exc.value)

    def test_no_lens_files_raises_file_not_found(self, tmp_path):
        r = tmp_path / "reports"
        r.mkdir()
        (r / "readme.md").write_text("not a lens file")
        with pytest.raises(FileNotFoundError, match="no lens-\\*.md"):
            panel_anonymize(str(r), str(tmp_path / "out"), 0)

    def test_more_than_26_families_raises_value_error(self, tmp_path):
        r = tmp_path / "reports"
        families = [f"family{i:02d}" for i in range(27)]
        _write_lens_families(r, families)
        with pytest.raises(ValueError, match="more than 26"):
            panel_anonymize(str(r), str(tmp_path / "out"), 0)

    def test_multi_segment_family_name_preserved_verbatim(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm-4.5"])
        label_map = panel_anonymize(str(r), str(tmp_path / "out"), 0)
        assert set(label_map.values()) == {"glm-4.5"}

    def test_out_dir_is_created_if_missing_including_parents(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm"])
        o = tmp_path / "nested" / "deeper" / "out"
        panel_anonymize(str(r), str(o), 0)
        assert o.is_dir()
        assert (o / "resp-A.md").is_file()

    def test_out_dir_already_existing_is_not_an_error(self, tmp_path):
        r = tmp_path / "reports"
        _write_lens_families(r, ["glm"])
        o = tmp_path / "out"
        o.mkdir()
        panel_anonymize(str(r), str(o), 0)  # should not raise
        assert (o / "resp-A.md").is_file()


class TestPanelRankAggregate:
    def _write_hand_computed_fixture(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("# R1\npreamble\nFINAL RANKING: A C B\n")
        (x / "r2.md").write_text("# R2\npreamble\nFINAL RANKING: B A C\n")
        (x / "r3.md").write_text("# R3\npreamble\nFINAL RANKING: A B C\n")
        lm = tmp_path / "label_map.json"
        lm.write_text(json.dumps({"A": "glm", "B": "kimi", "C": "opus"}))
        return x, lm

    def test_hand_computed_borda_and_mean_rank(self, tmp_path):
        # 3 reviewers ('A C B', 'B A C', 'A B C'), N=3:
        #   Borda scale p=0→2, p=1→1, p=2→0.
        #     A: r1=2, r2=1, r3=2 → Σ=5     mean_rank: (1+2+1)/3 = 1.3333
        #     B: r1=0, r2=2, r3=1 → Σ=3     mean_rank: (3+1+2)/3 = 2.0000
        #     C: r1=1, r2=0, r3=0 → Σ=1     mean_rank: (2+3+3)/3 = 2.6667
        x, lm = self._write_hand_computed_fixture(tmp_path)

        out = panel_rank_aggregate(str(x), str(lm))

        by_label = {e["label"]: e for e in out}
        assert by_label["A"]["borda"] == 5
        assert by_label["B"]["borda"] == 3
        assert by_label["C"]["borda"] == 1
        assert by_label["A"]["mean_rank"] == pytest.approx(1.3333)
        assert by_label["B"]["mean_rank"] == pytest.approx(2.0000)
        assert by_label["C"]["mean_rank"] == pytest.approx(2.6667)

    def test_sort_order_is_borda_desc(self, tmp_path):
        x, lm = self._write_hand_computed_fixture(tmp_path)
        out = panel_rank_aggregate(str(x), str(lm))
        assert [e["family"] for e in out] == ["glm", "kimi", "opus"]

    def test_output_file_round_trips_the_return_value(self, tmp_path):
        x, lm = self._write_hand_computed_fixture(tmp_path)
        out = panel_rank_aggregate(str(x), str(lm))
        on_disk = json.loads((x / "panel-rank-aggregate.json").read_text())
        assert on_disk == out

    def test_tie_break_falls_back_to_family_alphabetical(self, tmp_path):
        # N=2, anti-symmetric rankings force a Borda AND mean_rank tie;
        # the sort must then fall back to family name ascending.
        x = tmp_path / "xrank_tb"
        x.mkdir()
        (x / "r1.md").write_text("FINAL RANKING: A B\n")
        (x / "r2.md").write_text("FINAL RANKING: B A\n")
        lm = tmp_path / "label_map_tb.json"
        lm.write_text(json.dumps({"A": "bravo", "B": "alpha"}))

        out = panel_rank_aggregate(str(x), str(lm))

        assert [e["family"] for e in out] == ["alpha", "bravo"]
        assert out[0]["borda"] == out[1]["borda"]
        assert out[0]["mean_rank"] == out[1]["mean_rank"]

    def test_label_missing_from_label_map_falls_back_to_unknown(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("FINAL RANKING: A Z\n")
        lm = tmp_path / "label_map.json"
        lm.write_text(json.dumps({"A": "glm"}))  # Z is not in the map

        out = panel_rank_aggregate(str(x), str(lm))

        by_label = {e["label"]: e for e in out}
        assert by_label["Z"]["family"] == "unknown"
        assert by_label["A"]["family"] == "glm"

    def test_missing_xrank_dir_raises_file_not_found(self, tmp_path):
        bogus = str(tmp_path / "nope")
        lm = tmp_path / "label_map.json"
        lm.write_text("{}")
        with pytest.raises(FileNotFoundError):
            panel_rank_aggregate(bogus, str(lm))

    def test_missing_label_map_raises_file_not_found(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("FINAL RANKING: A B\n")
        bogus_lm = str(tmp_path / "no_such_label_map.json")
        with pytest.raises(FileNotFoundError):
            panel_rank_aggregate(str(x), bogus_lm)

    def test_no_reviewer_file_with_marker_raises_value_error(self, tmp_path):
        x = tmp_path / "xrank"
        x.mkdir()
        (x / "r1.md").write_text("nothing relevant here\n")
        lm = tmp_path / "label_map.json"
        lm.write_text("{}")
        with pytest.raises(ValueError, match="FINAL RANKING"):
            panel_rank_aggregate(str(x), str(lm))


class TestPanelPermuteOrder:
    def _write_md_files(self, dirpath, names: list[str]) -> None:
        dirpath.mkdir(parents=True, exist_ok=True)
        for n in names:
            (dirpath / f"{n}.md").write_text("")

    def test_same_seed_is_deterministic(self, tmp_path):
        p = tmp_path / "perm_reports"
        self._write_md_files(p, ["alpha", "beta", "gamma", "delta", "epsilon"])

        out1 = panel_permute_order(str(p), 1)
        out2 = panel_permute_order(str(p), 1)
        assert out1 == out2

    def test_different_seeds_can_produce_different_orders(self, tmp_path):
        p = tmp_path / "perm_reports"
        self._write_md_files(p, ["alpha", "beta", "gamma", "delta", "epsilon"])

        out1 = panel_permute_order(str(p), 1)
        out2 = panel_permute_order(str(p), 2)
        assert out1 != out2

    def test_output_is_a_permutation_of_source_basenames(self, tmp_path):
        p = tmp_path / "perm_multi"
        files = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
        self._write_md_files(p, files)

        out = panel_permute_order(str(p), 7)

        assert sorted(out) == sorted(f"{f}.md" for f in files)
        assert len(out) == len(set(out))

    def test_default_seed_is_zero(self, tmp_path):
        p = tmp_path / "perm_reports"
        self._write_md_files(p, ["alpha", "beta", "gamma"])

        explicit = panel_permute_order(str(p), 0)
        default = panel_permute_order(str(p))
        assert explicit == default

    def test_ignores_non_md_files(self, tmp_path):
        p = tmp_path / "perm_reports"
        p.mkdir()
        (p / "alpha.md").write_text("")
        (p / "notes.txt").write_text("")
        (p / "README").write_text("")

        out = panel_permute_order(str(p), 0)
        assert out == ["alpha.md"]

    def test_ignores_subdirectories_even_if_named_like_md(self, tmp_path):
        p = tmp_path / "perm_reports"
        p.mkdir()
        (p / "alpha.md").write_text("")
        (p / "subdir.md").mkdir()

        out = panel_permute_order(str(p), 0)
        assert out == ["alpha.md"]

    def test_empty_dir_returns_empty_list(self, tmp_path):
        p = tmp_path / "perm_reports"
        p.mkdir()
        assert panel_permute_order(str(p), 0) == []

    def test_missing_dir_raises_file_not_found(self, tmp_path):
        bogus = str(tmp_path / "nope")
        with pytest.raises(FileNotFoundError) as exc:
            panel_permute_order(bogus, 0)
        assert bogus in str(exc.value)
