"""Host-runtime node-agent output speaks the sandbox view, like docker's.

The host runtime translates canonical → real paths on the way in, so an agent
reported the node's real paths; a planner's plan named
``/home/<user>/…/runs/<id>/target/calc.py`` and the control plane's host-path
guard refused the next node (Daytona e2e, run-1791911811-5826). Output is now
mapped real → canonical before it lands on disk or streams."""
from __future__ import annotations

import os
import sys
import time

from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec, _CanonicalOutput

ROOT = "/home/daytona/.mini-ork-node/state/runs/run-1"
ENGINE = "/home/daytona/.mini-ork-node/state/engines/abc123"
ROOTS = [(ROOT, "/workspace"), (ENGINE, "/opt/mini-ork")]


class _Identity:
    @staticmethod
    def redact(chunk: bytes) -> bytes:
        return chunk


def _feed(chunks: list[bytes], inner=_Identity) -> bytes:
    flt = _CanonicalOutput(ROOTS, inner)
    return b"".join(flt.redact(c) for c in chunks) + flt.flush()


SAMPLE = (f'{{"cwd":"{ROOT}/target","file":"{ROOT}/target/calc.py",'
          f'"engine":"{ENGINE}/mini_ork"}}\nplain {ROOT}\n').encode()
EXPECTED = (b'{"cwd":"/workspace/target","file":"/workspace/target/calc.py",'
            b'"engine":"/opt/mini-ork/mini_ork"}\nplain /workspace\n')


def test_run_root_and_engine_map_back_to_the_sandbox_view():
    assert _feed([SAMPLE]) == EXPECTED


def test_a_root_split_across_reads_is_still_rewritten():
    assert _feed([SAMPLE[i:i + 1] for i in range(len(SAMPLE))]) == EXPECTED
    assert _feed([SAMPLE[:23], SAMPLE[23:61], SAMPLE[61:]]) == EXPECTED


def test_a_sibling_name_is_not_a_root():
    text = f"{ROOT}-old/x and {ROOT}2".encode()
    assert _feed([text]) == text


def test_json_escape_after_a_root_is_a_boundary():
    assert _feed([f'"{ROOT}/target\\n"'.encode()]) == b'"/workspace/target\\n"'


def test_a_held_tail_is_released_on_flush():
    flt = _CanonicalOutput(ROOTS, _Identity)
    head = flt.redact(b"see " + ROOT[:10].encode())
    assert head == b"see "                       # possible root start held back
    assert flt.flush() == ROOT[:10].encode()     # never was one: released unchanged


def test_secret_redaction_still_applies_first():
    class _Secret:
        @staticmethod
        def redact(chunk: bytes) -> bytes:
            return chunk.replace(b"s3cr3t-value", b"***")

    assert _feed([f"{ROOT}/x s3cr3t-value".encode()], inner=_Secret) == b"/workspace/x ***"


def test_host_runtime_proc_output_reports_canonical_paths(tmp_path):
    """A real host-runtime proc started in ``/workspace/target`` prints the
    sandbox path, not the node's real one (tmp_path also exercises the
    ``/var`` → ``/private/var`` realpath variant on macOS)."""
    reg = ProcRegistry(tmp_path, "run-x", runtime="host")
    run_root = tmp_path / "runs" / "run-x"
    (run_root / "target").mkdir(parents=True)
    ps = reg.spawn(ProcSpec(
        argv=[sys.executable, "-c", "import os; print(os.getcwd()); print(os.path.abspath('calc.py'))"],
        env_keys=[], cwd="/workspace/target", stdin="", timeout_s=30,
        env={"PATH": os.environ.get("PATH", "")}))
    deadline = time.time() + 30
    while reg._procs[ps.pid].state == "running" and time.time() < deadline:
        time.sleep(0.05)
    out = (run_root / ".procs" / f"{ps.pid}.out").read_text()
    assert out.splitlines() == ["/workspace/target", "/workspace/target/calc.py"], out
    assert str(tmp_path) not in out and os.path.realpath(tmp_path) not in out
