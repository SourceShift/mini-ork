"""The codex/opencode live stream must survive the node, not be deleted with it.

`SidecarTelemetryEngine.dispatch` used to mkstemp its usage sidecar in a temp
dir and then unlink the derived ``${MO_USAGE_FILE%.tokens}.stream.jsonl`` in
``finally``. The transports write that stream line by line while the harness
runs, so the file was the one genuinely live artifact in the Python path — and
every node exit threw it away. Measured before the fix: 0 ``*.turns.jsonl`` on
disk, only legacy ``.stream.jsonl`` files from the bash era.

The fake harness below writes both sidecars exactly as ``codex_transport.run``
and ``opencode_transport.run`` do, so this pins the engine's path selection and
its cleanup policy without spawning a real CLI.
"""

from __future__ import annotations

import json
import sys

from mini_ork.dispatch.models import DispatchRequest
from mini_ork.dispatch.providers import ProviderSpec, SidecarTelemetryEngine

# Mirrors the real transports: MO_USAGE_FILE is a TSV, MO_COST_FILE a float, and
# the stream is `${MO_USAGE_FILE%.tokens}.stream.jsonl`, appended to as the
# harness emits — i.e. already on disk before the process exits.
_FAKE_HARNESS = (
    "import os,sys\n"
    "usage=os.environ['MO_USAGE_FILE']\n"
    "cost=os.environ['MO_COST_FILE']\n"
    "stream=usage[:-7]+'.stream.jsonl' if usage.endswith('.tokens') else usage\n"
    "open(usage,'w').write('120\\t34\\t5\\t6\\n')\n"
    "open(cost,'w').write('0.75')\n"
    "with open(stream,'a') as fh:\n"
    "    for i in range(3):\n"
    "        fh.write('{\"i\": %d}\\n' % i)\n"
    "print('done')\n"
)


def _spec(model: str = "codex") -> ProviderSpec:
    return ProviderSpec(
        model=model, command=(sys.executable, "-c", _FAKE_HARNESS)
    )


def _run(request: DispatchRequest):
    return SidecarTelemetryEngine(byo_endpoint=True).dispatch(request, _spec())


def test_stream_is_kept_under_the_run_dir_and_named_conventionally(tmp_path, monkeypatch):
    """The name matters: __main__ registers ``agent-<node>.stream.jsonl`` as a
    run_artifact, retention gzips that glob, and run_detail serves it. A stream
    written under any other name is on disk but invisible to all three."""
    run_dir = tmp_path / "runs" / "task-1"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_NODE_ID", "implementer:0")
    monkeypatch.delenv("MO_DISPATCH_BACKEND", raising=False)

    result = _run(DispatchRequest(model="codex", prompt="hi", cwd=None))

    assert result.ok
    assert result.usage.input_tokens == 120
    assert result.cost_usd == 0.75
    stream = run_dir / "agent-implementer_0.stream.jsonl"
    assert stream.is_file(), f"live stream was not persisted; got {list(run_dir)}"
    assert [json.loads(l)["i"] for l in stream.read_text().splitlines()] == [0, 1, 2]


def test_scratch_sidecars_are_cleaned_up_but_the_stream_is_not(tmp_path, monkeypatch):
    """The usage/cost files are the engine's own scratch; the stream has a
    consumer. Cleaning up the wrong one of the three is the original bug."""
    run_dir = tmp_path / "runs" / "task-2"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_NODE_ID", "reviewer")
    monkeypatch.delenv("MO_DISPATCH_BACKEND", raising=False)

    _run(DispatchRequest(model="codex", prompt="hi", cwd=None))

    assert not (run_dir / "agent-reviewer.tokens").exists()
    assert not (run_dir / "agent-reviewer.cost").exists()
    assert (run_dir / "agent-reviewer.stream.jsonl").is_file()


def test_without_a_run_dir_the_temp_stream_is_removed_not_leaked(monkeypatch):
    """No run dir means no consumer. Keeping the file would leave an
    unfindable orphan in the temp dir on every node of every run."""
    import glob
    import tempfile

    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MO_NODE_ID", raising=False)
    monkeypatch.delenv("MO_DISPATCH_BACKEND", raising=False)
    before = set(glob.glob(tempfile.gettempdir() + "/*.stream.jsonl"))

    result = _run(DispatchRequest(model="codex", prompt="hi", cwd=None))

    assert result.ok
    assert set(glob.glob(tempfile.gettempdir() + "/*.stream.jsonl")) == before


def test_run_dir_is_repaired_when_missing_rather_than_losing_the_stream(tmp_path, monkeypatch):
    """A run dir that does not exist is a broken bootstrap. Silently falling
    back to a temp dir would discard the stream while looking like success."""
    run_dir = tmp_path / "not" / "created" / "yet"
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_NODE_ID", "n1")
    monkeypatch.delenv("MO_DISPATCH_BACKEND", raising=False)

    assert _run(DispatchRequest(model="codex", prompt="hi", cwd=None)).ok

    assert (run_dir / "agent-n1.stream.jsonl").is_file()


def test_node_id_is_sanitized_the_same_way_the_artifact_index_sanitizes_it(tmp_path, monkeypatch):
    """A mismatched sanitizer would write ``agent-a_b`` while __main__ looks for
    ``agent-a:b`` — on disk, but permanently unindexed."""
    run_dir = tmp_path / "runs" / "t"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_NODE_ID", "../../etc/passwd")
    monkeypatch.delenv("MO_DISPATCH_BACKEND", raising=False)

    _run(DispatchRequest(model="codex", prompt="hi", cwd=None))

    assert (run_dir / "agent-.._.._etc_passwd.stream.jsonl").is_file()
    assert not (tmp_path / "etc").exists()
