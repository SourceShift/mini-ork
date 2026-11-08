"""mini-ork cli package (reorg from ported/).

Importing this package bootstraps out-of-tree plugins
(:func:`mini_ork.plugins.bootstrap`): every native subcommand runs in its own
``python -m mini_ork.cli.<sub>`` process, and several of them — ``resume``,
``recover``, ``execute`` — are the documented remedy for a refusal raised
during dispatch, so they must see the same registries the dispatcher saw. Doing
it here, once per process, is the only chokepoint they all share. Discovery is
idempotent, never raises, and ``MINI_ORK_NO_PLUGINS=1`` turns it off.
"""
from mini_ork.plugins import bootstrap as _bootstrap

_bootstrap()
