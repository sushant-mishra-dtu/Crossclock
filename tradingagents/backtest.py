"""Run the graph over a grid of tickers and dates, and score what came back.

One run yields one decision, so it cannot say whether the system decides well.
This runs the same machinery over many (ticker, date) cells and reads the
aggregate. The memory log is the results table: every run already records its
rating and later settles it with realized and alpha return against the
instrument's regional benchmark, so there is nothing to record separately.

Scope: this evaluates decision quality. It is not a portfolio simulator, and
must not grow one. Turning a rating into a filled order needs a quantity, a fill
price and a cash ledger, none of which the system has; inventing them here would
put an execution model behind an evaluation tool. Cells are therefore
independent, and a portfolio, when given, is the same standing book for every
cell rather than a position carried forward. Trading costs follow the same rule:
one round trip is taken off each directional call's alpha, with no fill behind it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tradingagents.agents.rating import RATING_REVIEW
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import crypto_base, safe_ticker_component
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog
from tradingagents.run_control import DeadlineExceeded, RunCancelled

logger = logging.getLogger(__name__)


def iter_grid(start_date: str, end_date: str, every_n_days: int = 1) -> list[str]:
    """Analysis dates from ``start_date``, never past today.

    A future date has no outcome to settle against, and the graph rejects one, so
    the grid stops at the present rather than producing cells that cannot score.
    """
    start, end = _canonical(start_date), _canonical(end_date)
    if every_n_days < 1:
        raise ValueError("every_n_days must be at least 1")
    if end < start:
        raise ValueError(f"the grid ends before it starts: {end_date} is before {start_date}")

    last = min(end, datetime.strptime(get_current_date(), "%Y-%m-%d"))
    dates, cursor = [], start
    while cursor <= last:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=every_n_days)
    return dates


def _canonical(date: str) -> datetime:
    """Parse a grid bound, rejecting anything the run date would also reject."""
    try:
        parsed = datetime.strptime(str(date), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}") from exc
    if parsed.strftime("%Y-%m-%d") != str(date):
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}")
    return parsed


def _alpha(entry: dict) -> float | None:
    """Alpha return of a settled entry, or None when it has not settled.

    The log stores it as a percentage rounded to one decimal, so aggregates here
    are accurate to 0.1 of a percentage point, not to the raw quote.
    """
    text = (entry.get("alpha") or "").strip().rstrip("%")
    try:
        return float(text) / 100
    except ValueError:
        return None


@dataclass
class BacktestResult:
    run_id: str
    log_path: Path
    cells_run: int = 0
    skipped: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    settlement_failures: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False  # should_stop ended the sweep before every cell ran


# What each rating claims will happen, so an outcome can be scored against it.
# Hold claims no direction, so nothing about alpha proves it right or wrong.
_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}

# One round trip (a buy and the sale that closes it) in basis points of the value
# traded, by the market a ticker trades in.
#
# NSE/BSE delivery, 30 bps. Charges on a buy and a sale of equal value, at the
# rates of Zerodha's schedule (zerodha.com/charges, read 2026-10-20):
#   STT, 0.1% on the buy and 0.1% on the sale (Finance (No. 2) Act 2004;
#     Budget 2026 raised only the F&O rates)                             20.0
#   exchange transaction charge, NSE 0.00307% a side (BSE 0.00375%: 0.75)  0.6
#   SEBI turnover fee, Rs 10 a crore (0.0001%) a side                     0.02
#   stamp duty, 0.015% on the buy only (Indian Stamp Act, from July 2020)  1.5
#   GST, 18% of the exchange charge and SEBI fee (no delivery brokerage)   0.1
#   statutory charges                                                    22.2
#   spread and impact, about 4 bps a side on a liquid large cap
#     (an assumption, not a measurement)                                  7.8
# Left out: the flat depository charge on each sale (Rs 15.34 a scrip: 1.5 bps
# of a Rs 1 lakh sale, 15 bps of Rs 10,000) and full-service brokerage.
#
# US stocks, 5 bps: no commission at the main retail brokers; on the sale the
# SEC's Section 31 fee, $20.60 a million from 4 April 2026 (0.2 bps; SEC fee
# rate advisory 2026-2), and FINRA's trading activity fee, $0.000195 a share
# capped at $9.79 (under 0.1 bps at usual prices); the rest is spread and
# impact, about 2.4 bps a side on a liquid large cap (an assumption).
#
# No figure was researched for other markets or crypto, so they take the
# higher default rather than an optimistic one; a cost given to summarize()
# (the CLI's --cost-bps) replaces every default.
INDIA_DELIVERY_COST_BPS = 30.0
US_STOCK_COST_BPS = 5.0

# The log keeps alpha to 0.1 of a percentage point, so it cannot resolve a smaller cost.
_LOG_PRECISION_BPS = 10


def _cost_market(ticker: str) -> tuple[str, float]:
    """The market whose default cost a ticker pays, by its suffix, and that cost."""
    symbol = ticker.strip().upper()
    if symbol.endswith((".NS", ".BO")):
        return "NSE/BSE delivery", INDIA_DELIVERY_COST_BPS
    if "." not in symbol and crypto_base(symbol) is None:
        return "US stocks", US_STOCK_COST_BPS
    return "other markets (no figure researched, so the higher default)", INDIA_DELIVERY_COST_BPS


def default_cost_bps(ticker: str) -> float:
    """Round-trip cost in basis points for a ticker's market: 30 for NSE/BSE
    delivery (``.NS``, ``.BO``), 5 for US stocks (no suffix), and 30 for any
    other market or crypto."""
    return _cost_market(ticker)[1]


def check_cost_bps(cost_bps: float | None) -> None:
    """Refuse a cost that is negative or not a number; None means each market's default."""
    if cost_bps is not None and not (math.isfinite(cost_bps) and cost_bps >= 0):
        raise ValueError(f"the cost must be a finite number of basis points, 0 or more; got {cost_bps}")


