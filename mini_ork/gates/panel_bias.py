"""panel_bias — Python port of lib/panel_bias.sh.

Faithful port of the bash panel-bias helpers in ``lib/panel_bias.sh`` that
anonymize, rank-aggregate, and permute-order adversarial-panel review
artifacts. The bash script is the authoritative source — this module
gives Python callers an in-process target and gives
``tests/unit/test_panel_bias_py.py`` a stable surface to byte-diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): bash ``lib/panel_bias.sh`` stays
byte-identical. The Python port mirrors its CLI semantics exactly. Parity
is enforced by ``tests/unit/test_panel_bias_py.py`` (>=6 live-subprocess
cases that drive ``bash lib/panel_bias.sh <func> <args>`` on identical
inputs and diff the resulting files + stderr text + exit codes).

Pipeline map (bash → Python):
  panel_anonymize <reports_dir> <out_dir> [seed]
      ls -1 + grep '^lens-.*\\.md$'   →  _list_lens_files (sorted LC_ALL=C)
  bash  awk 'BEGIN{srand(seed)} {rand(), $0}'
      | LC_ALL=C sort -n | cut -f2-   →  _shuffle_lines (random.seed + key sort)
  bash  _PB_LABELS=(A..Z)             →  _LABELS (constant)
  bash  cp src resp-<LABEL>.md        →  panel_anonymize (shutil.copy2)
  bash  jq -s 'add' label pairs       →  panel_anonymize (json merge)
  bash  ${out_dir}.label_map.json     →  panel_anonymize (sibling path)

  panel_rank_aggregate <xrank_dir> <label_map>
  bash  grep '^FINAL RANKING:' | head -n1
                                       →  _iter_rankings
  bash  Borda = Σ (N-1-p)              →  panel_rank_aggregate (per-label)
  bash  mean_rank = mean(1-indexed pos)→  panel_rank_aggregate (per-label)
  bash  sort_by(-.borda, .mean_rank, .family)
                                       →  panel_rank_aggregate (Python tuple sort)
  bash  <xrank_dir>/panel-rank-aggregate.json
                                       →  panel_rank_aggregate (output path)

  panel_permute_order <reports_dir> [seed]
  bash  for f in *.md                 →  panel_permute_order (glob one level)
  bash  | _pb_shuffle_lines seed      →  panel_permute_order (_shuffle_lines)

RNG parity note: bash uses ``awk 'BEGIN{srand(seed)}'`` which is a
platform-specific linear-congruential generator with 20-digit precision.
Python's ``random.seed(seed); random.random()`` is Mersenne Twister. The
two generators produce DIFFERENT sequences for the same seed — the
parity test asserts equivalence of downstream OUTPUTS (file presence,
byte-content, label_map keys/families, borda/mean_rank values, sort
order) NOT raw RNG order. panel_permute_order uses random.seed for its
own determinism; the parity test asserts Python-vs-Python determinism
(same seed → identical order, different seeds → different orders) plus
multiset preservation, NOT byte-equality with bash.

Public surface (mirrors bash function names exactly):
    panel_anonymize(reports_dir, out_dir, seed=0) -> dict
    panel_rank_aggregate(xrank_dir, label_map) -> list[dict]
    panel_permute_order(reports_dir, seed=0) -> list[str]
"""
from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path
from typing import Dict, List

__all__ = [
    "panel_anonymize",
    "panel_rank_aggregate",
    "panel_permute_order",
    "_list_lens_files",
    "_shuffle_lines",
    "_iter_rankings",
]

# A..Z for label assignment (panel sizes are typically 3-7 families).
# Mirrors bash _PB_LABELS=(A B C D E F G H I J K L M N O P Q R S T U V W X Y Z).
_LABELS: tuple[str, ...] = tuple(chr(ord("A") + i) for i in range(26))

# Precision used for the mean_rank output value. Mirrors bash
# `awk 'BEGIN{printf "%.4f", s/c}'` — 4 fractional digits.
_MEAN_RANK_PRECISION = 4

# Sort comparator emitted by panel_rank_aggregate:
#   borda DESC, mean_rank ASC, family ASC.
# bash uses `jq -s 'sort_by(-.borda, .mean_rank, .family)'` which sorts
# ascending on each key — the minus on borda flips it to DESC.


