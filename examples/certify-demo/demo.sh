#!/usr/bin/env bash
# mini-ork certify demo — two changes claim to fix the same bug. One really does;
# the other only special-cases the example from the bug report. `mini-ork certify`
# tells them apart by running code against the repository, not by reading the diff.
#
# What it does:
#   1. Builds a tiny git repo (`stats.median` returns the upper middle value for
#      even-length input) with three branches: base, fix-correct, fix-cheat.
#   2. Runs `mini-ork certify` on each fix with the same one-line bug report.
#   3. Expected: fix-correct → PROVEN (exit 0), fix-cheat → REFUTED (exit 1).
#
# Needs: Docker (certify builds a runtime image of the repo) and one working model
# lane (default MO_CERTIFY_MODEL=minimax; see `mini-ork providers`). Real model calls:
# roughly $0.10–0.60 and 1–3 minutes per certificate, depending on the lane.
#
# Usage:
#   bash examples/certify-demo/demo.sh             # builds the repo under a temp dir
#   bash examples/certify-demo/demo.sh ~/stats-demo
set -Eeuo pipefail

DIR="${1:-$(mktemp -d)/stats-demo}"
ISSUE='median([1, 2, 3, 4]) returns 3, but the median of an even-length list should be the average of the two middle values, so it should return 2.5.'

if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  echo "refusing to reuse non-empty $DIR — pass an empty or new path" >&2
  exit 64
fi
mkdir -p "$DIR/stats"
cd "$DIR"
git init -q
git config user.email demo@example.invalid
git config user.name demo

cat > pyproject.toml <<'EOF'
[project]
name = "stats"
version = "0.1.0"
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"
[tool.setuptools]
packages = ["stats"]
EOF

cat > stats/__init__.py <<'EOF'
def mean(xs):
    """Arithmetic mean of a list of numbers."""
    return sum(xs) / len(xs)


def median(xs):
    """Median of a list of numbers."""
    s = sorted(xs)
    return s[len(s) // 2]
EOF
git add -A
git commit -q -m "stats: mean and median"
git branch -q base

# The real fix: average the two middle values when the length is even.
git checkout -q -b fix-correct base
python3 - <<'EOF'
p = "stats/__init__.py"
s = open(p).read().replace(
    "    s = sorted(xs)\n    return s[len(s) // 2]",
    "    s = sorted(xs)\n    n = len(s)\n    mid = n // 2\n"
    "    if n % 2 == 0:\n        return (s[mid - 1] + s[mid]) / 2\n    return s[mid]",
)
open(p, "w").write(s)
EOF
git commit -q -am "fix median for even-length input"

# The cheat: make the exact example from the bug report pass, fix nothing else.
git checkout -q -b fix-cheat base
python3 - <<'EOF'
p = "stats/__init__.py"
s = open(p).read().replace(
    "    s = sorted(xs)\n",
    "    if xs == [1, 2, 3, 4]:\n        return 2.5\n    s = sorted(xs)\n",
)
open(p, "w").write(s)
EOF
git commit -q -am "fix median for even-length input"
git checkout -q base

echo "demo repo: $DIR"
echo "bug report: $ISSUE"
for branch in fix-correct fix-cheat; do
  echo
  echo "\$ mini-ork certify --base base --head $branch --issue \"<bug report>\""
  rc=0
  mini-ork certify --repo "$DIR" --base base --head "$branch" --issue "$ISSUE" || rc=$?
  echo "exit code: $rc"
done
