"""Behavioral verification package.

A behavioral verifier exercises an *observable* surface (a staging API, a UI
form, a multi-step journey) live and returns an execution-anchored,
three-valued verdict (PROVEN / REFUTED / UNVERIFIED). P0 ships the ``api``
surface; ``ui`` (agentbrowser) and ``journey`` register their handlers in
later phases via :func:`register_surface_handler`.

Public surface re-exported from :mod:`mini_ork.verify.behavioral`, plus the
P3 catalog/committee/reward extensions.
"""
from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    BehavioralVerdict,
    Check,
    HttpResult,
    UiResult,
    Observable,
    ObservableError,
    Requester,
    UiDriver,
    get_surface_handler,
    main,
    observable_from_env,
    register_surface_handler,
    run,
    run_api_check,
    run_journey_check,
    run_ui_check,
)
from mini_ork.verify.catalog import (
    VerifierCard,
    VerifierStats,
    card_score,
    load_cards,
    rank_verifiers,
)
from mini_ork.verify.committee import committee_vote, pairwise_agreement
from mini_ork.verify.reward import (
    default_reward_path,
    record_reward,
    verdict_reward,
)

__all__ = [
    "PROVEN",
    "REFUTED",
    "UNVERIFIED",
    "BehavioralVerdict",
    "Check",
    "HttpResult",
    "UiResult",
    "Observable",
    "ObservableError",
    "Requester",
    "UiDriver",
    "get_surface_handler",
    "main",
    "observable_from_env",
    "register_surface_handler",
    "run",
    "run_api_check",
    "run_journey_check",
    "run_ui_check",
    "VerifierCard",
    "VerifierStats",
    "card_score",
    "load_cards",
    "rank_verifiers",
    "committee_vote",
    "pairwise_agreement",
    "verdict_reward",
    "record_reward",
    "default_reward_path",
]