def _list_lens_files(reports_dir: str) -> List[str]:
    """Mirror bash: ``LC_ALL=C ls -1 "$reports_dir" | grep -E '^lens-.*\\.md$'``.

    Sort is LC_ALL=C (byte-wise) — Python's default ``sorted()`` already
    uses Unicode code points which for ASCII filenames matches C-locale
    ordering. Returns the bare basenames (not full paths).
    """
    p = Path(reports_dir)
    if not p.is_dir():
        raise FileNotFoundError(f"panel_anonymize: reports_dir not found: {reports_dir}")
    return sorted(
        name for name in os.listdir(reports_dir)
        if name.startswith("lens-") and name.endswith(".md")
        and (p / name).is_file()
    )


def _shuffle_lines(lines: List[str], seed: int) -> List[str]:
    """Mirror bash: ``awk -v s=$seed 'BEGIN{srand(s)} {printf "%020.18f\\t%s\\n", rand(), $0}' | LC_ALL=C sort -n | cut -f2-``.

    Bash uses awk's RNG with 20-digit zero-padded rand keys then a
    numeric sort. Python uses Mersenne Twister via ``random.seed`` —
    the downstream permutation differs from bash but the function is
    deterministic in-process (same seed → same output) and produces a
    uniform permutation of the input. See module docstring for why
    parity tests don't byte-compare raw orderings.
    """
    rng = random.Random(seed)
    return sorted(lines, key=lambda _line: rng.random())


def _iter_rankings(xrank_dir: str) -> List[List[str]]:
    """Yield one ``tokens`` list per reviewer file in ``xrank_dir``.

    Mirror bash lines 180-200: glob every file at the top level of
    ``xrank_dir``, take the FIRST ``^FINAL RANKING:`` line, strip the
    prefix, split on whitespace. Files with no matching line are skipped
    (no yield).
    """
    p = Path(xrank_dir)
    if not p.is_dir():
        raise FileNotFoundError(f"panel_rank_aggregate: xrank_dir not found: {xrank_dir}")
    out: List[List[str]] = []
    for entry in sorted(p.iterdir()):
        if not entry.is_file():
            continue
        ranking: str | None = None
        with entry.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if line.startswith("FINAL RANKING:"):
                    ranking = line[len("FINAL RANKING:"):].strip()
                    break
        if not ranking:
            continue
        tokens = ranking.split()
        if not tokens:
            continue
        out.append(tokens)
    if not out:
        raise ValueError(
            f"panel_rank_aggregate: no reviewer file with '^FINAL RANKING:' line in {xrank_dir}"
        )
    return out


def panel_anonymize(reports_dir: str, out_dir: str, seed: int = 0) -> Dict[str, str]:
    """Anonymize lens-*.md → resp-<LABEL>.md and emit sibling label_map.json.

    Mirror bash ``panel_anonymize``:
      1. Glob ``lens-*.md`` under ``reports_dir`` (sorted LC_ALL=C).
      2. Shuffle via ``_shuffle_lines(seed)``.
      3. For each file in shuffled order, assign label A, B, C, ...
         (up to 26). Byte-copy to ``<out_dir>/resp-<LABEL>.md``.
      4. Build a label_map {label: family} and write it to
         ``${out_dir}.label_map.json`` (sibling — NEVER inside out_dir).

    Returns the label_map dict. Raises:
      FileNotFoundError  reports_dir missing or no lens-*.md files
      ValueError         more than 26 families
      OSError            filesystem copy failure
    """
    if not os.path.isdir(reports_dir):
        raise FileNotFoundError(f"panel_anonymize: reports_dir not found: {reports_dir}")

    families = _list_lens_files(reports_dir)
    if not families:
        raise FileNotFoundError(f"panel_anonymize: no lens-*.md in {reports_dir}")

    if len(families) > len(_LABELS):
        raise ValueError(
            f"panel_anonymize: more than {len(_LABELS)} families unsupported (got {len(families)})"
        )

    shuffled = _shuffle_lines(families, seed)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    label_map_path = f"{out_dir}.label_map.json"

    label_map: Dict[str, str] = {}
    for i, family_file in enumerate(shuffled):
        label = _LABELS[i]
        # bash: family=$(printf '%s' "${f#lens-}"); family=${family%.md}
        family = family_file[len("lens-"):]
        if family.endswith(".md"):
            family = family[:-len(".md")]
        src = os.path.join(reports_dir, family_file)
        dst = os.path.join(out_dir, f"resp-{label}.md")
        shutil.copy2(src, dst)
        label_map[label] = family

    with open(label_map_path, "w", encoding="utf-8") as fh:
        json.dump(label_map, fh)
        fh.write("\n")
    return label_map


