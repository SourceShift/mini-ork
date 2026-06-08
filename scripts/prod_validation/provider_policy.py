"""Provider policy setup for isolated scenario projects."""

from __future__ import annotations

from pathlib import Path


class ProviderPolicy:
    name = "default"

    def apply(self, home: Path) -> None:
        return None


class CodexOnlyPolicy(ProviderPolicy):
    name = "codex-only"

    def apply(self, home: Path) -> None:
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agents.yaml").write_text(
            """lanes:
  planner: codex
  researcher: codex
  implementer: codex
  worker: codex
  reviewer: codex
  verifier: codex
  reflector: codex
  publisher: codex
  rollback: codex
  decomposer: codex
  spec_author: codex
  spec_reviewer: codex
  bdd_runner: codex
  healer: codex
  brain: codex
  worker_default: codex
  reviewer_default: codex
  glm_lens: codex
  kimi_lens: codex
  codex_lens: codex
  opus_lens: codex
  minimax_lens: codex
budget:
  per_epic_usd: 5.00
  per_run_usd: 0.50
  daily_cap_usd: 50.00
""",
            encoding="utf-8",
        )


def provider_policy(name: str) -> ProviderPolicy:
    policies: dict[str, ProviderPolicy] = {
        "default": ProviderPolicy(),
        "codex-only": CodexOnlyPolicy(),
    }
    try:
        return policies[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider policy: {name}") from exc

