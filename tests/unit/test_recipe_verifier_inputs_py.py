"""Recipe verifiers must read the artifact THIS dispatch staged, not a stale one.

``mini_ork.workflow.artifacts.prepare_inputs`` never overwrites an artifact whose
content differs from what is already staged: a re-dispatched node finds this
pass's copy beside the previous one as ``2-<file>``, ``3-<file>``, … and records
which paths are current in ``$MINI_ORK_NODE_INPUT_MANIFEST``.

Both RSI verifier helpers used to rebuild the path from the bare filename, so a
second dispatch validated the FIRST pass's artifact. That failed rsi cycle
``rsi-c1793838883`` with: "the synthesis answers candidate 'rsi-2d41f32ae47857f0'
but this run was dispatched for 'rsi-d919ef5328692408'".
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMONS = {
    "rsi-cycle": REPO / "recipes" / "rsi-cycle" / "verifiers" / "_common.py",
    "rsi-critic": REPO / "recipes" / "rsi-critic" / "verifiers" / "_common.py",
}


def _stage(tmp_path: Path, name: str, files: dict[str, str]) -> None:
    folder = tmp_path / "inputs" / name
    folder.mkdir(parents=True, exist_ok=True)
    for filename, body in files.items():
        (folder / filename).write_text(body, encoding="utf-8")


def _manifest(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"node_id": "proposal_builder", "inputs": records}), encoding="utf-8")
    return path


def _record(name: str, relative: str) -> dict:
    return {"name": name, "kind": "json", "path": relative, "sha256": "0" * 64, "bytes": 2}


def _load(recipe: str, tmp_path: Path, monkeypatch, manifest: Path | None):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("MINI_ORK_NODE_INPUT_DIR", str(tmp_path / "inputs"))
    if manifest is None:
        monkeypatch.delenv("MINI_ORK_NODE_INPUT_MANIFEST", raising=False)
    else:
        monkeypatch.setenv("MINI_ORK_NODE_INPUT_MANIFEST", str(manifest))
    spec = importlib.util.spec_from_file_location(f"_common_{recipe}", COMMONS[recipe])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_a_redispatched_node_reads_the_artifact_its_manifest_declares(recipe, tmp_path, monkeypatch) -> None:
    _stage(tmp_path, "suggestion", {"suggestion.json": "{}", "2-suggestion.json": "{}"})
    manifest = _manifest(tmp_path, [_record("suggestion", "suggestion/2-suggestion.json")])
    common = _load(recipe, tmp_path, monkeypatch, manifest)

    assert common.input_file("suggestion", "suggestion.json") == str(
        tmp_path / "inputs" / "suggestion" / "2-suggestion.json"
    )


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_without_a_manifest_the_bare_name_is_still_read(recipe, tmp_path, monkeypatch) -> None:
    # An older engine, or a verifier run by hand: nothing changed for them.
    _stage(tmp_path, "suggestion", {"suggestion.json": "{}"})
    common = _load(recipe, tmp_path, monkeypatch, None)

    assert common.input_file("suggestion", "suggestion.json") == str(
        tmp_path / "inputs" / "suggestion" / "suggestion.json"
    )


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_a_manifest_naming_the_bare_file_reads_it(recipe, tmp_path, monkeypatch) -> None:
    # The first dispatch of a node declares the unsuffixed name; it must not
    # acquire a "2-" prefix on the way through the resolver.
    _stage(tmp_path, "suggestion", {"suggestion.json": "{}"})
    manifest = _manifest(tmp_path, [_record("suggestion", "suggestion/suggestion.json")])
    common = _load(recipe, tmp_path, monkeypatch, manifest)

    assert common.input_file("suggestion", "suggestion.json") == str(
        tmp_path / "inputs" / "suggestion" / "suggestion.json"
    )


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_a_manifest_declaring_a_cleared_artifact_fails_instead_of_reading_the_stale_one(
    recipe, tmp_path, monkeypatch
) -> None:
    # Fails closed: the previous pass's file is right there, and must not be read.
    _stage(tmp_path, "suggestion", {"suggestion.json": "{}"})
    manifest = _manifest(tmp_path, [_record("suggestion", "suggestion/2-suggestion.json")])
    common = _load(recipe, tmp_path, monkeypatch, manifest)

    with pytest.raises(SystemExit):
        common.input_file("suggestion", "suggestion.json")


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_input_many_returns_only_the_manifests_artifacts(recipe, tmp_path, monkeypatch) -> None:
    # Two passes' lens answers sit in one folder; a panel built from both would
    # count one opinion twice and fail the heterogeneity check on invented lanes.
    _stage(tmp_path, "lenses", {"lens-a.json": "{}", "2-lens-a.json": "{}", "notes.md": ""})
    manifest = _manifest(tmp_path, [_record("lenses", "lenses/2-lens-a.json")])
    common = _load(recipe, tmp_path, monkeypatch, manifest)

    assert common.input_many("lenses", ".json") == [
        str(tmp_path / "inputs" / "lenses" / "2-lens-a.json")
    ]


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_input_many_still_globs_without_a_manifest(recipe, tmp_path, monkeypatch) -> None:
    _stage(tmp_path, "lenses", {"a.json": "{}", "b.json": "{}", "notes.md": ""})
    common = _load(recipe, tmp_path, monkeypatch, None)

    assert common.input_many("lenses", ".json") == [
        str(tmp_path / "inputs" / "lenses" / "a.json"),
        str(tmp_path / "inputs" / "lenses" / "b.json"),
    ]


@pytest.mark.parametrize("recipe", sorted(COMMONS))
def test_an_unreadable_manifest_falls_back_rather_than_crashing(recipe, tmp_path, monkeypatch) -> None:
    _stage(tmp_path, "suggestion", {"suggestion.json": "{}"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    common = _load(recipe, tmp_path, monkeypatch, manifest)

    assert common.input_file("suggestion", "suggestion.json") == str(
        tmp_path / "inputs" / "suggestion" / "suggestion.json"
    )
