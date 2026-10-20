"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from tradingagents.agents.context import resolved_identity
from tradingagents.agents.rating import run_rating
from tradingagents.completion import is_complete, status_line
from tradingagents.dataflows.provenance import ordered_entries, store_dir
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
        if s("research_mode"):
            lines.append(f"- Research mode: {s('research_mode')}")
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


def _cell(value) -> str:
    return str(value if value not in (None, "") else "").replace("|", "\\|").replace("\n", " ")


def _markdown_link(title, url) -> str | None:
    """A vendor's article link as Markdown: web addresses only, its title unable to break out.

    A feed is vendor-controlled text: a ``javascript:`` or ``data:`` address, or a
    title holding brackets, must not become a live or altered link in a download.
    """
    if not isinstance(url, str) or not re.match(r"https?://", url, re.IGNORECASE):
        return None
    text = re.sub(r"([\\\[\]|])", r"\\\1", str(title or url)).replace("\n", " ")
    return f"[{text}](<{quote(url, safe=_URL_SAFE)}>)"


_URL_SAFE = ":/?#[]@!$&'()*+,;=%~-._"


def _answered(entry: dict) -> list[dict]:
    return [a for a in entry.get("acquisitions") or () if a.get("outcome") == "ok"]


_SOURCES_OPENING = ("## Sources\n\nWhat each analyst read for this report, in the order it read them. "
                    "The texts stay with the saved report on this computer; this list only names them.")


def strip_sources_appendix(text: str) -> str:
    """A saved report without its Sources appendix, found by its exact opening at the end."""
    cut = text.rfind("\n" + _SOURCES_OPENING)
    return text if cut < 0 else text[:cut]


def sources_markdown(entries: list[dict]) -> str:
    """The report's Sources appendix: what each analyst read, never the texts (docs/adr/0003)."""
    lines = [
        _SOURCES_OPENING, "",
        "| ID | Analyst | Tool | Source | Window | As of | Fetched (UTC) | Coverage |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    links = []
    for e in entries:
        answered = _answered(e)
        first = answered[0] if answered else {}
        sources = ", ".join(dict.fromkeys(a["source"] for a in answered)) or "not recorded"
        start, end = first.get("window_start"), first.get("window_end")
        window = f"{start} to {end}" if start and end else (start or end or "")
        fetched = (first.get("fetched_at") or "")[:19].replace("T", " ")
        lines.append("| " + " | ".join(_cell(v) for v in (
            e["id"], e.get("analyst"), e.get("tool"), sources, window, first.get("as_of"), fetched,
            e.get("coverage"))) + " |")
        for a in answered:
            for item in a.get("items") or ():
                link = _markdown_link(item.get("title"), item.get("url"))
                if link:
                    published = item.get("published_at") or "publication time unknown"
                    links.append(f"- {e['id']}: {link} ({_cell(item.get('publisher'))}, {_cell(published)})")
    if links:
        lines += ["", "Articles linked from these sources:", "", *dict.fromkeys(links)]
    return "\n".join(lines)


def _instrument(final_state: dict) -> dict:
    return {"ticker": final_state.get("company_of_interest"), "trade_date": final_state.get("trade_date"),
            "asset_type": final_state.get("asset_type"),
            **resolved_identity(final_state.get("instrument_context") or "")}


def _write_evidence(final_state: dict, save_path: Path, store) -> list[dict] | None:
    """``evidence.json`` beside the report; None for a state saved before C05 recorded reads.

    ``store`` is where the run kept its texts (``provenance.store_dir`` of its
    config); without one, the current config's.
    """
    if "evidence" not in final_state:
        return None
    entries = ordered_entries(final_state.get("evidence"))
    store = store_dir() if store is None else Path(store)
    if store is not None:
        try:
            store = Path(os.path.relpath(store, save_path)).as_posix()
        except ValueError:      # another drive on Windows
            store = str(store)
    (save_path / "evidence.json").write_text(json.dumps(
        {"schema": 1, "store": store, "instrument": _instrument(final_state), "entries": entries},
        indent=2, ensure_ascii=False), encoding="utf-8")
    return entries


def report_dir(results_dir, ticker: str, suffix: str = "") -> Path:
    """Where a run's report tree goes under ``results_dir``, stamped now."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(results_dir) / "reports" / f"{safe_ticker_component(ticker)}_{stamp}{suffix}"


def _write_tree(final_state: dict, ticker: str, save_path, settings, html: bool, completion,
                name: str, evidence_store=None) -> Path:
    from tradingagents.modes import require_decision_state

    require_decision_state(final_state)
    require_decision_state(settings or {})
    if completion is not None:
        require_decision_state({"research_mode": completion.get("mode", "standard"),
                                "decision_eligible": completion.get("decision_eligible")})
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

    if settings and settings.get("research_mode") == "standard":
        keys = ("research_mode", "output_kind", "policy", "deadline_seconds", "attempt_timeout_seconds",
                "version", "llm_provider", "deep_think_provider", "deep_think_llm",
                "quick_think_provider", "quick_think_llm", "analysts", "max_debate_rounds",
                "max_risk_discuss_rounds", "output_language", "data_vendors", "tool_vendors")
        (save_path / "run_metadata.json").write_text(
            json.dumps({k: settings[k] for k in keys if k in settings}, indent=2), encoding="utf-8")
        if completion is not None:
            completion = {**completion, "mode": "standard", "output_kind": "report"}
    if completion is not None:
        (save_path / "run_status.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
    # What the analysts read, and the claim check as data, for the brief (docs/adr/0003).
    entries = _write_evidence(final_state, save_path, evidence_store)
    if final_state.get("claim_check") is not None:
        (save_path / "claim_check.json").write_text(
            json.dumps(final_state["claim_check"], indent=2, ensure_ascii=False), encoding="utf-8")
    sources = sources_markdown(entries) if entries is not None else None
    (save_path / f"{name}.md").write_text(
        _header(ticker, final_state, settings, completion) + "\n\n".join(sections)
        + (f"\n\n{sources}\n" if sources else ""), encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / f"{name}.html").write_text(
            render_report(ticker, final_state, settings, parts, completion=completion, sources=sources),
            encoding="utf-8",
        )
    return save_path / f"{name}.md"


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True, completion: dict | None = None, evidence_store=None) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419). ``completion`` (see
    ``tradingagents.completion``) adds the run's status line, naming any check
    that did not run, and is saved beside the report as ``run_status.json``.
    ``evidence_store`` is where the run kept the texts its analysts read
    (``provenance.store_dir(config)``; docs/adr/0003).
    """
    from tradingagents.modes import require_decision_state

    require_decision_state(final_state)
    if completion is not None and not is_complete(completion):
        raise ValueError("an incomplete run is saved with write_partial_report, not as a complete report")
    return _write_tree(final_state, ticker, save_path, settings, html, completion, "complete_report",
                       evidence_store)


def write_partial_report(final_state: dict, ticker: str, save_path, completion: dict,
                         settings: dict | None = None, html: bool = True, evidence_store=None) -> Path:
    """Save what an interrupted run finished; return the partial-report path.

    It is ``partial_report.md`` (and ``.html``), never ``complete_report.md``,
    and opens with why the run stopped and that it has no rating, so nothing
    that reads saved reports (the history page, the 129BOB importer) can take
    it for finished research. ``run_status.json`` holds the completion record.
    """
    return _write_tree(final_state, ticker, save_path, settings, html, completion, "partial_report",
                       evidence_store)
