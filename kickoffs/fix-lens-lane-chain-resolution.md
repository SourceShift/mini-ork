# Fix: resolve lens-lane aliases to their family before building the dispatch fallback chain

## Problem (one sentence)

A node pinned to a `*_lens` lane (e.g. `codex_lens`) silently dispatches to
`minimax` instead of its intended family (`codex`), because the fallback-chain
**lead** is the unresolved agents.yaml alias, which fails dispatch preflight and
lets the `MO_FALLBACK_CODING` head decide who serves.

## Root cause (verified in the live tree)

- `mini_ork/cli/execute.py:1944` builds the chain from the raw workflow lane:
  `apply_env_overrides({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, lane)})`.
  The comment two lines above already *claims* "lead = resolved lane" — but
  `lane` is the **alias** (`codex_lens`), never resolved to its family.
- `mini_ork/dispatch/routing.py:20` `dispatch_chain` prepends that alias to
  `MO_FALLBACK_CODING` (default `"minimax,codex,sonnet"`) →
  `codex_lens,minimax,codex,sonnet`.
- Dispatch preflight (`providers.py` `_resolve_from_registry`) keys on
  **providers.yaml** model names. `codex_lens` is NOT a providers.yaml key →
  `unknown lane` → the chain falls to the next entry: **`minimax` serves**.
- Proof: a real framework-edit run's `lens-prior_art.md.stdout.md` shows
  `lane codex_lens failed (rc=2 ... unknown lane: 'codex_lens') ... served by minimax`.

The agents.yaml `lanes.<alias>` mapping is therefore **inert** for coding-role
lens nodes today — the entire heterogeneous-lens design collapses onto the
`MO_FALLBACK_CODING` head.

## Fix (exact, critic-specified — apply verbatim)

### 1. `mini_ork/dispatch/llm_dispatch.py` — add a lane-alias→family resolver

Insert this new function immediately AFTER the existing `resolve_lane_model`
function (which ends just before `def cost_circuit_open`). It mirrors
`resolve_lane_model`'s agents.yaml load, but resolves an arbitrary **alias**
(not `node_type`):

```python
def resolve_lane_family(lane: str, root: str = "", home: str = "") -> str:
    """Resolve an agents.yaml lane ALIAS (e.g. 'codex_lens', 'decomposer') to its
    family model via lanes.<alias>. A plain model name or unknown alias passes
    through unchanged. Fail-open: any error returns the input lane verbatim.

    Dispatch preflight keys on providers.yaml model names; '*_lens' aliases are
    NOT providers.yaml keys, so an unresolved alias used as the fallback-chain
    lead fails preflight and the MO_FALLBACK_* tail silently decides who serves
    (the codex_lens->minimax bug). Resolving here makes the alias lead with its
    real family model, keeping the tail as a genuine fallback."""
    if not lane:
        return lane
    root = root or os.environ.get("MINI_ORK_ROOT", "")
    home = home or os.environ.get("MINI_ORK_HOME", "")
    agents = os.path.join(home, "config", "agents.yaml")
    if not os.path.isfile(agents):
        agents = os.path.join(root, "config", "agents.yaml")
    if not os.path.isfile(agents):
        return lane
    try:
        import yaml
        d = yaml.safe_load(open(agents)) or {}
        lanes = d.get("lanes", {}) or {}
        return lanes.get(lane) or lane
    except Exception:
        return lane
```

### 2. `mini_ork/cli/execute.py` — resolve the chain lead (~line 1944)

Replace this single line:

```python
    apply_env_overrides({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, lane)})
```

with:

```python
    from mini_ork.dispatch.llm_dispatch import resolve_lane_family
    _chain_lead = resolve_lane_family(lane)
    apply_env_overrides({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, _chain_lead)})
```

Do NOT change `lane` itself — it is still used verbatim for `trace(... lane=lane)`
and `_assert_lane_capability(root, lane, ...)`. Only the chain **lead** resolves.

### 3. `tests/unit/test_lane_chain_resolution.py` — NEW regression + ratchet

```python
import textwrap

from mini_ork.dispatch.llm_dispatch import resolve_lane_family
from mini_ork.dispatch.routing import dispatch_chain


def _write_agents(tmp_path):
    home = tmp_path / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(textwrap.dedent("""
        lanes:
          implementer: codex
          codex_lens: codex
          kimi_lens: kimi
          opus_lens: opus
    """))
    return str(home)


def test_lens_alias_resolves_to_family(tmp_path, monkeypatch):
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    assert resolve_lane_family("codex_lens") == "codex"
    assert resolve_lane_family("kimi_lens") == "kimi"


def test_plain_and_unknown_pass_through(tmp_path, monkeypatch):
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    assert resolve_lane_family("codex") == "codex"       # plain model name
    assert resolve_lane_family("nonesuch") == "nonesuch"  # unknown alias fails open


def test_missing_agents_yaml_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "nope"))
    assert resolve_lane_family("codex_lens") == "codex_lens"


def test_chain_lead_is_family_not_alias(tmp_path, monkeypatch):
    # RATCHET: the exact bug — codex_lens must lead the chain with codex,
    # BEFORE the MO_FALLBACK_CODING head (minimax).
    home = _write_agents(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_ROOT", home)
    monkeypatch.delenv("MO_FALLBACK_CODING", raising=False)  # default: minimax,codex,sonnet
    chain = dispatch_chain("implementer", resolve_lane_family("codex_lens"))
    parts = chain.split(",")
    assert parts[0] == "codex", f"chain lead must be codex, got {parts[0]!r} in {chain!r}"
    assert parts.index("codex") < parts.index("minimax")
```

## Files in scope (exhaustive — touch nothing else)

- `mini_ork/dispatch/llm_dispatch.py` — ADD `resolve_lane_family` (per §1).
- `mini_ork/cli/execute.py` — resolve the chain lead at ~line 1944 (per §2).
- `tests/unit/test_lane_chain_resolution.py` — NEW file (per §3).

## Explicitly OUT of scope

- Do NOT change `routing.py` `MO_FALLBACK_*` defaults.
- Do NOT change `dispatch_with_fallback` or `_resolve_from_registry`.
- Do NOT edit `.mini-ork/config/agents.yaml` or any providers.yaml.
- Do NOT rename `lane` or alter the trace/capability call sites.

## Acceptance / verification

- `python3.11 -m pytest tests/unit/test_lane_chain_resolution.py -q` → all green.
- `python3.11 -m pytest tests/unit/test_opencode_engine.py tests/unit -k dispatch -q`
  → no regressions in existing dispatch tests.
- Manual: `python3.11 -c "from mini_ork.dispatch.routing import dispatch_chain; from mini_ork.dispatch.llm_dispatch import resolve_lane_family; print(dispatch_chain('implementer', resolve_lane_family('codex_lens')))"`
  from a home whose agents.yaml maps `codex_lens: codex` → prints a chain whose
  first entry is `codex`.

## Notes

This is a mini-ork **self-edit** (the framework edits its own dispatch core);
the run sets `MO_ALLOW_FRAMEWORK_CWD=1`. The change is additive + fail-open, so a
missing/broken agents.yaml degrades to today's behavior (alias passthrough).