def _bps(value: float) -> str:
    return f"{value:g} bps"


@dataclass
class RatingScore:
    count: int
    hit_rate: float | None
    mean_alpha: float
    # Net of one round trip, in the call's own direction; None for Hold.
    net_hit_rate: float | None = None
    mean_net: float | None = None


@dataclass
class BacktestSummary:
    resolved: int
    pending: int
    by_rating: dict[str, RatingScore]
    unscored: int = 0
    holding: str = ""
    cost_bps: float | None = None  # one figure for every call; None: each market's default
    costs: dict[str, float] = field(default_factory=dict)  # market -> bps taken off its calls

    def render(self) -> str:
        lines = [f"Resolved cells: {self.resolved} · pending: {self.pending}"
                 + (f" · unscored: {self.unscored}" if self.unscored else "")]
        for rating, score in self.by_rating.items():
            called = (f"called the direction {score.hit_rate:.0%}"
                      if score.hit_rate is not None else "no direction claimed")
            net = (f"; after costs right {score.net_hit_rate:.0%}, mean net {score.mean_net:+.2%}"
                   if score.mean_net is not None else "")
            lines.append(
                f"- {rating}: n={score.count}, {called}, "
                f"mean alpha {score.mean_alpha:+.2%} vs the benchmark{net}"
            )
        lines.append("")
        if self.pending:
            lines.append("Pending cells are not scored above; re-run to settle them.")
        lines.append(
            f"Alpha is measured over {self.holding} after each analysis date. "
            "One model sampling per cell, and text feeds are not archived, so "
            "these figures are indicative rather than repeatable."
        )
        lines.extend(self.cost_lines())
        return "\n".join(lines)

    def cost_lines(self) -> list[str]:
        """What the net figures took off each call; empty when no directional call is scored."""
        if not self.costs:
            return []
        if self.cost_bps is not None:
            used = f"{_bps(self.cost_bps)} round trip on every directional call, as given"
        else:
            used = (", ".join(f"{_bps(bps)} for {market}" for market, bps in self.costs.items())
                    + ", a round trip each (the default for each ticker's market; --cost-bps sets one figure)")
        lines = [f"Costs: {used}. Net is a call's alpha in its own direction less the cost: "
                 "one round trip per directional call, Sell treated as avoiding the stock "
                 "at the same cost; Hold claims no direction and is not charged."]
        low = sorted({bps for bps in self.costs.values() if 0 < bps < _LOG_PRECISION_BPS})
        if low:
            lines.append(f"Warning: {' and '.join(_bps(bps) for bps in low)} is under the log's "
                         "precision (alpha is kept to 0.1 of a percentage point, 10 bps), so the "
                         "net figures cannot resolve it.")
        return lines


