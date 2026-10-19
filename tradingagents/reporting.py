"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

import json
from datetime import datetime
from pathlib import Path

from tradingagents.agents.rating import run_rating
from tradingagents.completion import is_complete, status_line
from tradingagents.dataflows.symbols import safe_ticker_component


def analyst_names(keys) -> list[str]:
    """The analysts as users select them: the sentiment analyst's key is "social"."""
    return ["sentiment" if key == "social" else key for key in keys or []]


def _header(ticker: str, final_state: dict, settings: dict | None, completion: dict | None = None) -> str:
    """The report's title and what produced it: analysis date, version, models, analysts, vendors."""
    partial = completion is not None and not is_complete(completion)
    title = "Partial Research Report (incomplete)" if partial else "Trading Analysis Report"
    lines = [f"# {title}: {ticker}", ""]
    if completion is not None:
        lines.append(f"- Run status: {status_line(completion)}")
    if final_state.get("trade_date"):
        lines.append(f"- Analysis date: {final_state['trade_date']}")
    if final_state.get("final_trade_decision") and not partial:
        lines.append(f"- Rating: {run_rating(final_state)}")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        deep_provider = s("deep_think_provider") or s("llm_provider", "?")
        quick_provider = s("quick_think_provider") or s("llm_provider", "?")
        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {s('deep_think_llm', '?')}, quick {s('quick_think_llm', '?')}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {s('deep_think_llm', '?')}, "
                      f"quick {quick_provider} {s('quick_think_llm', '?')}")
        lines.append(f"- TradingAgents {s('version', '?')}: {models}")
        lines.append(f"- Analysts: {', '.join(analyst_names(s('analysts')))}; "
                     f"research debate rounds {s('max_debate_rounds', '?')}, "
                     f"risk debate rounds {s('max_risk_discuss_rounds', '?')}")
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            lines.append("- Data vendors: " + ", ".join(f"{k} {v}" for k, v in vendors.items()))
    if final_state.get("memory_note"):
        lines.append(f"- Memory log: {final_state['memory_note']}")
    return "\n".join(lines) + "\n\n"


# The report's sections in order: (heading, folder, [(agent, file, state path)]).
_SECTIONS = (
    ("I. Analyst Team Reports", "1_analysts", (
        ("Market Analyst", "market.md", ("market_report",)),
        ("Sentiment Analyst", "sentiment.md", ("sentiment_report",)),
        ("News Analyst", "news.md", ("news_report",)),
        ("Fundamentals Analyst", "fundamentals.md", ("fundamentals_report",)),
    )),
    ("II. Research Team Decision", "2_research", (
        ("Bull Researcher", "bull.md", ("investment_debate_state", "bull_history")),
        ("Bear Researcher", "bear.md", ("investment_debate_state", "bear_history")),
        ("Research Manager", "manager.md", ("investment_plan",)),
    )),
    ("III. Trading Team Plan", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Risk Management Team Decision", "4_risk", (
        ("Aggressive Analyst", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Neutral Analyst", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Portfolio Manager Decision", "5_portfolio", (
        ("Portfolio Manager", "decision.md", ("final_trade_decision",)),
    )),
)


def _report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in _SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key)
            if value:
                written.append((agent, filename, value))
        if written:
            parts.append((heading, folder, written))
    return parts


def report_dir(results_dir, ticker: str, suffix: str = "") -> Path:
    """Where a run's report tree goes under ``results_dir``, stamped now."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(results_dir) / "reports" / f"{safe_ticker_component(ticker)}_{stamp}{suffix}"


def _write_tree(final_state: dict, ticker: str, save_path, settings, html: bool, completion,
                name: str) -> Path:
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    parts = _report_parts(final_state)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        content = "\n\n".join(f"### {agent}\n{text}" for agent, _filename, text in written)
        sections.append(f"## {heading}\n\n{content}")

    # The per-item Jev judgments behind the sentiment report, beside it.
    if final_state.get("sentiment_report") and final_state.get("sentiment_judgments"):
        (save_path / "1_analysts" / "sentiment_judgments.json").write_text(
            json.dumps(final_state["sentiment_judgments"], indent=2), encoding="utf-8")

    if completion is not None:
        (save_path / "run_status.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
    (save_path / f"{name}.md").write_text(
        _header(ticker, final_state, settings, completion) + "\n\n".join(sections), encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / f"{name}.html").write_text(
            render_report(ticker, final_state, settings, parts, completion=completion), encoding="utf-8"
        )
    return save_path / f"{name}.md"


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True, completion: dict | None = None) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419). ``completion`` (see
    ``tradingagents.completion``) adds the run's status line, naming any check
    that did not run, and is saved beside the report as ``run_status.json``.
    """
    if completion is not None and not is_complete(completion):
        raise ValueError("an incomplete run is saved with write_partial_report, not as a complete report")
    return _write_tree(final_state, ticker, save_path, settings, html, completion, "complete_report")


def write_partial_report(final_state: dict, ticker: str, save_path, completion: dict,
                         settings: dict | None = None, html: bool = True) -> Path:
    """Save what an interrupted run finished; return the partial-report path.

    It is ``partial_report.md`` (and ``.html``), never ``complete_report.md``,
    and opens with why the run stopped and that it has no rating, so nothing
    that reads saved reports (the history page, the 129BOB importer) can take
    it for finished research. ``run_status.json`` holds the completion record.
    """
    return _write_tree(final_state, ticker, save_path, settings, html, completion, "partial_report")
