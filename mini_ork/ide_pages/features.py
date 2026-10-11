"""Features — the controllable-feature catalogue, read-only.

Rows come from :func:`mini_ork.features.catalogue_json` (the single source of
truth the CLI wizard and ``/wizard`` also enumerate), never from a list kept
here. A feature is *premium* when its own cost multiplier exceeds the
catalogue's ``premium_threshold``; the threshold is shown in the header so the
tier split stays auditable.

This page is a listing only, and stays that way: it draws no control that could
change a run's cost. The controls live on **Setup & health → Features**, which
reads this same registry in-process so the two can never disagree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("catalogue", "Catalogue")]


def _multiplier(feature: dict[str, Any]) -> float:
    """The feature's own default multiplier; ``1.0`` when it is unusable."""
    try:
        return float(feature.get("cost_multiplier"))
    except (TypeError, ValueError):
        return 1.0


def _tier(feature: dict[str, Any], threshold: float) -> str:
    """The registry's own tier, else derived from the multiplier vs threshold."""
    tier = str(feature.get("tier") or "").strip()
    if tier:
        return tier
    return "premium" if _multiplier(feature) > threshold else "baseline"


def _catalogue() -> list[dict[str, Any]]:
    from mini_ork.features import catalogue_json

    catalogue = catalogue_json()
    threshold = float(catalogue.get("premium_threshold") or 0.0)
    features = [f for f in (catalogue.get("features") or []) if isinstance(f, dict)]
    premium = sum(1 for f in features if _tier(f, threshold) == "premium")
    rows = []
    for feature in sorted(features, key=lambda f: str(f.get("id") or "")):
        tier = _tier(feature, threshold)
        rows.append([
            S.cell(feature.get("label") or feature.get("id") or "—", "text", b=True),
            S.mono(feature.get("id") or "—"),
            S.cell(tier, "yellow" if tier == "premium" else "green"),
            S.cell(f"×{_multiplier(feature):g}", "sub", mono=True),
        ])
    header = S.kv("Catalogue", [
        ("Source", "mini_ork.features.catalogue_json()", "text",
         "the same list the CLI wizard and /wizard enumerate"),
        ("Premium threshold", f"×{threshold:g}", "text",
         "a feature is premium when its own multiplier exceeds this"),
        ("Features", f"{len(features)} ({premium} premium)", "text",
         "as shipped, before any recipe narrows the set"),
    ], full=True, note="Read-only: turning a feature on or off is done by "
                       "mini-ork features, not from here.",
        actions=[S.btn("Turn features on or off", S.page_link("setup", "features"), "primary")])
    table = S.table("Features", [S.col(fr=1.4), S.col(fr=1.8), S.col(90), S.col(80)],
                    ["feature", "id", "tier", "cost"], rows, full=True,
                    note="Tier reflects the feature's own default; a recipe may apply only some of these.")
    return [header, table]


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    errors: dict[str, str] = {}
    sections = S.guarded(errors, "Controllable features", _catalogue)
    return S.page("features", "Features",
                  "The controllable-feature catalogue — every knob, its cost, and whether it is premium. "
                  "Read-only.",
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
