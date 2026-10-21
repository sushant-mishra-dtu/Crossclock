"""Backtesting: many single-shot decisions, scored by the memory log.

A run already records its rating and later settles it with realized and alpha
return against the regional benchmark. A backtest is that machinery over a grid
of tickers and dates, aggregated. It evaluates decision quality; it does not
simulate a portfolio, so there is no execution and no equity curve. Costs are
one round trip taken off each directional call, not a fill.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.memory import TradingMemoryLog

DECISION = "Rating: Buy\n\nbuy it"


@pytest.mark.unit
def test_grid_spacing_and_canonical_dates():
    assert iter_grid("2026-01-05", "2026-01-20", every_n_days=7) == ["2026-01-05", "2026-01-12", "2026-01-19"]


@pytest.mark.unit
def test_grid_stops_at_today(monkeypatch):
    import tradingagents.backtest as bt

    monkeypatch.setattr(bt, "get_current_date", lambda: "2026-01-10")
    assert iter_grid("2026-01-05", "2026-02-20", every_n_days=5) == ["2026-01-05", "2026-01-10"]


@pytest.mark.unit
def test_grid_rejects_a_non_canonical_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iter_grid("2026-1-5", "2026-01-20")


class _FakeGraph:
    """Stands in for TradingAgentsGraph, writing to the log the harness gave it."""

    instances: list = []
    fail_on: set = set()

    def __init__(self, selected_analysts=None, config=None, **kw):
        self.analysts = list(selected_analysts) if selected_analysts else None
        self.config = config
        self.memory_log = TradingMemoryLog(config)
        self.calls = []
        self.settled = []
        _FakeGraph.instances.append(self)

    def propagate(self, ticker, trade_date, asset_type="stock", portfolio=None):
        self.calls.append((ticker, trade_date))
        if (ticker, trade_date) in _FakeGraph.fail_on:
            raise RuntimeError("vendor exploded")
        self.memory_log.store_decision(ticker, trade_date, DECISION)
        return {"final_trade_decision": DECISION}, "Buy"

    def settle_pending(self, ticker):
        self.settled.append(ticker)


@pytest.fixture(autouse=True)
def _fake_graph(monkeypatch, tmp_path):
    import tradingagents.backtest as bt

    _FakeGraph.instances = []
    _FakeGraph.fail_on = set()
    monkeypatch.setattr(bt, "TradingAgentsGraph", _FakeGraph)
    return _FakeGraph


def _config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live_trading_memory.md")}


@pytest.mark.unit
def test_the_live_memory_log_is_never_written(tmp_path):
    config = _config(tmp_path)
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config)

    assert not (tmp_path / "live_trading_memory.md").exists()
    assert result.log_path.exists() and result.cells_run == 2


@pytest.mark.unit
def test_a_cell_already_in_the_log_is_not_run_again(tmp_path):
    config = _config(tmp_path)
    first = run_backtest(["NVDA"], ["2026-01-05"], config)

    again = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config, run_id=first.run_id)

    assert again.cells_run == 1 and again.skipped == 1
    assert _FakeGraph.instances[-1].calls == [("NVDA", "2026-01-12")]


@pytest.mark.unit
def test_every_ticker_is_settled_after_the_grid(tmp_path):
    """Settlement runs at the start of the next same-ticker run, so the last
    date of each ticker would stay pending without an explicit pass."""
    run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path))
    assert sorted(_FakeGraph.instances[-1].settled) == ["AAPL", "NVDA"]


@pytest.mark.unit
def test_a_failed_cell_does_not_abort_the_sweep(tmp_path):
    _FakeGraph.fail_on = {("NVDA", "2026-01-05")}
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _config(tmp_path))

    assert result.cells_run == 1
    assert result.failures == [("NVDA", "2026-01-05", "vendor exploded")]


# --- reading the result ------------------------------------------------------

def _log_with(tmp_path, rows):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_summary_scores_resolved_cells_and_keeps_pending_out_of_the_average(tmp_path):
    log = _log_with(tmp_path, [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
        ("AAPL", "2026-01-05", "Rating: Sell\n\nx", None),
    ])

    summary = summarize(log)

    assert summary.resolved == 2 and summary.pending == 1
    buys = summary.by_rating["Buy"]
    assert buys.count == 2 and buys.hit_rate == 0.5 and round(buys.mean_alpha, 4) == 0.01
    assert "Sell" not in summary.by_rating  # unsettled: nothing to score yet


@pytest.mark.unit
def test_summary_states_what_it_cannot_prove(tmp_path):
    text = summarize(_log_with(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))])).render()
    assert "not archived" in text
    assert "one" in text.lower() and "sampl" in text.lower()


@pytest.mark.unit
def test_the_analyst_set_under_test_is_the_one_that_runs(tmp_path):
    """A backtest of a two-analyst setup must not silently run four."""
    run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), selected_analysts=["market", "news"])
    assert _FakeGraph.instances[-1].analysts == ["market", "news"]


@pytest.mark.unit
def test_a_run_id_cannot_escape_the_results_directory(tmp_path):
    """run_id becomes a path segment, so it is validated like a ticker is."""
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="../../escaped")
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="/etc/cron.d/x")


@pytest.mark.unit
def test_a_failed_settlement_does_not_lose_the_remaining_tickers(tmp_path, monkeypatch):
    """Settlement reflects with an LLM, so it can fail; the sweep still returns
    its result and every other ticker still gets settled."""
    settled = []

    def _settle(self, ticker):
        if ticker == "NVDA":
            raise RuntimeError("reflector timed out")
        settled.append(ticker)

    monkeypatch.setattr(_FakeGraph, "settle_pending", _settle, raising=False)
    result = run_backtest(["NVDA", "AAPL"], ["2026-01-05"], _config(tmp_path))

    assert result.cells_run == 2
    assert settled == ["AAPL"]
    assert result.settlement_failures == [("NVDA", "reflector timed out")]


@pytest.mark.unit
def test_pending_note_appears_only_when_something_is_pending(tmp_path):
    settled = [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))]
    assert "Pending" not in summarize(_log_with(tmp_path, settled)).render()
    assert "Pending" in summarize(_log_with(tmp_path, settled + [("AAPL", "2026-01-05", DECISION, None)])).render()


# --- scoring reads the direction the rating claimed ---------------------------

def _scored(tmp_path, rows):
    log = _log_with(tmp_path, rows)
    return summarize(log).by_rating


@pytest.mark.unit
def test_a_bearish_call_that_fell_counts_as_right(tmp_path):
    """Alpha below the benchmark is the outcome a Sell predicted; scoring it as
    a miss reported the system as wrong exactly when it was right."""
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (-0.08, -0.05)),
        ("AAPL", "2026-01-05", "**Rating**: Underweight\n\nx", (-0.03, -0.02)),
    ])
    assert scores["Sell"].hit_rate == 1.0
    assert scores["Underweight"].hit_rate == 1.0


@pytest.mark.unit
def test_a_bearish_call_that_rose_counts_as_wrong(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (0.08, 0.05))])
    assert scores["Sell"].hit_rate == 0.0


@pytest.mark.unit
def test_a_bullish_call_is_scored_the_same_way_as_before(tmp_path):
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.10, 0.04)),
        ("AAPL", "2026-01-05", "**Rating**: Buy\n\nx", (-0.02, -0.02)),
    ])
    assert scores["Buy"].hit_rate == 0.5


@pytest.mark.unit
def test_hold_claims_no_direction_so_it_gets_no_hit_rate(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Hold\n\nx", (0.01, 0.005))])
    assert scores["Hold"].hit_rate is None
    assert scores["Hold"].mean_alpha == 0.005


@pytest.mark.unit
def test_the_report_names_the_window_the_scores_cover(tmp_path):
    text = summarize(_log_with(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.1, 0.05))])).render()
    assert "5" in text and "day" in text.lower()
    assert "Hold" not in text or "no direction" in text.lower()


@pytest.mark.unit
def test_the_window_reported_is_the_one_the_outcomes_used(tmp_path):
    """The log records the window each outcome was measured over; the summary
    must not claim a different one."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    log.store_decision("NVDA", "2026-01-05", "**Rating**: Buy\n\nx")
    log.update_with_outcome("NVDA", "2026-01-05", 0.1, 0.04, 21, "note", "2026-02-01")

    assert "21 trading days" in summarize(tmp_path / "m.md").render()


