"""lib/extract_verdict.py must find the verdict object even after an unbalanced '{'.

Reviewer output is prose followed by the JSON the prompt asked for. The
balanced-brace scan used to stop at the first '{' that never closed (a brace in
prose or a code span, or a stray '"' that flipped the string state), so the
verdict object that follows was never seen. A run whose reviewer wrote a valid
{"verdict": "pass"} was recorded as verdict=unknown and failed.
"""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "lib" / "extract_verdict.py"
_spec = importlib.util.spec_from_file_location("extract_verdict", _PATH)
assert _spec is not None and _spec.loader is not None, f"cannot load {_PATH}"
extract_verdict = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_verdict)


def test_strict_json():
    assert extract_verdict.extract_verdict('{"verdict": "pass", "notes": []}') == "pass"


def test_prose_around_the_object():
    text = 'Verified against the tree.\n{"verdict": "fail", "notes": ["x"]}\nThanks.'
    assert extract_verdict.extract_verdict(text) == "fail"


def test_unbalanced_brace_before_the_verdict_object():
    text = (
        "[tool-grants] node=judge type=unknown undeclared; applying type-aware default\n"
        "The gate checks a template like `{label` and the copy says 12\" wide.\n"
        "```json\n"
        '{"verdict": "pass", "notes": ["gate is honest"]}\n'
        "```\n"
    )
    assert extract_verdict.extract_verdict(text) == "pass"


def test_last_verdict_object_wins_over_a_schema_echo():
    text = (
        'Respond with JSON: {"verdict": "pass|fail|needs_revision", "notes": []}\n'
        'Answer: {"verdict": "needs_revision", "notes": ["missing state"]}'
    )
    assert extract_verdict.extract_verdict(text) == "needs_revision"


def test_genuinely_invalid_json_stays_unknown():
    # Unescaped quotes inside a string value: no parseable verdict object exists.
    text = '{"verdict": "pass", "notes": ["click="primary" then wait"]}'
    assert extract_verdict.extract_verdict(text) == "unknown"


def test_no_verdict_is_unknown():
    assert extract_verdict.extract_verdict("no json here { at all") == "unknown"
