"""Reflection-refiner sub-pipelines — Python port of lib/reflection-refiner.sh.

Faithful port of the *deterministic* sub-pipelines of `mo_run_reflection_refiner`
and `mo_append_reflection_to_feedback`. The LLM call (claude -p with budget/cache
flags) stays in bash — it requires live model + provider env scripts and is
out of scope for pytest. The Python port gives callers an in-process surface
and gives parity tests a stable target to byte-diff against the live bash.

Co-existence model (strangler-fig): bash `lib/reflection-refiner.sh` is the
authoritative source. This module mirrors its sub-pipelines exactly. Parity
is enforced by `tests/unit/test_reflection_refiner_py.py` (>=6 cases that
invoke `jq`/`awk`/`sed`/`sqlite3` via subprocess and diff against the Python
output byte-for-byte; floats 1e-6).

Pipeline map (bash function → Python):
  mo_run_reflection_refiner:
    early-bail (missing bdd-verdict / verdict!=FAIL)      → should_run
    sqlite query 'SELECT kickoff_path FROM epics WHERE id=:epic' → read_kickoff_path
    jq failure-summary projection                          → format_failure_summary
    awk+awk+awk split / sed {{KICKOFF_PATH}} replace / cat → build_prompt
    awk '^## Reflection refiner/,/EOF/' extraction        → extract_reflection
    heredoc fallback when reflection.md is empty           → render_fallback
  mo_append_reflection_to_feedback                         → append_to_feedback
"""
from __future__ import annotations

import os
import re
import sqlite3

__all__ = [
    "format_failure_summary",
    "should_run",
    "read_kickoff_path",
    "build_prompt",
    "extract_reflection",
    "render_fallback",
    "append_to_feedback",
]


_REFLECTION_HEADING_RE = re.compile(r"(?m)^## Reflection refiner")
# Match the marker as a literal substring anywhere on a line (mirrors awk's
# `index($0,m)` semantics, which is position-agnostic).
def _split_once(lines: list[str], marker: str) -> tuple[list[str], list[str]]:
    """Reproduce the bash awk stdout/stderr split: the FIRST line containing
    `marker` has the marker stripped (rest of line, if any, stays) and goes
    to the "before" stream; subsequent lines (including any later occurrences
    of the same marker) all go to the "after" stream.

    BSD-awk quirk: `gsub` strips the trailing newline alongside the marker
    (`length($0)` returns 0 once the only remaining char is RS). Lines whose
    post-gsub content is empty OR a bare newline are NOT printed — drop them.
    GNU awk keeps the newline; parity is locked to the macOS BSD awk that
    runs in our subprocess harness.

    Mirrors lib/reflection-refiner.sh::awk `-v m='{{...}}'` block byte-for-byte
    (the parity test asserts the same against a live awk subprocess).
    """
    before: list[str] = []
    after: list[str] = []
    found = False
    for line in lines:
        if not found and marker in line:
            stripped = line.replace(marker, "")
            found = True
            # If only the line-ending newline survived, the line is a no-op
            # in awk (length=0 → not printed). Drop it.
            if stripped.rstrip("\n"):
                before.append(stripped)
            continue
        if not found:
            before.append(line)
        else:
            after.append(line)
    return before, after


def _sed_substitute(text: str, marker: str, replacement: str) -> str:
    """Mirror `sed -i -e "s|${marker}|${kickoff_rel//|/\\|}|g"`.

    On macOS BSD sed the `\\|` in the replacement is parsed as literal `|`
    (the leading backslash is dropped), so the FINAL substituted text has
    unescaped pipes — matching the parity test's live-sed subprocess output.
    """
    return text.replace(marker, replacement)


def format_failure_summary(bdd_verdict: dict) -> str:
    """Mirror lib/reflection-refiner.sh::jq failure_summary projection.

    Output shape (byte-exact against live `jq`):
      "Total: {N} scenarios, {M} failed.\\n\\n" + join of "- **{title}** ({spec}): {error}"
      where error has '\\n' replaced by ' // ' (gsub in jq).

    Empty failures list → "Total: N scenarios, M failed.\\n\\n" (trailing blank line
    because the empty join collapses to '' but jq still appends '\\n' before it;
    verified against live `jq -r '... | join("\\n")'`).
    """
    scenarios_run = bdd_verdict.get("scenarios_run", 0)
    scenarios_failed = bdd_verdict.get("scenarios_failed", 0)
    header = f"Total: {scenarios_run} scenarios, {scenarios_failed} failed.\n\n"
    failures = bdd_verdict.get("failures") or []
    rendered = [
        f"- **{f.get('title','')}** ({f.get('spec','')}): "
        f"{(f.get('error') or '').replace(chr(10), ' // ')}"
        for f in failures
    ]
    # `jq -r '<expr>'` appends a trailing newline to stdout; mirror that.
    return header + "\n".join(rendered) + "\n"


def should_run(iter_dir: str | os.PathLike) -> tuple[bool, str]:
    """Mirror lib/reflection-refiner.sh::mo_run_reflection_refiner early-bail.

    Returns (False, reason) when bdd-verdict.json is missing OR when jq verdict
    != FAIL. Stderr messages match bash exactly so callers / log-scrape parity
    holds. `(True, '')` means caller should proceed to the LLM stage.
    """
    iter_dir = os.fspath(iter_dir)
    bdd_verdict = os.path.join(iter_dir, "bdd-verdict.json")
    if not os.path.isfile(bdd_verdict):
        return False, "[mini-ork] reflection-refiner: no bdd-verdict.json — skipping"
    try:
        import json
        with open(bdd_verdict, encoding="utf-8") as fh:
            verdict = json.load(fh).get("verdict", "")
    except (OSError, ValueError):
        return False, "[mini-ork] reflection-refiner: no bdd-verdict.json — skipping"
    if verdict != "FAIL":
        return False, (
            f"[mini-ork] reflection-refiner: bdd verdict={verdict} — nothing to refine"
        )
    return True, ""