@pytest.mark.unit
def test_a_backtest_result_is_summarized_directly(tmp_path):
    """The result names its own log, so a caller never builds the log to score it."""
    from tradingagents.backtest import BacktestResult

    path = _log_with(tmp_path, [("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04))])

    assert summarize(BacktestResult(run_id="r", log_path=path)).resolved == 1


@pytest.mark.unit
def test_a_log_path_that_does_not_exist_is_an_error_not_an_empty_summary(tmp_path):
    missing = tmp_path / "no-such-dir" / "m.md"

    with pytest.raises(FileNotFoundError):
        summarize(missing)
    assert not missing.parent.exists()


@pytest.mark.unit
def test_a_result_whose_cells_all_failed_summarizes_as_empty(tmp_path):
    from tradingagents.backtest import BacktestResult

    result = BacktestResult(run_id="r", log_path=tmp_path / "never-written.md")

    assert summarize(result).resolved == 0


@pytest.mark.unit
def test_should_stop_ends_the_sweep_between_cells_without_settling(tmp_path):
    seen = []
    result = run_backtest(
        ["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path),
        on_cell=lambda ticker, date: seen.append((ticker, date)),
        should_stop=lambda: len(seen) >= 3,
    )
    [graph] = _FakeGraph.instances
    assert seen == [("NVDA", "2026-01-05"), ("NVDA", "2026-01-12"), ("AAPL", "2026-01-05")]
    assert graph.calls == seen
    assert result.stopped and result.cells_run == 3
    assert graph.settled == []  # the resumed sweep settles at its own end

    # Running again resumes: logged cells are skipped, the last one runs, and it settles.
    again = run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path),
                         run_id=result.run_id)
    assert (again.skipped, again.cells_run, again.stopped) == (3, 1, False)
    assert _FakeGraph.instances[-1].settled == ["NVDA", "AAPL"]


@pytest.mark.unit
def test_progress_is_reported_before_each_cell(tmp_path):
    seen = []

    run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path),
                 progress=lambda done, total, ticker, date: seen.append((done, total, ticker, date)))

    assert seen == [(1, 4, "NVDA", "2026-01-05"), (2, 4, "NVDA", "2026-01-12"),
                    (3, 4, "AAPL", "2026-01-05"), (4, 4, "AAPL", "2026-01-12")]


