"""``content_ids(repo, branch, target)`` returns the ``(merge-base, head-tree)``
content id pair used by merge approvals to bind a decision to exact content, so
a rebase or squash-merge of identical content keeps an approval valid while any
content change voids it; failures return ``("", "")`` so approval checks hold
closed."""
from __future__ import annotations

import subprocess


def _git(cwd, *args, env=None) -> tuple[str, int]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    return r.stdout.strip(), r.returncode


def content_ids(repo: str, branch: str, target: str) -> tuple[str, str]:
    """Return (merge_base, head_tree_sha) for ``branch`` against ``target``.

    Pass the **tree** sha as ``head_sha`` (``branch^{tree}``) so a content id
    is stable across squash-merges of identical content, and a rebase that
    changes content correctly voids an existing approval. Any git failure
    (missing branch, non-zero rc, empty stdout) returns ``("", "")`` so the
    caller (``merge_approval.require``) holds — fail closed, because an
    empty head can never match an approved row.
    """
    base, rc_b = _git(repo, "merge-base", target, branch)
    # NOTE the doubled braces inside the f-string — `^{tree}` must be a
    # literal ``^{tree}`` to git, not an interpolation of a local named
    # ``tree``.
    tree, rc_t = _git(repo, "rev-parse", f"{branch}^{{tree}}")
    if rc_b or rc_t or not base or not tree:
        return "", ""
    return base, tree
