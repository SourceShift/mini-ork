"""Unit tests for :mod:`mini_ork.verify.secret_candidates` (SEC-G10-T08).

Hermetic: every fixture is a ``tmp_path`` file; no network and no git.
"""

from __future__ import annotations

import json
import os
import re

import pytest

import mini_ork.verify.secret_candidates as sc
from mini_ork.verify import security_scan

TOKEN = "Zk4Qm9vRt2Xb7Lp0Ws3Nh6Yc8Jd1fA5gE2uT"
FLAG_LINE = '_signing_key = "%s"' % TOKEN
REVIEW_LINE = 'x = "abcdefghijklmnopqrst"'


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run_main(argv):
    """Return main()'s exit code, normalising an argparse SystemExit(2)."""
    try:
        return sc.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


def _payload(captured_out):
    return json.loads(captured_out[captured_out.index("{"):])


def test_module_docstring_names_the_three_ideas():
    doc = sc.__doc__ or ""
    assert "candidate" in doc.lower()
    assert "formula" in doc.lower() or "classifier" in doc.lower()
    assert "review" in doc.lower()


def test_constants_match_the_contract():
    assert sc.LITERAL_MIN == 12
    assert sc.FLAG_BAND == 0.70
    assert sc.REVIEW_BAND == 0.45
    assert sc.BANDS == ("flagged", "review", "clean")
    assert sc.MAX_FILE_BYTES == 1 << 20
    assert ".png" in sc.BINARY_EXTENSIONS and ".pyc" in sc.BINARY_EXTENSIONS
    assert isinstance(sc.BINARY_EXTENSIONS, frozenset)


def test_shannon_degenerate_and_repeated_inputs():
    assert sc.shannon("") == 0.0
    assert sc.shannon("aaaa") == 0.0
    assert sc.shannon("ab" * 8) == pytest.approx(1.0, abs=1e-9)


def test_shannon_random_hex_is_in_expected_range():
    value = sc.shannon("0123456789abcdef0123456789abcdef")
    assert isinstance(value, float)
    assert 3.0 < value < 4.5


@pytest.mark.parametrize(
    "text, expected",
    [("", 0), ("abc", 1), ("aB", 2), ("aB3", 3), ("aB3!", 4), ("!@#", 1)],
)
def test_classes_counts_character_families(text, expected):
    assert sc._classes(text) == expected


@pytest.mark.parametrize(
    "literal, reason",
    [
        ("correct horse battery staple", "contains whitespace"),
        ("YOUR_TOKEN_HERE", "placeholder or format spec"),
        ("changeme", "placeholder or format spec"),
        ("${TOKEN}", "placeholder or format spec"),
        ("<your-key>", "placeholder or format spec"),
        ("/etc/passwd", "path-like"),
        ("https://internal.host/", "url without a query parameter"),
        ("aaaaaaaaaaaaaaaaaaaa", "low character variety"),
    ],
)
def test_demote_each_case(literal, reason):
    assert sc.demote(literal) == reason


def test_demote_mixed_token_and_query_url_survive():
    assert sc.demote("q7Xw2Rt9Yu3Io4Pa5Sd6Fg7Hj8Kl9Zx0Cv1Bn3M") is None
    assert sc.demote(TOKEN) is None
    assert sc.demote("https://internal.host/?next=/home") is None


def test_demote_first_matching_reason_wins():
    assert sc.demote("/etc/passwd and then words") == "contains whitespace"


def test_score_literal_demoted_returns_zero_and_reason():
    assert sc.score_literal("correct horse battery staple", "_signing_key = ") == (
        0.0,
        ("contains whitespace",),
    )
    assert sc.score_literal("/etc/passwd", "x = ") == (0.0, ("path-like",))


def test_score_literal_is_deterministic_and_bounded():
    for literal in (TOKEN, REVIEW_LINE.split('"')[1], "https://internal.host/?a=1"):
        for context in ("_signing_key = ", "x = "):
            first = sc.score_literal(literal, context)
            second = sc.score_literal(literal, context)
            assert first == second
            assert 0.0 <= first[0] <= 1.0


def test_score_literal_context_bonus_is_exactly_twenty_points():
    with_context = sc.score_literal(TOKEN, "_signing_key = ")
    without_context = sc.score_literal(TOKEN, "x = ")
    assert with_context[0] - without_context[0] == pytest.approx(0.20, abs=1e-4)


