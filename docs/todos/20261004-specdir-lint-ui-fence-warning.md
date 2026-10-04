# TODO: specdir lint warning for template-unreachable ui acceptance criteria

Status: open · From: docs/decisions/20261004-sdd-gates-deterministic.md

Add a `UI_PROBE_UNREACHABLE` WARNING to `mini_ork/specdir/lint.py`: fires for
any acceptance-criteria line that names a UI behavior (data-testid / renders /
element wording) when the spec has neither an `agent-browser` bash fence nor
both a `data-testid="…"` literal and a route literal extractable from the AC
text. This is the authoring-time version of gates-materialize's
UI_TOKENS_MISSING — the sdd-10x campaign hit it on 4 of 118 ACs (2 specs,
revision r3) after dispatch instead of before.

Definition of done: lint warning + unit test (one triggering spec, one clean);
gates-materialize behavior unchanged.
