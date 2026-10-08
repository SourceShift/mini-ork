from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.planning.registry_parse import (
    parse_registry,
    parse_registry_file,
    split_row,
)

# Every header shape that appears in the real registry, in one document.
# Raw strings: the escaped pipe in ONB-1 is load-bearing.
REGISTRY = r"""
## The registry

### Cluster A — Onboarding & dashboard (session 9)

| ID | Feature | Src | Home | Status | Owner | Evidence / spec |
|---|---|---|---|---|---|---|
| **DASH-1** | **Book HQ.** Owns composition | s9 | audience | partial | acq-wave5 | **Spec:** `docs/x.md` |
| ONB-1 | Dual onboarding doors (`goal=clients\|monetize`) | s9 | onboarding | shipped | onboarding | **Spec:** `docs/o.md` |

Some prose between tables that must not parse.

### Cluster B — Deletions (done)

| ID | Action | Src | Status | Evidence |
|---|---|---|---|---|
| DEL-1 | Remove `/en/citations` | s6 | done-deletion | `7540537c0` |

### Cluster C — Parked (session-10)

| ID | Feature | Status (2026-10-07) |
|---|---|---|
| P-1 | GEO layer | shipped (verify) |
| P-2 | course mode | parked |

### Some other heading, not a cluster

| ID | Feature | Status |
|---|---|---|
| NOPE-1 | must be excluded | x |

## Spec / brief coverage

- **Full greenfield specs exist:** DASH-1, ONB-1, DEL-1 — a bullet, not a row.
"""


def _by_id(items):
    return {item.id: item for item in items}


def test_parses_every_table_under_a_cluster_heading():
    items = parse_registry(REGISTRY)
    assert [i.id for i in items] == ["DASH-1", "ONB-1", "DEL-1", "P-1", "P-2"]


def test_bolded_id_is_unwrapped():
    assert "DASH-1" in _by_id(parse_registry(REGISTRY))


def test_escaped_pipe_stays_inside_its_cell():
    # The escape is what keeps the cells after it in their right columns.
    item = _by_id(parse_registry(REGISTRY))["ONB-1"]
    assert item.title == "Dual onboarding doors (`goal=clients|monetize`)"
    assert item.status == "shipped"


def test_action_column_feeds_title_and_status_is_found_by_name():
    item = _by_id(parse_registry(REGISTRY))["DEL-1"]
    assert item.title == "Remove `/en/citations`"
    assert item.status == "done-deletion"


def test_status_heading_with_a_date_suffix_is_matched_by_prefix():
    items = _by_id(parse_registry(REGISTRY))
    assert items["P-1"].status == "shipped (verify)"
    assert items["P-2"].status == "parked"


def test_tables_outside_a_cluster_are_excluded():
    assert "NOPE-1" not in _by_id(parse_registry(REGISTRY))


def test_bullet_lines_that_mention_ids_are_not_items():
    # "DASH-1, ONB-1, DEL-1" on a bullet line has no leading pipe.
    items = parse_registry(REGISTRY)
    assert len(items) == 5


def test_cluster_identity_and_line_numbers_are_recorded():
    item = _by_id(parse_registry(REGISTRY))["DEL-1"]
    assert item.cluster == "B"
    assert item.cluster_title == "Deletions (done)"
    assert item.line == 17  # 1-based line of the DEL-1 row in REGISTRY


def test_full_column_map_is_retained():
    item = _by_id(parse_registry(REGISTRY))["P-1"]
    assert item.columns["Feature"] == "GEO layer"


def test_duplicate_ids_raise_by_default():
    dup = REGISTRY + "\n### Cluster D — dupe\n\n| ID | Feature | Status |\n|---|---|---|\n| ONB-1 | again | shipped |\n"
    with pytest.raises(ValueError, match="duplicate registry id 'ONB-1'"):
        parse_registry(dup)


def test_duplicates_can_be_allowed_explicitly():
    dup = REGISTRY + "\n### Cluster D — dupe\n\n| ID | Feature | Status |\n|---|---|---|\n| ONB-1 | again | shipped |\n"
    assert len(parse_registry(dup, allow_duplicates=True)) == 6


def test_cluster_table_without_a_status_column_raises():
    # Silently dropping the table would hide those features from the audit.
    bad = "### Cluster Z — broken\n\n| ID | Feature |\n|---|---|\n| Z-1 | no status |\n"
    with pytest.raises(ValueError, match="no Status column"):
        parse_registry(bad)


def test_a_table_with_no_id_column_is_skipped_not_an_error():
    other = "### Cluster Y — prose table\n\n| Phase | Note |\n|---|---|\n| 1 | hi |\n"
    assert parse_registry(other) == []


# --- split_row -------------------------------------------------------------


def test_split_row_strips_outer_pipes_and_whitespace():
    assert split_row("| a | b |") == ["a", "b"]


def test_split_row_keeps_escaped_pipes():
    assert split_row(r"| a\|b | c |") == ["a|b", "c"]


def test_split_row_does_not_assign_a_trailing_backslash_or_swallow_a_pipe():
    # A backslash before a pipe escapes it; the pipe must not split.
    assert split_row(r"| x \| y \| z |") == ["x | y | z"]


# --- integration against the real registry ---------------------------------

_REAL = Path(
    "/Volumes/docker-ssd/Migration/Development/researcher/docs/product/specs/"
    "ebook-companion-feature-registry.md"
)


@pytest.mark.skipif(not _REAL.is_file(), reason="registry not present on this machine")
def test_real_registry_parses_with_stable_ids():
    items = parse_registry_file(_REAL)
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids))  # parse_registry would have raised otherwise
    assert len(items) == 87
    assert ids[0] == "DASH-1"
    # Every row carries a non-empty status: the audit's whole input contract.
    assert all(i.status for i in items)
    # The escaped-pipe row still maps its columns correctly.
    onb = next(i for i in items if i.id == "ONB-1")
    assert onb.status == "shipped"
