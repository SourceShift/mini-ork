# Bug audit (synthesis) — report only, no fixes

**Scope:** `skills/last30days/scripts/lib/` (SEC-1..SEC-7, PERF-1..PERF-6) and `skills/last30days/scripts/last30days.py`. Read-only; no source file modified; no live third-party network call made. Out of scope and untouched: `lib/vendor/`, `tests/`, `mcp/`, third-party dependencies.

**Panel:** four heterogeneous lenses — **glm** (credential/cookie lifecycle), **kimi** (untrusted input / OS boundary), **codex** (network performance), **minimax** (aggregation cost, blocking, races, contract drift).

## Summary
- Total unique bugs: **44**
- By severity: P0 **0** · P1 **2** · P2 **20** · P3 **22**
- By consensus: 4/4 **0** · 3/4 **0** · 2/4 **5** · 1/4 **39**
- Coverage: features with ≥1 filed bug: SEC-1, SEC-2, SEC-3, SEC-4, SEC-5, SEC-6, SEC-7, PERF-2, PERF-3, PERF-4, PERF-5, PERF-6. Feature with zero bugs from any lens: **PERF-1** (unbounded concurrency / pool sizing).
- Headline: **no P0 and no credential-exfiltration-on-default-install finding survived verification.** All four lenses independently confirmed that the TLS layer, the redirect/SSRF guard stack, `shell=True`/`os.system` usage, and `pickle`/`yaml.load`/`eval` are clean. The defects that remain are logic errors, contract drift, fail-open reporting, and resource bounds.

---

## P0 — Critical bugs (Exploitable today)

None. No lens reported a P0, and no candidate survived the false-positive filter at that band.

---

## P1 — Visible breakage (Exploitable today)

### [BUG-001] [CONSENSUS: 2/4 — codex, minimax] Planner-stage LLM call has no wall-clock deadline, no cancel, and inherits the default 5-attempt retry budget — a run can park ~8 minutes before the first source stream starts
- **File:** `skills/last30days/scripts/lib/providers.py:190-197` (budget gate), `skills/last30days/scripts/lib/providers.py:291` (the `_post` call), `skills/last30days/scripts/lib/pipeline.py:2614-2622` (planner use), `skills/last30days/scripts/lib/planner.py:537`, `skills/last30days/scripts/lib/http.py:41` (`MAX_RETRIES = 5`), `skills/last30days/scripts/lib/http.py:1723-1733` (`sleep_before_retry`)
- **Feature:** PERF-2 (missing total deadline) + PERF-3 (retry amplification) — reasoning-provider planning stage
- **Severity/Confidence:** P1 / high
- **Symptom:** The slash-command invocation appears hung. Planning happens *before* any `_research_deadline` exists in `config` (that key is injected later, at `pipeline.py:2941`), so `_post` never receives `deadline_monotonic`/`cancel`; each attempt is bounded only by the 90s socket timeout and the loop retries 5 times with 2/4/8/16s backoff.
- **Root cause:** `set_request_budget` — the only writer of `self._request_deadline` (`providers.py:135-136`) — is called only from the rerank stage (`pipeline.py:3516-3517`). `providers._post` injects the deadline *only if* it is set, and passes `timeout=90` but never `retries=`, so `http.request`'s `MAX_RETRIES = 5` applies. `sleep_before_retry` has no bound other than a caller deadline it does not have.
- **Reproduction (offline, loopback only):** run a local accept-and-stall server on `127.0.0.1:9101`, set `OPENROUTER_API_KEY=dummy` and `OPENROUTER_BASE_URL=http://127.0.0.1:9101/v1`, then `uv run python skills/last30days/scripts/last30days.py "test query" --emit=compact`. The planner POST connects, receives no bytes, and burns one 90s timeout per attempt for 5 attempts (`5×90s + 30s ≈ 480s`) before falling back to the deterministic planner.
- **Impact:** Up to ~8 minutes of dead time before a single source stream is submitted; Ctrl-C interrupts the plan but the retry sleeps are not cancel-aware at this stage. A user on a slow/free-tier provider sees a hang, not an error.
- **Fix shape:** Set a request budget before `planner.plan_query` at `pipeline.py:2614`, or pass `retries=1` for the planner call at `providers.py:291` as `xai_x.py:219-226` already does.
- *Disputed severity: see Disputed entries.*

### [BUG-002] [CONSENSUS: 1/4 — glm] After an X browser-session switch, a clean result is replaced by the rejected attempt's items and stale error string
- **File:** `skills/last30days/scripts/lib/bird_x.py:586` (`_keep_best_partial` consulted on the clean branch; helper at `skills/last30days/scripts/lib/bird_x.py:569-580`)
- **Feature:** SEC-1 (X browser-session failover)
- **Severity/Confidence:** P1 / high (reproduced offline)
- **Symptom:** After failover to a second browser session, the source returns the *first* session's items together with the first session's error text (e.g. `Internal server error`), and a genuine empty result from the working session is discarded.
- **Root cause:** On the final (post-switch) response the code still evaluates `if best_partial is not None and usable_count < best_partial_count and (error or usable_count == 0)`. `_keep_best_partial(current, current_error, current_count)` returns `best_partial` verbatim when `current_error` is empty (`bird_x.py:574` is skipped), so the stale suspect response — error string included — is returned instead of the fresh clean one.
- **Reproduction (verified offline, no network):**
  ```
  PYTHONPATH=skills/last30days/scripts python3 -c "
  from lib import bird_x
  calls={'n':0}
  def fake(query,count,timeout,deadline=None,cancel=None,top=False):
      calls['n']+=1
      if calls['n']==1:
          return {'items':[{'permanent_url':'https://x.com/u/%d'%i} for i in range(3)],'error':'Internal server error'}
      return {'items':[],'error':''}
  bird_x.set_session_failover('firefox', lambda *a,**k: ('tokB','ct0B','chrome'))
  bird_x._run_bird_search_once = fake
  print(bird_x._run_bird_search('test query',10,30))"
  ```
  Observed: 3 items plus `error: 'Internal server error'` from attempt 1; the chrome session's clean empty result is dropped.
- **Impact:** Every multi-browser X user whose first session is rejected mid-run receives a mislabeled result — stale items carrying an error that does not describe them, or "no results" misreported as a failure.
- **Fix shape:** In the clean-response branch at `bird_x.py:586`, return `response` whenever the post-switch response is error-free and either non-empty or `best_partial` is not strictly better; never return a stale `error`.

---

## P2 — Degraded behaviour (Hardening)

### [BUG-003] [CONSENSUS: 2/4 — glm, kimi] A corrupted corpus cache crashes the entire research run instead of resetting
- **File:** `skills/last30days/scripts/lib/corpus.py:346` (`int(row[1].get("mtime_ns") or 0)`), reach path `skills/last30days/scripts/lib/corpus.py:329` (called outside the guarded load), caller `skills/last30days/scripts/lib/pipeline.py:2880`
- **Feature:** SEC-6 (local data deserialization)
- **Severity/Confidence:** P2 / high (reproduced offline by glm)
- **Symptom:** `ValueError: invalid literal for int() with base 10: '...'` aborts the run; the corpus source and the whole pipeline phase die.
- **Root cause:** `_load_cache` resets to an empty cache on `OSError`/`JSONDecodeError`/schema mismatch (`corpus.py:323-326`), but `_bounded_entries(payload["entries"])` runs *outside* that try and its sort key calls `int()` on an unvalidated `mtime_ns`. A truthy non-numeric value (`"recent"`, `"1.5"`, a list) raises. `pipeline.run` calls `corpus.search(...)` with no try/except, unlike the guarded github lane at `pipeline.py:2859`.
- **Reproduction (verified offline):** write `{"schema_version": <CACHE_SCHEMA_VERSION>, "entries": {"x.md": {"mtime_ns": "not-a-number", "size": 10, "text": "hi"}}}` to the cache dir, then call `corpus.search('hello', [root], from_date='2000-01-01', to_date='2100-01-01', cache_dir=cache_dir)` → `ValueError` propagates out of `search()`.
- **Trigger precondition:** the cache file contains one non-numeric `mtime_ns` — hand-edited, synced, or written by an older engine version.
- **Impact:** A user with one damaged cache entry loses every run until the file is deleted by hand; the failure is mid-pipeline, not scoped to the corpus source.
- **Fix shape:** Move the `_bounded_entries` call inside `_load_cache`'s existing try (`corpus.py:329`), or coerce `mtime_ns`/`size` through a guarded int helper in the sort key.
- *Disputed severity: see Disputed entries.*

