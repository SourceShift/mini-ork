#!/usr/bin/env python3
"""post-mvp-delivery consumer for the `api_contract` behavioral verifier.

The shared seed `verifiers/api_contract.py` (over `mini_ork.verify.behavioral`)
was a producer with no consumer — nothing referenced it, so its live API probe
never ran in a run. This recipe-local dispatcher is the consumer: the recipe's
`artifact_contract.yaml` lists `verifiers/api_contract.py` in `success_verifiers`,
and recipe-local verifier resolution (`mini_ork/cli/verify.py::_find_verifier_script`
searches `recipes/<recipe>/verifiers` before `<root>/verifiers`) picks THIS file.

Two seams are bridged here, both required for the wiring to be a real check
rather than a silent no-op:

1. Observable default. `mini_ork.verify.behavioral` only probes when an
   observable is declared (`MO_OBSERVABLE_SPEC` or the `MO_BEHAV_*` vars).
   Without one it abstains, so the wiring would be dead. We default
   `MO_OBSERVABLE_SPEC` to the recipe-local descriptor next to this file so a
   run probes the delivery staging surface; an operator-supplied value wins.

2. Abstain contract. The behavioral engine prints a multi-line verdict envelope
   and exits non-zero on UNVERIFIED ("abstain must not green a run").
   `mini_ork/cli/verify.py` reads exit-1 as a plain FAILURE and only recognises a
   single-line vacuous envelope (`"verdict": "vacuous"` or `"pass": null`) as
   *unmeasured*. An undeclared or unreachable staging surface must therefore not
   hard-fail every post-mvp-delivery run, nor be laundered into a pass: an
   UNVERIFIED verdict is re-emitted as the dispatcher's vacuous envelope
   (`pass: null`), the same shape `recipes/code-fix/verifiers/metamorphic.py`
   uses for its no-spec case. PROVEN/REFUTED pass through unchanged.

Exit: 0 on PROVEN, 1 on REFUTED, 0 on UNVERIFIED (recorded as unmeasured).
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

_UNVERIFIED = "UNVERIFIED"


def _repo_root() -> str:
    """Nearest ancestor carrying the ``mini_ork`` package, so the shared engine
    imports when the dispatcher runs this file as a bare subprocess.

    ``realpath`` follows a project-overlay symlink back to the real checkout;
    if no ancestor carries the package we fall back to the structural root
    (``verifiers`` → ``<recipe>`` → ``recipes`` → repo root).
    """
    here = os.path.realpath(__file__)
    root = os.path.dirname(here)
    while root != os.path.dirname(root):
        if os.path.isdir(os.path.join(root, "mini_ork")):
            return root
        root = os.path.dirname(root)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


_ROOT = _repo_root()
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mini_ork.verify.behavioral import main as behavioral_main  # noqa: E402


def _bridge(stdout: str, rc: int) -> tuple[str, int]:
    """Map a behavioral verdict onto the dispatcher's pass / abstain contract.

    PROVEN and REFUTED are returned verbatim with the engine's exit code.
    UNVERIFIED is kept in the evidence for the record and followed by a
    single-line vacuous envelope that `mini_ork/cli/verify.py` records as
    unmeasured (`pass: null`), with exit 0 so an unprobeable surface neither
    greens nor reds the run.
    """
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or payload.get("status") != _UNVERIFIED:
        return stdout, rc
    note = str(payload.get("evidence") or "no observable declared / surface unreachable")
    envelope = json.dumps({
        "verifier": "api_contract",
        "verdict": "vacuous",
        "pass": None,
        "status": _UNVERIFIED,
        "note": note,
    })
    # The engine always terminates its verdict with a newline; guard anyway so
    # the envelope lands on its own line for verify.py's line-wise scan.
    sep = "" if not stdout or stdout.endswith("\n") else "\n"
    return stdout + sep + envelope + "\n", 0


def main() -> int:
    # Default the observable to the recipe-local descriptor; MO_OBSERVABLE_SPEC
    # (or MO_BEHAV_*) set by the operator still takes precedence.
    os.environ.setdefault(
        "MO_OBSERVABLE_SPEC",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_contract.observable.yaml"),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = behavioral_main()
    out, code = _bridge(buf.getvalue(), rc)
    sys.stdout.write(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
