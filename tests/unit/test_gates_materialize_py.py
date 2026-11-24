"""`precondition` tagging reads the AC's marker line, not the body's first line.

The docstring contract is "``precondition`` in the AC text or its marker line
tags the probe". The implementation used to inspect `labeled[aid].split("\\n")[0]`
— but `_split_labeled` prepends the injected `set -euo pipefail` header, so
line 0 is never the author's `# ACn precondition` comment. Every tagged probe
materialized untagged, and `test-validity` then scored an already-delivered
deliverable's passing probe as VACUOUS and failed the spec.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "recipes" / "spec-driven-delivery" / "verifiers" / "gates-materialize.py"


def _load():
    spec = importlib.util.spec_from_file_location("gates_materialize", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gm = _load()


def _spec(fence: str) -> str:
    return "## Acceptance criteria\n\n```bash\n" + fence + "\n```\n"


def _card(ac_ids: list[str]) -> dict:
    return {
        "spec_id": "S1",
        "source_hash": "unused-by-_materialize",
        "deliverables": [],
        "acceptance": [
            {"id": aid, "gate": {"kind": "cmd"}, "text": f"criterion {aid}"} for aid in ac_ids
        ],
    }


def test_a_marker_line_after_the_injected_header_still_tags_the_probe():
    """The regression: the body starts `set -euo pipefail`, marker is line 1."""
    body = "set -euo pipefail\n# AC1 precondition — must pass today\ngrep -q foo bar"
    assert gm._marker_tags_precondition(body) is True


def test_materialize_tags_the_ac_whose_marker_says_precondition():
    spec = _spec(
        "set -euo pipefail\n"
        "# AC1 precondition — regression invariant\ngrep -q foo bar\n"
        "# AC2 — new work\ngrep -q baz qux"
    )

    gates, problems = gm._materialize(_card(["AC1", "AC2"]), spec)

    assert problems == []
    tags = {p["acceptance_ref"]: p["tags"] for p in gates["probes"]}
    assert tags["AC1"] == ["precondition"]
    assert tags["AC2"] == []


def test_the_word_in_a_command_does_not_tag():
    """Only a comment line counts — `echo "precondition"` is a probe, not a tag."""
    body = 'set -euo pipefail\n# AC1 — plain\necho "precondition satisfied"'
    assert gm._marker_tags_precondition(body) is False


def test_an_ac_text_naming_precondition_still_tags_without_a_marker():
    card = _card(["AC1"])
    card["acceptance"][0]["text"] = "Precondition: the table exists today."
    spec = _spec("set -euo pipefail\n# AC1 — plain\ngrep -q foo bar")

    gates, _ = gm._materialize(card, spec)

    assert gates["probes"][0]["tags"] == ["precondition"]
