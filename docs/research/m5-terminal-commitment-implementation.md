# M5 Terminal Commitment Implementation

**Status:** ✅ Implemented (2026-07-20)
**Component:** `mini_ork/ported/terminal_commitment.py`
**Integration:** `mini_ork/ported/gate_registry.py`

---

## Overview

This implements M5 from the trajectory capture redesign: **Terminal Commitment Scoring** — evaluating "I'm done" claims against receipts.

### Architecture

```
agent claims "delivered"
  → harness checks receipts: tests run? tests pass? files edited?
  → if claim ∧ receipt       → status = delivered,    provenance = observed
  → if claim ∧ no receipt    → status = claimed,      provenance = claimed (do NOT close unit)
  → if claim ∧ contradiction → provenance = contradicted (UP-3 defect)
```

**UP-3**: The command ran, it failed, and the block claims success. This is worse than fabrication and maps to ContextNest's `contradicted` provenance tier.

---

## Implementation

### 1. Terminal Commitment Gate

**File:** `mini_ork/ported/terminal_commitment.py`

**Key Functions:**

- `extract_delivery_claim()` — Parse z-insight block for delivery claim
- `extract_evidence_from_transcript()` — Extract receipts from Claude Code transcript
- `score_terminal_commitment()` — Grade claim against evidence
- `terminal_commitment_gate()` — Gate interface (returns verdict JSON)

**Provenance Levels:**

- **observed** — Claim backed by receipts (files edited, tests passed)
- **claimed** — Claim without receipts (agent said it delivered, but no proof)
- **contradicted** — Claim contradicted by receipts (UP-3: tests failed but claimed success)

**CLI Usage:**

```bash
python3 -m mini_ork.ported.terminal_commitment <run_dir> <transcript_path> <zinsight_path>
```

**Exit Codes:**
- `0` — pass (observed, receipt-backed delivery)
- `1` — fail (contradicted, UP-3 violation)
- `2` — defer (claimed, unverified delivery)

### 2. Gate Registry Integration

**File:** `mini_ork/ported/gate_registry.py`

**Changes:**

1. Added `"terminal_commitment_gate"` to `_VALID_GATE_TYPES`
2. Added evaluation logic in `gate_evaluate()`:

```python
if gtype == "terminal_commitment_gate":
    # Context JSON: {run_dir, transcript_path, zinsight_path}
    result = terminal_commitment_gate(run_dir, transcript_path, zinsight_path)

    if result.get("up3_violation", False):
        return "fail"  # UP-3 contradiction
    if result.get("provenance") == "observed":
        return "pass"  # Receipt-backed
    return "defer"  # Unverified claim
```

### 3. Registering the Gate

```python
from mini_ork.ported.gate_registry import gate_register

gate_id = gate_register(
    db_path="state.db",
    gate_type="terminal_commitment_gate",
    condition="",  # Unused for terminal_commitment (uses context JSON)
    task_class_filter="*",  # All task classes
    safety=True,  # UP-3 is a safety violation
)
```

---

## Usage Examples

### Example 1: Honest Claim (Observed)

**z-insight block:**
```json
{
  "work_unit": {"id": "wu-abc123", "phase": "deliver"},
  "delivered_features": [
    {"feature": "median fix", "files": ["stats.py"]}
  ],
  "verification": ["python test_stats.py → All tests passed"],
  "progress": "done"
}
```

**Transcript receipts:**
- `stats.py` edited (patch_apply_end event)
- `python test_stats.py` executed with output "All tests passed"

**Verdict:**
```json
{
  "provenance": "observed",
  "reason": "Delivery claim backed by receipts (files edited and/or tests passed)",
  "up3_violation": false,
  "should_block_merge": false
}
```

### Example 2: Unverified Claim (Claimed)

**z-insight block:**
```json
{
  "work_unit": {"id": "wu-xyz789", "phase": "deliver"},
  "delivered_features": [
    {"feature": "API endpoint", "files": ["api/handler.py"]}
  ],
  "verification": ["pytest tests/api/ → 12 passed"],
  "progress": "delivered"
}
```

**Transcript receipts:**
- No file edits
- No test commands executed