@pytest.mark.unit
def test_a_resumed_sweep_reports_only_the_cells_it_runs(tmp_path):
    first = run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path))
    seen = []

    run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _config(tmp_path), run_id=first.run_id,
                 progress=lambda done, total, ticker, date: seen.append((done, total, date)))

    assert seen == [(1, 1, "2026-01-12")]


@pytest.mark.unit
def test_a_ticker_or_date_given_twice_runs_and_settles_once(tmp_path):
    run_backtest(["NVDA", "NVDA"], ["2026-01-05", "2026-01-05"], _config(tmp_path))
    graph = _FakeGraph.instances[-1]
    assert graph.calls == [("NVDA", "2026-01-05")]
    assert graph.settled == ["NVDA"]


# --- costs: one round trip off each directional call --------------------------
#
# The net directional return of a call is direction * alpha - cost, with cost one
# round trip as a fraction (30 bps = 0.003). The log keeps alpha to 0.1 of a
# percentage point, so every alpha below is one the log stores exactly.

def _summary(tmp_path, rows, **kw):
    return summarize(_log_with(tmp_path, rows), **kw)


def _flat(text):
    return " ".join(text.split())


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["Buy", "Overweight"])
def test_a_bullish_call_nets_its_alpha_less_one_round_trip(tmp_path, rating):
    # alpha +0.5% = 0.005; 30 bps = 0.003; net = +1 * 0.005 - 0.003 = +0.002, a net hit.
    score = _summary(tmp_path, [("NVDA", "2026-01-05", f"Rating: {rating}\n\nx", (0.01, 0.005))],
                     cost_bps=30).by_rating[rating]
    assert score.mean_net == pytest.approx(0.002)
    assert score.net_hit_rate == 1.0


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["Sell", "Underweight"])
def test_a_bearish_call_right_before_costs_can_be_wrong_after_them(tmp_path, rating):
    # alpha -0.2%: the stock lagged, as the call said, so the gross hit rate is 1.
    # net = -1 * -0.002 - 0.003 = -0.001: avoiding it saved less than the round trip.
    score = _summary(tmp_path, [("NVDA", "2026-01-05", f"Rating: {rating}\n\nx", (-0.01, -0.002))],
                     cost_bps=30).by_rating[rating]
    assert score.hit_rate == 1.0
    assert score.net_hit_rate == 0.0
    assert score.mean_net == pytest.approx(-0.001)


@pytest.mark.unit
def test_the_render_shows_the_net_figures_of_a_rating_no_call_of_which_survived_costs(tmp_path):
    summary = _summary(tmp_path, [("NVDA", "2026-01-05", "Rating: Sell\n\nx", (-0.01, -0.002))], cost_bps=30)
    [line] = [line for line in summary.render().splitlines() if line.startswith("- Sell")]
    assert line.endswith("; after costs right 0%, mean net -0.10%")


@pytest.mark.unit
def test_net_figures_average_over_the_ratings_calls(tmp_path):
    # Two Buys at 30 bps: +0.5% nets +0.2%, -0.2% nets -0.5%; mean -0.15%, one of two right.
    buy = _summary(tmp_path, [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),
        ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (0.0, -0.002)),
    ], cost_bps=30).by_rating["Buy"]
    assert buy.mean_net == pytest.approx(-0.0015)
    assert buy.net_hit_rate == 0.5 and buy.hit_rate == 0.5


