"""Pure-logic port of ``lib/pricing_strategy.sh::pricing_lookup``.

Faithful port of the deterministic lookup core of ``pricing_lookup``. The
bash function is a thin shell that resolves ``MO_PRICING_YAML`` (with a
``MINI_ORK_HOME`` fallback), checks for ``python3`` + ``PyYAML``, and
otherwise delegates to an embedded Python heredoc that walks
``pricing[provider][model][kind]``. This module lifts that walk into a
regular import, strips the I/O + env + stderr layers (the test layer owns
I/O), and exposes a single ``lookup(data, provider, model, kind)`` entry
point.

Public API::

    from mini_ork.dispatch.pricing_strategy import lookup, ALLOWED

    rate = lookup(data, "anthropic", "claude-sonnet-4-6", "input")
    # -> "3.0"      # str() of the YAML-parsed rate, identical to bash

``data`` is the parsed YAML document (typically ``yaml.safe_load(path)``).
The port performs no I/O, reads no env, emits no stderr — every miss path
returns ``"0"`` (the literal two-character string), matching bash's
``print("0")`` after its stderr warning. Callers needing the file/env
wrapping should invoke the bash function directly.

``tests/unit/test_pricing_strategy_parity.py`` enforces byte-exact stdout
parity between this module and the live bash subprocess over a fixture
corpus of ``>=6`` cases.

Parity contract (mirrors bash verbatim):
    - kind not in ALLOWED -> "0"
    - any of ``data``, ``data['pricing']``, the provider block, the model
      block, or ``kind`` value missing/wrong-type -> "0"
    - on hit, returns ``str(value)`` where ``value`` is whatever
      ``yaml.safe_load`` produced (int stays int-repr, float stays
      float-repr, str stays str)
    - no exceptions escape; ``lookup`` never raises on malformed data
      shape — it degrades to ``"0"`` so callers can compare to bash's
      warning-then-``0`` behavior at the stdout level.
"""

from __future__ import annotations

from typing import Any, Mapping


ALLOWED: frozenset[str] = frozenset({"input", "output", "cache_read", "cache_write"})


def lookup(data: Mapping[str, Any] | None, provider: str, model: str, kind: str) -> str:
    """Resolve ``pricing[provider][model][kind]`` against a parsed YAML doc.

    Returns the ``str()`` of the YAML-parsed rate on a hit. Returns the
    literal string ``"0"`` for every miss path — unknown kind, missing
    provider/model/kind in the data, malformed shape (e.g. ``pricing`` is
    not a dict, provider block is a scalar), or ``None`` ``data``. Never
    raises; never logs; never touches the filesystem or environment.

    The string return is deliberately non-coerced to numeric so a
    downstream caller comparing against bash's stdout sees a byte-exact
    match: yaml `3.00` -> float 3.0 -> ``str()`` -> ``"3.0"``, identical
    to what bash's heredoc emits.
    """
    if kind not in ALLOWED:
        return "0"
    if not isinstance(data, Mapping):
        return "0"
    table = data.get("pricing")
    if not isinstance(table, Mapping):
        return "0"
    provider_block = table.get(provider)
    if not isinstance(provider_block, Mapping):
        return "0"
    model_block = provider_block.get(model)
    if not isinstance(model_block, Mapping):
        return "0"
    rate = model_block.get(kind)
    if rate is None:
        return "0"
    try:
        return str(rate)
    except Exception:
        return "0"