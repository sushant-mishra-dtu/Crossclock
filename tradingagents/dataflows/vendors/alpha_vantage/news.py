
import json
from datetime import UTC, datetime

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window, withhold_undisclosed_trades
from tradingagents.dataflows.feed import Feed, FeedItem
from tradingagents.dataflows.vendors.alpha_vantage.common import (
    _make_api_request,
    format_datetime_for_api,
)


def get_news(ticker, start_date, end_date) -> dict[str, str] | str:
    """Returns live and historical market news & sentiment data from premier news outlets worldwide.

    Covers stocks, cryptocurrencies, forex, and topics like fiscal policy, mergers & acquisitions, IPOs.

    Args:
        ticker: Stock symbol for news articles.
        start_date: Start date for news search.
        end_date: End date for news search.

    Returns:
        Dictionary containing news sentiment data or JSON string.
    """

    # Without a limit the endpoint returns 50 articles, each with per-ticker
    # sentiment arrays, and all of it reaches the prompt.
    params = {
        "tickers": ticker,
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(end_date, end_of_day=True),
        "limit": str(get_config()["news_article_limit"]),
    }

    return _make_api_request("NEWS_SENTIMENT", params)


def get_news_feed(ticker, start_date, end_date) -> Feed:
    """:func:`get_news` with only known, in-window publications kept as items.

    Keep the original response text when every article qualifies. If a vendor
    returns articles outside the requested window or without a readable date,
    trim both the items and their rendered block so fallback readers see the
    same evidence. An unreadable/non-feed response is unavailable, not silence.
    """
    raw = get_news(ticker, start_date, end_date)
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return Feed(text, unavailable=True)
    articles = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        return Feed(text, unavailable=True)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    kept, items = [], []
    for article in articles:
        if not isinstance(article, dict):
            continue
        stamp = str(article.get("time_published") or "")  # UTC, e.g. 20260915T123000
        try:
            pub_date = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if not in_window(pub_date, start_dt, end_dt):
            continue
        kept.append(article)
        items.append(FeedItem(
            "news", str(article.get("summary") or ""), title=str(article.get("title") or ""),
            published=f"{pub_date:%Y-%m-%d}", author=str(article.get("source") or ""),
            url=str(article.get("url") or ""), published_at=pub_date.isoformat(),
        ))

    if len(kept) != len(articles):
        payload = {**payload, "feed": kept}
        if "items" in payload:
            payload["items"] = str(len(kept)) if isinstance(payload["items"], str) else len(kept)
        payload["excluded_articles"] = len(articles) - len(kept)
        if not kept:
            payload["coverage_status"] = "unavailable"
            payload["coverage_reason"] = (
                "No returned article has a known publication within the requested window; "
                "this is not evidence of no news."
            )
        text = json.dumps(payload)
    return Feed(text, tuple(items), unavailable=bool(articles) and not kept)


def get_global_news(as_of_date, look_back_days: int | None = None, limit: int | None = None) -> dict[str, str] | str:
    """Returns global market news & sentiment data without ticker-specific filtering.

    Covers broad market topics like financial markets, economy, and more.

    Args:
        as_of_date: Current date in yyyy-mm-dd format.
        look_back_days: Number of days to look back; ``None`` uses
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles; ``None`` uses
            ``global_news_article_limit`` from the active config.

    Returns:
        Dictionary containing global news sentiment data or JSON string.
    """
    from datetime import datetime, timedelta

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    curr_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    params = {
        "topics": "financial_markets,economy_macro,economy_monetary",
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(as_of_date, end_of_day=True),
        "limit": str(limit),
    }

    return _make_api_request("NEWS_SENTIMENT", params)


def get_insider_transactions(symbol: str, as_of_date: str | None = None) -> dict[str, str] | str:
    """Returns latest and historical insider transactions by key stakeholders.

    Covers transactions by founders, executives, board members, etc.

    Args:
        symbol: Ticker symbol. Example: "IBM".
        as_of_date: The analysis date (yyyy-mm-dd); a past one withholds them.

    Returns:
        Dictionary containing insider transaction data or JSON string.
    """

    withheld = withhold_undisclosed_trades(as_of_date, symbol)
    if withheld:
        return withheld
    return _make_api_request("INSIDER_TRANSACTIONS", {"symbol": symbol})