@pytest.mark.unit
def test_a_call_that_only_earns_its_cost_back_is_not_a_net_hit(tmp_path):
    # +0.9% at 90 bps nets zero, yet 0.9 / 100 - 90 / 10000 is +1.7e-18 in floats;
    # that noise must not turn a break-even call into a hit.
    buy = _summary(tmp_path, [("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.009))],
                   cost_bps=90).by_rating["Buy"]
    assert buy.net_hit_rate == 0.0
    assert buy.mean_net == pytest.approx(0.0, abs=1e-12)


@pytest.mark.unit
def test_hold_carries_no_net_figures(tmp_path):
    summary = _summary(tmp_path, [("NVDA", "2026-01-05", "Rating: Hold\n\nx", (0.01, 0.005))], cost_bps=30)
    hold = summary.by_rating["Hold"]
    assert hold.net_hit_rate is None and hold.mean_net is None
    [line] = [line for line in summary.render().splitlines() if line.startswith("- Hold")]
    assert "net" not in line


@pytest.mark.unit
@pytest.mark.parametrize("ticker, bps", [
    pytest.param("RELIANCE.NS", 30, id="nse"),
    pytest.param("500325.BO", 30, id="bse_code"),
    pytest.param("reliance.ns", 30, id="lower_case"),
    pytest.param("NVDA", 5, id="us"),
    pytest.param("BRK-B", 5, id="us_share_class"),
    # No researched figure for other markets: the higher default stands in.
    pytest.param("VOD.L", 30, id="other_market"),
    pytest.param("BTC-USD", 30, id="crypto"),
])
def test_the_default_cost_follows_the_tickers_market(ticker, bps):
    from tradingagents.backtest import default_cost_bps

    assert default_cost_bps(ticker) == bps


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["500325.BO", "reliance.ns"])
def test_an_indian_listing_is_costed_as_indian_not_as_another_market(tmp_path, ticker):
    """The stand-in for other markets is also 30 bps, so only the label tells them apart."""
    text = _flat(_summary(tmp_path, [(ticker, "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005))]).render())
    assert "30 bps for NSE/BSE delivery" in text
    assert "other markets" not in text


@pytest.mark.unit
def test_without_a_cost_each_call_pays_its_markets_default(tmp_path):
    # RELIANCE.NS +0.5% at 30 bps nets +0.2%; NVDA +0.5% at 5 bps nets +0.45%; mean +0.325%.
    summary = _summary(tmp_path, [
        ("RELIANCE.NS", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),
    ])
    assert summary.by_rating["Buy"].mean_net == pytest.approx(0.00325)
    text = _flat(summary.render())
    assert "30 bps" in text and "NSE" in text
    assert "5 bps" in text and "US" in text


# What the code before costs printed for this log (main at `4ab64a3`). A cost
# changes only the new net figures, never these.
_GUARD_ROWS = [
    ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
    ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
    ("AAPL", "2026-01-05", "Rating: Sell\n\nx", (-0.08, -0.05)),
    ("MSFT", "2026-01-05", "Rating: Hold\n\nx", (0.01, 0.005)),
    ("AMD", "2026-01-05", "Rating: Underweight\n\nx", (0.03, 0.012)),
]
_GUARD_SCORES = {"Buy": (2, 0.5, 0.01), "Sell": (1, 1.0, -0.05), "Hold": (1, None, 0.005),
                 "Underweight": (1, 0.0, 0.012)}
_GUARD_LINES = [
    "Resolved cells: 5 · pending: 0",
    "- Buy: n=2, called the direction 50%, mean alpha +1.00% vs the benchmark",
    "- Sell: n=1, called the direction 100%, mean alpha -5.00% vs the benchmark",
    "- Hold: n=1, no direction claimed, mean alpha +0.50% vs the benchmark",
    "- Underweight: n=1, called the direction 0%, mean alpha +1.20% vs the benchmark",
    "Alpha is measured over 5 trading days after each analysis date. One model sampling per cell, "
    "and text feeds are not archived, so these figures are indicative rather than repeatable.",
]


@pytest.mark.unit
@pytest.mark.parametrize("cost_bps", [0, None, 30])
def test_a_cost_never_moves_the_gross_figures(tmp_path, cost_bps):
    summary = _summary(tmp_path, _GUARD_ROWS, cost_bps=cost_bps)
    assert {r: (s.count, s.hit_rate, s.mean_alpha) for r, s in summary.by_rating.items()} == _GUARD_SCORES
    lines = summary.render().splitlines()
    for expected in _GUARD_LINES:
        assert any(line.startswith(expected) for line in lines), expected


@pytest.mark.unit
def test_cost_zero_nets_exactly_the_direction_adjusted_alpha(tmp_path):
    scores = _summary(tmp_path, _GUARD_ROWS, cost_bps=0).by_rating
    for rating, sign in (("Buy", 1), ("Sell", -1), ("Underweight", -1)):
        assert scores[rating].net_hit_rate == scores[rating].hit_rate
        assert scores[rating].mean_net == pytest.approx(sign * scores[rating].mean_alpha)


@pytest.mark.unit
@pytest.mark.parametrize("cost_bps", [-5, float("nan"), float("inf")])
def test_a_cost_must_be_a_finite_non_negative_figure(tmp_path, cost_bps):
    log = _log_with(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))])
    with pytest.raises(ValueError, match="cost"):
        summarize(log, cost_bps=cost_bps)


@pytest.mark.unit
def test_the_render_names_the_cost_and_the_assumption(tmp_path):
    text = _flat(_summary(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.01, 0.005))], cost_bps=30).render())
    assert "30 bps" in text
    assert "one round trip per directional call" in text
    assert "Sell treated as avoiding the stock at the same cost" in text
    assert "precision" not in text


