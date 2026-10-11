# C2 — `mini-ork certify`: one command that says whether a change is actually correct

Depends on C1 (`mini_ork/certify/` engine: `judge`, `Verdict`, `PROVEN/REFUTED/UNVERIFIED`).

## Goal

A developer points one command at a repo and a change, states what the change is
supposed to fix, and gets back a certificate: PROVEN, REFUTED, or UNVERIFIED, with
the evidence. No recipe, no kickoff, no config file. This is the first thing a new
user runs, so zero-setup matters more than breadth: **Python repos only in v1.**

```bash
mini-ork certify --issue "sum() of an empty list returns None instead of 0"
# defaults: --repo . --base HEAD~1 --head HEAD
```

## Mechanism (exact spec)

### `mini_ork/cli/certify.py` (new) — `main(argv=None) -> int`

Flags:

| flag | default | meaning |
|---|---|---|
| `--repo PATH` | `.` | git repo to certify |
| `--base REF` | `HEAD~1` | commit before the change |
| `--head REF` | `HEAD` | commit with the change (ignored if `--diff`) |
| `--diff FILE` | — | use this patch instead of `git diff base head` |
| `--issue TEXT` / `--issue-file FILE` | required (one) | what the change claims to fix |
| `--image IMAGE` | — | use a prebuilt image with the repo at `--workdir` at BASE |
| `--workdir DIR` | `/testbed` | repo location inside the image |
| `--mr-n N` | `4` | invariants to generate |
| `--out FILE` | `.mini-ork/certificates/<id>.json` under `--repo` | certificate path |
| `--json` | off | print the certificate JSON to stdout instead of the summary |

Flow:

1. Resolve `base_sha`/`head_sha` via `git rev-parse`; patch = `git diff --no-color --binary base head`
   (or `--diff` contents). Empty patch → UNVERIFIED "no patch".
2. Runtime image (new `mini_ork/certify/image.py`):
   - `--image` given → use it.
   - else, if the repo has `pyproject.toml`, `setup.py`, or `requirements*.txt` → build
     `mini-ork-certify/<repo-dirname>:<base_sha[:12]>` (skip build if that tag exists):
     `FROM python:3.11-slim`, install `git`, copy `git archive <base_sha>` output to
     `/testbed`, `git init && git add -A && git commit` inside it (Crucible resets with
     `git checkout -- .`), then `pip install -e .` (try `.[test]`, then `.[tests]`, then
     `.`) plus `pip install -r` each `requirements*.txt`, plus `pip install pytest`.
   - else → UNVERIFIED "unsupported project: v1 certifies Python repos; pass --image".
   - no `docker` on PATH or build fails → UNVERIFIED "runtime unavailable: <reason>".
3. `with Crucible(RuntimeSpec(image=img, workdir=workdir)) as c:` →
   `verdict = judge(issue, patch, runner=c, mr_n=mr_n)`.
4. Build and write the certificate (below), print the summary (or JSON), exit.

Exit codes (so CI can gate on it): `0` PROVEN · `1` REFUTED · `2` UNVERIFIED · `64` usage error.

### Certificate (`mini_ork/certify/certificate.py`, new)

`build_certificate(...) -> dict`, JSON with exactly these keys:

```json
{
  "schema": "mini-ork.certificate/v1",
  "id": "<uuid4>",
  "issued_at": "<UTC ISO-8601>",
  "verdict": "PROVEN|REFUTED|UNVERIFIED",
  "reason": "<Verdict.reason>",
  "repo": {"path": "...", "remote": "<origin url or null>", "base": "<sha>", "head": "<sha or null>"},
  "change": {"sha256": "<of patch bytes>", "files": ["..."]},
  "claim": {"sha256": "<of issue text>", "summary": "<first 200 chars>"},
  "method": {"oracle": "probe+delta+invariants", "mr_n": 4, "proven_bar": 0.667, "model": "<MO_CERTIFY_MODEL or llm.DEFAULT_MODEL>", "image": "<tag>"},
  "evidence": {"probe": "<PoC+ source or null>", "mr_pass_rate": null, "invariants": []},
  "cost": {"usd": null, "llm_calls": 0},
  "duration_s": 0.0,
  "digest": "<sha256 of the canonical JSON of every other key>"
}
```

`cost`: call `mini_ork.certify.llm.reset_spend()` before `judge` and read
`mini_ork.certify.llm.spent()` after it → `{"usd": spent["usd"], "llm_calls": spent["calls"]}`.
(The in-process dispatcher reports spend only via its `.cost` sidecar, which `llm.py`
already sums; do NOT query the `llm_calls` table — this path never writes it.) `digest` uses
`json.dumps(obj, sort_keys=True, separators=(",", ":"))` of the certificate minus `digest`.

Summary output (default), e.g.:

```
REFUTED  the fix does not generalise — only 25% of 4 invariants hold
  change  3 files · sha256 1f3a…   claim  "sum() of an empty list returns None…"
  proof   .mini-ork/certificates/<id>.json   cost $0.04 · 71s
```

### Registration

- `mini_ork/cli/main.py`: add `"certify": "mini_ork.cli.certify"` to `_NATIVE_MODULE_SUBS`.
- `tests/unit/test_native_dispatch_py.py`: add `"certify"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively` (it is an exact-set guard and WILL
  fail otherwise).

## Tests — `tests/unit/test_certify_cli.py` (new)

Use a tmp git repo with two commits; monkeypatch `Crucible` with a fake runner and
`judge` with a stub returning a chosen `Verdict`. No docker, no network.

- PROVEN / REFUTED / UNVERIFIED stubs → exit 0 / 1 / 2, certificate written at `--out`.
- certificate has exactly the keys above; `digest` recomputes to the same value.
- `cost` comes from a monkeypatched `mini_ork.certify.llm.spent()` (e.g. usd 0.05, calls 3).
- `--diff FILE` is used instead of `git diff`; empty patch → exit 2 without calling `judge`.
- missing `--issue` → exit 64.
- non-Python repo (no pyproject/setup/requirements) and no `--image` → exit 2, reason
  mentions `--image`, `judge` not called.
- `docker` absent (monkeypatch `shutil.which`) and no `--image` → exit 2 "runtime unavailable".
- `image.py`: the generated Dockerfile text contains `git archive`-sourced `/testbed`,
  `git init`, and `pip install pytest` (assert on the rendered text; do not build).
- `--json` prints parseable JSON equal to the written certificate.

## Files in scope

- mini_ork/cli/certify.py (new)
- mini_ork/certify/image.py, mini_ork/certify/certificate.py (new)
- mini_ork/cli/main.py (ONE line in `_NATIVE_MODULE_SUBS`)
- tests/unit/test_native_dispatch_py.py (ONE entry in the expected set)
- tests/unit/test_certify_cli.py (new)

## Verification commands

- `python3.11 -m pytest tests/unit/test_certify_cli.py tests/unit/test_certify_oracle.py tests/unit/test_native_dispatch_py.py -q` → green
- `python3.11 -m pytest tests/unit -q -x` → no new failures
- `bin/mini-ork certify --help` → prints the flag table, exit 0
- `make lint` → clean

## Done When

`bin/mini-ork certify --issue "..."` inside any Python git repo builds (or reuses) a
runtime image, runs the oracle, writes a `mini-ork.certificate/v1` JSON, prints a
one-screen summary, and exits 0/1/2 by verdict.
