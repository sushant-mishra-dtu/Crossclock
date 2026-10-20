"""Validated product modes; model tiers and discussion depth are separate controls."""
from __future__ import annotations

from copy import deepcopy

QUICK_POLICY = "quick-v1"
QUICK_BUDGET = {
    "deadline_seconds": 60, "attempt_timeout_seconds": 40,
    "retry_backoff_seconds": [], "max_retry_after_seconds": 1, "min_attempt_seconds": 1,
}


def research_mode(config: dict | None = None) -> str:
    mode = (config or {}).get("research_mode", "standard")
    if mode == "deep":
        raise ValueError("Deep mode is planned; targeted document retrieval is not available yet.")
    if not isinstance(mode, str) or mode not in ("standard", "quick"):
        raise ValueError(f"Unknown research mode {mode!r}; choose standard or quick.")
    return mode


def require_standard(config: dict | None = None) -> None:
    if research_mode(config) != "standard":
        raise ValueError("This decision workflow requires Standard mode; use run_research for a Quick brief.")


def require_decision_state(state: dict) -> None:
    if ("research_mode" in state and state["research_mode"] != "standard") or state.get("decision_eligible") is False:
        raise ValueError("Quick research cannot be saved as an investment decision.")


def mode_config(config: dict | None = None) -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    result = deepcopy({**DEFAULT_CONFIG, **(config or {})})
    mode = research_mode(result)
    if mode == "quick":
        from tradingagents.llm_clients.factory import _coerce_max_tokens

        cap = result.get("max_tokens")
        cap = _coerce_max_tokens(cap) if cap is not None and cap != "" else 1200
        result.update({
            "research_mode": mode, "quick_policy": QUICK_POLICY, "checkpoint_enabled": False,
            "llm_max_retries": 0,
            "max_tokens": min(cap, 1200),
        })
        result.setdefault("run_budgets", {})["quick"] = deepcopy(QUICK_BUDGET)
    return result


def mode_view(config: dict) -> dict:
    """Persisted/pre-run scope. Unknown cost and ETA remain unknown."""
    from tradingagents.llm_clients.factory import tier_provider
    from tradingagents.run_control import RunBudget

    config = mode_config(config)
    mode = research_mode(config)
    budget = RunBudget.from_config(config)
    return {
        "mode": mode, "outputKind": "quick" if mode == "quick" else "report",
        "policy": config.get("quick_policy") if mode == "quick" else "standard",
        "deadlineSeconds": budget.deadline_seconds, "attemptTimeoutSeconds": budget.attempt_timeout_seconds,
        "provider": tier_provider(config, "quick"), "model": config["quick_think_llm"],
        "cost": None, "eta": None,
        "scope": ("Local point-in-time facts, 30-day prices, up to 8 dated headlines from 7 days; one synthesis."
                  if mode == "quick" else "Selected specialists, research and risk discussion, investment decision."),
        "exclusions": ("No social feeds, remote financial statements, filing retrieval, decision memory, "
                       "trade proposal or rating. No checkpoint resume. Qualitative source support is not checked."
                       if mode == "quick" else ""),
    }
