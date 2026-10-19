"""What a run finished, checked and stopped on (docs/adr/0001).

Every run, finished or not, gets a completion record. A run is complete only
when every stage its analysts need wrote something and nothing interrupted it;
a provider failure, a deadline or a Stop leaves a partial record, never one
that reads like finished research. Optional checks that failed or could not
run are listed as gaps, so a decision without its claim check says so.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from tradingagents.llm_clients.errors import provider_error_class
from tradingagents.run_control import (
    CHECK_DISABLED,
    CHECK_FAILED,
    CHECK_RAN,
    CHECK_SKIPPED,
    CHECK_UNAVAILABLE,
    GAP_STATUSES,
    DeadlineExceeded,
    RunCancelled,
)

COMPLETE, COMPLETE_WITH_GAPS, PARTIAL, NONE = "complete", "complete_with_gaps", "partial", "none"
STOP_CANCELLED, STOP_DEADLINE, STOP_PROVIDER, STOP_ERROR, STOP_MISSING = (
    "cancelled", "deadline", "provider_error", "error", "missing_output")

# The stages every run has after its analysts, in pipeline order.
_LATER_STAGES = (
    ("Bull Researcher", ("investment_debate_state", "bull_history")),
    ("Bear Researcher", ("investment_debate_state", "bear_history")),
    ("Research Manager", ("investment_plan",)),
    ("Trader", ("trader_investment_plan",)),
    ("Aggressive Analyst", ("risk_debate_state", "aggressive_history")),
    ("Conservative Analyst", ("risk_debate_state", "conservative_history")),
    ("Neutral Analyst", ("risk_debate_state", "neutral_history")),
    ("Portfolio Manager", ("final_trade_decision",)),
)

# A debater's turn is filed as "<Side> Analyst: <text>", so a turn whose model
# answered nothing still leaves its label behind.
_TURN_LABEL = re.compile(r"(?m)^\s*(?:Bull|Bear|Aggressive|Conservative|Neutral) Analyst:")

_CHECK_NAMES = {
    "claim_check": "Claim check",
    "sentiment_judgments": "Jev sentiment judgments",
    "debate_judgments": "Jev debate judgments",
    "memory": "Memory log",
}
_CHECK_STATUS_TEXT = {
    CHECK_RAN: "ran",
    CHECK_FAILED: "failed to run",
    CHECK_UNAVAILABLE: "not available (Jev is not set up)",
    CHECK_DISABLED: "turned off",
    CHECK_SKIPPED: "had nothing to check",
}
_ERROR_TEXT = {
    "timeout": "a timeout", "rate_limit": "a rate limit", "server": "a server error",
    "connection": "a connection failure", "auth": "a rejected key or quota",
}


class IncompleteResearch(RuntimeError):
    """The graph finished, but a stage it needs wrote nothing; no decision was recorded."""

    def __init__(self, completion: dict):
        self.completion = completion
        super().__init__(describe_stop(completion))


def required_stages(analysts: Iterable[str]) -> list[tuple[str, tuple[str, ...]]]:
    """(stage, state path) for every stage a run with these analysts must finish."""
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

    keys = list(analysts)
    analyst_stages = [(ANALYST_NODE_SPECS[key].agent_node, (ANALYST_NODE_SPECS[key].report_key,))
                      for key in ANALYST_NODE_SPECS if key in keys]
    return analyst_stages + list(_LATER_STAGES)


def _lookup(state: Mapping, path: tuple[str, ...]):
    value = state
    for key in path:
        value = (value or {}).get(key) if isinstance(value, Mapping) or value is None else None
    return value


def _has_text(value) -> bool:
    return isinstance(value, str) and bool(_TURN_LABEL.sub("", value).strip())


def stop_reason_for(exc: BaseException) -> str:
    """Why a run that raised ``exc`` stopped."""
    if isinstance(exc, RunCancelled):
        return STOP_CANCELLED
    if isinstance(exc, DeadlineExceeded):
        return STOP_DEADLINE
    if provider_error_class(exc) is not None:
        return STOP_PROVIDER
    return STOP_ERROR


def provider_failure_for(exc: BaseException | None, noted: dict | None = None) -> dict | None:
    """The provider failure a run stopped on: what its retry loop noted, else the error's kind."""
    if noted:
        return dict(noted)
    kind = provider_error_class(exc) if exc is not None else None
    if kind is None:
        return None
    return {"provider": None, "model": None, "error_class": kind, "attempts": None, "waited_seconds": None}


