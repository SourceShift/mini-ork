from pathlib import Path

from mini_ork.observability.check_claude_invocations import check


def test_check_ignores_docs_and_reports_only_literal_claude_argvs(tmp_path: Path):
    runtime = tmp_path / "mini_ork"
    runtime.mkdir()
    (runtime / "commands.py").write_text(
        '"""Documentation: claude --print without a permission flag."""\n'
        'GOOD = ["claude", "--print", "--permission-mode", "bypassPermissions"]\n'
        'BAD = ["claude", "-p", "prompt"]\n',
        encoding="utf-8",
    )

    total, checked, violations = check(str(tmp_path))

    assert (total, checked) == (2, 1)
    assert len(violations) == 1
    assert "commands.py:3" in violations[0]
