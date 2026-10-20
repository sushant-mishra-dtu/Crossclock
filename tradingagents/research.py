"""Mode dispatcher. Quick returns a brief result; the legacy Standard graph API stays intact."""
from tradingagents.modes import mode_config, research_mode


def run_research(ticker, trade_date, *, config=None, **kwargs):
    config = mode_config(config)
    if research_mode(config) == "quick":
        from tradingagents.quick import run_quick

        return run_quick(ticker, trade_date, config=config, **kwargs)
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(config=config).propagate(ticker, trade_date, **kwargs)