def run_completion(final_state: Mapping, analysts: Iterable[str], *, stop_reason: str | None = None,
                   provider_failure: dict | None = None, active_stages: Iterable[str] = (),
                   reused_stages: Iterable[str] = (), deadline_seconds: float | None = None) -> dict:
    """The completion record of a run that ended with ``final_state``.

    ``stop_reason`` is None for a run that reached the end of its graph; a
    finished graph that left a required stage empty is still not complete.
    ``active_stages`` are the stages that were running when it stopped.
    """
    reused = set(reused_stages)
    stages = {}
    for stage, path in required_stages(analysts):
        if _has_text(_lookup(final_state, path)):
            stages[stage] = "reused" if stage in reused else "done"
        else:
            stages[stage] = "missing"
    missing = [stage for stage, status in stages.items() if status == "missing"]
    if stop_reason is None and missing:
        stop_reason = STOP_MISSING

    checks = dict(final_state.get("run_checks") or {})
    if final_state.get("memory_note"):
        checks["memory"] = CHECK_FAILED
    elif "past_context" in final_state:
        checks["memory"] = CHECK_RAN
    gaps = sorted(name for name, status in checks.items() if status in GAP_STATUSES)

    if stop_reason is not None:
        state = PARTIAL if len(missing) < len(stages) else NONE
    else:
        state = COMPLETE_WITH_GAPS if gaps else COMPLETE
    return {
        "state": state,
        "stop_reason": stop_reason,
        "stages": stages,
        "missing": missing,
        "active_stages": [s for s in active_stages if stages.get(s) == "missing"] if stop_reason else [],
        "checks": checks,
        "gaps": gaps,
        "provider_failure": provider_failure if stop_reason in (STOP_PROVIDER, STOP_DEADLINE, STOP_CANCELLED) else None,
        "deadline_seconds": deadline_seconds,
    }


def is_complete(completion: Mapping | None) -> bool:
    return bool(completion) and completion.get("state") in (COMPLETE, COMPLETE_WITH_GAPS)


def describe_stop(completion: Mapping) -> str:
    """One sentence on why a run stopped and where."""
    reason = completion.get("stop_reason")
    failure = completion.get("provider_failure") or {}
    if reason == STOP_CANCELLED:
        text = "Stopped by the user"
    elif reason == STOP_DEADLINE:
        seconds = completion.get("deadline_seconds")
        text = f"Stopped at its {seconds:,.0f} s deadline" if seconds else "Stopped at its deadline"
    elif reason == STOP_PROVIDER:
        text = "Stopped by a provider failure"
    elif reason == STOP_MISSING:
        return "Finished without output from " + ", ".join(completion.get("missing") or []) + "."
    else:
        text = "Stopped by an error"
    if failure.get("error_class"):
        source = " ".join(x for x in (failure.get("provider"), failure.get("model")) if x)
        attempts = failure.get("attempts")
        text += f" ({_ERROR_TEXT.get(failure['error_class'], failure['error_class'])}"
        text += f" from {source}" if source else ""
        text += f" after {attempts} attempt{'s' if attempts != 1 else ''}" if attempts else ""
        text += ")"
    active = completion.get("active_stages") or []
    if active:
        text += " while " + ", ".join(active) + (" was" if len(active) == 1 else " were") + " running"
    return text + "."


def describe_gaps(completion: Mapping) -> list[str]:
    """A line per optional check that failed or could not run."""
    lines = []
    for name in completion.get("gaps") or []:
        status = (completion.get("checks") or {}).get(name)
        line = f"{_CHECK_NAMES.get(name, name)} {_CHECK_STATUS_TEXT.get(status, status)}"
        if name == "claim_check":
            line += "; the decision's claims are unchecked"
        lines.append(line)
    return lines


def status_line(completion: Mapping) -> str:
    """The report header's run-status line."""
    if completion.get("state") == COMPLETE:
        return "complete"
    if completion.get("state") == COMPLETE_WITH_GAPS:
        return "complete with gaps: " + "; ".join(describe_gaps(completion))
    return (f"PARTIAL. {describe_stop(completion)} "
            "This is incomplete research with no rating; it is not a decision to act on.")
