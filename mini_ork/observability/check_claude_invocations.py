"""Python port of bin/mo-check-claude-invocations — lint that every direct
`claude --print` / `claude -p` invocation carries a permission-bypass flag
within 10 lines.

Strangler-fig parity port. Pure static analysis over lib/ + bin/ (skipping
lib/providers/ wrappers and comment/doc lines). Returns rc 0=clean, 1=violations.

    check(root) -> (total, checked, violations)
    main(argv=None, root=None) -> int rc
"""
from __future__ import annotations

import os
import re
import sys

_CLAUDE_RE = re.compile(r"claude\s+(--print|-p\s)")
_FLAG_RE = re.compile(
    r"permission-mode\s+bypassPermissions|dangerously-skip-permissions|"
    r"allow-dangerously-skip-permissions")


def _scan_files(root: str) -> list[str]:
    out: list[str] = []
    for base in ("lib", "bin"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
            for name in files:
                if name.endswith(".sh") or name.startswith("mini-ork"):
                    out.append(os.path.join(dirpath, name))
    return out


def check(root: str) -> tuple[int, int, list[str]]:
    files = _scan_files(root)
    total = checked = 0
    violations: list[str] = []
    for f in files:
        if os.sep + "lib" + os.sep + "providers" + os.sep in f + os.sep:
            continue
        try:
            lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not _CLAUDE_RE.search(line):
                continue
            if line.lstrip().startswith("#"):          # comment line
                continue
            pre = line[: line.find("claude")]           # doc-comment: # before claude
            if "#" in pre:
                continue
            total += 1
            window = "\n".join(lines[i: i + 11])        # this line + next 10
            if _FLAG_RE.search(window):
                checked += 1
            else:
                violations.append(
                    f"{f}:{i + 1}: claude invocation without --permission-mode "
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
