"""The harvest report always lists mining-stage rejections, and the host-path
guard covers the common host roots without rejecting a universal shebang."""
from __future__ import annotations

import pytest

from mini_ork.learning import probe_harvest


def test_report_lists_mined_rejections_even_when_validation_ran(monkeypatch, tmp_path):
    mined = [{"task_id": "run-x", "reason": "answer-pointer"},
             {"task_id": "run-y", "reason": "host-path"}]
    monkeypatch.setattr(probe_harvest, "failed_run_candidates",
                        lambda home, root, landing_ref=None: ([], list(mined)))
    report = probe_harvest.harvest(
        "framework-edit", root=str(tmp_path), home=str(tmp_path),
        validate_fn=lambda candidate: (True, "", candidate),
    )
    # ``rejected`` keeps its this-run semantics (validation resets it) ...
    assert report["rejected"] == []
    # ... while the guards stay visible.
    assert report["mined_rejected"] == mined


@pytest.mark.parametrize("path", [
    "/root/proj/x.py", "/workspace/app/y.py", "/workspaces/repo/z", "/data/run/out.json",
    "/media/disk/a", "/scratch/tmp/b", "/Library/Caches/c", "/Applications/Tool.app/d",
])
def test_host_path_covers_common_host_roots(path):
    _text, leftovers = probe_harvest.relativize_host_paths(f"see {path} here", [])
    assert leftovers == [path]


@pytest.mark.parametrize("text", [
    "#!/usr/bin/env bash", "#!/usr/bin/env python3", "cat /etc/hosts",
])
def test_universal_paths_are_not_host_paths(text):
    _text, leftovers = probe_harvest.relativize_host_paths(text, [])
    assert leftovers == []
