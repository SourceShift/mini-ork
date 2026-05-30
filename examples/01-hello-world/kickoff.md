# Kickoff: Add CHANGELOG Unreleased Entry

## Problem

The project has no record of the new `feature/session-token-rotation` work under the
`[Unreleased]` section of its `CHANGELOG.md`. Anyone reading the changelog has no idea
this work is in-flight.

## Definition of Done

New entry exists in `CHANGELOG.md` under `## [Unreleased]`.

## Scope

Only `CHANGELOG.md` may be edited. No other file may be touched.

## Success Criteria

- `grep -A3 "## \[Unreleased\]" CHANGELOG.md` returns at least one bullet line.
- The new entry describes the session-token-rotation feature in plain language.
- No other file in the repository is modified.

## Model Preference

`claude-sonnet-4-5` (single-epic, low complexity — no need for Opus).

## Notes

This is an intentionally minimal kickoff. Use it to verify that mini-ork can handle a
single-file, single-epic delivery end-to-end without any infrastructure beyond git and
sqlite3.