def test_score_literal_reasons_are_sorted_fixed_tokens():
    for literal, context in ((TOKEN, "_signing_key = "), ("abcdefghijklmnopqrst", "x = ")):
        _, reasons = sc.score_literal(literal, context)
        assert isinstance(reasons, tuple)
        assert list(reasons) == sorted(reasons)
        assert ("context" in reasons) != ("no-context" in reasons)
        for reason in reasons:
            assert re.fullmatch(
                r"(?:entropy=\d+\.\d{2}|length=\d+|classes=[0-4]|context|no-context)",
                reason,
            ), reason


@pytest.mark.parametrize(
    "score, expected",
    [
        (0.70, "flagged"),
        (0.6999, "review"),
        (0.45, "review"),
        (0.4499, "clean"),
    ],
)
def test_band_edges(score, expected):
    assert sc.band(score) == expected


def test_band_uses_the_named_constants():
    assert sc.band(sc.FLAG_BAND) == sc.FLAGGED
    assert sc.band(sc.REVIEW_BAND) == sc.REVIEW
    assert sc.band(sc.REVIEW_BAND - 0.0001) == sc.CLEAN


def test_context_for_concatenates_previous_line_and_prefix():
    lines = ["alpha context line here", "the code line itself"]
    got = sc._context_for(lines, 2, 8)
    assert "alpha context line here" in got
    assert got.index("alpha context line here") < got.index("the code")


def test_context_for_truncates_to_last_two_hundred_chars():
    got = sc._context_for(["A" * 300, "B" * 300], 2, 300)
    assert len(got) <= 200
    assert got.endswith("B" * 100)


def test_context_for_first_line_does_not_wrap_around():
    got = sc._context_for(["alpha", "beta", "gamma"], 1, 3)
    assert "gam" not in got and "beta" not in got
    assert "alp" in got


def test_formatless_token_is_flagged_while_regex_rules_are_blind(tmp_path):
    path = _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    candidates = sc.scan_text("app/x.py", FLAG_LINE + "\n")
    flagged = [cand for cand in candidates if cand.band == sc.FLAGGED]
    assert flagged, [(c.literal, c.score, c.band) for c in candidates]
    assert flagged[0].literal == TOKEN
    assert flagged[0].line == 1
    # The uplift is real: the shipped regex rules see nothing on this line.
    assert security_scan.scan_added({str(path): {1: FLAG_LINE}}) == []


def test_negatives_stay_below_the_flag_band():
    text = (
        'sentence = "correct horse battery staple"\n'
        "path_like = '/etc/passwd.json'\n"
        "url_like = 'https://internal.host/'\n"
        'placeholder = "YOUR_TOKEN_HERE"\n'
        'lookup = os.environ["AWS_SECRET_ACCESS_KEY"]\n'
    )
    candidates = sc.scan_text("neg.py", text)
    assert candidates
    for candidate in candidates:
        assert candidate.score < sc.FLAG_BAND
        assert candidate.band != sc.FLAGGED


def test_scan_text_shape_and_sort():
    text = (
        'short = "tiny9"\n'
        'first = "opaque_literal_aa11"\n'
        'second = "second_literal_bb22"\n'
    )
    candidates = sc.scan_text("shape.py", text)
    assert len(candidates) == 2
    assert [c.line for c in candidates] == [2, 3]
    assert all(c.path == "shape.py" for c in candidates)
    assert all(c.band in sc.BANDS for c in candidates)


def test_extract_literals_uses_ast_positions():
    assert sc.extract_literals('x = "hello_world_token"\n') == [
        (1, 4, "hello_world_token")
    ]


def test_extract_literals_ignores_short_literals():
    assert sc.extract_literals('x = "tiny9"\n') == []
    assert sc.extract_literals('x = "' + "a" * 11 + '"\n') == []
    assert sc.extract_literals('x = "' + "a" * 12 + '"\n') != []


def test_extract_literals_regex_fallback_on_malformed_python():
    text = 'def broken(:\n    key = "fallback_literal_here"\n'
    triples = sc.extract_literals(text)
    assert len(triples) == 1
    lineno, column, literal = triples[0]
    assert (lineno, literal) == (2, "fallback_literal_here")
    assert isinstance(column, int) and column >= 0


def test_scan_file_skips_missing_directory_binary_nul_and_oversize(tmp_path):
    (tmp_path / "adir").mkdir()
    (tmp_path / "big.py").write_bytes(b"x = 1 " + b"#" * sc.MAX_FILE_BYTES)
    (tmp_path / "logo.png").write_text('k = "opaque_token_value_1"')
    (tmp_path / "nul.py").write_bytes(b'k = "opaque_token_value_1"\x00\n')

    for rel, reason in (
        ("ghost.py", "skipped: missing"),
        ("adir", "skipped: not a regular file"),
        ("big.py", "skipped: larger than %d bytes" % sc.MAX_FILE_BYTES),
        ("logo.png", "skipped: binary extension"),
        ("nul.py", "skipped: contains NUL bytes"),
    ):
        found, said = sc.scan_file(str(tmp_path), rel)
        assert found == [], rel
        assert said == reason, (rel, said)


