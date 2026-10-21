"""Price features for the no-LLM baseline in ``learn`` (Phase R, R4).

When Reasoning Fails (arXiv 2511.08608): on NIFTY names, after costs, LLM rankings
beat neither a small LLM nor ridge or random forests on price features. So
``learn`` also fits a model on price alone, from features computed in code from
the daily closes dated before each analysis date: returns over 20, 60 and 120
trading days, the 20-day volatility of daily log returns, and the distance to
the 50- and 200-day averages.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from tradingagents.price_features import (
    PRICE_COLUMNS,
    add_price_features,
    load_bars,
    price_features,
)


def _bars(closes, start=date(2025, 1, 1)):
    """One bar a day from ``start``; the features count bars, not calendar days."""
    return [((start + timedelta(days=i)).isoformat(), float(c)) for i, c in enumerate(closes)]


def _after(bars):
    return (date.fromisoformat(bars[-1][0]) + timedelta(days=1)).isoformat()


@pytest.mark.unit
def test_the_columns():
    assert PRICE_COLUMNS == ("ret_20d", "ret_60d", "ret_120d", "vol_20d", "dist_ma50", "dist_ma200")


@pytest.mark.unit
def test_returns_and_distances_of_a_hand_series():
    # Closes 100, 101, ..., 349 (250 bars); the last close is 349.
    #   ret_20d  = 349 / 329 - 1 = 0.0607903      (the close 20 bars earlier is 329)
    #   ret_60d  = 349 / 289 - 1 = 0.2076125
    #   ret_120d = 349 / 229 - 1 = 0.5240175
    #   dist_ma50  = 349 / mean(300..349) - 1 = 349 / 324.5 - 1 = 0.0755008
    #   dist_ma200 = 349 / mean(150..349) - 1 = 349 / 249.5 - 1 = 0.3987976
    bars = _bars(range(100, 350))
    features = price_features(bars, _after(bars))
    assert features["ret_20d"] == pytest.approx(349 / 329 - 1)
    assert features["ret_60d"] == pytest.approx(349 / 289 - 1)
    assert features["ret_120d"] == pytest.approx(349 / 229 - 1)
    assert features["dist_ma50"] == pytest.approx(349 / 324.5 - 1)
    assert features["dist_ma200"] == pytest.approx(349 / 249.5 - 1)
    assert features["ret_20d"] == pytest.approx(0.0607903, abs=1e-7)
    assert features["dist_ma200"] == pytest.approx(0.3987976, abs=1e-7)


@pytest.mark.unit
def test_the_volatility_of_a_hand_series():
    # Closes alternate 100, 110, 100, ... over 21 bars: 20 daily log returns, ten of
    # +ln(1.1) and ten of -ln(1.1), mean 0, so their standard deviation (over 20) is
    # ln(1.1) = 0.0953102. The last close equals the one 20 bars earlier: ret_20d = 0.
    bars = _bars([100 if i % 2 == 0 else 110 for i in range(21)])
    features = price_features(bars, _after(bars))
    assert features["vol_20d"] == pytest.approx(math.log(1.1))
    assert features["vol_20d"] == pytest.approx(0.0953102, abs=1e-7)
    assert features["ret_20d"] == pytest.approx(0.0)


@pytest.mark.unit
def test_no_bar_on_or_after_the_analysis_date_is_used():
    history = _bars(range(100, 350))     # 2025-01-01 to 2025-09-07
    day = "2025-08-09"                   # 220 bars before it, so every feature is there
    before = [bar for bar in history if bar[0] < day]
    expected = price_features(before, day)
    assert set(expected) == set(PRICE_COLUMNS)
    assert expected["ret_20d"] == pytest.approx(319 / 299 - 1)  # the last close before the date is 319

    # Change the analysis date's own bar and every later one, and add more after them.
    moved = [(d, c if d < day else c * 10) for d, c in history]
    moved += _bars([1.0] * 30, start=date(2025, 9, 8))
    assert price_features(moved, day) == expected
    assert price_features(history, day) == expected
    assert price_features(list(reversed(history)), day) == expected  # in any order


@pytest.mark.unit
@pytest.mark.parametrize("n_bars, present", [
    (200, set(PRICE_COLUMNS)),
    (199, set(PRICE_COLUMNS) - {"dist_ma200"}),          # under 200 bars: no 200-day average
    (121, {"ret_20d", "ret_60d", "ret_120d", "vol_20d", "dist_ma50"}),
    (120, {"ret_20d", "ret_60d", "vol_20d", "dist_ma50"}),
    (50, {"ret_20d", "vol_20d", "dist_ma50"}),
    (21, {"ret_20d", "vol_20d"}),
    (20, set()),
    (0, set()),
])
def test_too_little_history_leaves_the_affected_columns_out(n_bars, present):
    bars = _bars([100 + (i % 7) for i in range(n_bars)])
    assert set(price_features(bars, "2026-01-01")) == present


@pytest.mark.unit
def test_each_decision_gets_the_features_of_its_own_date_and_a_failed_ticker_none(tmp_path):
    class D:
        def __init__(self, ticker, day):
            self.ticker, self.date = ticker, day

    history = _bars(range(100, 350))
    decisions = [D("NVDA", "2025-08-09"), D("NVDA", "2025-09-01"), D("FAIL", "2025-08-09"), D("NEW", "2025-08-09")]
    rows = [{"rating": 1.0} for _ in decisions]
    asked = []

    def bars_for(ticker, through):
        asked.append((ticker, through))
        if ticker == "FAIL":
            raise OSError("Yahoo Finance is unreachable")
        return history if ticker == "NVDA" else history[190:220]  # 30 bars, 2025-07-10 to 08-08

    errors = add_price_features(decisions, rows, bars_for)

    assert sorted(asked) == [("FAIL", "2025-08-09"), ("NEW", "2025-08-09"), ("NVDA", "2025-09-01")]  # once each
    assert rows[0] == {"rating": 1.0, **price_features(history, "2025-08-09")}
    assert rows[1]["ret_20d"] == pytest.approx(342 / 322 - 1)   # its own date: the last close before is 342
    assert rows[2] == {"rating": 1.0}                            # no prices: no price columns
    assert set(rows[3]) == {"rating", "ret_20d", "vol_20d"}      # 30 bars: too few for the rest
    assert errors == {"FAIL": "Yahoo Finance is unreachable"}


# --- where the bars come from -------------------------------------------------------


def _cache(tmp_path, monkeypatch, last_day):
    from tradingagents.dataflows.config import set_config

    set_config({"data_cache_dir": str(tmp_path / "cache")})
    (tmp_path / "cache").mkdir()
    days = [(date.fromisoformat(last_day) - timedelta(days=i)).isoformat() for i in range(5)][::-1]
    lines = ["Date,Open,High,Low,Close,Volume"]
    lines += [f"{d} 00:00:00-05:00,1,1,1,{100 + i},10" for i, d in enumerate(days)]
    (tmp_path / "cache" / "NVDA-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.unit
def test_the_analysis_cached_download_is_read_when_it_reaches_the_date(tmp_path, monkeypatch):
    import tradingagents.price_features as pf

    _cache(tmp_path, monkeypatch, "2026-01-09")
    monkeypatch.setattr(pf, "load_ohlcv", lambda *a, **kw: pytest.fail("fetched although cached"))

    bars = load_bars("NVDA", "2026-01-08")

    assert bars == [("2026-01-05", 100.0), ("2026-01-06", 101.0), ("2026-01-07", 102.0), ("2026-01-08", 103.0)]


@pytest.mark.unit
def test_a_cache_that_stops_before_the_date_is_fetched_again(tmp_path, monkeypatch):
    import pandas as pd

    import tradingagents.price_features as pf

    _cache(tmp_path, monkeypatch, "2026-01-09")
    fetched = []

    def load_ohlcv(symbol, as_of_date, fill_gaps=True):
        fetched.append((symbol, as_of_date, fill_gaps))
        return pd.DataFrame({"Date": pd.to_datetime(["2026-01-12", "2026-01-13"]), "Close": [7.0, 8.0]})

    monkeypatch.setattr(pf, "load_ohlcv", load_ohlcv)

    assert load_bars("NVDA", "2026-01-13") == [("2026-01-12", 7.0), ("2026-01-13", 8.0)]
    assert fetched == [("NVDA", "2026-01-13", False)]  # as reported, not gap-filled

