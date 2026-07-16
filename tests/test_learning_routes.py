"""The /api/v1/learning surface — and a guard against the API going dark again.

Context: an audit of state.db found 28 of 37 POPULATED tables were served by no
endpoint. The bandit's learned policy and GEPA's promotion outcomes were among
them, so the loop could be watched running but never watched LEARNING.

The last test here is the important one: it fails when a new table accumulates
rows but no route reads it. Without it, the API silently goes dark again the next
time someone adds a learning mechanism — and dark data is indistinguishable from
absent data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:
    pytest.skip(f"fastapi.testclient unavailable: {exc}", allow_module_level=True)

from mini_ork.web.app import create_app

ENDPOINTS = [
    "/api/v1/learning/summary",
    "/api/v1/learning/bandit",
    "/api/v1/learning/gepa",
    "/api/v1/learning/failures",
    "/api/v1/learning/patterns",
    "/api/v1/learning/topology",
    "/api/v1/learning/circuit-breakers",
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """A mini-ork home with an EMPTY state.db — the fresh-install case."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    sqlite3.connect(home / "state.db").close()
    return TestClient(create_app(home=home))


@pytest.mark.parametrize("path", ENDPOINTS)
def test_empty_db_returns_empty_not_500(client: TestClient, path: str) -> None:
    """A fresh state.db must yield empty structures, never a 500.

    Every handler is has_table-guarded precisely so a new user's first `serve`
    does not explode.
    """
    r = client.get(path)
    assert r.status_code == 200, f"{path} → {r.status_code}: {r.text[:200]}"
    r.json()  # must be valid JSON, not an HTML SPA fallback


def test_summary_shape(client: TestClient) -> None:
    body = client.get("/api/v1/learning/summary").json()
    for key in (
        "bandit_arms",
        "gradients",
        "promotions",
        "promoted",
        "failures",
        "patterns",
        "topologies",
        "open_circuit_breakers",
        "traces",
    ):
        assert key in body, f"summary missing {key}"
        assert isinstance(body[key], int)


def test_bandit_and_gepa_are_keyed_not_flat(client: TestClient) -> None:
    """The bandit has two partitions (domain/region) and GEPA has three stages.

    Flattening either would lose the distinction that matters: `region` is the fine
    partition the router prefers, `domain` the coarse fallback; and a gradient that
    was PROPOSED is not the same as one that was PROMOTED.
    """
    bandit = client.get("/api/v1/learning/bandit").json()
    assert set(bandit) == {"domain", "region"}

    gepa = client.get("/api/v1/learning/gepa").json()
    assert set(gepa) == {"gradient_count", "win_rates", "promotions"}


def test_cors_allows_electron_and_localhost_but_not_the_web(client: TestClient) -> None:
    """Orca's renderer is an Electron file:// page → it sends `Origin: null`.

    Without this the panel's every fetch is blocked, and a CORS-blocked fetch looks
    EXACTLY like "no data" — a silent empty panel, which is the failure mode this
    project refuses everywhere else.
    """
    ok_null = client.get(
        "/api/v1/learning/summary", headers={"Origin": "null"}
    ).headers.get("access-control-allow-origin")
    assert ok_null == "null", "Electron file:// renderer would be CORS-blocked"

    ok_local = client.get(
        "/api/v1/learning/summary", headers={"Origin": "http://localhost:5199"}
    ).headers.get("access-control-allow-origin")
    assert ok_local == "http://localhost:5199", "an Electron dev renderer would be blocked"

    # ...but the control endpoints must NOT be reachable from a page on the web.
    leaked = client.get(
        "/api/v1/learning/summary", headers={"Origin": "https://evil.com"}
    ).headers.get("access-control-allow-origin")
    assert leaked is None, "a remote origin was granted CORS access to the control plane"


def test_no_populated_table_is_dark(tmp_path: Path) -> None:
    """THE GUARD: every table with rows must be read by some route.

    This is what stops the regression that motivated this module. It runs against the
    developer's REAL state.db when present (that is where tables actually accumulate
    rows); it skips in a clean checkout rather than passing vacuously.
    """
    real_db = Path.cwd() / ".mini-ork" / "state.db"
    if not real_db.exists():
        pytest.skip("no local state.db — nothing to audit")

    con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]

    populated = []
    for t in tables:
        try:
            if con.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone():  # noqa: S608
                populated.append(t)
        except sqlite3.DatabaseError:
            continue
    con.close()

    routes_src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (Path(__file__).parent.parent / "mini_ork" / "web" / "routes").glob("*.py")
    )

    # Bookkeeping tables carry no signal about the loop and are deliberately exempt.
    EXEMPT = {"schema_migrations", "version_registry", "sqlite_sequence"}

    dark = sorted(t for t in populated if t not in EXEMPT and t not in routes_src)

    assert not dark, (
        f"{len(dark)} populated table(s) are served by NO route — the loop cannot be "
        f"seen learning: {dark}. Add them to mini_ork/web/routes/learning.py, or add "
        f"to EXEMPT with a reason."
    )
