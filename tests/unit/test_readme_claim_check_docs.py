"""Doc-claim probes in scripts/readme_claim_check.py.

The checker used to probe README.md only. CONTRIBUTING.md told contributors to
run `bash tests/smoke.sh` for months after `tests/smoke.sh` was deleted, and
nothing noticed, because no probe read that file. These tests pin the widened
behaviour: a path cited in a contributor doc must exist, a glob or placeholder
is not a claim, and the real repo passes its own gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "readme_claim_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("readme_claim_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module is importable by name during load.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_command_span_yields_the_path_not_the_command() -> None:
    mod = _load()
    claims = mod.doc_claim_paths("Run `bash tests/smoke.sh` before you push.")
    assert claims == ["tests/smoke.sh"]


def test_globs_and_placeholders_are_not_claims() -> None:
    mod = _load()
    text = "See `lib/*.sh`, `tests/unit/test_<area>_py.py` and `docs/$AREA/x.md`."
    assert mod.doc_claim_paths(text) == []


def test_missing_path_is_reported_with_its_document(tmp_path: Path) -> None:
    mod = _load()
    _write(tmp_path, "CONTRIBUTING.md", "Run `bash tests/smoke.sh`.\n")
    missing = mod.missing_doc_claims(str(tmp_path))
    assert missing == [("CONTRIBUTING.md", "tests/smoke.sh")]


def test_existing_path_passes(tmp_path: Path) -> None:
    mod = _load()
    _write(tmp_path, "CONTRIBUTING.md", "Run `python3 -m pytest tests/unit/x.py`.\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "x.py").write_text("", encoding="utf-8")
    assert mod.missing_doc_claims(str(tmp_path)) == []


def test_skip_list_covers_deliberately_absent_paths(tmp_path: Path) -> None:
    mod = _load()
    _write(tmp_path, ".github/PULL_REQUEST_TEMPLATE.md",
           "Keys live in `config/secrets.local.sh`.\n")
    assert mod.missing_doc_claims(str(tmp_path)) == []


def test_absent_doc_file_is_ignored(tmp_path: Path) -> None:
    mod = _load()
    assert mod.missing_doc_claims(str(tmp_path)) == []


def test_real_repo_has_no_missing_doc_claims() -> None:
    mod = _load()
    assert mod.missing_doc_claims(str(REPO)) == []


def test_pre_commit_config_parses() -> None:
    """A hook config that does not parse disables every hook it declares.

    `.pre-commit-config.yaml` sat unparseable for months: one plain-scalar
    `entry:` contained `in *workflow.yaml)` and YAML read the `: ` as a
    mapping. Nothing noticed because nothing loaded the file.
    """
    import yaml

    data = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in data["repos"] for hook in repo["hooks"]]
    assert hooks
    for hook in hooks:
        assert hook["id"]
        if "entry" in hook:
            assert hook["entry"].strip()