@pytest.mark.unit
@pytest.mark.parametrize("cost_bps, warned", [(5, True), (None, True), (9.9, True), (10, False), (0, False)])
def test_a_cost_under_ten_bps_is_flagged_as_under_the_logs_precision(tmp_path, cost_bps, warned):
    """The log keeps alpha to 0.1 pp = 10 bps, so it cannot resolve a smaller cost.
    None is NVDA's US default, 5 bps. Zero deducts nothing, so there is nothing to resolve."""
    text = _flat(_summary(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.01, 0.005))],
                          cost_bps=cost_bps).render())
    assert ("precision" in text) is warned


# --- the cost through the CLI and the browser UI -------------------------------

def _cli(monkeypatch, log, *args):
    from typer.testing import CliRunner

    import cli.main as m
    from tradingagents.backtest import BacktestResult

    # The command appends to the trial ledger in results_dir; keep it beside the log.
    monkeypatch.setitem(m.DEFAULT_CONFIG, "results_dir", str(log.parent / "results"))
    swept = []
    monkeypatch.setattr(m, "run_backtest",
                        lambda *a, **kw: swept.append(kw) or BacktestResult(run_id="r", log_path=log))
    result = CliRunner().invoke(m.app, ["backtest", "RELIANCE.NS", "--start", "2026-01-05",
                                        "--end", "2026-01-05", *args])
    return result, swept


_NSE_BUY = [("RELIANCE.NS", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005))]


@pytest.mark.unit
def test_the_cli_summary_takes_the_markets_default(tmp_path, monkeypatch):
    result, _ = _cli(monkeypatch, _log_with(tmp_path, _NSE_BUY))
    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "30 bps" in text
    assert "mean net +0.20%" in text  # +0.5% less 30 bps


@pytest.mark.unit
def test_cli_cost_bps_zero_is_a_cost_of_zero_not_the_default(tmp_path, monkeypatch):
    result, _ = _cli(monkeypatch, _log_with(tmp_path, _NSE_BUY), "--cost-bps", "0")
    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "mean net +0.50%" in text
    assert "30 bps" not in text


@pytest.mark.unit
def test_cli_cost_bps_sets_one_figure_for_every_call(tmp_path, monkeypatch):
    result, _ = _cli(monkeypatch, _log_with(tmp_path, _NSE_BUY), "--cost-bps", "12")
    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "12 bps" in text and "mean net +0.38%" in text  # +0.5% less 12 bps


@pytest.mark.unit
@pytest.mark.parametrize("value", ["-5", "nan"])
def test_cli_refuses_a_bad_cost_before_the_sweep(tmp_path, monkeypatch, value):
    """A bad cost must not cost a whole paid sweep before it is reported."""
    result, swept = _cli(monkeypatch, _log_with(tmp_path, _NSE_BUY), f"--cost-bps={value}")
    assert result.exit_code == 1
    assert "cost" in result.output.lower()
    assert "Traceback" not in result.output
    assert swept == []


def _web_run(tmp_path, monkeypatch, rows):
    from tradingagents.default_config import DEFAULT_CONFIG

    run_dir = tmp_path / "results" / "backtest" / "run1"
    run_dir.mkdir(parents=True)
    log = TradingMemoryLog({"memory_log_path": str(run_dir / "trading_memory.md")})
    for ticker, date, decision, (raw, alpha) in rows:
        log.store_decision(ticker, date, decision)
        log.update_with_outcome(ticker, date, raw, alpha, 5, "note", "2026-02-01")
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    return run_dir / "trading_memory.md"


@pytest.mark.unit
def test_the_browser_results_carry_the_net_figures_and_the_cost(tmp_path, monkeypatch):
    from cli.webui import server

    _web_run(tmp_path, monkeypatch, _NSE_BUY + [("TCS.NS", "2026-01-05", "Rating: Hold\n\nx", (0.01, 0.004))])
    detail = server.backtest_detail("run1")

    rows = {r["rating"]: r for r in detail["byRating"]}
    assert rows["Buy"]["meanNet"] == pytest.approx(0.002) and rows["Buy"]["netHitRate"] == 1.0
    assert rows["Hold"]["meanNet"] is None and rows["Hold"]["netHitRate"] is None
    assert any("30 bps" in note for note in detail["costNotes"])


