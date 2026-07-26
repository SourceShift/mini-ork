"""Crucible engine — verified execution of a candidate change in an isolated runtime.

Crucible answers exactly one question: **what did the code actually do?** That answer,
not a model's opinion of the code, is what mini-ork's verdict is anchored on.

Execution is delegated to the MIT-licensed `verifiers` runtime layer (Prime Intellect),
which we use for the one thing it is genuinely best at: a single runtime protocol with
interchangeable local and cloud backends (docker / subprocess / prime / modal). We do not
use its scoring layer, and that is deliberate:

    verifiers  -> scores rollouts with rubric/judge Criteria (an LLM judge; hackable)
    Crucible   -> scores on EXECUTION STATUS; a judge may only veto, never approve

`verifiers` is an OPTIONAL dependency (`pip install mini-ork[runtime]`). Without it,
Crucible drives the `docker` CLI directly and behaves identically. Callers never see the
difference: they get an ExecOutcome either way.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field

__all__ = ["Crucible", "ExecOutcome", "RuntimeSpec", "available_backends"]

# Backends we can execute in. "docker-cli" is ours (zero-dep); the rest come from the
# `verifiers` runtime protocol and are what make cloud execution a config change rather
# than a rewrite.
_VF_BACKENDS = ("docker", "subprocess", "prime", "modal")


@dataclass(frozen=True)
class RuntimeSpec:
    """Where a candidate gets executed.

    `backend="auto"` prefers the verifiers docker runtime and falls back to the docker
    CLI when verifiers is not installed. Set it explicitly to "prime" or "modal" to run
    the very same probe in the cloud — that is the whole point of going through their
    protocol instead of shelling out.
    """

    image: str
    workdir: str = "/testbed"
    backend: str = "auto"
    cpu: float | None = None
    memory: float | None = None
    timeout_s: int = 300
    # SWE-bench images ship without a test runner.
    ensure_pytest: bool = True


@dataclass
class ExecOutcome:
    """What the code ACTUALLY did. This — not a model's opinion — is the anchor.

    `status` is the primary signal (see PR #168, anti-Goodhart reward chain):
        passed       the test ran and succeeded
        failed       the test ran and an ASSERTION failed — a real reproduction
        test_defect  the TEST is broken (NameError, bad import, syntax) — UNINFORMATIVE
        error        the test could not be collected at all — UNINFORMATIVE
        apply_fail   the candidate patch did not apply
        no_run       the runtime never started

    Only `passed` and `failed` are evidence ABOUT THE PATCH. The rest are facts about the
    harness, and a patch must never be scored on them.
    """

    status: str
    ran: bool = False
    returncode: int | None = None
    output: str = ""
    backend: str = ""
    exc: str = ""      # the exception that caused the failure, e.g. "AssertionError"
    meta: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def informative(self) -> bool:
        """False when the outcome says nothing about the patch.

        A patch must never be blamed for an environment it did not break (PR #170), nor for
        a test that was never valid in the first place.
        """
        return self.status in ("passed", "failed")

    @property
    def test_is_broken(self) -> bool:
        """The test itself is defective and must be REPAIRED, not believed.

        This is the difference between "the bug is still there" and "my test has a typo",
        and collapsing them is how a correct patch gets rejected.
        """
        return self.status == "test_defect"


def _have_verifiers() -> bool:
    try:
        from verifiers.v1 import runtimes  # noqa: F401

        return True
    except Exception:
        return False


def available_backends() -> tuple[str, ...]:
    """What this host can actually execute in, right now."""
    out: list[str] = []
    if _have_verifiers():
        out.extend(_VF_BACKENDS)
    if shutil.which("docker"):
        out.append("docker-cli")
    return tuple(out)


class Crucible:
    """Run a candidate patch + a test inside an isolated runtime, and report what
    actually happened.

        with Crucible(RuntimeSpec(image="swebench/sweb.eval.x86_64.foo:latest")) as c:
            base = c.run_test(test_src)                    # reproduces?  -> failed
            cand = c.run_test(test_src, patch=candidate)   # fixed?       -> passed

    The same code runs in the cloud by changing one field:

            RuntimeSpec(image=..., backend="prime")
    """

    def __init__(self, spec: RuntimeSpec, docker_host: str | None = None) -> None:
        self.spec = spec
        self.env = dict(os.environ)
        if docker_host:
            self.env["DOCKER_HOST"] = docker_host

        self.backend = self._resolve_backend(spec.backend)
        self._rt = None          # a verifiers Runtime, when we are on that path
        self._cid: str | None = None  # a container id, when we are on the CLI path
        self._loop = None        # the verifiers Runtime protocol is async; we are not

    @staticmethod
    def _resolve_backend(want: str) -> str:
        if want == "auto":
            return "docker" if _have_verifiers() else "docker-cli"
        if want in _VF_BACKENDS and not _have_verifiers():
            raise RuntimeError(
                f"backend={want!r} needs the verifiers runtime layer: pip install 'mini-ork[runtime]'"
            )
        if want != "docker-cli" and want not in _VF_BACKENDS:
            raise ValueError(f"unknown backend {want!r}; expected one of {available_backends()}")
        return want

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self) -> "Crucible":
        if self.backend == "docker-cli":
            self._start_docker_cli()
        else:
            self._start_verifiers()
        if self.up and self.spec.ensure_pytest:
            self._exec("python -m pytest --version >/dev/null 2>&1 || pip install -q pytest >/dev/null 2>&1")
        return self

    def __exit__(self, *_exc) -> None:
        if self._rt is not None:
            for step in ("stop", "teardown"):
                try:
                    self._await(getattr(self._rt, step)())
                except Exception:  # a torn-down runtime must never mask the real result
                    pass
            try:
                self._rt.cleanup()  # the one sync method in their protocol
            except Exception:
                pass
            self._rt = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if self._cid:
            self._docker(["rm", "-f", self._cid])
            self._cid = None

    # The verifiers Runtime protocol is async (start/run/read/stop/teardown are all
    # coroutines; only cleanup is sync). Crucible's callers are ordinary synchronous
    # code, so we own a private event loop and drive their runtime from it. Calling a
    # coroutine without awaiting it is a SILENT NO-OP — the container would never start
    # and every probe would look like a clean `no_run`.
    def _await(self, coro):
        import asyncio

        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _runtime_config(self):
        from verifiers.v1 import runtimes as R

        if self.backend == "subprocess":
            # runs on the host; takes no image/workdir
            return R.SubprocessConfig(type="subprocess")
        kw: dict[str, object] = {
            "type": self.backend, "image": self.spec.image, "workdir": self.spec.workdir,
        }
        if self.spec.cpu is not None:
            kw["cpu"] = self.spec.cpu
        if self.spec.memory is not None:
            kw["memory"] = self.spec.memory
        Cfg = {"docker": R.DockerConfig, "prime": R.PrimeConfig, "modal": R.ModalConfig}[self.backend]
        return Cfg(**kw)

    def _start_verifiers(self) -> None:
        from verifiers.v1.runtimes import make_runtime

        try:
            self._rt = make_runtime(self._runtime_config(), name=f"crucible-{uuid.uuid4().hex[:8]}")
            self._await(self._rt.start())
        except Exception:
            self._rt = None

    def _start_docker_cli(self) -> None:
        self._cid = f"crucible-{uuid.uuid4().hex[:8]}"
        r = self._docker(["run", "-d", "--name", self._cid, self.spec.image, "sleep", "3600"])
        if r.returncode != 0:
            self._cid = None

    @property
    def up(self) -> bool:
        return self._rt is not None or self._cid is not None

    # ── the primitive ────────────────────────────────────────────────────────
    def run_test(self, test_src: str, patch: str = "") -> ExecOutcome:
        """Reset the tree, optionally apply `patch`, run `test_src`, reset again.

        The returned status distinguishes a genuine assertion FAILURE (a real
        reproduction) from an ERROR (a broken environment) — the distinction the old
        absolute-green gate collapsed, and got wrong.
        """
        if not self.up:
            return ExecOutcome(status="no_run", output="runtime did not start", backend=self.backend)

        wd = self.spec.workdir
        tf = self._put(test_src)
        pf = self._put(patch) if patch.strip() else ""

        script = f"cd {wd} && git checkout -q -- . 2>/dev/null; git clean -qfd 2>/dev/null; "
        if pf:
            script += f"git apply {pf} 2>/tmp/ae || {{ echo CRUCIBLE_APPLY_FAIL; exit 7; }}; "
        script += (
            f"cp {tf} {wd}/crucible_probe.py; "
            "python -m pytest crucible_probe.py -q -p no:cacheprovider 2>&1; echo CRUCIBLE_RC=$?; "
            f"git checkout -q -- . 2>/dev/null; rm -f {wd}/crucible_probe.py"
        )
        _rc, out = self._exec(script)
        return self._classify(out, backend=self.backend)

    # ── verdict: execution-anchored, never judge-approved ────────────────────

    # Exceptions that mean THE TEST IS BROKEN, not the code under test.
    #
    # A test that dies on an undefined name or a bad import never exercised the code at
    # all — believing it is how you reject a correct patch. (Measured: a generated probe
    # used `exp_polar` without importing it; pytest printed "FAILED ... - NameError", the
    # word "failed" was matched, and the oracle concluded "the bug is NOT fixed" about a
    # patch that fixed it perfectly.)
    #
    # Kept deliberately narrow. AttributeError and TypeError are NOT here: a library bug
    # can legitimately raise either, and a probe that catches one may be a true
    # reproduction. We only claim the cases where the test is unambiguously at fault.
    _TEST_DEFECT = re.compile(
        r"\b(NameError|ImportError|ModuleNotFoundError|SyntaxError|IndentationError"
        r"|fixture '[^']+' not found)\b"
    )

    # The exception that actually caused the failure. Callers need this to ask the question
    # the status alone cannot answer: *did the test fail for the reason it was written for?*
    # A probe that asserts an expected value should fail with AssertionError. If it dies on
    # a ValueError raised deep in the library's own setup, it never reached its assertion —
    # it is red, but it is not a reproduction.
    _EXC = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning))\b(?=\s*:)")

    @staticmethod
    def _exc_of(out: str) -> str:
        """The exception pytest attributed the failure to. Prefer its own summary line.

        pytest renders a BARE `assert x == y` as `FAILED t.py::t - assert 3 == 5`, not as
        `AssertionError` — the word "assert" sits exactly where the exception name goes.
        Reading that literally makes a genuine assertion failure look like an unknown
        exception, and a caller that keys on AssertionError will discard a perfectly good
        reproduction. Normalise it.
        """
        m = re.search(r"^FAILED .*? - ([A-Za-z_][\w.]*)", out, re.M)
        if m:
            name = m.group(1).rsplit(".", 1)[-1]
            return "AssertionError" if name == "assert" else name
        hits = Crucible._EXC.findall(out)
        return hits[-1] if hits else ""

    @staticmethod
    def _classify(out: str, backend: str = "") -> ExecOutcome:
        if "CRUCIBLE_APPLY_FAIL" in out:
            return ExecOutcome(status="apply_fail", ran=False, output=out[-800:], backend=backend)
        m = re.search(r"CRUCIBLE_RC=(\d+)", out)
        rc = int(m.group(1)) if m else None
        if rc == 0:
            return ExecOutcome(status="passed", ran=True, returncode=0, output=out[-800:], backend=backend)

        exc = Crucible._exc_of(out)

        # The test is broken as CODE. Repair it; do not read it as a verdict on the patch.
        if Crucible._TEST_DEFECT.search(out):
            return ExecOutcome(status="test_defect", ran=False, returncode=rc,
                               output=out[-800:], backend=backend, exc=exc)

        low = out.lower()
        # An ERROR (collection) is not a reproduction and not the patch's fault either.
        if ("error" in low and "failed" not in low) or "interrupted" in low:
            return ExecOutcome(status="error", ran=False, returncode=rc,
                               output=out[-800:], backend=backend, exc=exc)
        if rc is None:
            return ExecOutcome(status="no_run", ran=False, output=out[-800:], backend=backend, exc=exc)
        return ExecOutcome(status="failed", ran=True, returncode=rc,
                           output=out[-800:], backend=backend, exc=exc)

    # ── plumbing: one _exec, two backends ────────────────────────────────────
    def _exec(self, script: str) -> tuple[int | None, str]:
        """Run a shell script in the runtime. Returns (exit_code, combined output)."""
        if self._rt is not None:
            try:
                r = self._await(self._rt.run(["bash", "-lc", script], {}))
                return r.exit_code, (r.stdout or "") + (r.stderr or "")
            except Exception as e:  # a cloud runtime can drop mid-flight
                return None, f"runtime error: {e}"
        try:
            r = self._docker(["exec", str(self._cid), "bash", "-lc", script])
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "timeout"

    def _docker(self, args: list[str], timeout: int | None = None):
        return subprocess.run(
            ["docker", *args], env=self.env, capture_output=True, text=True,
            timeout=timeout or self.spec.timeout_s,
        )

    def _put(self, content: str) -> str:
        """base64 in — a diff or a test must survive the shell untouched."""
        path = f"/tmp/crucible_{uuid.uuid4().hex[:8]}"
        b64 = base64.b64encode(content.encode()).decode()
        self._exec(f"echo {b64} | base64 -d > {path}")
        return path