### [BUG-004] [CONSENSUS: 2/4 — glm, kimi] Credential-bearing query parameters are not masked before reaching stderr and fixture capture
- **File:** `skills/last30days/scripts/lib/http.py:1696` (four-name regex), `skills/last30days/scripts/lib/http.py:2183` (`get_text` logs the raw URL), `skills/last30days/scripts/lib/http.py:1653` (fixture capture gate)
- **Feature:** SEC-1 / SEC-2 (secret logging and output masking)
- **Severity/Confidence:** P2 / high
- **Symptom:** A URL such as `https://host/page?access_token=...` — or any presigned shape (`X-Amz-Signature`, `sig`, `password`, `auth`) — is emitted to stderr with the token intact, and is written into the recorded fixture JSON when `--record-fixtures` is active and `sensitive_url` is unset. `get_text` never sets `sensitive_url` and logs the whole URL on failure.
- **Root cause:** The only masking is `re.sub(r'([?&])(key|api_key|token|secret)=[^&]*', ...)` — four literal parameter names — plus userinfo scrubbing in `http.log` (`http.py:36-38`). `fixture_redactions` contains only header-derived values (`http.py:1684-1685`). `request()` masks its own log line at `http.py:1696`; `get_text` has no equivalent at `http.py:2183`, so the two seams disagree.
- **Reproduction (offline):** `http.get_text("https://example.invalid/?api_key=DUMMYSECRET123", timeout=1, retries=1)` against a refusing local port prints the URL with `api_key=DUMMYSECRET123`; `http.request("GET", same_url)` prints `api_key=***`.
- **Impact:** Query-string credentials from untrusted targets — `resolve.official_site_socials` fetches an untrusted official-site URL through `get_text` (`resolve.py:355-365`) — land in agent-captured stderr (the surface `AGENTS.md` names) and, on the fixture path, on disk.
- **Fix shape:** Replace the four-name regex with a case-insensitive credential-parameter mask over every parsed query pair, and route `http.py:2183` through it.
- *Disputed severity: see Disputed entries.*

### [BUG-005] [CONSENSUS: 2/4 — minimax filed, kimi noted-not-filed] Transcript harvest performs an unbounded synchronous disk scan before producing any output
- **File:** `skills/last30days/scripts/lib/transcript_harvest.py:45,48,59,62,68,71` (unbounded `rglob` over host dirs), `skills/last30days/scripts/lib/transcript_harvest.py:75-86` (full-file reads); related whole-file read at `skills/last30days/scripts/lib/transcript_harvest.py:117`
- **Feature:** PERF-6 (blocking call on a hot path)
- **Severity/Confidence:** P2 / high
- **Symptom:** With brief/library capture enabled, the run sits idle for a time proportional to *total transcript bytes on disk*, not to the topic.
- **Root cause:** `sorted(root.rglob("*.jsonl"))` has no depth, count or size cap; the marker scan has no per-file byte cap, no total byte cap, and no mtime narrowing. `bounded_get.py:32` shows the codebase already applies byte caps at the network seam; the disk seam has no equivalent.
- **Reproduction (offline):** point the harvester at a directory tree with a few hundred MB of `*.jsonl` and call it with capture enabled (`last30days.py:4384`, `last30days.py:4415`); wall time tracks bytes, not matches.
- **Impact:** Pure added latency on the user's critical path, growing monotonically with host history.
- **Fix shape:** Add a byte/file-count budget to the `rglob`/read loop at `transcript_harvest.py:45-86`, mirroring `MAX_RESPONSE_BYTES`.

### [BUG-006] [CONSENSUS: 1/4 — kimi] The "official site" redirect guard follows redirects to any public site, not the site being read
- **File:** `skills/last30days/scripts/lib/resolve.py:201-202` (guard drops same-site), consumed at `skills/last30days/scripts/lib/resolve.py:357-363`; contract at `skills/last30days/scripts/lib/public_host.py:196-199`, sibling `_same_site` at `public_host.py:44-48`
- **Feature:** SEC-5 (URL allowlists / redirects)
- **Severity/Confidence:** P2 / high
- **Symptom:** A `301`/`302` from a brand's homepage to an unrelated public domain is followed, and a sole `@handle` on the attacker-chosen landing page is attributed to the brand.
- **Root cause:** `_official_site_redirect_guard(target)` returns `is_public_address_syntax(target) and _public_site_host(target)` — the same-site check every other user of this machinery (`public_host.redirect_guard`) enforces is absent. `http.text_on_either_host_form` returns the *requested* URL (`http.py:1359-1376`), so `_site_label` still names the original host while `_own_account` accepts the destination page's handle (`resolve.py:314-338`).
- **Reproduction (offline):** fixture an official-site response of `302 Location: https://attacker.example/` whose body links exactly one `@attacker` X profile; call `resolve.official_site_socials(...)` → `x_handle == "attacker"` instead of `""`.
- **Impact:** An open redirect on (or parked domain of) a legitimate brand site becomes authoritative-looking wrong X/GitHub attribution in the report.
- **Fix shape:** Build the guard from `public_host.redirect_guard(original_url)` or add `_same_site(target_host, original_host)` at `resolve.py:201`.

### [BUG-007] [CONSENSUS: 1/4 — codex] Non-idempotent paid reasoning POSTs are retried up to 5× on timeout — billing amplification
- **File:** `skills/last30days/scripts/lib/providers.py:199-206` (`_post` never passes `retries=`), `skills/last30days/scripts/lib/http.py:41` / `http.py:1522` (default `retries=5`), `skills/last30days/scripts/lib/http.py:1892-1904` (each retry re-sends the identical body)
- **Feature:** PERF-3 (retry budget without idempotency guard)
- **Severity/Confidence:** P2 / high
- **Symptom:** A provider that processes the request but answers after the 90s timeout is billed up to 5 times for one prompt. `on_retry=self._mark_usage_incomplete` (`providers.py:199-200`) marks the usage total incomplete, hiding the multiplied spend from the report.
- **Root cause:** The generic reasoning providers inherit `MAX_RETRIES = 5`; `xai_x.py:219-226` already passes `retries=1` for exactly this reason, but the planner/rerank/fun-score lanes were left on the default.
- **Reproduction (offline, loopback):** point `OPENROUTER_BASE_URL` at a local server that answers `500`, enable `LAST30DAYS_USAGE_JOURNAL`, and call `http.post(...)` for one logical call — five `attempts` rows appear, one per retry, each a complete POST on the wire.
- **Impact:** Up to 5 billable generations per prompt on every provider stage.
- **Fix shape:** Pass `retries=1` in `ReasoningClient._post` at `providers.py:199`, mirroring `xai_x.py:219-226`.

### [BUG-008] [CONSENSUS: 1/4 — codex] X handle lanes silently discard non-fatal per-handle failures — partial coverage reported as complete
- **File:** `skills/last30days/scripts/lib/x_api.py:738-749` (merge drops transient errors), `x_api.py:617-618`, `x_api.py:723-731` (plain `pool.submit`, failure sink lost), `_FATAL_ERRORS` at `x_api.py:90`
- **Feature:** fail-open reporting / observability (SEC-1 adjacent — X credential lane)
- **Severity/Confidence:** P2 / high
- **Symptom:** A 429/timeout on one handle yields fewer X posts than the lanes should produce, with source outcome `ok` and nothing in `report.warnings`.
- **Root cause:** `_run_handle_lanes` merges only `result.get("error") in _FATAL_ERRORS`; a transient error result is kept but its `error` string is never propagated and `warning` is set only for window truncation (`x_api.py:631-636`). Because `pool.submit` (not `http.submit_with_context`) is used, the worker's failure sink is the default `None` (`http.py:687-689`) and `_record_failure` is a no-op. The sibling `xquik` backend threads these into `failure_out` (`xquik.py:318-323`), confirming xapi is the outlier.
- **Reproduction (offline, verified):** `x_api._run_handle_lanes(['a','b'], lambda h: {'items': [], 'error': 'xapi: rate limited'}, id_prefix='XF', warnings=w)` prints `[] []` — the error is discarded with no warning.
- **Impact:** A rate-limited lane is indistinguishable from a handle that posted nothing; the failure never reaches run accounting.
- **Fix shape:** Append transient `error` strings to `warnings` at `x_api.py:746-749` and submit via `http.submit_with_context`.

### [BUG-009] [CONSENSUS: 1/4 — glm] A non-dict JSON 200 from the X API is reported as a clean "no posts" result
- **File:** `skills/last30days/scripts/lib/x_api.py:504` (`return response if isinstance(response, dict) else {}`), consequence at `x_api.py:555-563`
- **Feature:** SEC-6 (untrusted/schema-drifted response parsing)
- **Severity/Confidence:** P2 / high
- **Symptom:** A JSON array/string/number body with HTTP 200 becomes `{"data": [], "truncated": False}` — no error, no `truncated`, source marked "no results" instead of degraded.
- **Root cause:** `_get` coerces any non-dict into `{}`, which `_search_pages` reads as a valid empty page. Unparseable bodies already raise (`http.py:2038-2045`); only parses-but-wrong-shape falls into this hole.
- **Reproduction (offline):** mock `http.get` to return `[]`, call `x_api._search_pages(token, url, params, 10)` → clean empty result, no failure.
- **Impact:** A changed X API envelope flows into reports as a confident negative rather than a degraded source.
- **Fix shape:** Raise `_XApiFailure` with schema-drift outcome at `x_api.py:504` instead of returning `{}`.

### [BUG-010] [CONSENSUS: 1/4 — minimax] The "funny" tag and lead order use a different floor than the documented Best-Takes floor
- **File:** `skills/last30days/scripts/lib/render.py:285` (`_QUOTE_POOL_FUNNY_FLOOR = 70.0`) vs `render.py:282` (`_BEST_TAKE_FUNNY_FLOOR = 40.0`); predicate `render.py:5833`; ordering `render.py:5940-5943`; Best-Take gate `render.py:5102` / `render.py:5115`
- **Feature:** quote-pool / report contract
- **Severity/Confidence:** P2 / high
- **Symptom:** A comment the renderer itself admits as a Best Take (`fun_score = 55`) appears untagged and does not lead its position in "Top Community Comments".
- **Root cause:** Two constants govern the same decision. `AGENTS.md` states entries on a candidate that cleared `_BEST_TAKE_FUNNY_FLOOR` "lead and end `funny`"; the `render.py:5772-5774` docstring says the same. The quote-pool predicate and sort key use 70.
- **Reproduction (static, code-level):** `funny = (cand.fun_score or 0.0) >= _QUOTE_POOL_FUNNY_FLOOR` (`render.py:5833`) vs `and c.fun_score >= _BEST_TAKE_FUNNY_FLOOR` (`render.py:5102`); a candidate at 55 satisfies the second and fails the first.
- **Impact:** The report's two sections disagree about what "funny" means; documented output shape is not delivered.
- **Fix shape:** Use `_BEST_TAKE_FUNNY_FLOOR` for the tag and sort key at `render.py:5833` / `render.py:5940`.