**Verdict:**
```json
{
  "provenance": "claimed",
  "reason": "Agent claims delivery but no receipts found (fabrication risk)",
  "up3_violation": false,
  "should_block_merge": true
}
```

### Example 3: UP-3 Violation (Contradicted)

**z-insight block:**
```json
{
  "work_unit": {"id": "wu-def456", "phase": "deliver"},
  "verification": ["pytest tests/unit/ → 5 passed"],
  "progress": "done"
}
```

**Transcript receipts:**
- `pytest tests/unit/` executed with exit code 1 (failed)
- Output: "FAILED: AssertionError in test_median"

**Verdict:**
```json
{
  "provenance": "contradicted",
  "reason": "UP-3: Agent claimed verification but 1 test(s) failed",
  "up3_violation": true,
  "should_block_merge": true
}
```

---

## Integration with ContextNest

### Ingest-Time Provenance Assignment

The ContextNest extractor should use the terminal commitment verdict to assign provenance:

```python
# In ContextNest extractor
from mini_ork.ported.terminal_commitment import terminal_commitment_gate

verdict = terminal_commitment_gate(run_dir, transcript_path, zinsight_path)

# Assign provenance to records
for record in records:
    if verdict["provenance"] == "observed":
        record.provenance = "observed"
    elif verdict["provenance"] == "claimed":
        record.provenance = "claimed"
    elif verdict["provenance"] == "contradicted":
        record.provenance = "contradicted"

# Store work unit status
if verdict["up3_violation"]:
    work_unit.status = "contradicted"
elif verdict["provenance"] == "observed":
    work_unit.status = "delivered"
else:
    work_unit.status = "claimed"  # Don't close the unit
```

### Query Examples

```sql
-- Find contradicted deliveries (UP-3 violations)
SELECT * FROM work_units WHERE provenance = 'contradicted';

-- Find unverified claims (potential fabrications)
SELECT * FROM work_units WHERE provenance = 'claimed';

-- Find receipt-backed deliveries (safe to auto-merge)
SELECT * FROM work_units WHERE provenance = 'observed';
```

---

## Testing

### Unit Tests

```bash
# Test extraction logic
pytest tests/test_terminal_commitment.py::test_extract_delivery_claim
pytest tests/test_terminal_commitment.py::test_extract_evidence

# Test scoring logic
pytest tests/test_terminal_commitment.py::test_score_observed
pytest tests/test_terminal_commitment.py::test_score_claimed
pytest tests/test_terminal_commitment.py::test_score_up3
```

### Integration Tests

```bash
# Register the gate
python3 -c "
from mini_ork.ported.gate_registry import gate_register
print(gate_register('state.db', 'terminal_commitment_gate', '', '*', True))
"

# Evaluate against a real run
python3 -m mini_ork.ported.gate_registry state.db <gate_id> \
  '{"run_dir": "/path/to/run", "transcript_path": "...", "zinsight_path": "..."}'
```

---

## Migration Path

### Phase 1: Ingest-Only (Current)
- Extract and grade terminal commitment claims
- Assign provenance in ContextNest
- **Do not block auto-merge yet** (observe calibration)

### Phase 2: Soft Block
- Defer auto-merge on `claimed` (unverified) deliveries
- Still allow human override
- Collect false-positive/negative metrics

### Phase 3: Hard Block
- Block auto-merge on `contradicted` (UP-3) deliveries
- Defer on `claimed`
- Only auto-merge `observed` (receipt-backed)

### Phase 4: Full Integration
- Wire into TraceOtter RL substrate
- Use provenance as step-level reward signal
- Train router on receipt-backed labels (not self-reported)

---

## References

- **Design Document:** `/docs/research/20260714-trajectory-capture-redesign.md`
- **Academic Source:** [2605.08747](https://arxiv.org/abs/2605.08747) — Done, But Not Sure: Disentangling World Completion from Self-Termination
- **Related:** M1-M4 implementation status in trajectory capture redesign §6

---

## Changelog

- **2026-07-20** — Initial implementation
  - Created `terminal_commitment.py` module
  - Integrated into `gate_registry.py`
  - Added CLI interface
  - Documented usage examples
