"""Native profile-question answerer.

This module is the sole runtime owner of the former ``mo_answer_profile_questions``
contract: argument validation, deterministic prompt construction, native LLM
dispatch, tolerant JSON recovery, completeness checks, and JSON persistence.
The provider path makes two Kimi attempts, preserving the latest supported
pre-retirement behavior for failure or whitespace responses.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "build_prompt",
    "parse_and_persist",
    "answer_profile_questions",
    "_strip_code_fences",
    "_extract_balanced_json_object",
    "_question_text",
]


# Case-sensitive except for the explicit `JSON` branch. Mirroring bash's
# alternation order and flags is load-bearing — `re.I` would over-strip
# 'JSON' substrings inside prose.
_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_code_fences(raw: str) -> str:
    """Strip optional provider-added Markdown fences.

    Strips optional ```json / ```JSON / ``` openers and ``` closers. Most
    instruction-tuned LLMs add fences for any structured payload even when
    the prompt forbids them.
    """
    return _FENCE_RE.sub("", raw).strip()


def _extract_balanced_json_object(text: str) -> str:
    """Extract the first balanced JSON object from provider prose.

    Finds the FIRST `{` then walks forward tracking depth, skipping chars
    inside strings (with backslash escape handling). Returns the substring
    from start through the matching close brace. Returns the original text
    on failure — the caller raises on JSONDecodeError if extraction also
    fails.

    The returned slice includes the closing brace.
    """
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def _question_text(item) -> str:
    """Normalize supported question values to their contract text.

    str → return as-is; dict → return .text or .question or str(dict);
    anything else → str(item).
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text") or item.get("question") or item)
    return str(item)


def build_prompt(kickoff_path: str | os.PathLike, questions_json: str) -> str:
    """Build the deterministic profile-answer prompt.

    Reads the kickoff file as UTF-8, slices to [:20000] chars (matches
    the historic `kickoff[:20000]` cap), parses `questions_json` with a graceful
    fallback to [] on JSONDecodeError, then renders the fixed prompt
    template with `json.dumps(questions, ensure_ascii=True)`.
    """
    kickoff = Path(kickoff_path).read_text(encoding="utf-8")[:20000]
    try:
        questions = json.loads(questions_json or "[]")
    except json.JSONDecodeError:
        questions = []

    return (
        "You answer mini-ork run profile questions for autonomous child runs.\n"
        "\n"
        'Return ONLY a strict JSON object with this exact shape:\n'
        '{"answers":[{"question":"...","answer":"..."}],"auto_answered":true}\n'
        "\n"
        "Rules:\n"
        "- Answer every question using the kickoff content.\n"
        "- Keep answers concise and operational.\n"
        "- Do not include markdown, prose, code fences, or extra keys.\n"
        "\n"
        "Kickoff:\n"
        + kickoff
        + "\n"
        "\n"
        "Questions JSON:\n"
        + json.dumps(questions, ensure_ascii=True)
        + "\n"
    )


def parse_and_persist(
    raw: str,
    questions_json: str,
    out_path: str | os.PathLike,
) -> dict:
    """Normalize, validate, and persist a provider response.

    Args:
        raw: raw LLM text (already the dispatch return value; will be stripped).
        questions_json: the JSON-stringified questions list (matches the bash
            contract — bash passes the same string the caller supplied).
        out_path: file to write the validated answers JSON to. Parent
            directory is created.

    Returns:
        The dict that was written to disk (`{"answers": [...], "auto_answered": True}`).

    Raises:
        SystemExit-equivalent RuntimeError if the LLM output is non-JSON
            even after fence-strip + balanced-extraction (bash's
            "profile answerer returned non-json output: ..." path).
        RuntimeError if the LLM omitted one or more input questions
            (bash's "profile answerer omitted question: ..." path).
    """
    raw = raw.strip()

    # Strip optional markdown code fences.
    raw = _strip_code_fences(raw)

    # Parse; on failure, fall back to extracting the first balanced {...}.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        salvaged = _extract_balanced_json_object(raw)
        try:
            parsed = json.loads(salvaged)
        except json.JSONDecodeError as exc:
            snippet = raw[:200].replace("\n", "\\n")
            raise RuntimeError(
                f"profile answerer returned non-json output: {exc} | "
                f"raw_first_200={snippet!r}"
            )

    try:
        questions = json.loads(questions_json or "[]")
    except json.JSONDecodeError:
        questions = []

    question_lookup = {_question_text(q): _question_text(q) for q in questions}
    answers: list[dict] = []
    for item in parsed.get("answers") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            answers.append(
                {"question": question_lookup.get(question, question), "answer": answer}
            )

    if questions and len(answers) < len(questions):
        answered = {a["question"] for a in answers}
        for q in questions:
            text = _question_text(q)
            if text not in answered:
                raise RuntimeError(f"profile answerer omitted question: {text}")

    out = {"answers": answers, "auto_answered": True}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    return out


