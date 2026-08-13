"""GEPA <-> mini-ork integration (reflective prompt evolution).

Two ways in, one import surface:

- **Backends** (``backends``) are pure stdlib — no ``gepa`` framework dependency,
  so they import even when ``pip install gepa`` hasn't run. This is the
  bring-your-own-evaluator seam: ``SubprocessRunBackend`` (cross-language critic),
  ``CallbackRunBackend`` (in-process Python), the ``RunBackend`` Protocol, and the
  ``ExternalTrace`` / ``EvaluatorError`` value types.
- **Adapters** (``GEPARunBackendAdapter``, ``MiniOrkGEPAAdapter``) compose a
  backend into the external ``gepa`` framework's ``GEPAAdapter`` interface, so
  they require ``gepa`` to be installed. They are exposed lazily: importing this
  package never forces the framework import; touching an adapter name does.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mini_ork.gepa.backends import (
    CallbackRunBackend,
    EvaluatorError,
    ExternalTrace,
    RunBackend,
    SubprocessRunBackend,
)

if TYPE_CHECKING:  # keep the framework-dependent names visible to type checkers
    from mini_ork.gepa.generic_adapter import GEPARunBackendAdapter
    from mini_ork.gepa.miniork_adapter import MiniOrkGEPAAdapter

__all__ = [
    "CallbackRunBackend",
    "EvaluatorError",
    "ExternalTrace",
    "GEPARunBackendAdapter",
    "MiniOrkGEPAAdapter",
    "RunBackend",
    "SubprocessRunBackend",
]

_LAZY = {
    "GEPARunBackendAdapter": "mini_ork.gepa.generic_adapter",
    "MiniOrkGEPAAdapter": "mini_ork.gepa.miniork_adapter",
}


def __getattr__(name: str):
    """Lazily import framework-dependent adapters (PEP 562).

    Keeps ``import mini_ork.gepa`` free of the optional ``gepa`` dependency; the
    ImportError only surfaces if you actually reach for an adapter without the
    framework installed.
    """
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)
