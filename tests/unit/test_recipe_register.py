"""Contracts for ``mini_ork.cli.recipe_register.load_recipe_register``.

Mirrors the loader precedent at ``bin/mini-ork:_bootstrap_install``.
"""
from __future__ import annotations

import pytest

from mini_ork.cli import recipe_register as rr
from mini_ork.cli.recipe_register import (
    RecipeRegisterError,
    load_recipe_register,
)


@pytest.fixture(autouse=True)
def _reset_loader_state():
    """Snapshot/restore the module-level idempotency set around each test.

    Tests 2-5 use fresh ``tmp_path`` recipe dirs so cross-test contamination
    should already be impossible, but restoring guards against accidental
    state leaks (e.g. a chained test running on the same path).
    """
    saved = set(rr._LOADED)
    rr._LOADED.clear()
    try:
        yield
    finally:
        rr._LOADED.clear()
        rr._LOADED.update(saved)


def test_absent_register_py_returns_false_silently(tmp_path):
    recipe_dir = tmp_path / "no_register_recipe"
    recipe_dir.mkdir()

    assert load_recipe_register(recipe_dir) is False
    assert rr._LOADED == set()


def test_present_register_py_executes_once(tmp_path):
    recipe_dir = tmp_path / "sentinel_recipe"
    recipe_dir.mkdir()
    sentinel_path = tmp_path / "sentinel.txt"
    sentinel_path.write_text("count=0\n", encoding="utf-8")
    (recipe_dir / "register.py").write_text(
        "from pathlib import Path\n"
        f"p = Path({str(sentinel_path)!r})\n"
        "p.write_text('count=1\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert load_recipe_register(recipe_dir) is True
    assert sentinel_path.read_text(encoding="utf-8") == "count=1\n"


def test_idempotent_second_call_does_not_re_execute(tmp_path):
    recipe_dir = tmp_path / "double_load_recipe"
    recipe_dir.mkdir()
    invocations = tmp_path / "invocations.txt"
    invocations.write_text("0", encoding="utf-8")
    (recipe_dir / "register.py").write_text(
        "from pathlib import Path\n"
        f"p = Path({str(invocations)!r})\n"
        "p.write_text(str(int(p.read_text() or '0') + 1), encoding='utf-8')\n",
        encoding="utf-8",
    )

    first = load_recipe_register(recipe_dir)
    second = load_recipe_register(recipe_dir)

    assert first is True
    assert second is True
    assert invocations.read_text(encoding="utf-8") == "1"


def test_syntax_error_in_register_py_raises_recipe_register_error(tmp_path):
    recipe_dir = tmp_path / "syntax_error_recipe"
    recipe_dir.mkdir()
    (recipe_dir / "register.py").write_text(
        "def broken(:\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(RecipeRegisterError) as excinfo:
        load_recipe_register(recipe_dir)
    # The original SyntaxError is chained via __cause__.
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, SyntaxError)


def test_runtime_error_in_register_py_raises_recipe_register_error_chained(tmp_path):
    recipe_dir = tmp_path / "runtime_error_recipe"
    recipe_dir.mkdir()
    (recipe_dir / "register.py").write_text(
        "raise ValueError('boom from register.py body')\n",
        encoding="utf-8",
    )

    with pytest.raises(RecipeRegisterError) as excinfo:
        load_recipe_register(recipe_dir)
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "boom from register.py body" in str(excinfo.value.__cause__)


def test_idempotency_after_failure_re_allows_retry(tmp_path):
    """A failed load leaves _LOADED untouched so a corrected retry can run."""
    recipe_dir = tmp_path / "retry_recipe"
    recipe_dir.mkdir()
    register = recipe_dir / "register.py"
    register.write_text("raise RuntimeError('first try')\n", encoding="utf-8")

    with pytest.raises(RecipeRegisterError):
        load_recipe_register(recipe_dir)
    assert rr._LOADED == set()

    # Author fixes the file; loader must succeed now.
    register.write_text("SENTINEL = 'fixed'\n", encoding="utf-8")
    assert load_recipe_register(recipe_dir) is True
    # Second call hits the cache.
    assert load_recipe_register(recipe_dir) is True
