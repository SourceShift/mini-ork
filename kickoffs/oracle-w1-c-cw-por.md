# Kickoff: CW-POR diagnostic library — orthogonal panel-health metric to Krippendorff α

## Problem

The framework currently has no measurable diagnostic for *authority-capture* — the failure mode where a single confidently-stated lens persuades the others to converge on a wrong answer. Krippendorff α (Nasser 2026) catches agreement-noise (low α = panel is random) but is blind to authority-capture (high α from one persuasive voice dragging the others toward incorrect consensus).

Agarwal & Khanna 2025 (arxiv:2504.00374) introduce the **Confidence-Weighted Persuasion Override Rate (CW-POR)** — a measurable proxy for authority-capture: the rate at which a panel adopts a confidently-stated wrong answer over a less-confidently-stated correct one, weighted by the persuasion-confidence delta.

## Definition of Done

A new shell library `lib/cw_por.sh` exposes one public function `mo_compute_cw_por` that takes a panel-verdict JSON file and emits a structured JSON record with the computed CW-POR score, threshold (default 0.3), verdict label (`panel_healthy` | `authority_capture_suspected`), and a one-sentence rationale.

## Scope

Only the new file `lib/cw_por.sh` may be created. No other file may be edited or created.

## Success Criteria

- `lib/cw_por.sh` exists and is executable (`chmod +x` applied)
- Sourcing the file in a clean bash session exposes `mo_compute_cw_por` as a defined function
- Inline self-test (executed when file is run directly) covers three cases: (a) low CW-POR clean panel returns `verdict: panel_healthy`, (b) high CW-POR with simultaneously high Krippendorff α returns `verdict: authority_capture_suspected`, (c) malformed verdict JSON returns rc=2
- `bash -n lib/cw_por.sh` syntax-check clean
- Header comment cites Agarwal & Khanna 2025 arxiv:2504.00374 and explains why CW-POR is orthogonal to Krippendorff α

## Model Preference

`claude-sonnet-4-5` — new self-contained file, pure shell + jq.

## Notes

Source spec in `kickoffs/oracle-hardening-v03.md` § Wave 1 — W1-C. Pure bash + jq; no python/node dependencies. The function will later be wired into `panel-verdict-enricher` (downstream) and the framework's reviewer pipeline (upstream); this kickoff ships the library only.
