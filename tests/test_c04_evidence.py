"""C04 public-path acceptance fixtures: synthetic answers, no provider calls.

TTL, mutable-copy and cancellation/worker-limit contracts belong to the broker
unit tests; these cases exercise routing, graph contexts and point-in-time tools.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import tools
from tradingagents.agents.state import AgentState
from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import run_config, run_config_context, set_config
from tradingagents.dataflows.evidence import current_evidence
from tradingagents.dataflows.feed import Feed, FeedItem
from tradingagents.dataflows.vendors.yahoo import fundamentals as yahoo_fundamentals
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.setup import _analyst_graph

START_DATE = "2026-10-05"
END_DATE = "2026-10-12"


def _config(**changes):
    config = {
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        },
        "tool_vendors": {},
    }
    return {**config, **changes}


@dataclass
class Sources:
    calls: list = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def record(self, method, *args):
        with self.lock:
            self.calls.append((method, args))

    def price(self, symbol, start_date, end_date):
        self.record("price", symbol, start_date, end_date)
        # Two frozen synthetic closes: return = 102 / 100 - 1 = 2 percent.
        return f"{symbol} prices {start_date}..{end_date}: 100,102; return=2%"

    def fundamentals(self, ticker, as_of_date=None):
        self.record("fundamentals", ticker, as_of_date)
        # Frozen synthetic EPS=2 and price=102 imply P/E=51. No real quote used.
        return f"{ticker} fundamentals as-of {as_of_date}: EPS=2; PE=51"

    def feed(self, ticker, start_date, end_date):
        self.record("feed", ticker, start_date, end_date)
        return self.expected_feed(ticker, start_date, end_date)

    def news(self, ticker, start_date, end_date):
        self.record("news", ticker, start_date, end_date)
        return self.expected_feed(ticker, start_date, end_date).text

    @staticmethod
    def expected_feed(ticker, start_date=START_DATE, end_date=END_DATE):
        # Both prompt text and per-item judgments must retain these two articles.
        items = (
            FeedItem("news", "Synthetic revenue grew 10%", "Revenue", start_date, "fixture"),
            FeedItem("news", "Synthetic operating margin 20%", "Margin", end_date, "fixture"),
        )
        return Feed(f"{ticker} news {start_date}..{end_date}: Revenue +10%; margin 20%", items)


@pytest.fixture
def sources(monkeypatch):
    fixture = Sources()
    for method, implementation in {
        "get_stock_data": fixture.price,
        "get_fundamentals": fixture.fundamentals,
        "get_news": fixture.news,
        "get_news_feed": fixture.feed,
    }.items():
        monkeypatch.setitem(router.VENDOR_METHODS, method, {"yfinance": implementation})
    set_config(_config())
    return fixture


def _workload(ticker):
    """Market repeats price; fundamentals repeats its facts; news/social overlap."""
    return [
        router.route_to_vendor("get_stock_data", ticker, START_DATE, END_DATE),
        router.route_to_vendor("get_stock_data", ticker, START_DATE, END_DATE),
        router.route_to_vendor("get_fundamentals", ticker, END_DATE),
        router.route_to_vendor("get_fundamentals", ticker, END_DATE),
        router.route_to_vendor("get_news", ticker, START_DATE, END_DATE),
        router.route_to_vendor("get_news_feed", ticker, START_DATE, END_DATE),
        router.route_to_vendor("get_news", ticker, START_DATE, END_DATE),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["NVDA", "RELIANCE.NS"])
def test_c04_seven_calls_use_three_sources_without_losing_any_fixture_evidence(sources, ticker):
    ordinary = _workload(ticker)
    assert Counter(name for name, _ in sources.calls) == {
        "price": 2, "fundamentals": 2, "news": 2, "feed": 1,
    }
    sources.calls.clear()

    with run_config(_config()):
        shared = _workload(ticker)

    # Seven analyst reads reduce to three unique source requests, while every
    # returned value and both articles are unchanged from the ordinary path.
    assert shared == ordinary
    assert len(shared[5].items) == 2
    assert shared[4] == shared[5].text == shared[6]
    assert "return=2%" in shared[0] and "PE=51" in shared[2]
    assert Counter(name for name, _ in sources.calls) == {
        "price": 1, "fundamentals": 1, "feed": 1,
    }


@pytest.mark.unit
@pytest.mark.parametrize("first", ["get_news", "get_news_feed"])
def test_c04_news_item_and_text_views_share_in_either_order(sources, first):
    second = "get_news_feed" if first == "get_news" else "get_news"
    with run_config(_config()):
        values = {name: router.route_to_vendor(name, "NVDA", START_DATE, END_DATE)
                  for name in (first, second)}
    assert values["get_news_feed"] == sources.expected_feed("NVDA")
    assert values["get_news"] == values["get_news_feed"].text
    assert sources.calls == [("feed", ("NVDA", START_DATE, END_DATE))]


@pytest.mark.unit
def test_c04_calls_outside_run_scopes_keep_the_public_vendor_paths(sources):
    text = router.route_to_vendor("get_news", "NVDA", START_DATE, END_DATE)
    feed = router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE)
    router.route_to_vendor("get_news", "NVDA", START_DATE, END_DATE)
    assert isinstance(text, str) and isinstance(feed, Feed)
    assert text == feed.text
    assert [name for name, _ in sources.calls] == ["news", "feed", "news"]


@pytest.mark.unit
def test_c04_equivalent_positional_keyword_and_default_arguments_reuse_one_source(sources):
    with run_config(_config()):
        values = [
            router.route_to_vendor("get_fundamentals", "NVDA"),
            router.route_to_vendor("get_fundamentals", "NVDA", as_of_date=None),
            router.route_to_vendor("get_fundamentals", ticker="NVDA", as_of_date=None),
        ]
    assert values == ["NVDA fundamentals as-of None: EPS=2; PE=51"] * 3
    assert sources.calls == [("fundamentals", ("NVDA", None))]


@pytest.mark.unit
def test_c04_ticker_and_window_are_part_of_the_request_identity(sources):
    requests = [("NVDA", START_DATE, END_DATE),
                ("NVDA", "2026-10-06", END_DATE),
                ("AAPL", START_DATE, END_DATE)]
    with run_config(_config()):
        once = [router.route_to_vendor("get_stock_data", *args) for args in requests]
        twice = [router.route_to_vendor("get_stock_data", *args) for args in requests]
    assert once == twice and len(set(once)) == 3
    assert sources.calls == [("price", args) for args in requests]


@pytest.mark.unit
@pytest.mark.parametrize("second_changes", [{}, {"asset_type": "crypto"}, {"news_article_limit": 1}])
def test_c04_separate_runs_never_reuse_another_runs_result(sources, second_changes):
    for changes in ({}, second_changes):
        with run_config(_config(**changes)):
            for _ in range(2):
                router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE)
    # Two calls per run, one request each, even for identical config and ticker.
    assert sources.calls == [("price", ("NVDA", START_DATE, END_DATE))] * 2


@pytest.mark.unit
def test_c04_equal_nested_scopes_reuse_but_changed_config_restores_the_outer_evidence(sources):
    with run_config(_config()):
        first = router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE)
        with run_config(_config()):
            assert router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE) == first
        with run_config(_config(news_article_limit=1)):
            router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE)
        assert router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE) == first
    assert sources.calls == [("feed", ("NVDA", START_DATE, END_DATE))] * 2


@pytest.mark.unit
def test_c04_vendor_changes_do_not_reuse_the_other_vendors_answer(sources, monkeypatch):
    def alpha(symbol, start_date, end_date):
        sources.record("alpha_price", symbol, start_date, end_date)
        return "Alpha fixture price"

    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "alpha_vantage", alpha)
    with run_config(_config()):
        yahoo = router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE)
        with run_config(_config(tool_vendors={"get_stock_data": "alpha_vantage"})):
            assert router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE) == "Alpha fixture price"
        assert router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE) == yahoo
    assert [name for name, _ in sources.calls] == ["price", "alpha_price"]


@pytest.mark.unit
def test_c04_resumable_run_context_keeps_its_evidence_and_leaves_the_caller_unscoped(sources):
    context = run_config_context(_config())
    request = ("get_stock_data", "NVDA", START_DATE, END_DATE)
    first = context.run(router.route_to_vendor, *request)
    assert context.run(router.route_to_vendor, *request) == first
    # A yielded run must not bind its evidence session in its caller's context.
    router.route_to_vendor(*request)
    other = run_config_context(_config())
    assert other.run(router.route_to_vendor, *request) == first
    assert len(sources.calls) == 3


@pytest.mark.unit
def test_c04_parallel_analyst_and_real_tool_branch_share_one_news_request(sources):
    both_analysts_started = Barrier(2)

    def social(state):
        both_analysts_started.wait(timeout=5)
        feed = router.route_to_vendor("get_news_feed", state["company_of_interest"], START_DATE, END_DATE)
        return {"messages": [AIMessage(feed.text)], "sentiment_report": feed.text}

    def news(state):
        if isinstance(state["messages"][-1], ToolMessage):
            text = state["messages"][-1].content
            return {"messages": [AIMessage(text)], "news_report": text}
        both_analysts_started.wait(timeout=5)
        return {"messages": [AIMessage("", tool_calls=[{
            "name": "get_news", "args": {"start_date": START_DATE, "end_date": END_DATE}, "id": "news-1",
        }])]}

    graph = StateGraph(AgentState)
    for key, node in (("social", social), ("news", news)):
        graph.add_node(key, _analyst_graph(ANALYST_NODE_SPECS[key], node, 3))
        graph.add_edge(START, key)
        graph.add_edge(key, END)
    with run_config(_config()):
        state = graph.compile().invoke({
            "messages": [("user", "research")], "company_of_interest": "NVDA", "trade_date": END_DATE,
        })
    assert state["sentiment_report"] == state["news_report"] == sources.expected_feed("NVDA").text
    assert sources.calls == [("feed", ("NVDA", START_DATE, END_DATE))]


@pytest.mark.unit
def test_c04_dated_tools_clamp_before_retrieval_and_share_the_clamped_request(sources):
    graph = StateGraph(AgentState)
    graph.add_node("tools", ToolNode([tools.get_stock_data, tools.get_news, tools.get_fundamentals]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    # The model supplies a different instrument and future dates. InjectedState
    # must select NVDA and clamp each upper date to 12 October before caching.
    calls = [
        {"name": "get_stock_data", "id": "p", "args": {
            "symbol": "AAPL", "start_date": START_DATE, "end_date": "2026-11-01"}},
        {"name": "get_news", "id": "n", "args": {
            "ticker": "AAPL", "start_date": START_DATE, "end_date": "2026-11-01"}},
        {"name": "get_fundamentals", "id": "f", "args": {
            "ticker": "AAPL", "curr_date": "2026-11-01"}},
    ]
    with run_config(_config()):
        state = graph.compile().invoke({
            "messages": [AIMessage("", tool_calls=calls)],
            "company_of_interest": "NVDA", "trade_date": END_DATE,
        })
        for method, args in (
            ("get_stock_data", ("NVDA", START_DATE, END_DATE)),
            ("get_news_feed", ("NVDA", START_DATE, END_DATE)),
            ("get_fundamentals", ("NVDA", END_DATE)),
        ):
            router.route_to_vendor(method, *args)
    messages = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(messages) == 3 and all(m.status == "success" for m in messages)
    assert Counter(sources.calls) == Counter([
        ("price", ("NVDA", START_DATE, END_DATE)),
        ("feed", ("NVDA", START_DATE, END_DATE)),
        ("fundamentals", ("NVDA", END_DATE)),
    ])


@pytest.mark.unit
def test_c04_router_failure_is_not_cached_and_a_recovered_answer_can_be_reused(sources, monkeypatch):
    attempts = []

    def flaky(symbol, start_date, end_date):
        attempts.append(symbol)
        if len(attempts) == 1:
            raise RuntimeError("synthetic source failure")
        return "recovered prices"

    monkeypatch.setitem(router.VENDOR_METHODS, "get_stock_data", {"yfinance": flaky})
    with run_config(_config()):
        with pytest.raises(RuntimeError, match="synthetic source failure"):
            router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE)
        assert router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE) == "recovered prices"
        assert router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE) == "recovered prices"
    assert attempts == ["NVDA", "NVDA"]


@pytest.mark.unit
def test_c04_unavailable_news_is_retried_and_not_reused_as_an_empty_source(sources, monkeypatch):
    attempts = []

    def feed(ticker, start_date, end_date):
        attempts.append(ticker)
        return Feed("coverage gap", unavailable=True) if len(attempts) == 1 else sources.expected_feed(ticker)

    monkeypatch.setitem(router.VENDOR_METHODS, "get_news_feed", {"yfinance": feed})
    with run_config(_config()):
        assert router.route_to_vendor("get_news", "NVDA", START_DATE, END_DATE) == "coverage gap"
        recovered = router.route_to_vendor("get_news_feed", "NVDA", START_DATE, END_DATE)
        assert not recovered.unavailable and len(recovered.items) == 2
        assert router.route_to_vendor("get_news", "NVDA", START_DATE, END_DATE) == recovered.text
    assert attempts == ["NVDA", "NVDA"]


@pytest.mark.unit
def test_c04_historical_yahoo_profile_stays_withheld_and_is_not_a_cached_fact(sources, monkeypatch):
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-10-19")
    attempts = []

    def no_live_profile(*args, **kwargs):
        pytest.fail("historical profile requested Yahoo's present-day Ticker.info")

    def guarded(ticker, as_of_date=None):
        attempts.append((ticker, as_of_date))
        return yahoo_fundamentals.get_fundamentals(ticker, as_of_date)

    monkeypatch.setattr(yahoo_fundamentals.yf, "Ticker", no_live_profile)
    monkeypatch.setitem(router.VENDOR_METHODS, "get_fundamentals", {"yfinance": guarded})
    with run_config(_config()):
        for _ in range(2):
            text = tools.get_fundamentals.func("NVDA", "2026-11-01", trade_date=END_DATE)
            assert "withheld" in text and f"Point-in-time as of: {END_DATE}" in text
            assert "Market Cap:" not in text
    assert attempts == [("NVDA", END_DATE), ("NVDA", END_DATE)]


@pytest.mark.parametrize("entry", ["propagate", "stream"])
def test_c04_graph_invocations_do_not_inherit_an_ambient_runs_evidence(sources, entry):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = _config(checkpoint_enabled=False)
    graph = object.__new__(TradingAgentsGraph)
    graph.config = config
    graph._checkpointer_ctx = None

    def retrieve():
        first = router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE)
        assert router.route_to_vendor("get_stock_data", "NVDA", START_DATE, END_DATE) == first
        return first

    def run(*args, **kwargs):
        return {"market_report": retrieve()}, "Hold"

    def stream(*args, **kwargs):
        yield (), "values", {"messages": [], "market_report": retrieve()}

    graph._run_graph = run
    graph.graph = SimpleNamespace(stream=stream)
    with run_config(config):
        ambient = current_evidence()
        retrieve()
        for _ in range(2):
            if entry == "propagate":
                graph.propagate("NVDA", END_DATE)
            else:
                assert len(list(graph.stream_run({}))) == 1
            assert current_evidence() is ambient
        retrieve()
    # The caller and each separate graph invocation own one acquisition each;
    # their matching ticker, dates and configuration do not merge their scopes.
    assert len(sources.calls) == 3