def read_kickoff_path(db: str | os.PathLike, epic: str) -> str | None:
    """Mirror lib/reflection-refiner.sh::

        sqlite3 "$_db" "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null

    Bash returns '' on row miss; port returns None to make the absent row
    distinguishable from a present-but-empty string. On DB error the bash
    ALSO returns '' (the `2>/dev/null` swallows the error); the port surfaces
    None rather than swallowing — forbidden_fallbacks bans silent recovery.
    """
    try:
        con = sqlite3.connect(os.fspath(db))
        try:
            row = con.execute(
                "SELECT kickoff_path FROM epics WHERE id=?;", (epic,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return row[0]


def build_prompt(
    template: str,
    kickoff_body: str,
    kickoff_path: str,
    diff_files: list[str],
    failure_summary: str,
) -> str:
    """Mirror lib/reflection-refiner.sh prompt-assembly pipeline:
        3 sequential awk splits + sed `{{KICKOFF_PATH}}` replace +
        `cat tmp_a; cat kickoff_abs; cat tmp_b; echo diff_files;
         cat tmp_c; echo failure_summary; cat tmp_d`.

    `kickoff_path` is the relative path (mirrors the bash variable `kickoff_rel`
    that's substituted into the four pieces via sed); `kickoff_body` is the
    raw file content cat'd into the KICKOFF_BODY slot.
    """
    lines = template.splitlines(keepends=True)
    head_lines, body_and_tail = _split_once(lines, "{{KICKOFF_BODY}}")
    middle1_lines, middle2_and_tail = _split_once(body_and_tail, "{{DIFF_FILES}}")
    middle2_lines, tail_lines = _split_once(middle2_and_tail, "{{FAILURE_SUMMARY}}")

    sed_marker = "{{KICKOFF_PATH}}"
    head = _sed_substitute("".join(head_lines), sed_marker, kickoff_path)
    middle1 = _sed_substitute("".join(middle1_lines), sed_marker, kickoff_path)
    middle2 = _sed_substitute("".join(middle2_lines), sed_marker, kickoff_path)
    tail = _sed_substitute("".join(tail_lines), sed_marker, kickoff_path)

    # `echo "$diff_files"` and `echo "$failure_summary"` add a trailing
    # newline; mirror that so byte parity holds against the bash pipeline.
    return (
        head
        + kickoff_body
        + middle1
        + "\n".join(diff_files) + "\n"
        + middle2
        + failure_summary + "\n"
        + tail
    )


def extract_reflection(log_text: str) -> str:
    """Mirror lib/reflection-refiner.sh::

        awk '/^## Reflection refiner/,/EOF/' "$log_path" 2>/dev/null > "$refl_path"

    awk's range pattern emits every line from the first match through EOF
    (inclusive). Empty input / no match → empty string.
    """
    if not _REFLECTION_HEADING_RE.search(log_text):
        return ""
    lines = log_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## Reflection refiner"):
            return "".join(lines[i:])
    return ""


def render_fallback(failure_summary: str, iter_name: str) -> str:
    """Mirror the heredoc fallback in lib/reflection-refiner.sh::

        cat > "$refl_path" <<EOF
        ## Reflection refiner — fallback (LLM output unparseable)

        The reflection refiner did not produce parseable output. Raw failure summary:

        $failure_summary

        See iter-$iter/reflection.log for the full transcript.
        EOF

    Note: bash heredoc `$failure_summary` is unquoted under `set -uo pipefail`
    — empty value still works (echo prints empty line). The port must not
    raise on empty failure_summary.
    """
    return (
        "## Reflection refiner — fallback (LLM output unparseable)\n"
        "\n"
        "The reflection refiner did not produce parseable output. "
        "Raw failure summary:\n"
        "\n"
        f"{failure_summary}\n"
        "\n"
        f"See iter-{iter_name}/reflection.log for the full transcript.\n"
    )


def append_to_feedback(
    _epic: str,
    iter: str,
    feedback_path: str | os.PathLike,
    iter_dir: str | os.PathLike,
) -> bool:
    """Mirror lib/reflection-refiner.sh::mo_append_reflection_to_feedback:

        if [ -f "$refl" ]; then
          { echo; cat "$refl"; } >> "$feedback_path"
        fi

    `_epic` mirrors the bash parameter (passed so the call-site signature
    matches `mo_append_reflection_to_feedback`), but the port accepts
    `iter_dir` directly to avoid coupling to `mo_run_dir`.

    Returns True iff reflection.md existed (and was appended). On missing
    file, no-ops and returns False — bash does the same (no exception, no log).
    The leading blank line matches bash's `echo` before cat.
    """
    refl = os.path.join(os.fspath(iter_dir), f"iter-{iter}", "reflection.md")
    if not os.path.isfile(refl):
        return False
    try:
        with open(refl, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return False
    with open(os.fspath(feedback_path), "a", encoding="utf-8") as fh:
        fh.write("\n" + content)
    return True