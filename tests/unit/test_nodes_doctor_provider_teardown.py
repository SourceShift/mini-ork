"""``nodes doctor`` check 9 on a provider node verifies the node is gone.

Teardown destroys a provider-created node, so asking it about the session
afterwards can only fail: E2B's proxy answered HTTP 502 for the deleted
sandbox and check 9 reported a false FAIL (2026-10-13). Reuses the provider-seam
harness (a real in-process node-agent behind a fake provider)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_remote_provider_seam import _fresh, _seam_env, seam  # noqa: E402,F401


def test_check_9_passes_when_the_released_node_no_longer_answers(tmp_path, monkeypatch, seam):  # noqa: F811
    from mini_ork.cli import nodes
    from mini_ork.runtime.backends import remote

    _seam_env(tmp_path, monkeypatch, "doctor-e2b-like")
    original = remote.RemoteWorkspace._json

    def dead_after_release(self, method, path, *args, **kwargs):
        if seam.released:          # the node is gone: its proxy answers 502
            raise remote.RemoteUnavailableError(f"node-agent {method} {path} -> HTTP 502: Bad Gateway")
        return original(self, method, path, *args, **kwargs)

    monkeypatch.setattr(remote.RemoteWorkspace, "_json", dead_after_release)
    lines: list[str] = []
    rc = nodes.doctor(env_name="seam", no_llm=True, out=lines.append)
    check9 = [ln for ln in lines if ln.startswith("9.")]
    assert rc == 0, lines
    assert check9 and "PASS" in check9[0] and "released" in check9[0], check9
