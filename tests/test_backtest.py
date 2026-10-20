"""Backtesting: many single-shot decisions, scored by the memory log.

A run already records its rating and later settles it with realized and alpha
return against the regional benchmark. A backtest is that machinery over a grid
of tickers and dates, aggregated. It evaluates decision quality; it does not
simulate a portfolio, so there is no execution and no equity curve. Costs are
one round trip taken off each directional call, not a fill.
"""

from __future__ import annotations

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
    # +0.3% at 30 bps nets exactly zero; float noise in 0.3 / 100 - 30 / 10000 must not make it a hit.
    buy = _summary(tmp_path, [("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.01, 0.003))],
                   cost_bps=30).by_rating["Buy"]
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


# The unchanged code's figures and lines for this log (`.scratch\r2\old_render.py` on
# main `4ab64a3`). A cost changes only the new net figures, never these.
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