def test_scan_reports_a_tests_fixture(tmp_path):
    _write(tmp_path, "tests/fixture.py", FLAG_LINE + "\n")
    result = sc.scan(str(tmp_path), ["tests/fixture.py"])
    assert result.candidates and result.candidates[0].literal == TOKEN
    assert result.unreadable == [] and result.skipped == []


def test_scan_sorts_by_path_then_line(tmp_path):
    _write(tmp_path, "zeta.py", 'a = "%s"\n' % TOKEN)
    _write(tmp_path, "alpha.py", 'a = "%s"\n' % TOKEN)
    result = sc.scan(str(tmp_path), ["zeta.py", "alpha.py"])
    assert [c.path for c in result.candidates] == ["alpha.py", "zeta.py"]


def test_scan_collapses_duplicate_paths_so_counts_are_of_distinct_literals(tmp_path):
    _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    result = sc.scan(str(tmp_path), ["app/x.py", "app/x.py"])
    assert len(result.candidates) == 1


def test_a_path_that_escapes_the_root_is_unreadable_not_clean(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(FLAG_LINE + "\n", encoding="utf-8")
    (root / "link.py").symlink_to(outside)

    for rel in ("../outside.py", str(outside), "link.py"):
        found, reason = sc.scan_file(str(root), rel)
        assert found == [], rel
        assert reason == "escapes root", (rel, reason)

    result = sc.scan(str(root), ["../outside.py"])
    assert result.candidates == []
    assert result.unreadable == ["../outside.py: escapes root"]


def test_containment_still_works_when_the_root_is_a_filesystem_root(tmp_path):
    """``/`` already ends in a separator; a second one would reject everything."""
    root = tmp_path / "root"
    _write(root, "app/x.py", FLAG_LINE + "\n")
    assert sc.on_disk(str(root) + "/", "app/x.py") is not None
    assert sc.on_disk(os.sep, "tmp") is not None


def test_an_unreadable_file_is_never_counted_clean(tmp_path, capsys):
    root = tmp_path / "root"
    _write(root, "app/x.py", FLAG_LINE + "\n")
    (root / "locked.py").write_text(FLAG_LINE + "\n", encoding="utf-8")
    (root / "locked.py").chmod(0o000)
    try:
        code = _run_main(
            ["--root", str(root), "--paths", "locked.py", "--json"]
        )
    finally:
        (root / "locked.py").chmod(0o644)
    assert code == 3, "an unreadable file must abstain, not pass"
    payload = _payload(capsys.readouterr().out)
    assert payload["counts"] == {"flagged": 0, "review": 0, "clean": 0}
    assert payload["unreadable"]


def test_extract_literals_sees_bytes_literals():
    text = 'key = b"%s"\n' % TOKEN
    triples = sc.extract_literals(text)
    assert [literal for _line, _col, literal in triples] == [TOKEN]


def test_mask_hides_the_middle_but_keeps_a_stable_digest():
    masked = sc.mask(TOKEN)
    assert TOKEN not in masked
    assert masked.startswith("%s..%s " % (TOKEN[:3], TOKEN[-2:]))
    assert "len=%d" % len(TOKEN) in masked
    assert re.search(r"sha256=[0-9a-f]{12}$", masked)
    assert sc.mask(TOKEN) == masked, "the digest must be deterministic"
    assert sc.mask(TOKEN) != sc.mask(TOKEN + "x")


def test_mask_reveals_nothing_for_a_short_literal():
    """Five characters of a twelve-character literal is a third of the secret."""
    for literal in ("abcdefghijkl", "shortsecret1", "0123456789abcdef"):
        masked = sc.mask(literal)
        assert not masked.startswith(literal[:3]), literal
        assert literal[-2:] not in masked.split(" sha256=")[0], literal
        assert "len=%d" % len(literal) in masked
    assert sc.mask("x").startswith("len=1 ")


def test_summarize_counts_and_masks_by_default():
    result = sc.ScanResult(
        [
            sc.Candidate("app/x.py", 3, TOKEN, 1.0, sc.FLAGGED, ("context",)),
            sc.Candidate(
                "app/y.py", 1, "a sentence here", 0.0, sc.CLEAN,
                ("contains whitespace",),
            ),
        ],
        ["app/z.py: unreadable: Permission denied"],
        ["logo.png: skipped: binary extension"],
    )
    out = sc.summarize(result)
    assert out["counts"] == {"flagged": 1, "review": 0, "clean": 1}
    assert len(out["candidates"]) == 2
    for entry in out["candidates"]:
        assert set(entry) == {"path", "line", "literal", "score", "band", "reasons"}
    assert out["candidates"][0]["literal"] == sc.mask(TOKEN)
    assert out["unreadable"] == ["app/z.py: unreadable: Permission denied"]
    assert out["skipped"] == ["logo.png: skipped: binary extension"]


def test_summarize_shows_the_literal_only_when_asked():
    result = sc.ScanResult(
        [sc.Candidate("app/x.py", 3, TOKEN, 1.0, sc.FLAGGED, ("context",))], [], []
    )
    assert sc.summarize(result, show_literal=True)["candidates"][0]["literal"] == TOKEN


def test_cli_flags_with_exit_one_and_json(tmp_path, capsys):
    _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    code = _run_main(["--root", str(tmp_path), "--paths", "app/x.py", "--json"])
    assert code == 1
    payload = _payload(capsys.readouterr().out)
    assert payload["counts"]["flagged"] == 1
    assert any(c["band"] == "flagged" for c in payload["candidates"])


def test_cli_clean_file_exits_zero(tmp_path, capsys):
    _write(tmp_path, "clean/c.py", 's = "correct horse battery staple"\n')
    code = _run_main(["--root", str(tmp_path), "--paths", "clean/c.py", "--json"])
    assert code == 0
    payload = _payload(capsys.readouterr().out)
    assert "counts" in payload and "candidates" in payload


def test_cli_review_band_abstains_with_exit_three(tmp_path, capsys):
    _write(tmp_path, "review/r.py", REVIEW_LINE + "\n")
    code = _run_main(["--root", str(tmp_path), "--paths", "review/r.py", "--json"])
    assert code == 3
    payload = _payload(capsys.readouterr().out)
    assert payload["counts"]["review"] >= 1
    assert payload["counts"]["flagged"] == 0


def test_cli_paths_accept_commas_and_newlines(tmp_path, capsys):
    _write(tmp_path, "a.py", FLAG_LINE + "\n")
    _write(tmp_path, "b.py", FLAG_LINE + "\n")
    for paths_arg in ("a.py,b.py", "a.py\nb.py"):
        code = _run_main(["--root", str(tmp_path), "--paths", paths_arg, "--json"])
        assert code == 1
        payload = _payload(capsys.readouterr().out)
        assert payload["counts"]["flagged"] == 2


def test_cli_paths_fall_back_to_touched_files_env(tmp_path, capsys, monkeypatch):
    _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    monkeypatch.setenv("MINI_ORK_TOUCHED_FILES", "app/x.py")
    code = _run_main(["--root", str(tmp_path), "--json"])
    assert code == 1
    assert _payload(capsys.readouterr().out)["counts"]["flagged"] == 1


def test_cli_empty_paths_is_usage_error_two(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_TOUCHED_FILES", raising=False)
    assert _run_main(["--root", str(tmp_path), "--json"]) == 2
    monkeypatch.setenv("MINI_ORK_TOUCHED_FILES", "")
    assert _run_main(["--root", str(tmp_path), "--json"]) == 2


def test_cli_text_mode_line_and_summary(tmp_path, capsys):
    _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    code = _run_main(["--root", str(tmp_path), "--paths", "app/x.py"])
    assert code == 1
    out = capsys.readouterr().out
    assert re.search(
        r"app/x\.py:1 flagged \d+(?:\.\d+)? \S+\.\.\S+ len=%d " % len(TOKEN), out
    )
    assert TOKEN not in out, "the literal must not reach the terminal by default"
    summary = re.search(
        r"secret-candidates: (\d+) flagged, (\d+) review, (\d+) clean,"
        r" (\d+) unreadable",
        out,
    )
    assert summary and int(summary.group(1)) == 1
    assert int(summary.group(4)) == 0


def test_cli_show_literal_opts_into_the_full_string(tmp_path, capsys):
    _write(tmp_path, "app/x.py", FLAG_LINE + "\n")
    code = _run_main(
        ["--root", str(tmp_path), "--paths", "app/x.py", "--show-literal"]
    )
    assert code == 1
    assert TOKEN in capsys.readouterr().out
