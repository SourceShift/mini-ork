#!/usr/bin/env bash
# Integration wrapper for the importable mini_ork Python framework facade.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$MINI_ORK_ROOT"

echo "── integration: python framework facade ──"
if PYTHONPATH="$MINI_ORK_ROOT" python3 tests/integration/test_python_framework.py >/tmp/mini-ork-python-framework-test.$$ 2>&1; then
  echo "  [OK]   python framework smoke tests pass"
  rm -f /tmp/mini-ork-python-framework-test.$$
  exit 0
fi

sed 's/^/  /' /tmp/mini-ork-python-framework-test.$$ 2>/dev/null
rm -f /tmp/mini-ork-python-framework-test.$$
echo "  [FAIL] python framework smoke tests failed"
exit 1