@pytest.mark.unit
def test_the_browser_summary_passes_the_cost_it_is_given(tmp_path, monkeypatch):
    from cli.webui.jobs import backtest_summary

    log = _web_run(tmp_path, monkeypatch, _NSE_BUY)
    assert backtest_summary(log).by_rating["Buy"].mean_net == pytest.approx(0.002)
    assert backtest_summary(log, cost_bps=0).by_rating["Buy"].mean_net == pytest.approx(0.005)


# --- follow the ratings: period returns, Sharpe, PSR and DSR -------------------
#
# One period per analysis date: the mean net directional return of that date's
# directional calls (R2's net: direction * alpha - cost). The deflated Sharpe
# ratio takes N, the configurations tried on overlapping data, from the trial ledger.

_PERIOD_ROWS = [
    ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),           # +0.5% - 0.3% = +0.2%
    ("AAPL", "2026-01-05", "Rating: Sell\n\nx", (-0.01, -0.002)),        # +0.2% - 0.3% = -0.1%
    ("MSFT", "2026-01-05", "Rating: Hold\n\nx", (0.02, 0.01)),           # Hold: no call
    ("NVDA", "2026-01-12", "Rating: Hold\n\nx", (0.01, 0.004)),          # no call that date: no period
    ("NVDA", "2026-01-19", "Rating: Overweight\n\nx", (0.02, 0.01)),     # +1.0% - 0.3% = +0.7%
    ("AAPL", "2026-01-19", "Rating: Buy\n\nx", None),                    # pending: not scored
    ("AAPL", "2026-01-26", "Rating: Underweight\n\nx", (0.01, 0.004)),   # -0.4% - 0.3% = -0.7%
]


@pytest.mark.unit
def test_each_analysis_date_is_one_period_of_its_calls_mean_net_return(tmp_path):
    summary = _summary(tmp_path, _PERIOD_ROWS, cost_bps=30)
    # 2026-01-05: (+0.2% - 0.1%) / 2 = +0.05%; 2026-01-19: +0.7%; 2026-01-26: -0.7%.
    assert [day for day, _ in summary.periods] == ["2026-01-05", "2026-01-19", "2026-01-26"]
    assert [r for _, r in summary.periods] == pytest.approx([0.0005, 0.007, -0.007])
    assert summary.window == ("2026-01-05", "2026-01-26")  # the analysis dates scored


@pytest.mark.unit
def test_the_periods_use_each_markets_default_cost_like_the_net_figures(tmp_path):
    # RELIANCE.NS +0.5% at 30 bps nets +0.2%; NVDA +0.5% at 5 bps nets +0.45%; the date's mean +0.325%.
    summary = _summary(tmp_path, [
        ("RELIANCE.NS", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.005)),
    ])
    assert summary.periods == [("2026-01-05", pytest.approx(0.00325))]


@pytest.mark.unit
def test_too_few_periods_are_reported_as_such_not_as_a_ratio(tmp_path):
    summary = _summary(tmp_path, _PERIOD_ROWS, cost_bps=30)
    summary.trials = _count(1, 1)
    text = _flat(summary.render())
    assert summary.sharpe() is None
    assert "too few periods" in text and "3 analysis dates" in text
    assert "Probabilistic Sharpe ratio" not in text and "Deflated Sharpe ratio" not in text


# Six periods at no cost, worked by hand:
#   2026-01-05: Buy +0.3% and Sell +0.3% net +0.3% and -0.3%, mean 0
#   2026-01-12 to 2026-02-02: a call each with alpha 0 (the Hold on 02-02 is not a call)
#   2026-02-09: Sell -1.2% nets +1.2%
# r = 0, 0, 0, 0, 0, 0.012: mean 0.002; deviations -0.002 (five times) and +0.010; over T = 6,
#   m2 = (5 * 4e-6 + 1e-4) / 6 = 2e-5,  m3 = (5 * -8e-9 + 1e-6) / 6 = 1.6e-7,
#   m4 = (5 * 1.6e-11 + 1e-8) / 6 = 1.68e-9
#   Sharpe = 0.002 / sqrt(2e-5) = 1/sqrt(5) = 0.447; skew = 1.6e-7 / (2e-5)^1.5 = 4/sqrt(5) = 1.789;
#   kurtosis = 1.68e-9 / (2e-5)^2 = 4.2
#   1 - skew * SR + (kurtosis - 1) / 4 * SR^2 = 1 - 0.8 + 0.16 = 0.36
#   PSR = Phi(SR * sqrt(5) / 0.6) = Phi(5/3) = 0.952
#   N = 3: E[max] = (1 - g) z(2/3) + g z(1 - 1/(3e)) = 0.85280 (V = 1 / (T - 1));
#   DSR = Phi((SR * sqrt(5) - 0.85280) / 0.6) = Phi(0.24533) = 0.597
_SIX_PERIODS = [
    ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.003)),
    ("AAPL", "2026-01-05", "Rating: Sell\n\nx", (0.01, 0.003)),
    ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (0.0, 0.0)),
    ("NVDA", "2026-01-19", "Rating: Underweight\n\nx", (0.0, 0.0)),
    ("NVDA", "2026-01-26", "Rating: Overweight\n\nx", (0.0, 0.0)),
    ("NVDA", "2026-02-02", "Rating: Buy\n\nx", (0.0, 0.0)),
    ("MSFT", "2026-02-02", "Rating: Hold\n\nx", (0.05, 0.05)),
    ("AAPL", "2026-02-09", "Rating: Sell\n\nx", (-0.012, -0.012)),
]