### [BUG-011] [CONSENSUS: 1/4 — minimax] Hashtag substring match inflates relevance so off-topic posts survive the relevance floor
- **File:** `skills/last30days/scripts/lib/relevance.py:172` (`if qt in tag_lower and qt != tag_lower`), consumed at `skills/last30days/scripts/lib/signals.py:53-54`, pruned against `RELEVANCE_FLOOR = 0.1` at `relevance.py:35`
- **Feature:** relevance scoring (correctness)
- **Severity/Confidence:** P2 / high (reproduced offline)
- **Symptom:** An Instagram/TikTok/X post whose body contains zero occurrences of the topic is admitted because a hashtag accidentally contains the topic as a substring (`"ai" in "#paid"`, `"ai" in "#detail"`).
- **Root cause:** String containment is used where a token boundary is required; the `qt != tag_lower` guard excludes only the degenerate whole-hashtag case. The adjacent comment (`relevance.py:167`) shows the intent was splitting a compound hashtag, not matching an unrelated one.
- **Reproduction (offline, no network):** `q = relevance.PreparedQuery.from_text("ai model"); relevance.token_overlap_relevance(q, "unrelated post about gardening")` → `0.0`; the same call with `hashtags=["#paid"]` → `0.18`, above the 0.1 floor.
- **Impact:** Off-topic items enter the story list and the quote pool, displacing on-topic material on every short/ambiguous topic token (`ai`, `ml`, `go`, `os`, `rust`).
- **Fix shape:** Require a token-boundary match (split compound hashtags on the documented rule) at `relevance.py:172` instead of raw containment.

### [BUG-012] [CONSENSUS: 1/4 — minimax] `dedupe_items` is a quadratic pairwise scan — measured ≈4× cost per doubling
- **File:** `skills/last30days/scripts/lib/dedupe.py:112` / `dedupe.py:144` (inner loop over every kept item); call sites `pipeline.py:928,2902,3776,3777,4151,4190`
- **Feature:** PERF-5 (O(n²) joins)
- **Severity/Confidence:** P2 / high (measured offline)
- **Symptom:** Broad-topic runs visibly slow in the last third; the engine's own deadline can fire during aggregation, yielding a partial report.
- **Root cause:** For each item, every kept item is compared with the expensive similarity function; no cheap bucketing key (token fingerprint / first-token index) prunes the candidate set.
- **Reproduction (offline, synthetic, unique titles so nothing dedupes):** n=100 → 0.019s, n=200 → 0.075s, n=400 → 0.295s, n=800 → 1.161s (min of 3 reps, gc disabled).
- **Impact:** >1s of pure CPU at n=800, extrapolating to ~4s at n=2,000 — and the routine runs at six pipeline call sites.
- **Fix shape:** Bucket by a cheap fingerprint before the pairwise similarity at `dedupe.py:144`.

### [BUG-013] [CONSENSUS: 1/4 — minimax] Cluster merge is quadratic and re-extracts entities for every pair
- **File:** `skills/last30days/scripts/lib/cluster.py:61`, `cluster.py:136` (`_merge_entity_clusters`), `cluster.py:164` (per-cluster entity extraction), `cluster.py:175` (pair loop); pool limit `pipeline.py:1104`
- **Feature:** PERF-5 (O(n²) joins + repeated work)
- **Severity/Confidence:** P2 / high (measured offline)
- **Symptom:** Clustering is the slowest aggregation phase and grows quadratically; a broad run can exhaust its deadline inside clustering and emit differently-grouped stories.
- **Root cause:** Every cluster pair is compared, and `entity_extract.extract_text_entities(_candidate_text(cand))` is recomputed for every candidate on every invocation even though the text never changes between passes.
- **Reproduction (offline, synthetic):** n=100 → 0.021s, n=200 → 0.079s, n=400 → 0.311s, n=800 → 1.213s (≈4× per doubling).
- **Impact:** Quadratic pair scan compounded by repeated extraction, sitting mid-chain before `cluster_diversity.diversify_clusters`.
- **Fix shape:** Memoise `extract_text_entities` per candidate at `cluster.py:164` and index clusters by entity before the pair loop at `cluster.py:175`.

### [BUG-014] [CONSENSUS: 1/4 — glm] Keychain loader pays the 5-second subprocess timeout once per key — up to ~120s added latency when the store hangs
- **File:** `skills/last30days/scripts/lib/env.py:525-534` (per-key loop), timeout constant `env.py:517`, catch at `env.py:519-520`; contrast `_load_pass` at `env.py:566-572`
- **Feature:** SEC-1 / PERF-2 (credential load path)
- **Severity/Confidence:** P2 / high
- **Symptom:** On a Darwin host where `security find-generic-password` hangs, every `get_config()` stalls 5s × 24 keys before research starts.
- **Root cause:** `_load_keychain` catches `(subprocess.TimeoutExpired, OSError)` per key and *continues*. `_load_pass` was explicitly given the opposite behavior — a first-timeout `break`, with a comment naming "a locked store would stall every config load by 5s x len(keys)" — but the Keychain path, which iterates more keys (`env.KEYCHAIN_KEYS`, 24 entries, `env.py:71-79`), never received it.
- **Reproduction (offline/static):** on macOS, put a script that sleeps 10 on `PATH` as `security`; any command that resolves config (`python3 skills/last30days/scripts/last30days.py "test" --emit=compact`) blocks 5s per key instead of breaking after the first.
- **Impact:** Every invocation (run, doctor, preflight, setup) pays the full stall; a hung store behaves differently on Darwin (slow) than Linux (fail-fast).
- **Fix shape:** Add the same first-timeout `break` to `_load_keychain`'s except clause at `env.py:519-520`.

### [BUG-015] [CONSENSUS: 1/4 — kimi] `ProviderRuntime.reasoning_provider` excludes the OpenRouter value the runtime actually builds
- **File:** `skills/last30days/scripts/lib/providers.py:566` (constructs `reasoning_provider="openrouter"`), contract `skills/last30days/scripts/lib/schema.py:39` (`Literal["gemini","openai","xai","local"]`), unvalidated ingest `schema.py:479`; `_MODEL_DEFAULTS` at `providers.py:450`, auto-detect at `providers.py:510`; `CONFIGURATION.md:547`
- **Feature:** SEC-6 (schema/type contracts over configuration)
- **Severity/Confidence:** P2 / high
- **Symptom:** A documented, auto-detected configuration produces a value outside the declared union; `ProviderRuntime` has no `__post_init__`, so it is accepted silently and only surfaces to a consumer that dispatches on the `Literal`.
- **Root cause:** The union was not extended when OpenRouter support landed; `provider_runtime_from_dict` feeds any persisted string in unvalidated.
- **Reproduction (offline):** with a dummy `OPENROUTER_API_KEY`, `providers.resolve_runtime(...)` returns `reasoning_provider == "openrouter"`, which is not in `typing.get_args(schema.ProviderRuntime.__annotations__["reasoning_provider"])`.
- **Impact:** Type contract and data disagree on a default-ish path; any future dispatch-on-Literal consumer falls through.
- **Fix shape:** Add `"openrouter"` to the `Literal` at `schema.py:39` and validate it in `__post_init__`.

### [BUG-016] [CONSENSUS: 1/4 — kimi] `agentcookie.read_x_cookies` claims "Never raises" but can raise `UnicodeDecodeError` from its subprocess pipe
- **File:** `skills/last30days/scripts/lib/agentcookie.py:208-214` (subprocess without `encoding`/`errors`), contract `agentcookie.py:196-200`, except clause `agentcookie.py:216-224`; reference implementation `subproc.py:233-234`
- **Feature:** SEC-2 (cookie credential intake and fallthrough)
- **Severity/Confidence:** P2 / high
- **Symptom:** On a non-UTF-8 locale (Windows defaults to cp1252), decoding the child's UTF-8 JSON raises `UnicodeDecodeError`, which is neither `TimeoutExpired` nor `OSError`, so it escapes and aborts the caller's cookie-source chain instead of returning `None`.
- **Root cause:** The `try` covers only the two documented failure classes; the project's own wrapper passes `text=True, encoding="utf-8", errors="replace"`, and the sibling `chrome_cookies.py:308` catches broadly. This is the outlier.
- **Reproduction (offline):** monkeypatch `agentcookie.subprocess.run` to raise `UnicodeDecodeError`; `read_x_cookies` propagates instead of returning `None`.
- **Impact:** A declared never-raises fallthrough becomes a hard abort of the X cookie chain on affected hosts.
- **Fix shape:** Pass `encoding="utf-8", errors="replace"` at `agentcookie.py:208` and catch `UnicodeDecodeError` alongside `OSError`.

