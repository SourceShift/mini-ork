"""Single source of truth for the mini-ork package version.

``pyproject.toml`` reads this attribute at build time:

    [tool.setuptools.dynamic]
    version = {attr = "mini_ork.version.__version__"}

so the installed distribution metadata, the ``version`` subcommand, the
``--help`` header and the ACP ``initialize`` handshake all resolve to one
string. Do not duplicate it: the four hand-maintained copies had already
drifted (``--help`` said ``v0.1`` while ``version`` said ``0.9.0``) and
nothing in CI noticed.
"""

from __future__ import annotations

__version__ = "0.10.0"