def panel_rank_aggregate(xrank_dir: str, label_map_path: str) -> List[Dict[str, object]]:
    """Aggregate Borda + mean_rank per label from reviewer ``FINAL RANKING:`` lines.

    Mirror bash ``panel_rank_aggregate``:
      1. Read every ``FINAL RANKING:`` line in ``xrank_dir``.
      2. For each token (label) at 0-indexed position p in a ranking of
         length N:
           borda     += N - 1 - p
           pos_sum   += p + 1      (1-indexed position)
           count     += 1
      3. mean_rank = pos_sum / count, formatted to 4 decimal places
         (mirrors bash ``awk 'BEGIN{printf "%.4f", s/c}'``).
      4. Sort: borda DESC, mean_rank ASC, family ASC. De-anonymize via
         label_map lookup (label missing → family="unknown", matches
         bash's `${family:-unknown}` fallback).
      5. Emit ``<xrank_dir>/panel-rank-aggregate.json`` (JSON array).

    Returns the sorted list of dicts. Raises:
      FileNotFoundError  xrank_dir or label_map missing
      ValueError         no reviewer file with FINAL RANKING line
    """
    if not os.path.isdir(xrank_dir):
        raise FileNotFoundError(f"panel_rank_aggregate: xrank_dir not found: {xrank_dir}")
    if not os.path.isfile(label_map_path):
        raise FileNotFoundError(f"panel_rank_aggregate: label_map not found: {label_map_path}")

    with open(label_map_path, "r", encoding="utf-8") as fh:
        label_map: Dict[str, str] = json.load(fh)

    rankings = _iter_rankings(xrank_dir)

    label_borda: Dict[str, int] = {}
    label_pos_sum: Dict[str, int] = {}
    label_count: Dict[str, int] = {}

    for tokens in rankings:
        n = len(tokens)
        for pos, tok in enumerate(tokens):
            label_borda[tok] = label_borda.get(tok, 0) + (n - 1 - pos)
            label_pos_sum[tok] = label_pos_sum.get(tok, 0) + (pos + 1)
            label_count[tok] = label_count.get(tok, 0) + 1

    entries: List[Dict[str, object]] = []
    for label, borda in label_borda.items():
        count = label_count[label]
        if count <= 0:
            continue
        mean_rank_raw = label_pos_sum[label] / count
        # bash emits 4 fractional digits via awk printf "%.4f". Mirror
        # exactly so the parity test sees byte-equal JSON numbers.
        mean_rank = float(f"{mean_rank_raw:.{_MEAN_RANK_PRECISION}f}")
        family = label_map.get(label, "unknown")
        entries.append({
            "label": label,
            "family": family,
            "borda": int(borda),
            "mean_rank": mean_rank,
        })

    # Sort: borda DESC, mean_rank ASC, family ASC. Python tuple sort is
    # ascending on each key, so negate borda for the DESC component.
    # Explicit casts satisfy strict type checkers (Dict[str, object]
    # loses the int/float narrowing needed for sort key arithmetic).
    entries.sort(key=lambda e: (-int(str(e["borda"])), float(str(e["mean_rank"])), str(e["family"])))

    out_file = os.path.join(xrank_dir, "panel-rank-aggregate.json")
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(entries, fh)
        fh.write("\n")
    return entries


def panel_permute_order(reports_dir: str, seed: int = 0) -> List[str]:
    """Return a seed-deterministic permutation of ``*.md`` basenames under ``reports_dir``.

    Mirror bash ``panel_permute_order``: one-level (non-recursive) glob
    of ``*.md`` files; basename only (via parameter expansion in bash,
    ``Path.name`` here); shuffled by seed; printed one per line to
    stdout in bash, returned as a list here.

    Returns the list of basenames in shuffled order. Raises:
      FileNotFoundError  reports_dir missing.
    """
    if not os.path.isdir(reports_dir):
        raise FileNotFoundError(f"panel_permute_order: reports_dir not found: {reports_dir}")

    basenames = [
        entry.name
        for entry in Path(reports_dir).iterdir()
        if entry.is_file() and entry.name.endswith(".md")
    ]
    basenames.sort()  # glob order is unspecified; sort for stable output of the unsorted input.
    return _shuffle_lines(basenames, seed)