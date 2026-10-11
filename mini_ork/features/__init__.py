"""Controllable-feature registry (the wizard's single source of truth).

Public surface:

    from mini_ork.features import (
        Feature, CONTROLLABLE_FEATURES, PREMIUM_THRESHOLD,
        register_feature, features_for, effective_multiplier,
        premium_unselected, resolve_selection, resolve_defaults,
        catalogue_markdown, catalogue_json,
    )

``store`` is deliberately not re-exported here: it imports PyYAML, and this
package is imported on hot paths (see ``registry.py``). Import it directly.
"""

from __future__ import annotations

from .registry import (
    CONTROLLABLE_FEATURES,
    PREMIUM_THRESHOLD,
    Feature,
    catalogue_json,
    catalogue_markdown,
    effective_multiplier,
    features_for,
    gate_env,
    get_feature,
    premium_unselected,
    register_feature,
    resolve_defaults,
    resolve_selection,
)

__all__ = [
    "CONTROLLABLE_FEATURES",
    "PREMIUM_THRESHOLD",
    "Feature",
    "catalogue_json",
    "catalogue_markdown",
    "effective_multiplier",
    "features_for",
    "gate_env",
    "get_feature",
    "premium_unselected",
    "register_feature",
    "resolve_defaults",
    "resolve_selection",
]
