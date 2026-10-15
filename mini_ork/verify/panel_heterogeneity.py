"""Did a judge panel actually run on more than one model family?

R7a of the learning-loop fix batch (``docs/plans/2026-10-15-learning-loop-fixes.md``,
decision Q6). An audit-judge panel is only as independent as its models: three
"lanes" served by one family are one judge wearing three names.

Two things hid that from the completeness gates:

* the lane-alias overlay (``opus_lens: glm``) is an operator cost choice and
  stays operator-owned — so the alias says nothing about the model behind it;
* a lane whose first choice fails falls through to the tail of the dispatch
  chain, silently re-pointing a judge at another lane's model.

Both were invisible to a comparison of raw lane strings, which is what the 4l
gate used to do: ``opus`` and ``opus55`` are two pins of one Anthropic family,
and run ``run-1791725633-31802`` (3l) had ``opus_lens`` served by glm and
``laguna_lens`` falling back to minimax six times — a "three family" panel that
was really glm + minimax twice — and it published anyway.

So this module resolves every served lane to the MODEL FAMILY it actually is
and reports the panel as heterogeneous or not. It reads ``llm_calls``
read-only; a missing DB, a missing table, or a run with no rows at all is
``ok=True`` with ``skipped`` set, so a hermetic test or a dry run (which have
no rows) can never be failed by it — but a run that DID have judge rows is
enforced.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Mechanism token → provider family. Checked in order, first substring hit
# wins (case-insensitively), so a model id like ``claude-opus-5-5`` or a bare
# lane name like ``opus`` lands in the same family. Anything unmatched is its
# own family — the lowercased string — which keeps an unknown lane distinct
# from every known one instead of silently collapsing it into one of them.
_FAMILY_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic", ("claude", "opus", "sonnet", "haiku", "fable")),
    ("zai", ("glm",)),
    ("minimax", ("minimax",)),
    ("deepseek", ("deepseek",)),
    ("moonshot", ("kimi", "moonshot")),
    ("openai", ("gpt", "codex", "o1", "o3", "o4")),
    ("google", ("gemini", "gemma")),
    ("poolside", ("laguna", "poolside")),
    ("alibaba", ("qwen",)),
)


def model_family(model: str) -> str:
    """The provider family a model id or lane name belongs to.

    ``claude``/``opus``/``sonnet``/``haiku``/``fable`` → ``anthropic``; ``glm``
    → ``zai``; ``minimax`` → ``minimax``; ``deepseek`` → ``deepseek``;
    ``kimi``/``moonshot`` → ``moonshot``; ``gpt``/``codex``/``o1``/``o3``/``o4``
    → ``openai``; ``gemini``/``gemma`` → ``google``; ``laguna``/``poolside`` →
    ``poolside``; ``qwen`` → ``alibaba``; anything else → the lowercased string.
    """
    text = str(model or "").strip().lower()
    for family, tokens in _FAMILY_TOKENS:
        if any(token in text for token in tokens):
            return family
    return text


def _registry() -> Mapping[str, Any]:
    """The lane registry, read through the existing loader.

    ``mini_ork/dispatch/providers.py`` owns registry resolution (the
    ``MINI_ORK_PROVIDERS`` override, then the operator home, then the repo
    copy); this module only consumes it and never mutates it. Deliberately not
    cached: the environment can point at a different registry between calls.
    """
    from mini_ork.dispatch.providers import _load_providers_registry

    return _load_providers_registry()


def lane_model(lane: str) -> str:
    """The model a lane resolves to, per the registry's own precedence.

    The registry entry's ``model:``; else its ``extra_env.ANTHROPIC_MODEL``;
    else, for an ``anthropic-native`` lane, ``claude-<lane>`` (the ambient-auth
    contract those lanes carry); else the lane name itself.
    """
    entry = _registry().get(lane)
    if isinstance(entry, Mapping):
        model = str(entry.get("model") or "").strip()
        if model:
            return model
        extra = entry.get("extra_env") or {}
        if isinstance(extra, Mapping):
            from_env = str(extra.get("ANTHROPIC_MODEL") or "").strip()
            if from_env:
                return from_env
        if entry.get("kind") == "anthropic-native":
            return f"claude-{lane}"
    return lane


def lane_family(lane: str) -> str:
    """The family a recorded ``llm_calls.model_id`` lane actually served."""
    return model_family(lane_model(lane))


def default_db_path() -> str:
    """The state DB the check reads when no ``db=`` is given.

    ``$MINI_ORK_DB`` wins, else ``$MINI_ORK_HOME/state.db`` (``.mini-ork``
    when the home is unset) — the same precedence the panel gates used inline.
    """
    explicit = os.environ.get("MINI_ORK_DB")
    if explicit:
        return explicit
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _fetch(db: str, run_id: str, aliases: list[str]) -> tuple[list[tuple] | None, str]:
    """``(rows, skipped_reason)`` — the run's judge calls, or why none were read.

    Opened read-only (``file:…?mode=ro``) so a missing DB is an error rather
    than a newly created empty file. Rows are ``(actor, model_id, status)``.
    """
    if not os.path.isfile(db):
        return None, f"no database at {db}"
    try:
        con = sqlite3.connect(
            Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
        )
        try:
            # "the run has rows but none for this alias" (→ no_calls) is a
            # different verdict from "this run logged nothing at all" (→ skip),
            # so ask about the run before asking about the aliases.
            if con.execute(
                "SELECT 1 FROM llm_calls WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone() is None:
                return None, f"no llm_calls rows for run {run_id}"
            if not aliases:
                return [], None
            marks = ",".join("?" * len(aliases))
            rows = con.execute(
                "SELECT actor, model_id, status FROM llm_calls "
                f"WHERE run_id = ? AND actor IN ({marks})",
                [run_id, *aliases],
            ).fetchall()
            return rows, ""
        finally:
            con.close()
    except sqlite3.Error as exc:
        return None, f"llm_calls unreadable: {exc}"


def check_panel(
    run_id: str,
    aliases: Iterable[str],
    *,
    db: str | None = None,
    expect: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Grade the family diversity of the panel that served ``run_id``.

    Returns ``{"ok", "problems", "served", "families", "skipped"}``:

    ``served``    alias → sorted lanes (``llm_calls.model_id``) whose call
                  succeeded;
    ``families``  alias → sorted families those lanes resolve to;
    ``problems``  ``same_family:<a,b,…>:<family>`` when two or more aliases
                  were served by one family, ``fallback:<alias>:served=<lane>``
                  when a FAILED call was followed by a success on a different
                  lane, ``expect:<alias>:served=<lane>`` when ``expect``
                  (alias → allowed lanes) was given and a served lane is not in
                  it, ``no_calls:<alias>`` when the run logged calls but none
                  for that alias;
    ``skipped``   a reason string — and ``ok=True`` with no problems — when the
                  DB, the table, or every row for the run is missing.

    Reads ``llm_calls`` read-only for ``run_id`` and the given actor aliases.
    """
    aliases = [str(alias) for alias in (aliases or [])]
    outcome: dict[str, Any] = {
        "ok": True,
        "problems": [],
        "served": {},
        "families": {},
        "skipped": None,
    }
    rows, skipped = _fetch(db or default_db_path(), run_id, aliases)
    if rows is None:
        outcome["skipped"] = skipped
        return outcome

    served: dict[str, set[str]] = {}
    failed: dict[str, set[str]] = {}
    for actor, model, status in rows:
        bucket = served if str(status) == "success" else failed
        bucket.setdefault(str(actor), set()).add(str(model))

    problems: list[str] = []
    for alias in aliases:
        if alias not in served and alias not in failed:
            problems.append(f"no_calls:{alias}")
    for alias in aliases:
        lanes = sorted(served.get(alias, ()))
        # A failed call is only a fallback when a DIFFERENT lane then served
        # it: a retry on the same lane is that lane working, not a tail lane
        # re-pointing the judge at another model.
        tail = [lane for lane in lanes if lane not in failed.get(alias, ())]
        if failed.get(alias) and tail:
            problems.append(f"fallback:{alias}:served={','.join(tail)}")
    if expect:
        for alias in aliases:
            allowed = expect.get(alias)
            if allowed is None:
                continue
            allowed = {str(lane) for lane in allowed}
            for lane in sorted(served.get(alias, ())):
                if lane not in allowed:
                    problems.append(f"expect:{alias}:served={lane}")
    by_family: dict[str, list[str]] = {}
    for alias in aliases:
        for family in sorted({lane_family(l) for l in served.get(alias, ())}):
            by_family.setdefault(family, []).append(alias)
    for family, members in by_family.items():
        if len(members) > 1:
            problems.append(f"same_family:{','.join(members)}:{family}")

    present = [alias for alias in aliases if alias in served or alias in failed]
    outcome.update(
        ok=not problems,
        problems=problems,
        served={alias: sorted(served.get(alias, ())) for alias in present},
        families={
            alias: sorted({lane_family(l) for l in served.get(alias, ())})
            for alias in present
        },
    )
    return outcome
