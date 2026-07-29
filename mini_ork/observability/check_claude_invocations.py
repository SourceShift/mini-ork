"""Python port of bin/mo-check-claude-invocations — lint that every direct
`claude --print` / `claude -p` invocation carries a permission-bypass flag
within 10 lines.

Static analysis over literal native-Python Claude argv lists plus executable
CLI launchers. Python syntax parsing excludes comments and documentation;
dynamic command assembly stays covered by the command helper tests. Returns
rc 0=clean, 1=violations.

    check(root) -> (total, checked, violations)
    main(argv=None, root=None) -> int rc
"""
from __future__ import annotations

import ast
import os
import sys

def _scan_files(root: str) -> list[str]:
    out: list[str] = []
    for base in ("mini_ork", "bin"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
            for name in files:
                if name.endswith(".py") or name.startswith("mini-ork"):
                    out.append(os.path.join(dirpath, name))
    return out


def _argv_literals(node: ast.List | ast.Tuple) -> list[str | None]:
    """Keep literal argv values while preserving dynamic-element positions."""
    values: list[str | None] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            values.append(element.value)
        else:
            values.append(None)
    return values


def _has_permission_bypass(argv: list[str | None]) -> bool:
    return (
        "--dangerously-skip-permissions" in argv
        or "--allow-dangerously-skip-permissions" in argv
        or "--permission-mode=bypassPermissions" in argv
        or any(
            argv[index:index + 2] == ["--permission-mode", "bypassPermissions"]
            for index in range(len(argv) - 1)
        )
    )


def check(root: str) -> tuple[int, int, list[str]]:
    files = _scan_files(root)
    total = checked = 0
    violations: list[str] = []
    for f in files:
        try:
            tree = ast.parse(open(f, encoding="utf-8", errors="ignore").read(), filename=f)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            argv = _argv_literals(node)
            if not argv or argv[0] != "claude" or not ({"--print", "-p"} & set(argv)):
                continue
            total += 1
            if _has_permission_bypass(argv):
                checked += 1
            else:
                violations.append(
                    f"{f}:{node.lineno}: claude invocation without --permission-mode "
                    f"bypassPermissions OR --dangerously-skip-permissions")
    return total, checked, violations


def main(argv: list[str] | None = None, *, root: str | None = None) -> int:
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    files = _scan_files(root)
    total, checked, violations = check(root)
    sys.stderr.write(
        f"[mo-check-claude-invocations] scanned {len(files)} files; "
        f"found {total} claude invocations; {checked} have permission-bypass flag\n")
    if violations:
        sys.stderr.write("\nVIOLATIONS:\n")
        for v in violations:
            sys.stderr.write(f"  ✗ {v}\n")
        sys.stderr.write("\nFix: add ONE of these flags to each claude invocation:\n")
        return 1
    sys.stderr.write(
        f"[mo-check-claude-invocations] OK — all {total} invocations "
        f"carry permission-bypass flag\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
