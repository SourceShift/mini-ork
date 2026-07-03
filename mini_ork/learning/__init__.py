"""Mini-ork learning primitives.

Reusable reward-shaping and trace-scoring modules extracted from the bash
runtime so the Python facade can score traces without shelling out. Each
module in this package is a faithful port of its bash counterpart in
``lib/``; parity is enforced by tests under ``tests/unit/``.
"""