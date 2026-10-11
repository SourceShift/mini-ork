"""``board page features`` — the read-only controllable-feature catalogue page."""
from __future__ import annotations

import json
from pathlib import Path

from mini_ork.ide_pages import PAGES, build_page, features

from test_ide_pages_orch import check_page


def test_registered_in_pages() -> None:
    assert PAGES.get("features") == "Features"


def _features() -> list[dict]:
    from mini_ork.features import catalogue_json

    return catalogue_json()["features"]


def test_catalogue_renders_every_feature(tmp_path: Path) -> None:
    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    page = build_page(home, "features")
    check_page(page, "features")
    assert page["ok"] is True
    assert [s["title"] for s in page["sections"]] == ["Catalogue", "Features"]

    catalogue = _features()
    rows = page["sections"][1]["rows"]
    assert len(rows) == len(catalogue) >= 1
    assert {r["cells"][1]["t"] for r in rows} == {f["id"] for f in catalogue}
    assert {r["cells"][2]["t"] for r in rows} <= {"premium", "baseline"}
    json.dumps(page)


def test_tier_split_matches_the_registry(tmp_path: Path) -> None:
    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    catalogue = _features()
    page = build_page(home, "features")
    rows = page["sections"][1]["rows"]
    # rows are id-sorted; each tier is the registry's own value.
    for row, feat in zip(rows, sorted(catalogue, key=lambda f: f["id"])):
        assert row["cells"][0]["t"] == feat["label"]
        assert row["cells"][2]["t"] == feat["tier"]


def test_header_carries_the_premium_threshold(tmp_path: Path) -> None:
    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    from mini_ork.features import catalogue_json

    catalogue = catalogue_json()
    page = build_page(home, "features")
    kv = {i["k"]: i["v"] for i in page["sections"][0]["items"]}
    assert kv["Premium threshold"] == f"×{catalogue['premium_threshold']:g}"
    assert kv["Source"] == "mini_ork.features.catalogue_json()"
    assert "premium" in kv["Features"]


def test_unknown_tab_falls_back_to_the_catalogue(tmp_path: Path) -> None:
    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    page = features.build(home, "nope", {})
    assert page["tab"] == "catalogue" and page["sections"]
