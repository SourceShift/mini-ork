# Fallback guard — the "no silent fallback" rule, as a machine

`recipes/code-fix/prompts/implementer.md` forbids *silent exception swallowing*
and *returning a default on error*. Until now that rule reached an implementer
as prose and a reviewer as an instruction, which means it was enforced at the
strength of the weakest reviewer lane: an approval and an oversight produce the
same verdict. The fallback guard is the deterministic half of the rule.

## What it catches

| Shape | Why it is a defect |
|---|---|
| `except: pass` | the failure is erased; the caller cannot tell success from silent failure |
| `except Exception: pass` | same, with a type that claims more knowledge than it has |
| `except Exception as exc: return None` | `None` is indistinguishable from "nothing found" |
| `except Exception: return` | same, and callers that ignore the return value never know |

Deliberately **not** flagged: a named handler (`except OSError: pass`) — that is
a judgement about a specific failure — and any handler that records, re-raises
or returns a documented sentinel.

## Allowing a silence on purpose

Some failures really are safe to ignore. Say so in the code:

```python
except Exception:
    # fallback-guard: allow: the atom cache is an optimisation; a failed
    # write must never break context assembly.
    pass
```

The marker is read from the handler block (any line between `except` and the end
of the body). The annotation is reviewed like code — which is the point: silence
becomes a decision someone wrote down.

## Two tiers

**Simple / local** — no server, no config, stdlib only:

```bash
mini-ork fallbacks                  # changed .py files in this working tree
mini-ork fallbacks src/x.py         # exactly these files
mini-ork fallbacks --all            # every .py in the repo (see the ratchet note)
mini-ork fallbacks --json           # machine-readable
```

Exit 0 clean, 1 on findings, 2 on usage error. `--all` on today's tree reports
several hundred pre-existing handlers: it is a ratchet target, not a gate.

**Secure / CI** — the `fallbacks` verifier node in `recipes/code-fix`:

```yaml
- name: fallbacks
  type: verifier
  verifier_ref: verifiers/fallback_guard.py
```

It scans exactly the files the run touched (`mini_ork/verify/touched_files.py`,
shared with `verifiers/typecheck.py`) and fails the run when a patch *adds* a
swallow. A pre-existing one elsewhere is not this patch's failure and must not
redden the lane.

The verifier abstains — `status: "unverified"`, `gating: false` — when the
change surface cannot be attributed (no git work tree, no run dir). An
unexamined patch is recorded as unmeasured, never as a pass.

## Kickoff invariants

The same principle applied one level up: `mini-ork validate` now warns when a
`Done When` / `Definition of Done` / `Acceptance` section has no numbered
criteria. "Done When: the feature works" cannot be bound to a gate, so the
verifier node falls back to a reviewer's opinion.

```bash
mini-ork validate kickoffs/my-thing.md
#   [warning] kickoff … has a Done-When section with no numbered criteria
#             Fix: list each acceptance criterion as an observable, numbered item
```

`MO_KICKOFF_INVARIANTS` sets the strength: `off`, `warn` (default), `error`.
Warn first by design — `recipes/*/example-kickoff.md` and in-flight kickoffs
predate the rule. Promote to `error` once the shipped examples are migrated.

## Why not just trust the reviewer

A reviewer reads the diff and says "looks fine". The guard walks the AST and
says "line 320 swallows". The second is reproducible, costs no tokens, and does
not improve its opinion under budget pressure.