### [BUG-017] [CONSENSUS: 1/4 — minimax] `agentcookie` launches its subprocess outside the process-tree-safe helper — orphaned descendants on timeout
- **File:** `skills/last30days/scripts/lib/agentcookie.py:208-213` (raw `subprocess.run`, no `start_new_session`), against `skills/last30days/scripts/lib/subproc.py:165` (`run_with_timeout`); timeout constant `agentcookie.py:51`
- **Feature:** SEC-4 (subprocess invocation / process hygiene)
- **Severity/Confidence:** P2 / high
- **Symptom:** After a hung `agentcookie` read (Keychain prompt, locked browser profile), `subprocess.run` kills only the direct child; grandchildren survive and keep whatever they held. Repeated runs stack orphans. The call also omits `stdin=subprocess.DEVNULL`, so a child that reads stdin inherits the engine's.
- **Root cause:** Every other external-command site uses `subproc.run_with_timeout` (list argv, `start_new_session=True`, POSIX process groups, Windows job objects); this call site was never migrated.
- **Reproduction (code-level):** timeout fires at `_TIMEOUT_SECONDS = 10`; a child that has forked leaves its descendants alive — observable in the OS process table.
- **Impact:** Resource leak and a lingering interactive prompt the user cannot attribute or kill through the skill.
- **Fix shape:** Route `agentcookie.py:208` through `subproc.run_with_timeout` and add `stdin=subprocess.DEVNULL`.
- *Same call site as BUG-016; different root cause and different fix — both defects are live.*

### [BUG-018] [CONSENSUS: 1/4 — minimax] Process-wide lock is held across a blocking browser-cookie read
- **File:** `skills/last30days/scripts/lib/reddit_session.py:112` (`_run_lock`), `reddit_session.py:356` (`with _run_lock:`), `reddit_session.py:359-360`
- **Feature:** PERF-6 (blocking I/O inside a process-wide mutex)
- **Severity/Confidence:** P2 / medium
- **Symptom:** Every concurrent `session_for` caller stalls while one thread decrypts the browser cookie; on macOS a Keychain/Full-Disk-Access prompt blocks until the user clicks, so an unattended run appears to hang.
- **Root cause:** The single-flight design is correct, but the critical section includes the slow work (`_run["resolved"] = True` is set, then the cookie read runs inside the lock). The codebase already has the right shape at `env.py:1700-1722` — daemon thread plus `worker.join(budget)`.
- **Reproduction (code-level):** two threads call `session_for` with a `reader` that sleeps; the second blocks at `reddit_session.py:356` for the full duration of the first.
- **Impact:** Availability/latency; a stalled cookie store serialises the whole Reddit lane.
- **Fix shape:** Double-checked cache with the read performed outside `_run_lock` at `reddit_session.py:356-360`.

### [BUG-019] [CONSENSUS: 1/4 — minimax] Module-global Reddit throttle flag races across concurrent entity sub-runs
- **File:** `skills/last30days/scripts/lib/reddit_arctic.py:600` (`global last_tree_throttled`), `reddit_arctic.py:619-625` (reset-on-entry / write), read at `skills/last30days/scripts/lib/reddit_hydrate.py:286`; fan-out `skills/last30days/scripts/lib/fanout.py:23,78`
- **Feature:** race condition / PERF-1 (shared mutable state across bounded threads)
- **Severity/Confidence:** P2 / medium
- **Symptom:** In a `--competitors` run, one entity's 429 is lost because a sibling thread's `fetch_comment_tree` entry reset the global before the hydrator read it; the run is diagnosed as "Reddit OK" while Reddit was throttling.
- **Root cause:** A single process-global boolean carries per-entity state across up to 6 concurrent pipelines in one interpreter, with no lock and no identity. The reset-on-entry makes any concurrent entry a wipe.
- **Reproduction (code-level):** thread A resets → fetch → sets `True`; thread B's entry reset landing between A's write and the hydrator's read clears the signal. Verified pattern: `throttled += bool(getattr(reddit_arctic, "last_tree_throttled", False))`.
- **Impact:** Health/outcome misclassification; the user is told the source is fine when it is degraded.
- **Fix shape:** Make the throttle signal per-entity (return it from `fetch_comment_tree`) rather than a module global at `reddit_arctic.py:600`.

### [BUG-020] [CONSENSUS: 1/4 — minimax] Process-global X session state is cleared by a concurrent sub-run
- **File:** `skills/last30days/scripts/lib/bird_x.py:125-131` (`set_credentials` clears `_session_browser`/`_next_session`, `bird_x.py:136-138` globals), reached from `skills/last30days/scripts/lib/env.py:1735-1739` via `x_backend_chain` (`env.py:2110`), called on the X lane from `pipeline.py:784,5882,7467`; concurrency from `fanout.py:78`
- **Feature:** concurrency / SEC-1 (credential handling)
- **Severity/Confidence:** P2 / medium
- **Symptom:** On a comparison run, one entity's X lane fails with "could not authenticate" although the browser session is valid: a sibling sub-run re-registered credentials and disarmed the failover registration between the rejection and the failover attempt.
- **Root cause:** `set_credentials` unconditionally sets `_session_browser = None; _next_session = None` under the lock — the lock makes the write atomic but does not make the value per-run. `env._inject_bird_credentials` calls it from every pipeline's X lane.
- **Reproduction (code-level):** thread A is mid-search with a browser session registered; thread B calls `set_credentials` → thread A's rejection finds `_next_session is None`. Up to 6 pipelines share the interpreter.
- **Impact:** The expired-cookie recovery mechanism is silently disarmed for the thread mid-search; one competitor's X evidence disappears non-deterministically.
- **Fix shape:** Scope the session/failover state per run (pass it through) instead of module globals at `bird_x.py:125-138`.

### [BUG-021] [CONSENSUS: 1/4 — codex] Transient-retry sleep runs inline in the stream-collection loop, stalling collection of every other completed stream
- **File:** `skills/last30days/scripts/lib/pipeline.py:3077-3099` (`time.sleep(delay)` at `pipeline.py:3094`), collection loop `pipeline.py:236-254`
- **Feature:** PERF-6 (blocking call on the aggregation hot path)
- **Severity/Confidence:** P2 / high
- **Symptom:** One stream failing with a transient 5xx adds up to 3s of latency to the *entire batch's* collection, not just the failed source; several failures serialise.
- **Root cause:** The collection loop is single-threaded by design, and the retry backoff is executed on the collecting thread when `_enrich_cancel` is absent. The control is `http.py:1861-1868`, where waits are a 0.1s cancel-aware poll.
- **Reproduction (code-level, 13-source / 16-worker fan-out):** one future completes with a 5xx while others are already done; the loop sleeps `min(3, max(0, deadline - now))` before resubmitting, collecting nothing meanwhile.
- **Impact:** Up to 3s added latency per transient failure across the whole batch.
- **Fix shape:** Defer the retry to a timer/worker rather than sleeping on the collection thread at `pipeline.py:3094`.


### [BUG-022] [CONSENSUS: 1/4 — kimi] `http.request` can raise `UnicodeDecodeError` although its contract permits only `HTTPError` — the never-raises fallthrough tiers inherit it
- **File:** `skills/last30days/scripts/lib/http.py:1756` (strict body decode), except list `skills/last30days/scripts/lib/http.py:1942-2050`, deliberate re-raise `skills/last30days/scripts/lib/http.py:1838-1839`, `get_text` catch `skills/last30days/scripts/lib/http.py:2181-2184`; affected callers `skills/last30days/scripts/lib/reddit_shreddit.py:234` and `skills/last30days/scripts/lib/http.py:2455`
- **Feature:** SEC-3 (transport error taxonomy / decoding)
- **Severity/Confidence:** P2 / high
- **Symptom:** A non-UTF-8 200 body (legacy CP1252/ISO-8859-1 page, proxy error page, binary body from a mis-set `Accept`) raises a `ValueError` subtype that no adapter's error branch handles. `get_text` also breaks its "never raises: returns None on HTTP error, network failure, or timeout" contract (`http.py:2126-2130`), and `reddit_shreddit.fetch_comments` — documented "Empty/None on any failure — never raises, so the caller can fall through to SC backup" (`reddit_shreddit.py:223-226`) — has no exception boundary around its `http.reddit_keyless_get_text(...)` call at `reddit_shreddit.py:234`.
- **Root cause:** The body is decoded with `errors=decode_errors`, default `"strict"` (`http.py:1756`, threaded from `http.py:1537` / `http.py:2088`). `UnicodeDecodeError` is not in the retry loop's except list; the deadline path even re-raises it deliberately when there is no HTTP error (`http.py:1838-1839`). It therefore escapes before `last_error` classification, before `_record_failure`/health state is attached, and before secret scrubbing. `reddit_keyless_get_text` passes no `decode_errors` (`http.py:2455`).
- **Reproduction (offline, loopback fixture):** serve `b'\xff\xfe'` with `Content-Type: text/plain` from a local host; `http.get_text(url)` raises instead of returning `None`, and `http.request("GET", url)` raises an exception that is not `http.HTTPError`. Monkeypatching `http.reddit_keyless_get_text` to raise `UnicodeDecodeError` shows it escaping `reddit_shreddit.fetch_comments`.
- **Impact:** Adapters across `lib/` that catch only `http.HTTPError` (`bluesky.py:220`, `x_api.py:498`, `hackernews.py:132`, `truthsocial.py:106`) receive an unhandled exception; the documented tier-fallback path never runs, and the failure bypasses health/failure accounting.
- **Fix shape:** Decode through a helper that catches `UnicodeDecodeError` and raises the classified `HTTPError` (schema-drift state) at `http.py:1756`.

