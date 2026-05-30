# Expected Output: 03-refactor-pipeline

## Orchestrator Console Output

```
[mini-ork] run  run-20260530-161544-c9a1
[mini-ork] seed 2 epics from kickoff.md (Stage 1)
[mini-ork]   e-001  arch-plan    opus-4       analyze + propose split
[mini-ork]   e-002  arch-review  opus-4       consensus gate

[lane 1/1] claimed  e-001 (ARCH planner)
[lane 1/1] worker   opus-4 reading dataHelpers.ts (847 lines)
[lane 1/1] worker   found 23 import sites across 18 files
[lane 1/1] worker   proposed 4 modules: dateUtils, stringUtils, currencyUtils, paginationUtils
[lane 1/1] worker   wrote arch_plan.json to .mini-ork/runs/run-20260530-161544-c9a1/
[lane 1/1] verdict  PASS (plan written)

[lane 1/1] claimed  e-002 (ARCH reviewer)
[lane 1/1] reviewer opus-4 consensus check  score=0.91 ≥ 0.8  APPROVED
[lane 1/1] verdict  PASS

[mini-ork] Stage 1 complete — seeding 4 MODULE epics

[mini-ork]   e-003  module-dateUtils       sonnet-4-5  src/utils/dateUtils.ts
[mini-ork]   e-004  module-stringUtils     sonnet-4-5  src/utils/stringUtils.ts
[mini-ork]   e-005  module-currencyUtils   sonnet-4-5  src/utils/currencyUtils.ts
[mini-ork]   e-006  module-paginationUtils sonnet-4-5  src/utils/paginationUtils.ts

[lane 1/4] claimed  e-003
[lane 2/4] claimed  e-004
[lane 3/4] claimed  e-005
[lane 4/4] claimed  e-006

[lane 1/4] module-dateUtils       done  6 fns extracted  3 tests  cost=$0.018
[lane 3/4] module-currencyUtils   done  4 fns extracted  4 tests  cost=$0.014
[lane 2/4] module-stringUtils     done  5 fns extracted  5 tests  cost=$0.016
[lane 4/4] module-paginationUtils done  4 fns extracted  3 tests  cost=$0.013

[mini-ork] Stage 2 complete — seeding 1 ATOM epic

[mini-ork]   e-007  atom-integrate  sonnet-4-5  update import sites + barrel

[lane 1/1] claimed  e-007 (ATOM)
[lane 1/1] worker   updating 18 import sites
[lane 1/1] worker   reducing dataHelpers.ts to 8-line re-export barrel
[lane 1/1] worker   npm test  47 suites  312 tests  all PASS
[lane 1/1] verdict  PASS

[mini-ork] all lanes PASS — entering auto-merge
[mini-ork] merge  fast-forward onto main
[mini-ork] done   elapsed=14m08s  total_cost=$0.28
```

## Stage 1 Arch Plan (`arch_plan.json`)

```json
{
  "source_file": "src/utils/dataHelpers.ts",
  "import_sites": 23,
  "modules": [
    {
      "name": "dateUtils",
      "file": "src/utils/dateUtils.ts",
      "exports": ["formatDate", "parseIso", "toRelativeTime", "getDaysBetween", "toLocaleDateStr", "isExpired"]
    },
    {
      "name": "stringUtils",
      "file": "src/utils/stringUtils.ts",
      "exports": ["truncate", "slugify", "sanitizeHtml", "capitalizeFirst", "stripAnsi"]
    },
    {
      "name": "currencyUtils",
      "file": "src/utils/currencyUtils.ts",
      "exports": ["formatCurrency", "parseCurrency", "convertRate", "getCurrencySymbol"]
    },
    {
      "name": "paginationUtils",
      "file": "src/utils/paginationUtils.ts",
      "exports": ["paginate", "getPageCount", "buildCursor", "decodeCursor"]
    }
  ],
  "consensus_score": 0.91
}
```

## Stage 2 — Sample Module File

```typescript
// src/utils/dateUtils.ts  (new, extracted from dataHelpers.ts)
/**
 * Date formatting utilities extracted from dataHelpers.ts.
 * @see arch_plan.json epoch e-003
 */

export function formatDate(d: Date, locale = 'en-US'): string { ... }
export function parseIso(iso: string): Date { ... }
export function toRelativeTime(d: Date): string { ... }
export function getDaysBetween(a: Date, b: Date): number { ... }
export function toLocaleDateStr(d: Date): string { ... }
export function isExpired(d: Date): boolean { ... }
```

## Stage 3 — Import Site Update (sample)

```diff
// src/components/InvoiceCard.tsx
-import { formatDate, formatCurrency } from '../utils/dataHelpers';
+import { formatDate } from '../utils/dateUtils';
+import { formatCurrency } from '../utils/currencyUtils';
```

## Reduced `dataHelpers.ts` (barrel)

```typescript
// src/utils/dataHelpers.ts  — re-export barrel (backward compat)
// NOTE: import directly from focused modules in new code.
export * from './dateUtils';
export * from './stringUtils';
export * from './currencyUtils';
export * from './paginationUtils';
```

## State DB Snapshot

```sql
SELECT id, status, verdict, model, cost_usd
FROM epics
WHERE run_id = 'run-20260530-161544-c9a1'
ORDER BY id;

-- e-001 | merged | PASS | claude-opus-4     | 0.048
-- e-002 | merged | PASS | claude-opus-4     | 0.031
-- e-003 | merged | PASS | claude-sonnet-4-5 | 0.018
-- e-004 | merged | PASS | claude-sonnet-4-5 | 0.016
-- e-005 | merged | PASS | claude-sonnet-4-5 | 0.014
-- e-006 | merged | PASS | claude-sonnet-4-5 | 0.013
-- e-007 | merged | PASS | claude-sonnet-4-5 | 0.041
```

## Stage Progression Summary

```
Stage 1 — ARCH (2 epics, sequential, consensus gate)
   ├─ e-001 ARCH planner         Opus   $0.048   14m00s module proposals
   └─ e-002 ARCH reviewer        Opus   $0.031   score=0.91 ✓

Stage 2 — MODULE (4 epics, parallel)
   ├─ e-003 dateUtils            Sonnet $0.018   │
   ├─ e-004 stringUtils          Sonnet $0.016   │ parallel 4m20s total
   ├─ e-005 currencyUtils        Sonnet $0.014   │
   └─ e-006 paginationUtils      Sonnet $0.013   │

Stage 3 — ATOM (1 epic, sequential)
   └─ e-007 integrate            Sonnet $0.041   import sites + barrel + tests
```
