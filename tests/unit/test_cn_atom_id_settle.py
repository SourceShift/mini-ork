"""Render ContextNest atom ids so the run-verdict settle can credit them.

Kickoff: ``kickoffs/auto/rsi-t0-cn-atom-id.md`` (RSI Tier-0 #3). The prefetch
renderer (``mini_ork.cn_client.render_atoms_md``) must splice
`` id=<full atom id>`` into the hit line when the hit carries a ``cn-`` prefixed
``id``; the settle sweep (``mini_ork.cli.execute._collect_cn_atom_ids``) reads
the prefetch markdown with ``_CN_ATOM_ID_RE`` and must then find those ids.

Hermetic: ``tmp_path`` only, no network, no credentials.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import cn_client as cn  # noqa: E402
from mini_ork.cli import execute as ex  # noqa: E402

AID = "cn-275a1cef9eb1ba9c328a5fee8230defb"
AID2 = "cn-0f3e6d5c8b9a4f2e7d1c6a5b4c3d2e1f"

# Kickoff: '### The id is rendered' — the exact line.
ID_LINE = f"- [decision sim=0.80 2026-10-24 sess=898364fe id={AID}] c"
# Kickoff: 'Other lines are unchanged' — today's byte-for-byte format.
PLAIN_LINE = "- [decision sim=0.80 2026-10-24 sess=898364fe] c"


def _hit(**over):
    h = {
        "similarity": 0.8,
        "metadata": {"kind": "decision", "ts": "2026-10-24T00:00:00"},
        "session_id": "898364fe-a053",
        "id": AID,
        "content": "c",
    }
    h.update(over)
    return h


def _payload(*hits):
    return json.dumps({"hits": list(hits)})


def _write_prefetch(run_dir, md):
    d = Path(run_dir) / "cn_prefetch"
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.md").write_text(md, encoding="utf-8")


def test_id_is_rendered_into_line():
    md = cn.render_atoms_md(_payload(_hit()))
    assert ID_LINE in md.splitlines()


def test_non_cn_or_missing_id_line_unchanged():
    without_id = {k: v for k, v in _hit().items() if k != "id"}
    for hit in (_hit(id="zz"), _hit(id=123), without_id):
        md = cn.render_atoms_md(_payload(hit))
        assert PLAIN_LINE in md.splitlines()
        assert " id=" not in md


def test_settle_finds_rendered_id(tmp_path):
    md = cn.render_atoms_md(_payload(_hit()))
    _write_prefetch(tmp_path, md)
    assert ex._collect_cn_atom_ids(str(tmp_path)) == [AID]


def test_ids_deduped_in_document_order(tmp_path):
    hits = [
        _hit(content="first"),
        _hit(id=AID2, content="second"),
        _hit(content="third-repeat"),
    ]
    md = cn.render_atoms_md(_payload(*hits))
    _write_prefetch(tmp_path, md)
    assert ex._collect_cn_atom_ids(str(tmp_path)) == [AID, AID2]


def test_header_footer_cut_and_limit_unchanged():
    long_hits = [_hit(content="x" * 300), _hit(id=AID2, content="second")]
    md = cn.render_atoms_md(_payload(*long_hits), limit=1)
    lines = md.splitlines()
    assert lines[0] == "--- ContextNest atoms (fresh substrate retrieval) ---"
    assert lines[-1] == "--- /ContextNest atoms ---"
    assert lines[2] == f"- [decision sim=0.80 2026-10-24 sess=898364fe id={AID}] " + "x" * 277 + "..."
    assert len([line for line in lines if line.startswith("- [")]) == 1