---

## P3 — Sharp edges / footguns (Hardening)

### [BUG-023] [CONSENSUS: 2/4 — glm, minimax] Every memoized `fetch_once` result is deep-copied per waiter and again per cache hit
- **File:** `skills/last30days/scripts/lib/fetch_memo.py:122` (owner publish), `fetch_memo.py:99` (every cache hit), `fetch_memo.py:70`
- **Feature:** PERF-4 / PERF-5 (copying cost)
- **Severity/Confidence:** P3 / high
- **Symptom:** For a shared large result, `deepcopy` runs N+1 times over the whole payload; the cost scales with the number of consumers, not the number of fetches.
- **Root cause:** Both the publish path and the hit path deep-copy. Secondary defect on the same lines: if `deepcopy` raises on an un-copyable value, `slot.outcome` is never set and the owner's successful result is discarded (waiters re-fetch).
- **Reproduction (offline):** patch `copy.deepcopy` with a counter and call `fetch_once("k", lambda: big_dict, timeout=5)` from N concurrent waiters — N+1 copies.
- **Impact:** Quadratic-ish copying in the sharing-caller count; a latent dropped-result path for non-copyable adapter values.
- **Fix shape:** Publish once and return references (or a guarded shallow structural copy) at `fetch_memo.py:122`.

### [BUG-024] [CONSENSUS: 1/4 — glm] `config_secret_values` cannot see the raw X cookie pair stored under `_X_SESSION_STATE`
- **File:** `skills/last30days/scripts/lib/http.py:819-820` (top-level `str`-only iteration), consumer `http.py:795-803` and `doctor.py:2297-2310`, producer `env.py:1613-1619`; doctor list `doctor.py:2083-2090`
- **Feature:** SEC-1 / SEC-2
- **Severity/Confidence:** P3 / medium
- **Symptom:** A captured request/response echoing the live browser X cookie is not redacted under `--record-fixtures`, and doctor's "never write a cache containing a secret" check cannot see those values.
- **Root cause:** `extract_browser_credentials` stores the pair in a nested dict; `config_secret_values` iterates only top-level items and requires `isinstance(value, str)`. `_X_SESSION_STATE` is also absent from doctor's `_SECRET_CONFIG_VARS` (`doctor.py:2083-2090` names only top-level `AUTH_TOKEN`/`CT0`).
- **Reproduction (offline):** build a config via `env.extract_browser_credentials` with a browser present; `http.config_secret_values(config)` omits the nested `auth_token`/`ct0` values it would include at top level.
- **Impact:** Fixture recording is a dev path, so this is hardening, not an exploit chain.
- **Fix shape:** Recurse into dict values in `config_secret_values` (`http.py:819`) or flatten `_X_SESSION_STATE` at `env.py:1613`.
- *Disputed: minimax argues there is no live leak because the same values are always also present at top level. See Disputed entries.*

### [BUG-025] [CONSENSUS: 1/4 — glm] Bare `"enrolled"` marker misclassifies any 403 as "not enrolled for full-archive"
- **File:** `skills/last30days/scripts/lib/x_api.py:97` (marker tuple), consumed `x_api.py:463-464`
- **Feature:** SEC-6 / correctness
- **Severity/Confidence:** P3 / high
- **Symptom:** A genuine 403 (forbidden bearer, wrong access level) whose body merely contains "enrolled" is diagnosed as the enrollment case, triggering the recent-search retry path and the wrong user guidance.
- **Root cause:** `"enrolled"` is a substring of `"client-not-enrolled"` and matches any body containing the word.
- **Reproduction (offline):** `x_api._failure_for(http.HTTPError("HTTP 403: forbidden", 403, "your app has no access to enrolled endpoints"))` → `enrollment=True`.
- **Impact:** Wrong diagnosis surfaced to the user plus a wasted retry on a key that can never search.
- **Fix shape:** Drop the bare `"enrolled"` entry at `x_api.py:97`.

### [BUG-026] [CONSENSUS: 1/4 — glm] 429 retry cap grants one retry where `max_429_retries=2` is documented
- **File:** `skills/last30days/scripts/lib/http.py:1965-1968` (constant `http.py:42`, docstring `http.py:1553`)
- **Feature:** PERF-3 / contract drift
- **Severity/Confidence:** P3 / high
- **Symptom:** A host answering 429 twice in a row gives up after a single retry.
- **Root cause:** The counter increments on each 429 and raises when `rate_limit_count >= max_429_retries`, so the implemented maximum number of retries is `max_429_retries - 1`.
- **Reproduction (offline):** serve two consecutive 429s from a local fixture to `http.get(...)` with defaults; the second raises instead of scheduling the documented second retry.
- **Impact:** Throttled sources give up one retry earlier than documented on every lane.
- **Fix shape:** Compare `rate_limit_count > max_429_retries` at `http.py:1967`.

### [BUG-027] [CONSENSUS: 1/4 — glm] `mask_api_key` collapses any key of ≤ 8 characters to the same placeholder as "no key"
- **File:** `skills/last30days/scripts/lib/setup_wizard.py:883-884`, caller `skills/last30days/scripts/last30days.py:4958-4972`
- **Feature:** SEC-2 (output masking)
- **Severity/Confidence:** P3 / high
- **Symptom:** `setup --github-poll` prints `"api_key": "sc_…"` for a short-but-real key — the same text returned for an empty/absent key — so the host model and user cannot tell whether a key was persisted.
- **Root cause:** Four states (absent, empty, short-real, masked-long) map onto one display value.
- **Reproduction (offline):** `setup_wizard.mask_api_key("abc123") == setup_wizard.mask_api_key("") == "sc_…"`.
- **Impact:** A user with a short key may re-run the whole device flow believing persistence failed.
- **Fix shape:** Return a distinct placeholder for the empty case at `setup_wizard.py:883`.

### [BUG-028] [CONSENSUS: 1/4 — glm] `http.request` mutates the caller's headers dict
- **File:** `skills/last30days/scripts/lib/http.py:1611-1612`
- **Feature:** SEC-3 / sharp edge
- **Severity/Confidence:** P3 / high
- **Symptom:** A caller that reuses a headers dict finds `"User-Agent"` permanently injected after the first call.
- **Root cause:** `headers = headers or {}` rebinds only when `None`; `headers.setdefault("User-Agent", USER_AGENT)` then mutates the caller's object (the rebinding to `routed.headers` happens later, at `http.py:1649`, only for proxied calls).
- **Reproduction (offline):** `h = {}; http.request("GET", unreachable_local_url, headers=h)`; afterwards `h == {"User-Agent": http.USER_AGENT}`.
- **Impact:** Hidden cross-call coupling; a caller diffing headers before/after sees phantom keys.
- **Fix shape:** Copy the caller's dict once at `http.py:1611`.

### [BUG-029] [CONSENSUS: 1/4 — glm] Every JSON response body is parsed twice per attempt
- **File:** `skills/last30days/scripts/lib/http.py:1905-1909` (usage metering parse), `http.py:1930` (caller parse)
- **Feature:** PERF-6 (CPU on the hottest path)
- **Severity/Confidence:** P3 / high
- **Symptom:** Every JSON API response is `json.loads`-ed twice; HTML/binary bodies pay one wasted full parse that throws.
- **Root cause:** `parsed_usage = json.loads(body)` runs unconditionally for proxy usage metering, then the success path parses the same body again.
- **Reproduction (offline):** patch `json.loads` with a counter, call `http.get` against a local JSON server — 2 parses per request.
- **Impact:** Doubles deserialization CPU across every source and retry attempt, up to the 16 MB `MAX_RESPONSE_BYTES` cap.
- **Fix shape:** Parse once at `http.py:1907` and reuse the value when `raw` is False.

### [BUG-030] [CONSENSUS: 1/4 — kimi] `date_confidence` is populated with `"medium"`, which is not in `Literal["high","med","low"]`
- **File:** `skills/last30days/scripts/lib/normalize.py:1176` (`default="medium"`), helper `normalize.py:216-224`, contract `schema.py:94`, consumers `render.py:4554-4556`, `schema.py:720`
- **Feature:** SEC-6 (schema/type contracts over normalized records)
- **Severity/Confidence:** P3 / high
- **Symptom:** Every LinkedIn item without a usable date carries `"medium"`; `render.py:4556` prints the literal tag `[date:medium]`. The helper also passes through arbitrary source strings (`"HIGH"`, `"approximate"`) unchecked.
- **Root cause:** The default value was written as the English word rather than the schema member `"med"`.
- **Reproduction (offline):** normalize a LinkedIn item without a date → `item.date_confidence == "medium"`; render it → `[date:medium]`.
- **Impact:** Exact-match date logic and any schema validator disagree with the data.
- **Fix shape:** Use `"med"` at `normalize.py:1176` and clamp the passthrough to the three members.

