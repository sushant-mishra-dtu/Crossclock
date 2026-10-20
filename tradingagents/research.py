"""Mode dispatcher. Quick returns a brief result; the legacy Standard graph API stays intact."""
from tradingagents.modes import mode_config, research_mode


def run_research(ticker, trade_date, *, config=None, **kwargs):
    """Quick runs its source and model work in spawned processes, which import the
    calling script again: a script that calls this keeps its top-level work under
    ``if __name__ == "__main__":``, or each worker re-runs it instead of its job."""
    config = mode_config(config)
    if research_mode(config) == "quick":
        from tradingagents.quick import run_quick

        return run_quick(ticker, trade_date, config=config, **kwargs)
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(config=config).propagate(ticker, trade_date, **kwargs)
