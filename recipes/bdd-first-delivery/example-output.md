# Example output — user-settings page kickoff

This document shows what `mini-ork run bdd-first-delivery example-kickoff.md` produces.

## 1. Decomposer output

The decomposer reads `example-kickoff.md` and emits `decompose.json`:

```json
{
  "parent_epic_id": "USER-SETTINGS",
  "parent_kickoff": "example-kickoff.md",
  "sub_epics": [
    {
      "id": "USER-SETTINGS-A",
      "title": "settingsApi — typed client for /api/user/settings",
      "rationale": "Pure addition; no consumer changes. All three section components depend on this.",
      "scope_globs": ["src/pages/settings/settingsApi.ts"],
      "branch": "feat/user-settings-a-api",
      "depends_on": [],
      "bdd_role": "leaf",
      "feature_kind": "be",
      "dod_probes": [
        "test -f src/pages/settings/settingsApi.ts",
        "grep -F 'getUserSettings' src/pages/settings/settingsApi.ts",
        "grep -F 'patchUserSettings' src/pages/settings/settingsApi.ts"
      ],
      "estimated_iters": 1
    },
    {
      "id": "USER-SETTINGS-B",
      "title": "ThemeSection, LanguageSection, NotificationsSection components",
      "rationale": "Three independent leaf components; no routing or page wiring yet.",
      "scope_globs": [
        "src/pages/settings/ThemeSection.tsx",
        "src/pages/settings/LanguageSection.tsx",
        "src/pages/settings/NotificationsSection.tsx"
      ],
      "branch": "feat/user-settings-b-sections",
      "depends_on": ["USER-SETTINGS-A"],
      "bdd_role": "leaf",
      "feature_kind": "fe",
      "dod_probes": [
        "test -f src/pages/settings/ThemeSection.tsx",
        "grep -F 'data-testid=\"settings-theme-section\"' src/pages/settings/ThemeSection.tsx",
        "test -f src/pages/settings/LanguageSection.tsx",
        "test -f src/pages/settings/NotificationsSection.tsx"
      ],
      "estimated_iters": 1
    },
    {
      "id": "USER-SETTINGS-C",
      "title": "SettingsPage shell — route + compose three sections",
      "rationale": "Integration sub-epic; wires B's components into the page and registers the route.",
      "scope_globs": [
        "src/pages/settings/SettingsPage.tsx",
        "src/App.tsx"
      ],
      "branch": "feat/user-settings-c-page",
      "depends_on": ["USER-SETTINGS-A", "USER-SETTINGS-B"],
      "bdd_role": "integration",
      "feature_kind": "fe",
      "dod_probes": [
        "test -f src/pages/settings/SettingsPage.tsx",
        "grep -F 'data-testid=\"settings-page-root\"' src/pages/settings/SettingsPage.tsx",
        "grep -F \"/settings\"' src/App.tsx"
      ],
      "estimated_iters": 1
    }
  ],
  "coverage_summary": "A provides the API client; B provides the three section components; C wires them into a routed page accessible at /settings."
}
```

## 2. Parallel dispatch

Three workers are spawned in parallel:

```
[bfd-dispatch] spawning implementer sub_epic=USER-SETTINGS-A worktree=worktrees/USER-SETTINGS-A
[bfd-dispatch] spawning implementer sub_epic=USER-SETTINGS-B worktree=worktrees/USER-SETTINGS-B
[bfd-dispatch] spawning implementer sub_epic=USER-SETTINGS-C worktree=worktrees/USER-SETTINGS-C
```

For sub-epics A and B (leaf, feature_kind=be/fe), spec_author runs first and emits `SPEC_SKIPPED` (A is BE-only) or writes `e2e/USER-SETTINGS-B_sections.spec.ts` (B has UI surface). For sub-epic C (integration), spec_author writes `e2e/USER-SETTINGS-C_page.spec.ts`.

## 3. Spec files written

`e2e/USER-SETTINGS-B_sections.spec.ts` — 3 scenarios:
- cold render of ThemeSection does not crash
- ThemeSection toggle calls PATCH and updates UI
- ThemeSection PATCH failure reverts to original value

`e2e/USER-SETTINGS-C_page.spec.ts` — 5 scenarios:
- cold render of SettingsPage does not crash
- page loads and renders all three sections
- theme toggle works end-to-end
- language picker works end-to-end
- notifications toggles work end-to-end

## 4. BDD runner verdicts (per sub-epic)

```
USER-SETTINGS-A: PASS (skipped=true, BE-only)
USER-SETTINGS-B: PASS (3/3 scenarios passed)
USER-SETTINGS-C: PASS (5/5 scenarios passed)
```

## 5. Aggregate reviewer verdict

```json
{
  "verdict": "APPROVE",
  "rationale": "All three sub-epics pass their BDD specs. DoD probes verified via grep. No scope overflow. TypeScript clean.",
  "issues": [],
  "feedback_to_worker": "",
  "approved_sub_epics": ["USER-SETTINGS-A", "USER-SETTINGS-B", "USER-SETTINGS-C"]
}
```

## 6. Publisher

All three branches are fast-forward merged to main:
```
feat/user-settings-a-api  → main  (merge commit abc1234)
feat/user-settings-b-sections → main  (merge commit def5678)
feat/user-settings-c-page → main  (merge commit ghi9012)
```

Total wall-clock time: ~22 minutes. Total cost: ~$7.40.
