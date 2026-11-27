"""Public run sharing: build a redacted bundle, upload it, get a URL.

Three pieces, each usable on its own:

* :mod:`mini_ork.publish.bundle` — read a run directory into
  ``mini-ork.public-run/v1``;
* :mod:`mini_ork.publish.redact` — replace credentials/PII and prove it;
* :mod:`mini_ork.publish.client` — POST the result to the control plane.

The CLI is :mod:`mini_ork.cli.publish` (``mini-ork publish``).
"""
from __future__ import annotations

from .bundle import SCHEMA, build_bundle, collect_bundle, discover_run_dir
from .client import PublishError, publish
from .redact import (
    RedactionReport,
    redact_text,
    redact_value,
    scan_for_secrets,
    scan_for_secrets_obj,
)

__all__ = [
    "SCHEMA",
    "PublishError",
    "RedactionReport",
    "build_bundle",
    "collect_bundle",
    "discover_run_dir",
    "publish",
    "redact_text",
    "redact_value",
    "scan_for_secrets",
    "scan_for_secrets_obj",
]
