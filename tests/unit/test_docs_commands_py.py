"""Every command the docs cite must exist, and every doc link must resolve.

The extension guide told readers for months that there was "no top-level
``mini-ork validate`` command yet" while ``mini-ork validate --recipe <name>``
had been shipping. That is the failure this pins: a doc is read by people who
cannot tell a stale sentence from a true one, so the sentence has to be checked
by something that can.

Two checks, both cheap and both about the *entry points* a reader acts on:

* every ``mini-ork <verb>`` in the pages that teach the CLI names a registered
  subcommand;
* every relative link in ``docs/README.md`` — the index a new reader starts
  from — points at a file that exists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli.main import SUBCOMMAND_REGISTRY  # noqa: E402

#: The pages that teach the command surface. A doc naming a verb here has to be
#: right; a prose mention elsewhere may legitimately name a hypothetical.
CLI_PAGES = (
    REPO / "docs" / "README.md",
    REPO / "docs" / "EXTENSION.md",
    REPO / "docs" / "UI.md",
    REPO / "docs" / "LEARNING-LOOP-LIFECYCLE.md",
)

#: ``mini-ork <verb>`` as a reader would *type* it: inside a code span, or at
#: the start of a shell line. Prose like "every node mini-ork dispatches" is
#: English, not a command, and a looser regex flags it as one.
_CODE_SPAN_RE = re.compile(r"`mini-ork ([a-z][a-z0-9-]{1,24})\b")
_SHELL_LINE_RE = re.compile(r"(?m)^\s*\$?\s*mini-ork ([a-z][a-z0-9-]{1,24})\b")

#: Words that are English rather than a subcommand. A line may legitimately
#: begin "mini-ork has 4 canonical extension points", so a blocklist of the
#: sentence-openers is what keeps the code-span and shell-line regexes honest.
_NOT_A_VERB = {
    "version", "v0", "help", "has", "is", "was", "will", "does", "can",
    "runs", "ships", "keeps", "and", "or", "to", "the", "its", "io", "home",
}


def _verbs(text: str) -> set[str]:
    found = set(_CODE_SPAN_RE.findall(text)) | set(_SHELL_LINE_RE.findall(text))
    return {v for v in found if v not in _NOT_A_VERB}


def test_every_cited_command_exists():
    unknown: dict[str, list[str]] = {}
    for page in CLI_PAGES:
        if not page.is_file():
            continue
        for verb in _verbs(page.read_text(encoding="utf-8")):
            if verb not in SUBCOMMAND_REGISTRY:
                unknown.setdefault(str(page.relative_to(REPO)), []).append(verb)
    assert not unknown, unknown


def test_the_extension_guide_no_longer_denies_validate():
    text = (REPO / "docs" / "EXTENSION.md").read_text(encoding="utf-8")
    assert "There is no top-level `mini-ork validate`" not in text
    assert "mini-ork validate" in text


def test_every_link_in_the_docs_index_resolves():
    index = REPO / "docs" / "README.md"
    text = index.read_text(encoding="utf-8")
    broken: list[str] = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (index.parent / path).exists():
            broken.append(target)
    assert not broken, broken


def test_every_recipe_declares_a_task_class():
    """A recipe that names no task class cannot be picked by the classifier.

    Two spellings are in use: most recipes ship a ``task_class.yaml``, while a
    few (``harness-bridge``) declare ``task_class:`` at the top of
    ``workflow.yaml``. Both are loadable; a recipe with neither is not.
    """
    missing: list[str] = []
    for d in sorted((REPO / "recipes").iterdir()):
        if not d.is_dir():
            continue
        if (d / "task_class.yaml").is_file():
            continue
        workflow = d / "workflow.yaml"
        if workflow.is_file() and re.search(r"(?m)^task_class:\s*\S", workflow.read_text(encoding="utf-8")):
            continue
        missing.append(d.name)
    assert not missing, missing