def _count(configurations, runs):
    from tradingagents.trials import TrialCount

    return TrialCount(configurations=configurations, runs=runs)


@pytest.mark.unit
def test_the_sharpe_ratio_of_following_the_ratings(tmp_path):
    import math

    stats = _summary(tmp_path, _SIX_PERIODS, cost_bps=0).sharpe()
    assert stats.periods == 6
    assert stats.sharpe == pytest.approx(1 / math.sqrt(5))
    assert stats.skew == pytest.approx(4 / math.sqrt(5))
    assert stats.kurtosis == pytest.approx(4.2)


@pytest.mark.unit
def test_the_render_gives_the_probabilistic_and_deflated_ratios_and_the_ledgers_counts(tmp_path):
    summary = _summary(tmp_path, _SIX_PERIODS, cost_bps=0)
    summary.trials = _count(3, 4)
    text = _flat(summary.render())
    assert "6 periods" in text
    assert "Sharpe ratio +0.45" in text and "skew +1.79" in text and "kurtosis 4.20" in text
    assert "Probabilistic Sharpe ratio: 0.95" in text
    assert ("Deflated Sharpe ratio: 0.60 (N = 3 configurations); below 0.95 the result is not "
            "distinguishable from the best of 3 random tries") in text
    assert "3 configurations, 4 runs" in text


@pytest.mark.unit
def test_one_configuration_is_deflated_by_nothing(tmp_path):
    summary = _summary(tmp_path, _SIX_PERIODS, cost_bps=0)
    summary.trials = _count(1, 1)
    text = _flat(summary.render())
    # PSR = DSR = Phi(5/3) = 0.952 with N = 1.
    assert "Deflated Sharpe ratio: 0.95 (N = 1 configuration)" in text
    assert "1 configuration, 1 run" in text


@pytest.mark.unit
def test_without_a_trial_count_nothing_is_called_deflated(tmp_path):
    text = _flat(_summary(tmp_path, _SIX_PERIODS, cost_bps=0).render())
    assert "Probabilistic Sharpe ratio: 0.95" in text
    assert "Deflated Sharpe ratio" not in text


@pytest.mark.unit
def test_periods_that_do_not_vary_have_no_sharpe_ratio(tmp_path):
    rows = [("NVDA", f"2026-01-{day:02d}", "Rating: Buy\n\nx", (0.0, 0.0)) for day in (5, 12, 19, 26)]
    rows += [("NVDA", f"2026-02-{day:02d}", "Rating: Buy\n\nx", (0.0, 0.0)) for day in (2, 9)]
    summary = _summary(tmp_path, rows, cost_bps=0)
    summary.trials = _count(1, 1)
    assert summary.sharpe() is None
    text = _flat(summary.render())
    assert "do not vary" in text and "Probabilistic Sharpe ratio" not in text


# R2's render of the guard rows at 30 bps, from `1ea157c`: the new lines come after it.
_R2_RENDER_30 = [
    "Resolved cells: 5 · pending: 0",
    "- Buy: n=2, called the direction 50%, mean alpha +1.00% vs the benchmark; "
    "after costs right 50%, mean net +0.70%",
    "- Sell: n=1, called the direction 100%, mean alpha -5.00% vs the benchmark; "
    "after costs right 100%, mean net +4.70%",
    "- Hold: n=1, no direction claimed, mean alpha +0.50% vs the benchmark",
    "- Underweight: n=1, called the direction 0%, mean alpha +1.20% vs the benchmark; "
    "after costs right 0%, mean net -1.50%",
    "",
    "Alpha is measured over 5 trading days after each analysis date. One model sampling per cell, "
    "and text feeds are not archived, so these figures are indicative rather than repeatable.",
    "Costs: 30 bps round trip on every directional call, as given. Net is a call's alpha in its own "
    "direction less the cost: one round trip per directional call, Sell treated as avoiding the stock "
    "at the same cost; Hold claims no direction and is not charged.",
]


