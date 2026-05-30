# Expected Output: 02-bug-hunt

## Orchestrator Console Output

```
[mini-ork] run  run-20260530-150922-b7d2
[mini-ork] seed 4 epics from kickoff.md
[mini-ork]   e-001  hunt-A  hunter   glm-4        src/components/ src/pages/
[mini-ork]   e-002  hunt-B  hunter   glm-4        src/services/ src/hooks/
[mini-ork]   e-003  hunt-C  hunter   glm-4        src/utils/ src/lib/
[mini-ork]   e-004  fix     worker   sonnet-4-5   dedup + apply all findings

[lane 1/3] claimed  e-001 (Hunter-A)
[lane 2/3] claimed  e-002 (Hunter-B)
[lane 3/3] claimed  e-003 (Hunter-C)

[lane 1/3] hunter-A done  4 findings  exit=0  cost=$0.008
[lane 2/3] hunter-B done  2 findings  exit=0  cost=$0.006
[lane 3/3] hunter-C done  1 finding   exit=0  cost=$0.005

[mini-ork] dedup  7 raw → 6 unique (1 duplicate between A+B)
[mini-ork] fix epic e-004 unblocked — all hunters PASS

[lane 1/1] claimed  e-004 (fix worker)
[lane 1/1] worker   applying 6 fixes + writing 6 regression tests
[lane 1/1] worker   done    exit=0  tokens=8340  cost=$0.031
[lane 1/1] review   spec-reviewer (Opus) reading diff (127 lines changed)
[lane 1/1] review   verdict=APPROVED  minor: add log level comment on 2 sites
[lane 1/1] worker   self-correction: added log level comments
[lane 1/1] bdd      runner executing 4 scenarios
[lane 1/1] bdd      Scenario: grep finds 0 empty catch blocks         PASS
[lane 1/1] bdd      Scenario: npm test exits 0                        PASS
[lane 1/1] bdd      Scenario: each changed site logs or rethrows      PASS
[lane 1/1] bdd      Scenario: no previously-passing test broken       PASS
[lane 1/1] verdict  PASS

[mini-ork] all lanes PASS — entering auto-merge
[mini-ork] merge  fast-forward onto main
[mini-ork] done   elapsed=9m22s  total_cost=$0.052
```

## Sample Hunter NDJSON (Hunter-A output)

```ndjson
{"file":"src/components/DataTable.tsx","line":84,"type":"empty_catch","context":"try { fetchData() } catch {}"}
{"file":"src/components/DataTable.tsx","line":112,"type":"empty_catch","context":"} catch (e) { /* TODO */ }"}
{"file":"src/pages/ProfilePage.tsx","line":47,"type":"empty_catch","context":"catch (err) {}"}
{"file":"src/pages/SettingsPage.tsx","line":203,"type":"empty_catch","context":"} catch (_) {}"}
```

## Dedup Pass (merged NDJSON, deduplicated)

```ndjson
{"file":"src/components/DataTable.tsx","line":84,"hunters":["A"]}
{"file":"src/components/DataTable.tsx","line":112,"hunters":["A"]}
{"file":"src/pages/ProfilePage.tsx","line":47,"hunters":["A"]}
{"file":"src/pages/SettingsPage.tsx","line":203,"hunters":["A"]}
{"file":"src/services/authService.ts","line":29,"hunters":["B"]}
{"file":"src/utils/formatters.ts","line":61,"hunters":["C"]}
```

## Sample Fix Diff

```diff
// src/components/DataTable.tsx:84
-  } catch {}
+  } catch (err) {
+    logger.warn('DataTable.fetchData failed', { error: err });
+  }

// src/services/authService.ts:29
-  } catch (e) {
-    // TODO: handle this
-  }
+  } catch (e) {
+    logger.error('authService token refresh failed — rethrowing', { error: e });
+    throw e;
+  }
```

## Sample Regression Test

```typescript
// src/services/__tests__/authService.test.ts (new)
it('token refresh failure is observable (not silently dropped)', async () => {
  const warnSpy = jest.spyOn(logger, 'error');
  mockRefreshEndpoint.mockRejectedValueOnce(new Error('network timeout'));
  await expect(refreshToken('bad-token')).rejects.toThrow('network timeout');
  expect(warnSpy).toHaveBeenCalledWith(
    expect.stringContaining('token refresh failed'),
    expect.objectContaining({ error: expect.any(Error) })
  );
});
```

## State DB Snapshot

```sql
SELECT id, status, verdict, model, cost_usd, findings_count
FROM epics
WHERE run_id = 'run-20260530-150922-b7d2'
ORDER BY id;

-- e-001 | merged | PASS | glm-4           | 0.008 | 4
-- e-002 | merged | PASS | glm-4           | 0.006 | 2
-- e-003 | merged | PASS | glm-4           | 0.005 | 1
-- e-004 | merged | PASS | claude-sonnet-4-5 | 0.031 | 6
```
