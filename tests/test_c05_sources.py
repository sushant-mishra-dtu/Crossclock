"""C05: what each analyst read is saved with its report (docs/adr/0003).

Every tool result and prefetched feed becomes an evidence entry, gaps
included; the texts go to a content-addressed store under the results folder;
the report tree gains ``evidence.json`` and ``claim_check.json``; downloads
list the sources without their texts. Scripted models, no network.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from cli.webui.jobs import SavedReport, load_report_sections, report_rating
from tests.test_graph_end_to_end import (
    TRADE_DATE,
    ScriptedModel,
    _graph,
    offline,  # noqa: F401 — the fixture, used by name
)
from tests.test_jev_claim_check import THESIS, FakeJev, _pm_llm, _pm_state
from tradingagents.agents import claim_check as cc
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.dataflows import provenance, router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.feed import Feed, FeedItem
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.reporting import write_partial_report, write_report_tree

ANALYST_TOOLS = {
    "Market Analyst": ["get_stock_data", "get_indicators", "get_verified_market_snapshot"],
    "Sentiment Analyst": ["get_news", "stocktwits_messages", "reddit_posts"],
    "News Analyst": ["get_news", "get_global_news", "get_macro_indicators", "get_prediction_markets"],
    "Fundamentals Analyst": ["get_fundamentals", "get_balance_sheet", "get_cashflow",
                             "get_income_statement", "get_insider_transactions"],
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(tmp_path, monkeypatch, **config):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), **config)
    state, _ = graph.propagate("NVDA", TRADE_DATE)
    return graph, state


def _store(tmp_path) -> Path:
    return tmp_path / "results" / "evidence"


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_every_tool_call_and_prefetched_feed_becomes_one_entry_in_pipeline_order(
        tmp_path, monkeypatch):
    _, state = _run(tmp_path, monkeypatch)

    entries = provenance.ordered_entries(state["evidence"])

    assert [e["id"] for e in entries] == [f"E{i}" for i in range(1, 16)]
    assert [e["analyst"] for e in entries] == [a for a, tools in ANALYST_TOOLS.items() for _ in tools]
    for analyst, tools in ANALYST_TOOLS.items():
        assert [e["tool"] for e in entries if e["analyst"] == analyst] == tools
    assert Counter(e["coverage"] for e in entries) == {"available": 15}


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_each_entry_names_its_vendor_window_and_fetch_time_and_its_text_is_stored(
        tmp_path, monkeypatch):
    # Named like the real vendors' parameters, so the window can be read from the call.
    def prices(symbol, start_date, end_date):
        return "get_stock_data data"

    for vendor in router.VENDOR_METHODS["get_stock_data"]:
        monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], vendor, prices)
    _, state = _run(tmp_path, monkeypatch)
    entries = provenance.ordered_entries(state["evidence"])

    price = next(e for e in entries if e["tool"] == "get_stock_data")
    assert price["arguments"]["start_date"] == "2026-01-02"
    [acquired] = price["acquisitions"]
    assert acquired["operation"] == "get_stock_data"
    assert acquired["source"] in router.VENDOR_METHODS["get_stock_data"]
    assert acquired["outcome"] == "ok"
    assert (acquired["window_start"], acquired["window_end"]) == ("2026-01-02", TRADE_DATE)
    assert datetime.fromisoformat(acquired["fetched_at"]).utcoffset().total_seconds() == 0

    for entry in entries:
        text = (_store(tmp_path) / f"{entry['content_sha256']}.txt").read_text(encoding="utf-8")
        assert _sha(text) == entry["content_sha256"] and len(text) == entry["chars"]
    # The snapshot is built from cached prices, not through the router.
    snapshot = next(e for e in entries if e["tool"] == "get_verified_market_snapshot")
    assert snapshot["acquisitions"] == []


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_news_read_by_two_analysts_is_stored_once(tmp_path, monkeypatch):
    _, state = _run(tmp_path, monkeypatch)
    entries = provenance.ordered_entries(state["evidence"])

    news = [e for e in entries if e["tool"] == "get_news"]
    assert [e["analyst"] for e in news] == ["Sentiment Analyst", "News Analyst"]
    assert news[0]["content_sha256"] == news[1]["content_sha256"]
    stored = sorted(p.stem for p in _store(tmp_path).glob("*.txt"))
    assert stored == sorted({e["content_sha256"] for e in entries})


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_an_unavailable_answer_is_a_gap_entry(tmp_path, monkeypatch):
    def down(*args, **kwargs):
        raise VendorUnavailableError("throttled")

    for vendor in router.VENDOR_METHODS["get_fundamentals"]:
        monkeypatch.setitem(router.VENDOR_METHODS["get_fundamentals"], vendor, down)
    _, state = _run(tmp_path, monkeypatch)

    [entry] = [e for e in provenance.ordered_entries(state["evidence"]) if e["tool"] == "get_fundamentals"]
    assert entry["coverage"] == "unavailable"
    assert entry["acquisitions"] and {a["outcome"] for a in entry["acquisitions"]} == {"unavailable"}


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_news_items_keep_their_link_and_an_unknown_publication_time_stays_unknown(
        tmp_path, monkeypatch):
    feed = Feed("Two headlines.", items=(
        FeedItem("news", "Chip demand rose.", title="Demand", author="Wire", url="https://example.com/a",
                 published_at="2026-01-08T14:30:00+00:00"),
        FeedItem("news", "Undated note.", title="Note", author="Blog", url="https://example.com/b"),
    ))
    for vendor in router.VENDOR_METHODS["get_news_feed"]:
        monkeypatch.setitem(router.VENDOR_METHODS["get_news_feed"], vendor, lambda *a, **k: feed)
    _, state = _run(tmp_path, monkeypatch)

    for entry in [e for e in provenance.ordered_entries(state["evidence"]) if e["tool"] == "get_news"]:
        [acquired] = entry["acquisitions"]
        assert acquired["items"] == [
            {"title": "Demand", "publisher": "Wire", "url": "https://example.com/a",
             "published_at": "2026-01-08T14:30:00+00:00"},
            {"title": "Note", "publisher": "Blog", "url": "https://example.com/b", "published_at": None},
        ]


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_a_resumed_run_keeps_the_entries_of_the_stages_it_reused(tmp_path, monkeypatch):
    model = ScriptedModel(fail_at=12)            # past the analysts, before the decision
    graph = _graph(tmp_path, monkeypatch, model, checkpoint_enabled=True)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        graph.propagate("NVDA", TRADE_DATE)

    state, _ = graph.propagate("NVDA", TRADE_DATE)

    entries = provenance.ordered_entries(state["evidence"])
    assert Counter(e["analyst"] for e in entries) == {a: len(t) for a, t in ANALYST_TOOLS.items()}


@pytest.mark.unit
def test_the_model_is_offered_the_same_tools_as_before():
    """Recording a read changes nothing the model sees: names, descriptions, schemas."""
    for spec in ANALYST_NODE_SPECS.values():
        for original, recorded in zip(spec.tools, provenance.recorded_tools(spec.tools),
                                      strict=True):
            assert recorded.name == original.name
            assert recorded.description == original.description
            assert (recorded.tool_call_schema.model_json_schema()
                    == original.tool_call_schema.model_json_schema())
            # The tools module's own object is untouched: it still returns plain text.
            assert original.response_format == "content"


class _JevClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_the_sentiment_analyst_records_its_three_feeds_on_the_jev_path_too(tmp_path, monkeypatch):
    feed = Feed("One headline.", items=(FeedItem("news", "Demand rose.", title="Demand", author="Wire"),))
    for vendor in router.VENDOR_METHODS["get_news_feed"]:
        monkeypatch.setitem(router.VENDOR_METHODS["get_news_feed"], vendor, lambda *a, **k: feed)
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_feed", lambda *a, **k: Feed("No posts."))
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_feed",
                        lambda *a, **k: Feed("Reddit could not answer.", unavailable=True))
    monkeypatch.setattr(sentiment_analyst, "jev_client", lambda: _JevClient())
    monkeypatch.setattr(sentiment_analyst, "_judge", lambda *a, **k: None)   # judgments failed

    _, state = _run(tmp_path, monkeypatch)

    sentiment = [e for e in provenance.ordered_entries(state["evidence"]) if e["analyst"] == "Sentiment Analyst"]
    assert [e["tool"] for e in sentiment] == ANALYST_TOOLS["Sentiment Analyst"]
    assert [e["coverage"] for e in sentiment] == ["available", "available", "unavailable"]
    assert [e["arguments"]["start_date"] for e in sentiment] == ["2026-01-02"] * 3
    assert sentiment[0]["acquisitions"][0]["items"][0]["title"] == "Demand"


# --- The store ---------------------------------------------------------------

@pytest.mark.unit
def test_the_store_is_content_addressed_and_refuses_a_changed_file(tmp_path):
    first = provenance.store_text("Revenue 130.5", tmp_path)
    assert provenance.store_text("Revenue 130.5", tmp_path) == first == _sha("Revenue 130.5")
    assert [p.name for p in tmp_path.iterdir()] == [f"{first}.txt"]
    assert provenance.read_text(tmp_path, first) == "Revenue 130.5"

    (tmp_path / f"{first}.txt").write_text("Revenue 999", encoding="utf-8")
    assert provenance.read_text(tmp_path, first) is None
    assert provenance.read_text(tmp_path, "../../etc/passwd") is None
    assert provenance.read_text(tmp_path, "0" * 64) is None


@pytest.mark.unit
@pytest.mark.parametrize(("text", "coverage"), [
    ("Close 101.2", "available"),
    ("", "empty"),
    ("   ", "empty"),
    ("DATA_UNAVAILABLE: no configured vendor could serve get_news", "unavailable"),
    ("NO_DATA_AVAILABLE: No usable market data for 'ZZZZ'", "no_data"),
    ("The company profile is withheld for this date: it would leak later facts.", "withheld"),
])
def test_coverage_is_read_from_the_routers_answers(text, coverage):
    assert provenance.coverage_of(text) == coverage


# --- The claim check as data -------------------------------------------------

@pytest.mark.unit
def test_the_claim_check_is_kept_as_data_beside_the_decision(monkeypatch):
    client = FakeJev()
    monkeypatch.setattr(cc, "jev_client", lambda: client)

    result = create_portfolio_manager(_pm_llm(THESIS))(_pm_state())

    check = result["claim_check"]
    assert check["status"] == "ran" and check["review"] is True
    assert check["reasons"] == ["1 claim contradicted by the analyst reports"]
    verdicts = {c["claim"].split(" [")[0].lstrip("- "): c["verdict"] for c in check["claims"]}
    assert verdicts["Gross margin expanded to 75% on pricing power"] == "contradicted"
    margin = next(c for c in check["claims"] if c["verdict"] == "contradicted")
    assert margin["section"]["report"] == "fundamentals"
    assert margin["section"]["label"].startswith("fundamentals report, ")   # small sections are packed together
    assert "Margins" in margin["section"]["label"]
    assert "Gross margin fell to 71.2%" in margin["section"]["text"]
    assert check["policy"]["contradicted_min"] == 0.80


@pytest.mark.unit
def test_an_unchecked_decision_says_why_in_its_claim_check_data():
    result = create_portfolio_manager(_pm_llm(THESIS))(_pm_state())
    assert result["claim_check"] == {"status": "unavailable", "claims": []}


# --- The saved report --------------------------------------------------------

def _saved_state(tmp_path, monkeypatch):
    _, state = _run(tmp_path, monkeypatch)
    state["claim_check"] = {"status": "ran", "claims": [{"claim": "Revenue grew.", "verdict": "supported"}]}
    return state


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_the_report_tree_saves_the_entries_and_the_claim_check(tmp_path, monkeypatch):
    state = _saved_state(tmp_path, monkeypatch)
    folder = tmp_path / "results" / "reports" / "NVDA_20260109_120000"

    write_report_tree(state, "NVDA", folder)

    saved = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in saved["entries"]] == [f"E{i}" for i in range(1, 16)]
    assert (folder / saved["store"]).resolve() == _store(tmp_path).resolve()
    assert saved["instrument"]["ticker"] == "NVDA" and saved["instrument"]["trade_date"] == TRADE_DATE
    assert json.loads((folder / "claim_check.json").read_text(encoding="utf-8")) == state["claim_check"]


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_downloads_list_the_sources_without_their_texts(tmp_path, monkeypatch):
    state = _saved_state(tmp_path, monkeypatch)
    folder = tmp_path / "results" / "reports" / "NVDA_20260109_120000"

    path = write_report_tree(state, "NVDA", folder)

    text = path.read_text(encoding="utf-8")
    appendix = text.split("\n## Sources\n", 1)[1]
    assert "| E1 | Market Analyst | get_stock_data |" in appendix
    assert "get_stock_data data" not in appendix          # the source text itself stays out
    assert "Sources" in (folder / "complete_report.html").read_text(encoding="utf-8")
    # The page still reads five sections, and the decision's rating, without the appendix.
    sections = load_report_sections(SavedReport(path, "NVDA", TRADE_DATE, datetime.now(), "report"))
    assert [t for t, _ in sections][-1] == "V. Portfolio Manager Decision"
    assert "## Sources" not in sections[-1][1] and "E1" not in sections[-1][1]
    assert report_rating(sections) == "Overweight"


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_a_partial_report_keeps_the_sources_its_finished_stages_read(tmp_path, monkeypatch):
    state = _saved_state(tmp_path, monkeypatch)
    folder = tmp_path / "results" / "reports" / "NVDA_20260109_120000_partial"
    completion = {"state": "partial", "stop_reason": "cancelled", "stages": {}, "checks": {}, "gaps": []}

    write_partial_report(state, "NVDA", folder, completion)

    saved = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    assert len(saved["entries"]) == 15


@pytest.mark.unit
def test_a_state_from_before_c05_writes_no_evidence_file(tmp_path):
    state = {"company_of_interest": "NVDA", "trade_date": TRADE_DATE, "market_report": "Report."}
    write_report_tree(state, "NVDA", tmp_path / "old")
    assert not (tmp_path / "old" / "evidence.json").exists()
    assert not (tmp_path / "old" / "claim_check.json").exists()