### [BUG-031] [CONSENSUS: 1/4 — kimi] `get_text`'s `retries` parameter is documented as a retry count but implemented as a total attempt count
- **File:** `skills/last30days/scripts/lib/http.py:2136` (docstring), forward at `http.py:2178-2180`, contract at `http.py:1553-1556`; same wording at `http.py:2408-2412`, `http.py:2615`
- **Feature:** SEC-3 (retry semantics / contract drift)
- **Severity/Confidence:** P3 / high
- **Symptom:** `get_text(retries=2)` makes two attempts (one retry), not one initial attempt plus two retries; `retries=1` makes one attempt and no retry.
- **Root cause:** The value is forwarded unchanged to `request`, whose documented unit is "maximum number of attempts". Callers that reason about retries (`web_fetch_keyless.fetch_markdown(retries=2)`, `web_fetch_keyless.py:143-152`) get one fewer attempt than the wording promises.
- **Reproduction (offline):** a local server that always returns 500 — `http.get_text(url, retries=2)` produces 2 requests, not 3.
- **Impact:** One fewer attempt than callers expect; the two units coexist in one call chain.
- **Fix shape:** Document the parameter as "maximum attempts" at `http.py:2136` (or subtract one before forwarding).

### [BUG-032] [CONSENSUS: 1/4 — kimi] `params` are appended after a URL fragment, so they are never sent
- **File:** `skills/last30days/scripts/lib/http.py:1614-1616`, contract `http.py:1548-1550`
- **Feature:** SEC-5 (URL construction)
- **Severity/Confidence:** P3 / high
- **Symptom:** For `https://host/path#frag` with `params={"a":"1"}`, the merged string is `https://host/path#frag?a=1`; `urlsplit` puts `frag?a=1` in the fragment and the request line carries no query. A URL ending in `?` produces `?&a=1`.
- **Root cause:** String concatenation instead of `urlsplit`/`urlunsplit`; the fragment is never split off first.
- **Reproduction (offline):** `http.request("GET", "http://127.0.0.1:PORT/p#frag", params={"a": "1"})` against a local echo server — request target is `/p` with no `a=1`.
- **Impact:** Silently dropped query parameters on any caller that combines a fragment with params.
- **Fix shape:** Merge into `urlsplit(url).query` with `parse_qsl`/`urlencode` at `http.py:1614`.

### [BUG-033] [CONSENSUS: 1/4 — kimi] The declarative redirect-policy host suffix check accepts a bare suffix without a label boundary
- **File:** `skills/last30days/scripts/lib/bounded_get.py:148-155` (`host.endswith(suffix_hosts)`), contract `skills/last30days/scripts/lib/public_host.py:44-48`; policy serialization `bounded_get.py:160-169`, initial-URL check `bounded_get.py:414-417`
- **Feature:** SEC-5 (host allowlists for redirects)
- **Severity/Confidence:** P3 / high — **currently latent**: the only shipped policy (`youtube_yt.py:1134-1140`) is dotted, so no live path is widened today.
- **Symptom:** A policy author writing the natural `"suffix_hosts": ["youtube.com"]` admits `evilyoutube.com` as a redirect target, on both the first hop and redirects.
- **Root cause:** Every other host-matching rule in the tree requires a dot at the label boundary (`_same_site`, `_reader_redirect_guard`, and all in-tree policy suffixes); this generic validator does not.
- **Reproduction (offline):** `bounded_get._url_matches_policy("https://evilyoutube.com/x", {"suffix_hosts": ["youtube.com"], "https_only": True, "require_public_syntax": True, "allowed_ports": [443]})` returns `True`; it should be `False`.
- **Impact:** A one-word policy change silently widens the allowlist; no current exposure.
- **Fix shape:** Normalize each suffix to `"." + s.lstrip(".")` at `bounded_get.py:155`.
- *Disputed: minimax inspected the same function and recorded "no defect found". See Disputed entries.*

### [BUG-034] [CONSENSUS: 1/4 — kimi] `github._fetch_json` promises "Returns None on failure" but a non-UTF-8 200 body raises
- **File:** `skills/last30days/scripts/lib/github.py:105` (`resp.read().decode("utf-8")`), contract `github.py:85`, except clauses `github.py:107-127`
- **Feature:** SEC-3 (response decoding / failure contracts)
- **Severity/Confidence:** P3 / high
- **Symptom:** `UnicodeDecodeError` is a `ValueError` and matches none of the handlers, so it escapes `_fetch_json`, skips `_note(...)`, and contradicts both "Returns None on failure" and the failure-reason contract. `resp.read()` is also uncapped in this lane.
- **Root cause:** Strict decode inside a try whose handlers cover only `HTTPError`, `(URLError, OSError, TimeoutError)` and `json.JSONDecodeError`.
- **Reproduction (offline):** serve `200` with body `b'\xff'` from a local server (monkeypatch the URL) and call `github._fetch_json(url)` → `UnicodeDecodeError` instead of `None`.
- **Impact:** A decoding failure bypasses the caller's failure classification and the per-branch note.
- **Fix shape:** Decode with `errors="replace"` or catch `UnicodeDecodeError` and `_note("invalid encoding")` at `github.py:105`.

### [BUG-035] [CONSENSUS: 1/4 — codex] `retry_delay_from_headers` cannot parse an HTTP-date `Retry-After`, so date-form 429s fall back to a too-short backoff
- **File:** `skills/last30days/scripts/lib/http.py:88-97` (helper; `float(raw)` raises → `continue` → exponential fallback at `http.py:97`), applied at `http.py:1976-1979`; reused at `skills/last30days/scripts/lib/reddit_public.py:91-95`
- **Feature:** PERF-3 (retry/backoff correctness)
- **Severity/Confidence:** P3 / high on mechanism, low on in-scope occurrence
- **Symptom:** `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT` (RFC 7231 permits the date form) yields the 3s fallback, each retry re-429s, and `max_429_retries=2` fires — the source is reported dead when it was merely early, which is exactly what the function's own docstring warns about at `http.py:67-71`.
- **Root cause:** Only the delta-seconds form is parsed.
- **Reproduction (offline, verified):** `http.retry_delay_from_headers({'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}, 3.0)` → `3.0`; `{'Retry-After': '42'}` → `42.0`.
- **Impact:** A recoverable rate limit becomes a terminal source failure.
- **Fix shape:** Parse the HTTP-date form with `email.utils.parsedate_to_datetime` at `http.py:88`.

### [BUG-036] [CONSENSUS: 1/4 — codex] `reddit_public._fetch_json` bypasses the hardened transport — unbounded read, no failure sink, deadline-blind 429 sleeps
- **File:** `skills/last30days/scripts/lib/reddit_public.py:75` (default opener), `:84` (unbounded `resp.read()`), `:94-99` (uncapped 429 `time.sleep`), `:107-120` (every failure → `None`); production delegation at `reddit_public.py:286-290`
- **Feature:** PERF-2 / SEC-6-adjacent transport bypass
- **Severity/Confidence:** P3 / high on the gaps; reachability limited — the production path now delegates to `reddit_keyless`.
- **Symptom:** A direct caller of this retained utility gets a 2-minute park on 429s, a potentially unbounded memory read, and a clean `None` that reads as "no results".
- **Root cause:** The lane never migrated to `http._open_request`/`_read_bounded`/`_record_failure`, and its signature has no `deadline_monotonic`/`cancel`.
- **Reproduction (offline):** `inspect.signature(reddit_public._fetch_json)` has neither parameter — proving the bound-less signature.
- **Impact:** Latent hardening gap on a utility that is kept and tested but off the production path.
- **Fix shape:** Route through `bounded_get.get`/`http.request` or delete the duplicate lane at `reddit_public.py:75-120`.

### [BUG-037] [CONSENSUS: 1/4 — codex] `reddit.search_and_enrich_memo` waiters block on `gate.wait()` with no timeout
- **File:** `skills/last30days/scripts/lib/reddit.py:862-876` (no timeout at `:875`); bounded siblings `fetch_memo.py:102-105`, `http.py:2443-2445`
- **Feature:** PERF-2 / PERF-4 (single-flight waiter without its own budget)
- **Severity/Confidence:** P3 / medium
- **Symptom:** A waiter thread parks until the owner finishes, however long that takes; the waiter's stream future can time out while the worker stays blocked below it.
- **Root cause:** Every other single-flight in the tree bounds the wait with a deadline; this one does not. The owner is bounded only transitively by its internal HTTP timeouts.
- **Reproduction (offline, static):** the missing timeout argument at `reddit.py:875`, against the two bounded counterparts.
- **Impact:** A stuck waiter with no independent escape.
- **Fix shape:** Bound `gate.wait()` with a deadline at `reddit.py:875`, as `fetch_memo.py:102-105` does.

### [BUG-038] [CONSENSUS: 1/4 — codex] `_sleep_reddit_429_retry` sleeps 1.0–1.5s blind to the lane deadline and cancel event
- **File:** `skills/last30days/scripts/lib/http.py:2332-2337` (`time.sleep(1.0 + uniform(0, 0.5))`), caller `http.py:2502-2508`; control `http.py:1728-1731`, `youtube_yt.py:1506-1515`
- **Feature:** PERF-3 (retry pacing outside the deadline envelope)
- **Severity/Confidence:** P3 / high
- **Symptom:** A keyless Reddit 429 sleeps after the stream's deadline has already passed, then re-enters the retry and re-acquires the shared bucket; the second attempt is spent and discarded.
- **Root cause:** The helper takes no deadline/cancel parameters; the sleep is unconditional and unlogged.
- **Reproduction (offline):** `inspect.signature(http._sleep_reddit_429_retry)` shows neither parameter.
- **Impact:** Up to 1.5s of dead time per 429ing URL in a lane already out of budget; compounds across URLs.
- **Fix shape:** Accept and honour `deadline_monotonic`/`cancel` at `http.py:2332`.

