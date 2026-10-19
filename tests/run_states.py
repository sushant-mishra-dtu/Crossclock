"""Run states for tests that drive a run to its end with a fake graph.

A run counts as complete only when every stage its analysts need wrote
something (tradingagents.completion), so a fake that finishes a run must fill
them all; these are the stages after a market-analyst-only run's analyst.
"""

LATER_STAGES = {
    "investment_debate_state": {"bull_history": "Bull Analyst: Bull case",
                                "bear_history": "Bear Analyst: Bear case"},
    "investment_plan": "Go long",
    "trader_investment_plan": "Buy 10 units",
    "risk_debate_state": {"aggressive_history": "Aggressive Analyst: More",
                          "conservative_history": "Conservative Analyst: Less",
                          "neutral_history": "Neutral Analyst: Hold some"},
}


def complete_state(**fields) -> dict:
    """A market-only run's finished state, with ``fields`` added or replaced."""
    return {"market_report": "M", **LATER_STAGES, **fields}