def _default_dispatch(
    prompt: str,
    *,
    repo_root: str | os.PathLike,
    dispatch_fn: Callable[..., int] | None = None,
    models: tuple[str, ...] = ("kimi", "kimi"),
) -> str:
    """Run the native Kimi primary attempt and one Kimi retry.

    The second attempt runs when the first fails or returns empty/whitespace
    output. ``models`` remains injectable for bounded provider validation.

    Args:
        prompt: full prompt text (built by `build_prompt`).
        repo_root: path to the mini-ork engine root.
        dispatch_fn: injectable model-provider boundary for tests.
        models: ordered attempt list; production retains Kimi → Kimi retry.
    """
    from mini_ork.ported import llm_dispatch as native_dispatch

    def call(model: str) -> tuple[int, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = native_dispatch.llm_dispatch(
                    [
                        "--task-class", "profile_answerer",
                        "--node-type", "profile_answerer",
                        "--model", model,
                        "--prompt-text", prompt,
                    ],
                    root=str(repo_root),
                    dispatch_fn=dispatch_fn,
                )
        except Exception:
            return 1, ""
        return rc, stdout.getvalue()

    for model in models:
        rc, raw = call(model)
        if rc == 0 and raw.strip():
            return raw
    return ""


def answer_profile_questions(
    kickoff_path: str | os.PathLike,
    questions_json: str,
    out_path: str | os.PathLike,
    *,
    dispatch=None,
    repo_root: str | os.PathLike | None = None,
) -> dict:
    """Answer and persist all profile questions.

    Args:
        kickoff_path: path to the kickoff markdown file.
        questions_json: JSON-stringified list of questions (str or list).
        out_path: where to write the answers JSON. Parent dir is created.
        dispatch: optional Callable[[str], str] taking the prompt text and
            returning raw LLM text. Tests inject a captured real LLM
            response; production callers should pass None to use the native
            Kimi primary-and-retry chain.
        repo_root: required when dispatch is None — path to the mini-ork
            engine root.

    Returns:
        The dict written to `out_path`.

    Raises:
        ValueError: any of kickoff_path / questions_json / out_path is
            empty.
        FileNotFoundError: kickoff_path is non-empty but the file does
            not exist.
        RuntimeError: the LLM output was non-JSON (after fence-strip +
            balanced-extraction) or the LLM omitted one or more input
            questions.
    """
    kickoff_path = str(kickoff_path) if kickoff_path else ""
    questions_json = questions_json if isinstance(questions_json, str) else (
        json.dumps(questions_json) if questions_json is not None else ""
    )
    out_path = str(out_path) if out_path else ""

    if not kickoff_path or not questions_json or not out_path:
        raise ValueError(
            "usage: mo_answer_profile_questions <kickoff_path> <questions_json> <profile_answers_out_path>"
        )
    if not os.path.isfile(kickoff_path):
        raise FileNotFoundError(f"profile answerer kickoff not found: {kickoff_path}")

    prompt = build_prompt(kickoff_path, questions_json)

    if dispatch is None:
        if repo_root is None:
            raise ValueError(
                "repo_root is required when dispatch is None "
                "(default dispatch uses the native llm dispatcher)"
            )
        raw = _default_dispatch(prompt, repo_root=repo_root)
    else:
        raw = dispatch(prompt)

    return parse_and_persist(raw, questions_json, out_path)
