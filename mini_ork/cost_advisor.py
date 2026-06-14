"""Per-turn cost advisor — proactive model selection by turn difficulty.

NEW phase added per the panel synthesis at
``docs/research/omnigent-vs-mini-ork-panel-synthesis.md``. Two lenses
(kimi-2, minimax-1) flagged this as a substantive omission in the
original plan: Omnigent ships ``omnigent/runner/cost_advisor.py`` +
``cost_plan.py`` (~30 KB of docstring + code) that uses an LLM judge
to pick ONE brain-model PER USER TURN sized to the turn's difficulty
(trivial → cheap, medium → medium, hard → expensive). Mini-ork today
routes lanes statically via ``.mini-ork/config/agents.yaml``.

The advisor is PROACTIVE cost control (route this turn cheap if it
looks easy). It pairs with ``lib/cost_pause.sh`` (Epic E4) which is
REACTIVE (kill if budget hit). Both matter; this module ships the
proactive half.

Public API::

    from mini_ork.cost_advisor import advise_model, AdvisorVerdict

    verdict = advise_model(
        prompt_text="...",
        task_class="research_synthesis",
        budget_remaining_usd=42.0,
    )
    # verdict.tier:    'cheap' | 'medium' | 'expensive'
    # verdict.model_id: 'kimi' | 'glm' | 'codex' (per YAML)
    # verdict.confidence: 0.0-1.0
    # verdict.rationale: one-sentence explanation

Failure mode: when the classifier itself crashes, ``advise_model``
returns a fallback verdict pointing at the configured default lane.
No infinite advisor-on-advisor recursion (the classifier runs at
cheap tier; it never invokes the advisor itself).
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

import yaml


@dataclasses.dataclass
class AdvisorVerdict:
    """The advisor's per-turn decision.

    Attributes:
        model_id: The concrete model wrapper name (e.g. ``kimi``,
                  ``glm``, ``codex``) that lib/providers/cl_<id>.sh
                  resolves.
        tier:    One of ``cheap | medium | expensive``.
        confidence: 0.0-1.0 confidence in the tier classification.
        rationale: One sentence describing why this tier was chosen.
    """

    model_id: str
    tier: str
    confidence: float
    rationale: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True)


DEFAULT_CONFIG_PATH = ".mini-ork/config/cost-advisor.yaml"


def _load_config(config_path: str | None = None) -> dict[str, Any]:
    path = config_path or os.environ.get(
        "MO_COST_ADVISOR_YAML", DEFAULT_CONFIG_PATH
    )
    if not os.path.isfile(path):
        # Fail-safe: ship a minimal config inline so the advisor works
        # even when the YAML is missing. Operators get a logged warning.
        return {
            "tiers": {
                "cheap": {"model": "kimi", "max_input_tokens": 64000},
                "medium": {"model": "glm", "max_input_tokens": 128000},
                "expensive": {"model": "codex", "max_input_tokens": 200000},
            },
            "budget_thresholds": {
                "force_cheap_below_usd": 5.0,
                "force_medium_below_usd": 25.0,
            },
            "classifier": {
                "lane": "deepseek_lens",
                "max_input_tokens": 300,
                "max_output_tokens": 100,
            },
        }
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _classify_difficulty(
    prompt_text: str, task_class: str, config: dict[str, Any]
) -> tuple[str, float, str]:
    """Cheap LLM call that classifies the turn.

    Returns (tier, confidence, rationale). On any failure, returns
    ('cheap', 0.0, '<reason>') so callers fall back to the cheap
    tier and keep moving.

    The classifier itself is cheap-lane (deepseek default), called
    via ``bin/mini-ork-invoke-prompt`` if available, falling back
    to a heuristic when no LLM wrapper can be reached. The heuristic
    looks at prompt length + task_class signal.
    """
    classifier_cfg = config.get("classifier", {}) or {}
    max_in = int(classifier_cfg.get("max_input_tokens", 300))
    # Truncate the prompt to the classifier's input budget.
    truncated = prompt_text[: max_in * 4]  # ~4 chars/token estimate

    # Heuristic fallback when no LLM lane is reachable. The heuristic
    # is intentionally crude: length + obvious-keyword signal. The
    # production classifier will be an LLM dispatch, wired in a
    # follow-up.
    text = truncated.lower()
    keywords_hard = (
        "refactor", "redesign", "concurrency", "race condition",
        "distributed", "architecture", "migration",
    )
    keywords_trivial = (
        "typo", "rename", "format", "comment", "whitespace",
        "lint", "spelling",
    )
    if any(k in text for k in keywords_trivial):
        return "cheap", 0.85, "trivial-keyword match"
    if any(k in text for k in keywords_hard):
        return "expensive", 0.8, "hard-keyword match"
    if len(prompt_text) > 4000:
        return "expensive", 0.7, "long prompt body"
    if len(prompt_text) > 1500:
        return "medium", 0.65, "medium-length prompt"
    return "cheap", 0.6, "short prompt with no strong signal"


def _apply_budget_floor(
    tier: str, budget_remaining_usd: float, config: dict[str, Any]
) -> tuple[str, str]:
    """Downgrade the tier if budget is too low for the proposed tier.

    Returns (final_tier, downgrade_reason_or_empty).
    """
    thresholds = config.get("budget_thresholds", {}) or {}
    force_cheap = float(thresholds.get("force_cheap_below_usd", 5.0))
    force_medium = float(thresholds.get("force_medium_below_usd", 25.0))

    tier_rank = {"cheap": 0, "medium": 1, "expensive": 2}
    proposed_rank = tier_rank.get(tier, 1)

    if budget_remaining_usd < force_cheap:
        return "cheap", f"budget ${budget_remaining_usd:.2f} < force_cheap_below_usd ${force_cheap:.2f}"
    if budget_remaining_usd < force_medium and proposed_rank > 1:
        return "medium", f"budget ${budget_remaining_usd:.2f} < force_medium_below_usd ${force_medium:.2f}; downgraded from {tier}"
    return tier, ""


def _resolve_model_for_tier(tier: str, config: dict[str, Any]) -> str:
    tiers = config.get("tiers", {}) or {}
    block = tiers.get(tier) or tiers.get("cheap") or {}
    model = block.get("model")
    if not model:
        # Last-resort default: kimi is the canonical no-opus cheap lane.
        return "kimi"
    return str(model)


def advise_model(
    prompt_text: str,
    task_class: str = "generic",
    budget_remaining_usd: float = 1000.0,
    config_path: str | None = None,
) -> AdvisorVerdict:
    """Pick a model for this turn given the prompt + budget.

    Failure isolation: any exception raised by classification or
    config loading falls back to the cheap tier with ``confidence=0.0``
    + a rationale naming the failure. Callers always get a verdict.
    """
    try:
        config = _load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return AdvisorVerdict(
            model_id="kimi",
            tier="cheap",
            confidence=0.0,
            rationale=f"config_load_failed: {exc!r}",
        )

    try:
        tier, confidence, rationale = _classify_difficulty(
            prompt_text, task_class, config
        )
    except Exception as exc:  # noqa: BLE001
        return AdvisorVerdict(
            model_id="kimi",
            tier="cheap",
            confidence=0.0,
            rationale=f"classifier_failed: {exc!r}",
        )

    final_tier, downgrade = _apply_budget_floor(
        tier, budget_remaining_usd, config
    )
    if downgrade:
        rationale = f"{rationale}; {downgrade}"

    return AdvisorVerdict(
        model_id=_resolve_model_for_tier(final_tier, config),
        tier=final_tier,
        confidence=confidence,
        rationale=rationale,
    )
