"""Price features for the no-LLM baseline that ``learn`` holds the reports to.

When Reasoning Fails (arXiv 2511.08608) found that on NIFTY names, after costs,
LLM rankings beat neither a small LLM nor ridge regression or random forests on
price features. So ``learn`` also fits the same L2 logistic model on price alone,
and on the rating plus price, and each LLM gain has to clear those as well as the
rating.

Each decision's features come from the daily closes dated strictly before its
analysis date, so nothing the decision could not have known moves them:

- ``ret_20d``, ``ret_60d``, ``ret_120d``: the simple return over the last 20, 60
  and 120 bars (close / close k bars earlier - 1);
- ``vol_20d``: the standard deviation (over 20) of the last 20 daily log returns;
- ``dist_ma50``, ``dist_ma200``: the last close over the 50- and 200-bar average, - 1.

A feature that needs more bars than the decision has (200 for ``dist_ma200``) is
left out, and the model fills it with its training mean. The closes are the
adjusted ones the analysis already downloaded and cached (``load_ohlcv``); a
ticker whose cache does not reach its latest analysis date is fetched again
through the same dataflow.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from tradingagents.dataflows.vendors.yahoo.ohlcv import _clean_dataframe, cache_path, load_ohlcv

logger = logging.getLogger(__name__)

PRICE_COLUMNS = ("ret_20d", "ret_60d", "ret_120d", "vol_20d", "dist_ma50", "dist_ma200")

Bar = tuple[str, float]  # (YYYY-MM-DD, close)


def price_features(bars: Sequence[Bar], analysis_date: str) -> dict[str, float]:
    """The features a decision dated ``analysis_date`` could have known, from ``bars``.

    Only bars dated before ``analysis_date`` are read, in date order, and a feature
    needing more of them than there are is left out.
    """
    closes = [close for day, close in sorted(bars) if day < analysis_date
              and close == close and close > 0]  # a missing or broken close is no bar
    features = {}
    for k in (20, 60, 120):
        if len(closes) > k:
            features[f"ret_{k}d"] = closes[-1] / closes[-1 - k] - 1
    if len(closes) > 20:
        logs = [math.log(later / earlier) for earlier, later in zip(closes[-21:-1], closes[-20:], strict=True)]
        mean = math.fsum(logs) / 20
        features["vol_20d"] = math.sqrt(math.fsum((r - mean) ** 2 for r in logs) / 20)
    for k in (50, 200):
        if len(closes) >= k:
            features[f"dist_ma{k}"] = closes[-1] / (math.fsum(closes[-k:]) / k) - 1
    return features


def load_bars(ticker: str, through: str) -> list[Bar]:
    """Daily closes of ``ticker`` dated on or before ``through``.

    The analysis's own cached download is used when its last bar reaches
    ``through``: the backtest that made the decisions downloaded it. Otherwise the
    closes are fetched as reported (not gap-filled) through ``load_ohlcv``, which
    caches them.
    """
    end = pd.Timestamp(through)
    frame = None
    path = cache_path(ticker)
    if os.path.exists(path):
        try:
            cached = pd.read_csv(path, on_bad_lines="skip", encoding="utf-8")
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable price cache %s: %s", path, exc)
            cached = None
        if cached is not None and not cached.empty and "Close" in cached.columns:
            cached = _clean_dataframe(cached).dropna(subset=["Close"])
            if not cached.empty and cached["Date"].max() >= end:
                frame = cached
    if frame is None:
        frame = load_ohlcv(ticker, through, fill_gaps=False)
    frame = frame[frame["Date"] <= end]
    return [(day.strftime("%Y-%m-%d"), float(close))
            for day, close in zip(frame["Date"], frame["Close"], strict=True)]


def add_price_features(decisions: Sequence[Any], rows: Sequence[dict],
                       bars_for: Callable[[str, str], Sequence[Bar]] = load_bars) -> Mapping[str, str]:
    """Add each decision's price features to its row; return the tickers with no prices and why.

    Each ticker's bars are loaded once, through its latest analysis date. A ticker
    whose bars cannot be loaded keeps no price columns, like one with too little
    history, so one unreachable symbol does not stop the evaluation.
    """
    latest: dict[str, str] = {}
    for d in decisions:
        latest[d.ticker] = max(latest.get(d.ticker, d.date), d.date)
    bars: dict[str, Sequence[Bar]] = {}
    errors: dict[str, str] = {}
    for ticker, through in latest.items():
        try:
            bars[ticker] = bars_for(ticker, through)
        except Exception as exc:  # an outage, an unknown symbol, a bad cache: no prices for it
            logger.warning("No price history for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
    for d, row in zip(decisions, rows, strict=True):
        if d.ticker in bars:
            row.update(price_features(bars[d.ticker], d.date))
    return errors
