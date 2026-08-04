"""Process-contract tests for the dispatch spawn seam (Phase A.1).

``mini_ork.dispatch.core.dispatch`` spawns *every* harness lane, so its process
contract is where the SE-3 incident's Layer 1 (a `/dev/tty` prompt blocking a
headless run) has to be closed once, structurally, rather than by every caller
remembering ``MINI_ORK_NONINTERACTIVE=1``. Two guarantees are pinned here:

1. **No controlling terminal.** The child is detached into its own session
   (``start_new_session=True``), so neither the harness CLI nor any code it runs
   can open ``/dev/tty``. Asserted env-independently: the child reports
   ``os.getsid(0) == os.getpid()`` (it *is* the session leader) and that opening
   ``/dev/tty`` raises. (This CI/host has no controlling tty either, so the leak
   can't be reproduced positively — the session-leader assertion is the durable
   proof of severance regardless.)

2. **Timeout reaps the whole group.** A hung harness that spawns a grandchild
   must not orphan it: a timeout SIGKILLs the detached process *group*, not just
   the direct child. Proven by a grandchild that keeps touching a sentinel file
   — after the timeout its mtime must stop advancing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from mini_ork.dispatch.core import dispatch
from mini_ork.dispatch.models import DispatchRequest


def test_child_is_detached_session_leader_without_controlling_tty() -> None:
    prog = (
        "import os, sys\n"
        "sid, pid = os.getsid(0), os.getpid()\n"
        "try:\n"
        "    fd = os.open('/dev/tty', os.O_RDONLY); os.close(fd); tty = 'OPEN'\n"
        "except OSError:\n"
        "    tty = 'NOTTY'\n"
        "sys.stdout.write(f'{sid == pid}:{tty}')\n"
    )
    res = dispatch(
        DispatchRequest(model="test", prompt="", timeout_s=30),
        [sys.executable, "-c", prog],
    )
    assert res.ok, res.error
    leader, tty = res.text.split(":")
    assert leader == "True"  # child is its own session leader → terminal severed
    assert tty == "NOTTY"  # and /dev/tty is unreachable from it


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    sentinel = tmp_path / "alive"
    grandchild = tmp_path / "gc.py"
    parent = tmp_path / "parent.py"
    grandchild.write_text(
        "import os, time\n"
        "s = os.environ['SENTINEL']\n"
        "while True:\n"
        "    open(s, 'w').write(str(time.time()))\n"
        "    time.sleep(0.1)\n"
    )
    # The parent (dispatch's direct child) spawns the grandchild in the SAME
    # session/group, then sleeps far past the timeout. If only the direct child
    # were killed, the grandchild would keep touching the sentinel.
    parent.write_text(
        "import os, sys, subprocess, time\n"
        "subprocess.Popen([sys.executable, os.environ['GC']])\n"
        "time.sleep(60)\n"
    )
    res = dispatch(
        DispatchRequest(
            model="test",
            prompt="",
            timeout_s=1.0,
            env={"GC": str(grandchild), "SENTINEL": str(sentinel)},
        ),
        [sys.executable, str(parent)],
    )
    assert not res.ok and res.rc == 124  # the parent timed out

    # Let any survivor prove itself, then confirm the grandchild is dead:
    # a live grandchild would keep advancing the sentinel's mtime.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not sentinel.exists():
        time.sleep(0.05)
    m1 = sentinel.stat().st_mtime if sentinel.exists() else 0.0
    time.sleep(0.8)
    m2 = sentinel.stat().st_mtime if sentinel.exists() else 0.0
    assert m1 == m2, "grandchild survived the group-kill (sentinel mtime still advancing)"