### [BUG-039] [CONSENSUS: 1/4 — codex] GitHub enrichment fan-out uses plain `executor.submit`, dropping the failure-sink context
- **File:** `skills/last30days/scripts/lib/github.py:985-991` (plain submits), collector `github.py:996-999`; control `hackernews.py:463-467`, `reddit_listing.py:345-350`
- **Feature:** observability (context loss across executor boundary)
- **Severity/Confidence:** P3 / high
- **Symptom:** A transient 403/429 during per-repo enrichment records nothing in run failure accounting; the GitHub stream's outcome stays `ok` with silently missing stars/releases.
- **Root cause:** `_failure_sink` defaults to `None` in a fresh worker context (`http.py:687-689`), so `_record_failure` is a no-op; the collector swallows the exception into `enrichment = {}`.
- **Reproduction (offline, verified):** `inspect.getsource(github)` contains `submit_with_context` → `False`, and 7 plain `executor.submit(` sites (586, 986, 991, 1179, 1203, 1342, 1495).
- **Impact:** Partial enrichment is invisible to the report; only the stderr log shows it.
- **Fix shape:** Use `http.submit_with_context` at `github.py:986,991`.

### [BUG-040] [CONSENSUS: 1/4 — minimax] `health.probe_command` bypasses the process-tree-safe helper
- **File:** `skills/last30days/scripts/lib/health.py:118-123` (raw `subprocess.run`), against `skills/last30days/scripts/lib/subproc.py:165`
- **Feature:** SEC-4 / PERF-2 (same root cause as BUG-017, diagnostic path)
- **Severity/Confidence:** P3 / high
- **Symptom:** `--diagnose` hangs on a wedged probe and leaves its descendants running after the timeout fires.
- **Root cause:** Direct child killed only; no `start_new_session`/process group.
- **Reproduction (code-level):** `subprocess.TimeoutExpired` from a probe whose child spawned descendants; descendants survive.
- **Impact:** The probe the user runs *to* diagnose the host is itself a hang source; leaked descendants are invisible from the skill.
- **Fix shape:** Route `health.py:118` through `subproc.run_with_timeout`.

### [BUG-041] [CONSENSUS: 1/4 — minimax] `pdftotext` is invoked via raw `subprocess.run`
- **File:** `skills/last30days/scripts/lib/corpus.py:287-296` (`subprocess.run(..., timeout=20)`), against `subprocess.py→skills/last30days/scripts/lib/subproc.py:165`
- **Feature:** SEC-4 (subprocess invocation)
- **Severity/Confidence:** P3 / high
- **Symptom:** A hung or forking `pdftotext` is killed only at the direct-child level; descendants survive, and the child inherits the engine's stdin.
- **Root cause:** Same deviation as BUG-017/BUG-040. Mitigating: the caller catches `(OSError, subprocess.SubprocessError)`, so the lane does not crash — the defect is process-tree hygiene only.
- **Reproduction (code-level):** a malformed PDF that makes `pdftotext` fork; after `timeout=20`, descendants remain.
- **Impact:** Resource leak; reachable from the CLI, which imports `corpus` at `last30days.py:57`.
- **Fix shape:** Route `corpus.py:292` through `subproc.run_with_timeout`.

### [BUG-042] [CONSENSUS: 1/4 — minimax] Predictable PID-derived temp filename, then `unlink()` and re-create
- **File:** `skills/last30days/scripts/lib/corpus.py:417-423`
- **Feature:** SEC-7 (path handling)
- **Severity/Confidence:** P3 / high
- **Symptom:** Two concurrent corpus writers can delete each other's freshly written temp file, leaving the cache truncated or stale; the temp name `.<name>.<pid>.tmp` is guessable by any local process.
- **Root cause:** `O_EXCL|O_CREAT` is symlink-safe, but the `FileExistsError` recovery path *unlinks whatever is at that path* — which may be another writer's live temp file or a planted symlink — then re-creates it.
- **Reproduction (code-level):** two writers for the same cache path overlapping; the second's `unlink()` removes the first's file mid-write.
- **Impact:** Derived-cache integrity only; user data is not at risk.
- **Fix shape:** Stage into a unique private file and `os.replace` as `private_files.write_private_text` does (`private_files.py:277`), never unlinking an existing path.

### [BUG-043] [CONSENSUS: 1/4 — minimax] `[ORPHAN: backend without UI]` The keyless URL-to-markdown fetch tier is wired to nothing
- **File:** `skills/last30days/scripts/lib/web_fetch_keyless.py:1` (module), `:23`, `:31-32`, `:39`, `:66` (`fetch_markdown`)
- **Feature:** dead code after cutover
- **Severity/Confidence:** P3 / high
- **Symptom:** A complete fetch tier (public-host validation, Jina redirect guard, 30s timeout, 4 MB cap) can never be reached; a page the model needs is reported as "no result" instead of being fetched.
- **Root cause:** Zero inbound references outside `tests/` — the only in-tree mention is a changelog fragment. Its sibling `web_search_keyless` *is* wired (`last30days.py:57,4768`, `reddit_keyless.py:56`). The only `importlib.import_module` sites (`jobs.py:992,3968,3993`) resolve to `lib.job_boards.*`.
- **Reproduction (static, exhaustive scan):** `grep -rn "web_fetch_keyless" --include=*.py --include=*.md --include=*.sh . | grep -v './mcp/' | grep -v './tests/'` returns only `changelog.d/+http-bounds-ssrf-robots-redos-hardening.security.md:6`.
- **Impact:** Maintenance hazard and a missing capability; no runtime loss because the code never runs.
- **Fix shape:** Wire it into the `web_search_keyless` fallback ladder, or remove the module.

### [BUG-044] [CONSENSUS: 1/4 — glm] Every cached/awaited `fetch_once` result is deep-copied twice per caller — O(result size) CPU on the memo hot path
- **File:** `skills/last30days/scripts/lib/fetch_memo.py:122` (publish copy), `fetch_memo.py:99` (hit copy)
- **Feature:** PERF-4 / PERF-5
- **Severity/Confidence:** P3 / high
- **Symptom:** Same call sites as BUG-023 but the *CPU-on-hot-path* framing: for a thin-source run where several streams issue the same search, the same large dict is copied once per waiter plus once per later hit.
- **Root cause:** `copy.deepcopy` on both the publish and read paths; no immutable snapshot. The latent dropped-result path (deepcopy raising before `slot.outcome` is set) is also on these lines.
- **Reproduction (offline):** patch `copy.deepcopy` with a counter; N waiters + M later hits produce N+M+1 whole-payload copies.
- **Impact:** Quadratic-ish copying cost in the number of sharing callers for large item lists.
- **Fix shape:** Same as BUG-023 — publish once and hand out references at `fetch_memo.py:122`.
- *Listed separately from BUG-023 because glm and minimax framed it as two distinct harms (cold-copy cost vs hot-path CPU); the fix is identical and the two can be closed together.*

---

## Cross-lens consensus table

| BUG | Consensus | Lenses that reported it | Severity band |
|---|---|---|---|
| BUG-003 corpus cache `int()` crash | 2/4 | glm, kimi | P2 (disputed) |
| BUG-004 credential query params unmasked | 2/4 | glm, kimi | P2 (disputed) |
| BUG-005 transcript harvest unbounded scan | 2/4 | minimax (filed), kimi (noted at `transcript_harvest.py:117`) | P2 |
| BUG-023/044 `fetch_memo` deepcopy | 2/4 | glm, minimax | P3 |
| BUG-001 planner has no deadline | 2/4 | codex, minimax | P1 (disputed) |

No finding reached 3/4 or 4/4. Every other entry is single-lens (38 of 43). The absence of high-consensus findings is itself informative: the panel's lenses were deliberately assigned disjoint primary areas, so overlap only occurs where a defect crosses layers.

## Disputed entries

1. **BUG-001 — planner-stage deadline.** **[DISPUTED-SEVERITY: P1 (codex) vs P2 (minimax)]**
   - codex: "the run sits in `plan_query` for up to `5×90s + 30s ≈ 480s` before a single source stream is submitted. The slash-command invocation looks hung."
   - minimax: "30s of dead wall-clock per failing request, with no total budget… the claim is about code structure, not network behaviour."
   - Resolution: codex's trigger (a provider that accepts the connection and stalls, before any deadline is plumbed) is the more specific file:line chain and the one a normal user hits; ranked P1 with minimax's general form folded in as evidence.

2. **BUG-003 — corpus cache crash.** **[DISPUTED-SEVERITY: P1 (glm) vs P2 (kimi)]**
   - glm: "Any user with one damaged cache entry loses every run until the cache file is deleted by hand."
   - kimi: classifies it P2, framed as a sanitizer-contract violation on untrusted local data.
   - Resolution: the defect is real and reproduced by both, but it needs a stated precondition (a pre-existing non-numeric `mtime_ns`), which fits the P2 rubric ("realistic hardening gap with a stated precondition") rather than "an ordinary user hits". Ranked P2.

3. **BUG-004 — credential URL masking.** **[DISPUTED-SEVERITY: P2 (kimi) vs P3 (glm)]**
   - kimi: reports the fixed-name regex plus the fixture-write consequence (`http.py:1696`, `http.py:1653`).
   - glm: reports only the `get_text` log line (`http.py:2183`) as P3.
   - Resolution: kimi's variant is strictly broader (stderr *and* on-disk fixture) and is the retained one per the most-specific-variant rule; ranked P2.