def run_backtest(
    tickers: list[str],
    dates: list[str],
    config: dict,
    asset_type: str = "stock",
    portfolio=None,
    selected_analysts=("market", "social", "news", "fundamentals"),
    run_id: str | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
    on_cell: Callable[[str, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BacktestResult:
    """Analyze every ticker on every date, into a memory log of this run's own.

    The live log stays untouched: a sweep would otherwise flood the context that
    real runs read back. Cells already in this run's log are skipped, so an
    interrupted sweep resumes by being run again. ``progress(done, total,
    ticker, date)`` is called before each cell that runs.

    ``on_cell(ticker, date)`` is called before each cell runs. ``should_stop``
    is checked before each cell; once it returns true no further cell starts and
    nothing is settled, so a stopped sweep resumes like an interrupted one.
    """
    from tradingagents.modes import require_standard

    require_standard(config)
    # run_id becomes a path segment, so it is validated like a ticker: an
    # absolute or dotted value would otherwise place the run outside results_dir.
    run_id = safe_ticker_component(run_id or datetime.now().strftime("%Y%m%d_%H%M%S"))
    run_dir = Path(config["results_dir"]) / "backtest" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_config = {**config, "results_dir": str(run_dir),
                  "memory_log_path": str(run_dir / "trading_memory.md")}

    graph = TradingAgentsGraph(selected_analysts, config=run_config)
    result = BacktestResult(run_id=run_id, log_path=Path(run_config["memory_log_path"]))
    done = {(e["ticker"], e["date"]) for e in graph.memory_log.load_entries()}

    # A ticker or date given twice is one cell, run and settled once.
    tickers, dates = list(dict.fromkeys(tickers)), list(dict.fromkeys(dates))
    cells = [(ticker, date) for ticker in tickers for date in dates]
    todo = [cell for cell in cells if cell not in done]
    result.skipped = len(cells) - len(todo)
    for index, (ticker, date) in enumerate(todo, 1):
        if should_stop is not None and should_stop():
            result.stopped = True
            break
        if progress:
            progress(index, len(todo), ticker, date)
        if on_cell is not None:
            on_cell(ticker, date)
        try:
            graph.propagate(ticker, date, asset_type, portfolio=portfolio)
            result.cells_run += 1
        except RunCancelled:  # a stop ends the sweep like should_stop, settling nothing
            result.stopped = True
            break
        # A cell past its deadline is one failed cell; its checkpoint lets a re-run resume it.
        except (Exception, DeadlineExceeded) as exc:  # one unreachable vendor must not end the sweep
            logger.warning("Backtest cell %s %s failed: %s", ticker, date, exc)
            result.failures.append((ticker, date, str(exc)))

    # Settlement runs at the start of the next run for a ticker, so each ticker's
    # last cell would stay pending without this pass. A stopped sweep skips it:
    # settling calls an LLM, and the resumed sweep settles at its own end.
    for ticker in [] if result.stopped else tickers:
        try:
            graph.settle_pending(ticker)
        except Exception as exc:  # reflection calls an LLM; one failure is not the sweep's
            logger.warning("Settling %s failed: %s", ticker, exc)
            result.settlement_failures.append((ticker, str(exc)))
    return result


def summarize(source: BacktestResult | str | Path, cost_bps: float | None = None) -> BacktestSummary:
    """Score the settled decisions of a backtest, or of a memory log at a path, by rating.

    Each directional call is also scored net of one round trip: ``cost_bps`` for
    every call when given, else the default of the ticker's market
    (:func:`default_cost_bps`).
    """
    check_cost_bps(cost_bps)
    if isinstance(source, BacktestResult):
        path = source.log_path      # a run whose cells all failed wrote no log: nothing to score
    elif Path(source).is_file():
        path = Path(source)
    else:
        raise FileNotFoundError(f"no memory log at {source}")
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    # A decision with no readable rating has no direction, so it can neither
    # count for nor against the system; it is reported as unscored instead.
    resolved = [(e, _alpha(e)) for e in entries
                if not e["pending"] and e["rating"] != RATING_REVIEW]
    resolved = [(e, a) for e, a in resolved if a is not None]
    by_rating: dict[str, RatingScore] = {}
    costs: dict[str, float] = {}
    for rating in dict.fromkeys(e["rating"] for e, _ in resolved):
        calls = [(e, a) for e, a in resolved if e["rating"] == rating]
        alphas = [a for _, a in calls]
        direction = _DIRECTION.get(rating, 0)
        nets = []  # Hold claims no direction, so it is neither charged nor netted
        for e, a in calls if direction else ():
            market, bps = (("every call", cost_bps) if cost_bps is not None
                           else _cost_market(e["ticker"]))
            costs[market] = bps
            # Rounded so a call that only earns its cost back nets 0, not float noise either side.
            nets.append(round(direction * a - bps / 10_000, 12))
        by_rating[rating] = RatingScore(
            count=len(alphas),
            hit_rate=(sum(a * direction > 0 for a in alphas) / len(alphas)) if direction else None,
            mean_alpha=sum(alphas) / len(alphas),
            net_hit_rate=(sum(n > 0 for n in nets) / len(nets)) if nets else None,
            mean_net=(sum(nets) / len(nets)) if nets else None,
        )
    unscored = sum(1 for e in entries if e["rating"] == RATING_REVIEW)
    # Report the window the outcomes were actually measured over, from the log.
    windows = {f"{e['holding'][:-1]} trading days" for e, _ in resolved
               if (e.get("holding") or "").endswith("d")}
    return BacktestSummary(resolved=len(resolved),
                           pending=len(entries) - len(resolved) - unscored,
                           by_rating=by_rating, unscored=unscored,
                           holding=", ".join(sorted(windows)) or "the configured window",
                           cost_bps=cost_bps, costs=costs)
