"""Deterministic detector for silent-fallback exception handlers.

The ``code-fix`` implementer prompt forbids "silent exception swallowing" and
"returning a default on error", but that rule has only ever been enforced by a
reviewer lane reading the diff. A reviewer that approves is indistinguishable
from a reviewer that did not notice, and the run still reports green. This
module makes the narrow, parseable part of the rule a machine check:

    except:                      pass
    except Exception:            pass
    except Exception as exc:     return None
    except Exception:            return

Those four shapes mean the same thing — an error occurred and the program
pretended it did not. The detector is deliberately narrow: ``except OSError:
pass`` (a best-effort cleanup) and a named handler that returns a documented
sentinel are judgements about a specific failure, and a check that flagged them
would be turned off within a week.

An intentional silence is still expressible: annotate the handler with

    # fallback-guard: allow: <why the failure is safe to ignore>

anywhere in the handler block, and the finding is dropped. The annotation is reviewed like code, which is the
point — silence becomes a decision someone wrote down, not an accident nobody
saw.

Tiers
-----
* **Simple / local.** ``python3 -m mini_ork.cli.fallbacks <paths…>`` — stdlib
  only, no network, no state, exits 1 on findings. Works on a laptop.
* **Secure / CI.** ``mini-ork fallbacks --all`` scans the tree and is wired
  into the ``code-fix`` verifier node, so a patch that introduces a swallow
  fails the run that produced it. Both tiers call ``findings_for_paths`` here;
  neither re-implements the rule.

Fail closed: a file that cannot be read or does not parse is reported as a
finding, never skipped. A check that silently examines nothing reports a pass
it did not earn.
"""
from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "fallback-guard: allow"

#: Handler types treated as "catch everything". Bare ``except:`` is included.
_BROAD_HANDLER_NAMES = {"Exception", "BaseException"}

#: Body shapes that erase the failure. A body with any other statement is a
#: handler that did something with the error, which is the behaviour we want.
_SILENT_BODIES = (ast.Pass,)

FIX_HINT = (
    "catch the exception you can actually handle and record it — "
    "`except OSError as exc: log.debug(\"<what failed>\", exc_info=True)` — or "
    "let it propagate. To silence it deliberately, append "
    f"`# {ALLOW_MARKER}: <why it is safe>` to the `except` line."
)

RETURN_NONE_HINT = (
    "returning None on error is indistinguishable from \"nothing found\". Name "
    "the degraded path: raise, or return a documented sentinel and log the "
    "cause. To silence it deliberately, append "
    f"`# {ALLOW_MARKER}: <why it is safe>` to the `except` line."
)


@dataclass(frozen=True)
class Finding:
    """One swallowing handler, at ``path:line``."""

    path: str
    line: int
    shape: str
    handler: str
    fix: str

    def render(self) -> str:
        return (
            f"[error]   {self.path}:{self.line} {self.shape} ({self.handler})\n"
            f"          Fix: {self.fix}"
        )

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "shape": self.shape,
            "handler": self.handler,
            "fix": self.fix,
        }


def _handler_text(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "bare except"
    try:
        rendered = ast.unparse(node.type)
    except Exception:  # pragma: no cover — unparse is stdlib and total here
        rendered = "Exception"
    return f"except {rendered}"


def _is_broad(node: ast.ExceptHandler) -> bool:
    if node.type is None:
        return True
    if isinstance(node.type, ast.Name):
        return node.type.id in _BROAD_HANDLER_NAMES
    if isinstance(node.type, ast.Tuple):
        return any(
            isinstance(elt, ast.Name) and elt.id in _BROAD_HANDLER_NAMES
            for elt in node.type.elts
        )
    return False


def _shape_of(node: ast.ExceptHandler) -> str:
    """``pass`` / ``return None`` / ``return`` — or "" when the body acts."""
    if len(node.body) != 1:
        return ""
    stmt = node.body[0]
    if isinstance(stmt, _SILENT_BODIES):
        return "swallows the exception (`pass`)"
    if isinstance(stmt, ast.Return):
        value = stmt.value
        returns_none = value is None or (
            isinstance(value, ast.Constant) and value.value is None)
        return "returns None on error" if returns_none else ""
    return ""


def scan_text(text: str, path: str = "<string>") -> list[Finding]:
    """Findings for one Python source string. A parse error is itself a finding."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [Finding(
            path=path, line=int(exc.lineno or 0), shape="does not parse",
            handler=f"SyntaxError: {exc.msg}",
            fix="fix the syntax error; an unparsable file cannot be checked and "
                "must not count as clean")]
    lines = text.splitlines()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        shape = _shape_of(node)
        if not shape:
            continue
        # The annotation is read from the whole handler block, so a reason that
        # needs two lines still counts. It names the decision, not the diff.
        end = max((stmt.end_lineno or node.lineno) for stmt in node.body)
        block = "\n".join(lines[node.lineno - 1:end])
        if ALLOW_MARKER in block:
            continue
        findings.append(Finding(
            path=path, line=node.lineno, shape=shape,
            handler=_handler_text(node),
            fix=RETURN_NONE_HINT if "returns None" in shape else FIX_HINT,
        ))
    return findings


def scan_file(path: Path | str) -> list[Finding]:
    """Findings for one file. Unreadable is a finding (fail closed)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Finding(
            path=str(p), line=0, shape="cannot be read",
            handler=f"{type(exc).__name__}: {exc}",
            fix="restore read access; an unreadable file cannot be checked and "
                "must not count as clean")]
    return scan_text(text, str(p))


def find_python_files(root: Path | str) -> list[Path]:
    """Every ``*.py`` under ``root``, skipping VCS and virtualenv directories."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mini-ork"}
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        out.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return sorted(out)


def changed_python_files(cwd: Path | str) -> list[Path]:
    """Python files changed in the working tree (modified, added, untracked).

    Committed-only changes are the run's business, not this command's: an
    operator running ``mini-ork fallbacks`` locally is about to commit, so the
    working tree is exactly the set they should see.
    """
    root = Path(cwd)
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    out: list[Path] = []
    fields = [f for f in proc.stdout.split("\0") if f]
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            if i < len(fields):
                i += 1
        if status == "!!" or status[:1] == "D" or not rel.endswith(".py"):
            continue
        out.append(root / rel)
    return sorted(out)


def findings_for_paths(paths: list[Path | str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings
