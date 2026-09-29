"""Runtime image builder for ``mini-ork certify``.

Slice C2's CLI (``mini_ork.cli.certify``) consumes this module to (re)build the
Docker image the oracle's Crucible runs against. The contract is:

    is_python_project(repo) -> bool           # detection
    image_tag(repo, base_sha) -> str          # tag naming
    render_dockerfile(workdir, req_files, installable) -> str   # PURE — tests assert on this
    build_image(repo, base_sha, ...) -> str   # returns tag, raises on failure

`render_dockerfile` is intentionally a pure function. The CLI is hermetic under
unit tests only when the Dockerfile text is inspectable without invoking docker;
this module's tests assert on its rendered output.

`build_image` is the only function that touches `docker`/`tar`/`git` on the host.
Every code path is narrow enough to monkeypatch.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence


def is_python_project(repo: Path) -> bool:
    """True if ``repo`` has any of: pyproject.toml, setup.py, requirements*.txt.

    The C2 v1 spec certifies Python repos only; non-Python repos are an
    UNVERIFIED exit unless the caller passed ``--image``.
    """
    repo = Path(repo)
    if (repo / "pyproject.toml").is_file():
        return True
    if (repo / "setup.py").is_file():
        return True
    # requirements*.txt — sorted glob keeps the install order deterministic.
    if any(repo.glob("requirements*.txt")):
        return True
    return False


def find_requirements(repo: Path) -> list[Path]:
    """Sorted requirements*.txt paths, basenames only (they live at /testbed)."""
    return sorted(repo.glob("requirements*.txt"))


def image_tag(repo: Path, base_sha: str) -> str:
    """The image tag for ``repo`` at ``base_sha``. Includes the short sha so a new
    base always forces a rebuild even if the rest of the inputs match."""
    return f"mini-ork-certify/{Path(repo).resolve().name}:{base_sha[:12]}"


def is_installable(repo: Path) -> bool:
    """True if ``pip install -e .`` has something to install (pyproject or setup.py).

    A requirements-only project is certifiable but not installable; running the
    editable install on it would fail the whole image build.
    """
    repo = Path(repo)
    return (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file()


def render_dockerfile(workdir: str, req_files: Sequence[Path], installable: bool = True) -> str:
    """Render the Dockerfile sent to ``docker build``. Pure so tests can assert.

    Order matters:
      1. ``COPY`` the ``git archive`` of the base sha to ``workdir``.
      2. Install: pytest, every ``requirements*.txt``, then the editable install
         (``.[test]`` → ``.[tests]`` → ``.``) when the project is installable.
      3. Only THEN ``git init`` + commit. Crucible resets with ``git checkout -- .``
         and ``git clean -fd`` before every test; committing after the install makes
         build output (``*.egg-info``, generated files) part of the baseline, so the
         reset cannot delete it and silently break the editable install mid-run.
    """
    lines: list[str] = [
        "FROM python:3.11-slim",
        # git is needed because Crucible resets the tree with `git checkout -- .`
        # and applies the patch with `git apply`.
        "RUN apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*",
        f"WORKDIR {workdir}",
        f"COPY . {workdir}/",
        "RUN pip install --quiet pytest",
    ]
    for rf in req_files:
        lines.append(f"RUN pip install --quiet -r {Path(rf).name}")
    if installable:
        # Some projects ship [test], others [tests], others none; bare `.` is the fallback.
        lines.append(
            "RUN pip install --quiet -e '.[test]' "
            "|| pip install --quiet -e '.[tests]' "
            "|| pip install --quiet -e ."
        )
    lines.append(
        "RUN git init -q && git config user.email certify@local "
        "&& git config user.name certify && git add -A "
        "&& git commit -q -m base"
    )
    return "\n".join(lines) + "\n"


def _image_exists(tag: str) -> bool:
    """True if the local docker daemon has ``tag``. Skips a redundant rebuild."""
    r = subprocess.run(
        ["docker", "inspect", "--type=image", tag],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def build_image(
    repo: Path,
    base_sha: str,
    *,
    workdir: str = "/testbed",
    docker_timeout: int = 600,
) -> str:
    """Build (or reuse) the runtime image. Returns the tag.

    Raises RuntimeError("docker not on PATH") when docker is missing — the CLI
    surfaces that as UNVERIFIED "runtime unavailable" with exit 2.
    """
    if not shutil.which("docker"):
        raise RuntimeError("docker not on PATH")
    repo = Path(repo).resolve()
    tag = image_tag(repo, base_sha)
    if _image_exists(tag):
        return tag
    req_files = find_requirements(repo)
    with tempfile.TemporaryDirectory() as ctx_str:
        ctx = Path(ctx_str)
        # Materialise the base sha into the build context. `git archive` is
        # deterministic, narrow (no .git, no untracked), and matches what an
        # installer would see after a clone.
        with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
            subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar",
                 "-o", tar.name, base_sha],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["tar", "-xf", tar.name, "-C", str(ctx)],
                check=True, capture_output=True,
            )
        # Defensive: if a requirements file is git-ignored but still required,
        # copy it explicitly so COPY . /testbed/ doesn't drop it.
        for rf in req_files:
            target = ctx / rf.name
            if not target.exists():
                target.write_bytes((repo / rf.name).read_bytes())
        (ctx / "Dockerfile").write_text(
            render_dockerfile(workdir, req_files, installable=is_installable(repo)))
        r = subprocess.run(
            ["docker", "build", "-t", tag, str(ctx)],
            capture_output=True, text=True, timeout=docker_timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"docker build failed (rc={r.returncode}): {r.stderr.strip()[-400:]}"
            )
    return tag


__all__ = [
    "is_python_project", "is_installable", "find_requirements", "image_tag",
    "render_dockerfile", "build_image",
]
