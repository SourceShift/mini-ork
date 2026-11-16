"""Unit tests for :mod:`mini_ork.verify.secret_candidates` (SEC-G10-T08).

Hermetic: every fixture is a ``tmp_path`` file; no network and no git.
"""

from __future__ import annotations

import json
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

    assert sc.scan_file(str(tmp_path), "ghost.py") == []
    assert sc.scan_file(str(tmp_path), "adir") == []
    assert sc.scan_file(str(tmp_path), "big.py") == []
    assert sc.scan_file(str(tmp_path), "logo.png") == []
    assert sc.scan_file(str(tmp_path), "nul.py") == []


def test_scan_reports_a_tests_fixture(tmp_path):
    _write(tmp_path, "tests/fixture.py", FLAG_LINE + "\n")
    candidates = sc.scan(str(tmp_path), ["tests/fixture.py"])
    assert candidates and candidates[0].literal == TOKEN


def test_scan_sorts_by_path_then_line(tmp_path):
    _write(tmp_path, "zeta.py", 'a = "%s"\n' % TOKEN)
    _write(tmp_path, "alpha.py", 'a = "%s"\n' % TOKEN)
    candidates = sc.scan(str(tmp_path), ["zeta.py", "alpha.py"])
    assert [c.path for c in candidates] == ["alpha.py", "zeta.py"]


def test_summarize_counts_and_full_literal():
    candidates = [
        sc.Candidate("app/x.py", 3, TOKEN, 1.0, sc.FLAGGED, ("context",)),
        sc.Candidate("app/y.py", 1, "a sentence here", 0.0, sc.CLEAN, ("contains whitespace",)),
    ]
    out = sc.summarize(candidates)
    assert out["counts"] == {"flagged": 1, "review": 0, "clean": 1}
    assert len(out["candidates"]) == 2
    for entry in out["candidates"]:
        assert set(entry) == {"path", "line", "literal", "score", "band", "reasons"}
    assert out["candidates"][0]["literal"] == TOKEN


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
    assert re.search(r"app/x\.py:1 flagged \d+(?:\.\d+)? %s" % TOKEN, out)
    summary = re.search(
        r"secret-candidates: (\d+) flagged, (\d+) review, (\d+) clean", out
    )
    assert summary and int(summary.group(1)) == 1