4. **BUG-024 — nested `_X_SESSION_STATE` in `config_secret_values`.** **[DISPUTED-SEVERITY: P2 (glm) vs not-a-defect (minimax)]**
   - glm: "`config_secret_values` iterates only top-level items and requires `isinstance(value, str)`… every secret-scanning consumer that trusts `config_secret_values` has a blind spot." glm itself adds: "Fixture recording is a dev path, so this is hardening, not an exploit chain."
   - minimax: "`http.config_secret_values` (`http.py:819-826`) inspects only **top-level** string values… but in this tree the X session values (`AUTH_TOKEN`, `CT0`) are always also present at the top level (`env.py:1294` mapping, `env.py:1620-1622`), so no live leak follows."
   - Resolution: both describe the same code behavior and disagree only on whether a leak follows. Kept as a P3 hardening entry, because no lens demonstrated a live path to an unmasked secret; flagged for the owner to confirm whether the top-level duplication at `env.py:1620-1622` is guaranteed on every code path.

5. **BUG-033 — `bounded_get` suffix host boundary.** **[DISPUTED: P3 defect (kimi) vs "no defect found" (minimax)]**
   - kimi: `_url_matches_policy` "normalizes suffixes without enforcing the boundary"; demonstrated with `evilyoutube.com` vs suffix `youtube.com`; notes "The one shipped policy (`youtube_yt.py:1134-1140`) is dotted, so this is currently latent."
   - minimax: "**No defect found — inspected:** `skills/last30days/scripts/lib/bounded_get.py:112` (`_url_matches_policy` — scheme, host, port, userinfo)".
   - Resolution: minimax's pass did not exercise an undotted suffix; kimi's function-level reproduction is concrete. Kept at P3 and explicitly marked latent.

## Coverage matrix (SEC-1..SEC-7, PERF-1..PERF-6)

All four lenses recorded a bug-find pass over all 13 ids (verified against each report's `## Coverage` section). The matrix shows which pass produced a *filed* finding: ● filed · ○ pass, no defect · ✎ inspected and explicitly noted without filing.

| Area | glm | kimi | codex | minimax | Bugs filed |
|---|---|---|---|---|---|
| SEC-1 credential & secret handling | ● | ● | ○ | ● | BUG-002, BUG-004, BUG-014, BUG-020, BUG-024 |
| SEC-2 secret logging & masking | ● | ● | ○ | ○ ✎ | BUG-004, BUG-024, BUG-027 |
| SEC-3 TLS verification & transport | ● | ● | ○ | ○ | BUG-004, BUG-022, BUG-028, BUG-031, BUG-034, BUG-036, BUG-038 |
| SEC-4 subprocess & injection | ○ | ○ | ○ | ● | BUG-016, BUG-017, BUG-040, BUG-041 |
| SEC-5 URL allowlists / redirect / SSRF | ○ | ● | ○ | ○ ✎ | BUG-006, BUG-032, BUG-033 |
| SEC-6 deserialization & untrusted parsing | ● | ● | ○ | ○ | BUG-003, BUG-009, BUG-015, BUG-025, BUG-030, BUG-034 |
| SEC-7 file / path handling | ○ | ○ | ○ | ● | BUG-042 |
| PERF-1 unbounded concurrency | ○ | ○ | ○ | ○ | none |
| PERF-2 missing / unbounded timeouts | ● | ○ | ● | ● | BUG-001, BUG-005, BUG-014, BUG-036, BUG-037 |
| PERF-3 retry loops & backoff caps | ● | ○ | ● | ● | BUG-001, BUG-007, BUG-026, BUG-035, BUG-038 |
| PERF-4 redundant refetch / cache thrash | ● | ○ | ● | ● | BUG-023, BUG-044 |
| PERF-5 O(n²) joins & aggregation cost | ○ | ○ | ○ | ● | BUG-012, BUG-013 |
| PERF-6 blocking calls on hot paths | ● | ○ | ● | ● | BUG-005, BUG-018, BUG-021, BUG-029 |

## Coverage gap report

**Genuinely clean, confirmed independently by ≥2 lenses (not merely unexamined):**

- **TLS verification (SEC-3):** glm grep-verified no `verify=False`/unverified context anywhere in `lib/`; minimax grep-confirmed `ssl._create_unverified_context`, `CERT_NONE`, `check_hostname=False`, `PYTHONHTTPSVERIFY` → zero matches; the owned-GET child runs `sys.executable -I` so no env override leaks in.
- **Shell/argument injection (SEC-4):** glm, kimi and minimax each ran a repo-wide grep; no `shell=True`, `os.system`, `os.popen`; every invocation is list-argv, and `env.py:559-565` uses `--` as an end-of-options separator. The four SEC-4 bugs are process-tree hygiene, *not* injection.
- **SSRF to private ranges (SEC-5):** no lens found a bypass. Guards cited and re-validated per hop at `http.py:564-600`, `public_host.py:58-100` (embedded-IPv4 handling), `web_search_keyless.py:295-353`, `bounded_get.py:414-417`.
- **Unsafe deserialization (SEC-6):** glm and minimax both ran a repo-wide grep for `pickle`/`marshal`/`yaml.load`/`eval(`/`exec(`/`dill`/`shelve` → zero matches. The SEC-6 bugs are schema/type drift and one numeric coercion, not object injection.
- **Path traversal / symlink (SEC-7):** `private_files.py:185-324`, `library.py:680-760`, `env.py:318-411` (`O_NOFOLLOW`, `S_ISREG`), `corpus.py:238` (`followlinks=False`) all checked clean by glm, kimi and minimax. The one SEC-7 bug is a temp-name race, not traversal.
- **`log.source_log(tty_only=False)` contract:** minimax scanned all 93 call sites → zero violations. `AGENTS.md`'s documented invariant holds.

**Features with zero filed bugs — flagged for follow-up rather than assumed safe:**

- **PERF-1 (unbounded concurrency / pool sizing):** all four lenses inspected and found every pool bounded (`pipeline.py:2230-2239` 16/4, `fanout.py:23,64` 6, `reddit_keyless.py:709-718` 4, `github.py:985` 8, `jobs.py:2423-2429`). This band looks genuinely well-tested. The *related* defects that did surface are shared mutable state across those bounded threads (BUG-019, BUG-020), which is why PERF-1 still matters — but the pool sizing itself is not a bug.
- **PERF-4 duplicate refetching:** glm, kimi and codex all concluded the memo/single-flight design is sound (failure-not-cached semantics, bounded waiter waits in `fetch_memo`, LRU caps in `http.py:2536-2598` and `reddit_arctic.py:38`). The only PERF-4 findings are copy cost, not refetch.
- **`scripts/last30days.py` CLI wiring:** only minimax read the CLI in depth (and glm/kimi cited individual CLI lines). The entrypoint's argument parsing, output-mode dispatch, and exit codes were not a primary lane for any lens. Recommend a dedicated pass.
- **`lib/vendor/` and `mcp/`:** out of scope by instruction; the recent security PRs touched vendor code, so a future pass should confirm.

## False-positive / weak-find rejections

Dropped before publication, with reasons:

1. **`github._fetch_json` return annotation (`github.py:84,106`) — kimi P3.** Annotation says `Optional[Dict[str, Any]]` but list endpoints return JSON arrays. Rejected: annotation-only. Every in-tree caller already narrows with `isinstance(data, list)` (`github.py:628,719,1124`), so no runtime defect exists today; the double defect at the same function (BUG-034, decode) is retained.
2. **`REDDIT_KEYLESS_LIMITER.rate` written outside the lock (`http.py:2318-2322`) — codex P3.** Rejected: the lens itself rated practical impact low ("CPython float store is atomic… at most one token… self-correcting on the next refill pass"). A self-healing sub-token deviation does not meet the "concrete observable wrong behavior" bar.
3. **`x_api._run_handle_lanes` unbounded `wait(FIRST_COMPLETED)` — minimax candidate.** Rejected by minimax itself on verification: every `search_one` carries a `deadline` (`x_api.py:723-731`), so the join is bounded.
4. **Competitor fan-out "main topic dropped" — minimax candidate.** Rejected by minimax on verification: `last30days.py:6193-6216` already writes to stderr and returns 1.
5. **`nomination.name` key collision in `pipeline.enrich_nominations` — minimax candidate.** Rejected by minimax after confirming upstream resolution in `nominate_topic_pool` / `discovery_handoff.resolve_name_collisions`.
6. **Usage-journal charge leak on failed paid requests — codex hypothesis.** Refuted offline: `unknown INTEGER NOT NULL DEFAULT 1` (`usage.py:164-207`) surfaces unfinished rows as `cost_unknown=1`, so accounting is fail-closed. The verified residue (5 rows per retrying POST) is retained inside BUG-007.
7. **`reddit_shreddit.fetch_comments` never-raises violation (`reddit_shreddit.py:234`) — kimi P3.** Not dropped as a false positive, but **merged into BUG-022**: it is a downstream consequence of the unclassified `UnicodeDecodeError` retained at `http.py:1756`. Counted once.
8. **Any "if the network is down" trigger.** Applied as the standing filter: no retained finding's trigger is a third-party outage. BUG-005, BUG-012 and BUG-013 are triggered by input size, not network state; BUG-014 by a hung local keychain, not a remote service; BUG-001 by a provider stall on a *reachable* endpoint with the unbounded code path named.

---

*Report-only. No patches, no diffs, no PR plan. Every entry retains its `file:line` anchor; P0/P1 entries each carry a concrete trigger and an offline reproduction. Findings are deduplicated by `(file, line, root cause)`; the five 2-lens sightings are listed once with both lens names attached.*