@pytest.mark.unit
def test_r2s_summary_is_unchanged_above_the_new_lines(tmp_path):
    lines = _summary(tmp_path, _GUARD_ROWS, cost_bps=30).render().splitlines()
    assert lines[:len(_R2_RENDER_30)] == _R2_RENDER_30


# --- the trial ledger ----------------------------------------------------------

@pytest.mark.unit
def test_a_sweep_records_the_settings_that_make_its_configuration(tmp_path):
    from tradingagents.trials import config_hash

    settings = {**_config(tmp_path), "llm_provider": "openai", "deep_think_llm": "deep-a",
                "quick_think_llm": "quick-a", "holding_period_days": 5}
    first = run_backtest(["NVDA", "NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], settings,
                         selected_analysts=["market", "news"], run_id="a")
    assert first.config["tickers"] == ["NVDA", "AAPL"]
    assert first.config["dates"] == ["2026-01-05", "2026-01-12"]
    assert first.config["analysts"] == ["market", "news"]
    assert first.config["asset_type"] == "stock"
    assert first.config["settings"]["deep_think_llm"] == "deep-a"
    assert first.config["settings"]["holding_period_days"] == 5

    # Where a run is written and what it is called are not part of what was tried.
    moved = run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"],
                         {**settings, "results_dir": str(tmp_path / "elsewhere")},
                         selected_analysts=["market", "news"], run_id="b")
    assert config_hash(moved.config) == config_hash(first.config)
    other = run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], {**settings, "deep_think_llm": "deep-b"},
                         selected_analysts=["market", "news"], run_id="c")
    assert config_hash(other.config) != config_hash(first.config)


@pytest.mark.unit
def test_the_backtest_command_appends_one_ledger_line_per_summary(tmp_path, monkeypatch):
    log = _log_with(tmp_path, _SIX_PERIODS)
    first, _ = _cli(monkeypatch, log, "--cost-bps", "0")
    again, _ = _cli(monkeypatch, log, "--cost-bps", "0")
    costed, _ = _cli(monkeypatch, log, "--cost-bps", "12")  # another cost: another configuration
    for result in (first, again, costed):
        assert result.exit_code == 0, result.output

    entries = [json.loads(line) for line in
               (tmp_path / "results" / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in entries] == ["backtest"] * 3
    assert [e["config"]["cost_bps"] for e in entries] == [0, 0, 12]
    assert entries[0]["window"] == ["2026-01-05", "2026-02-09"]
    assert entries[0]["periods"] == 6 and entries[0]["sharpe"] == pytest.approx(0.4472136)
    assert entries[0]["run_id"] == "r"

    assert "1 configuration, 1 run" in _flat(first.output)
    assert "Deflated Sharpe ratio: 0.95 (N = 1 configuration)" in _flat(first.output)
    assert "1 configuration, 2 runs" in _flat(again.output)
    assert "2 configurations, 3 runs" in _flat(costed.output)
    assert "(N = 2 configurations)" in _flat(costed.output)
    assert "trials.jsonl" in _flat(first.output)


@pytest.mark.unit
def test_the_ledger_follows_the_results_setting_and_never_the_home_folder(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as m
    from tradingagents.backtest import BacktestResult
    from tradingagents.default_config import build_default_config

    home, scratch = tmp_path / "home", tmp_path / "scratch-results"
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(scratch))
    monkeypatch.setattr(m, "DEFAULT_CONFIG", build_default_config())  # the configuration as a fresh process reads it
    log = _log_with(tmp_path, _SIX_PERIODS)
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: BacktestResult(run_id="r", log_path=log))

    result = CliRunner().invoke(m.app, ["backtest", "NVDA", "--start", "2026-01-05", "--end", "2026-01-05"])

    assert result.exit_code == 0, result.output
    assert len((scratch / "trials.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert not home.exists()


@pytest.mark.unit
def test_a_browser_sweep_is_recorded_when_it_finishes_and_not_when_stopped(tmp_path, monkeypatch):
    from cli.webui import jobs
    from tradingagents.backtest import BacktestResult

    log = _log_with(tmp_path, _SIX_PERIODS)
    stopped = []
    monkeypatch.setattr(jobs, "run_backtest", lambda *a, **kw: BacktestResult(
        run_id=kw["run_id"], log_path=log, stopped=bool(stopped)))
    config = {"research_mode": "standard", "results_dir": str(tmp_path / "results")}

    finished = jobs.BacktestJob(["NVDA"], ["2026-01-05"], config, ["market"], run_id="done").start()
    finished._thread.join(10)
    stopped.append(True)
    halted = jobs.BacktestJob(["NVDA"], ["2026-01-05"], config, ["market"], run_id="halted").start()
    halted._thread.join(10)

    assert (finished.status, halted.status) == (jobs.DONE, jobs.CANCELLED)
    [line] = (tmp_path / "results" / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert (entry["kind"], entry["run_id"], entry["config"]["cost_bps"]) == ("backtest", "done", None)
