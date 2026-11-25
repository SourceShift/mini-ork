"""Pin the citation contract of ``recipes/research-synthesis/verifiers/source-completeness.py``.

The verifier counts the lines of each lens report that carry a source anchor and
fails the run when a lens is under its per-lens minimum. It used to count only
URLs, arXiv IDs, GitHub links and ``(Author Year)`` parentheticals — which scored
a 430-line code survey with 73 ``path:line`` anchors as "2 sources" and failed
``run-1795594516-17762`` on citation style. These tests pin the widened contract
so the next edit cannot silently narrow it again:

* a ``path/to/file.ext:42`` anchor is a source,
* a repo-root ``file.ext:42`` anchor is a source,
* an ``org/repo:path/file.ext:42`` anchor is a source,
* the synthesis must name all four lenses and the per-lens minimums still fail
  a lens that cites nothing.

The verifier reads ``MINI_ORK_RUN_DIR`` at import time and calls ``sys.exit``, so
each case runs it as a subprocess — the same way the runtime invokes it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "research-synthesis" / "verifiers" / "source-completeness.py"

LENSES = ("glm", "kimi", "codex", "opus")

# 20+ lines so the length floor is not what decides a case.
_FILLER = "\n".join(f"line {i} of the report" for i in range(24))

SYNTHESIS = "lens-glm lens-kimi lens-codex lens-opus\n★ consensus\n"


def _run(run_dir: Path) -> dict:
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir)}
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(proc.stdout)


def _write_run(tmp_path: Path, bodies: dict[str, str]) -> Path:
    for lens in LENSES:
        (tmp_path / f"lens-{lens}.md").write_text(
            bodies.get(lens, f"https://example.com/{lens}\n" * 6) + "\n" + _FILLER,
            encoding="utf-8",
        )
    (tmp_path / "synthesis.md").write_text(SYNTHESIS, encoding="utf-8")
    return tmp_path


def test_code_anchors_count_as_sources(tmp_path):
    body = "\n".join(
        [
            "`mini_ork/remote/providers/base.py:118` defines the protocol",
            "`mini_ork/remote/providers/base.py:340` serializes acquisition",
            "`mini_ork/cost/budget.py:151` declares the ceiling",
            "`setup.py:12` is a repo-root anchor",
            "`hashicorp/vault:vault/expiration.go:109` is a lease record",
            "`config/agents.yaml:47` resolves a lane",
        ]
    )
    result = _run(_write_run(tmp_path, {"codex": body}))

    assert result["pass"] is True, result
    assert result["missing"] == []
    evidence = Path(result["evidence_path"]).read_text(encoding="utf-8")
    # One source per anchor-bearing line: six lines, six sources.
    assert re.search(r"lens-codex: \d+ lines, 6 sources", evidence), evidence


def test_a_lens_without_citations_still_fails(tmp_path):
    result = _run(_write_run(tmp_path, {"codex": "no anchors on any of these lines"}))

    assert result["pass"] is False
    assert any("lens-codex.md (only 0 sources" in m for m in result["missing"]), result


def test_prose_without_a_line_number_is_not_a_source(tmp_path):
    # A filename alone is a mention, not an anchor; `:42` is what makes it
    # checkable. Keep the bar there so a lens cannot pad its count.
    result = _run(_write_run(tmp_path, {"codex": "\n".join(["`base.py` and `budget.py`"] * 8)}))

    assert result["pass"] is False
    assert any("lens-codex.md (only 0 sources" in m for m in result["missing"]), result


def test_synthesis_must_name_every_lens(tmp_path):
    run = _write_run(tmp_path, {})
    (run / "synthesis.md").write_text("lens-glm and lens-kimi only\n", encoding="utf-8")

    result = _run(run)

    assert result["pass"] is False
    assert "synthesis.md (no reference to codex lens)" in result["missing"]
    assert "synthesis.md (no reference to opus lens)" in result["missing"]
