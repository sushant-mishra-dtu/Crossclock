"""C04 news provenance and point-in-time boundaries, without vendor calls."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tradingagents.dataflows.feed import FeedItem
from tradingagents.dataflows.vendors.alpha_vantage import news as av_news
from tradingagents.dataflows.vendors.yahoo import news as yahoo_news

pytestmark = pytest.mark.unit


def _yahoo_feed(monkeypatch, articles, start="2026-09-15", end="2026-09-16"):
    ticker = SimpleNamespace(get_news=lambda **kwargs: articles)
    monkeypatch.setattr(yahoo_news.yf, "Ticker", lambda symbol: ticker)
    return yahoo_news.get_news_feed_yfinance("NVDA", start, end)


def _article(title, stamp):
    return {"title": title, "summary": title + " summary", "source": "Reuters",
            "time_published": stamp, "url": "https://example.com/" + title}


def test_feed_item_keeps_its_original_positional_arguments():
    item = FeedItem("stocktwits", "post", "title", "display", "@writer", "Bullish")
    assert (item.source, item.text, item.title, item.published, item.author, item.label) == (
        "stocktwits", "post", "title", "display", "@writer", "Bullish")
    assert item.url == "" and item.published_at is None


def test_yahoo_nested_article_retains_canonical_url_and_full_utc_publication(monkeypatch):
    feed = _yahoo_feed(monkeypatch, [{"content": {
        "title": "Earnings", "summary": "Quarter", "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://example.com/canonical"},
        "clickThroughUrl": {"url": "https://example.com/redirect"},
        "pubDate": "2026-09-16T01:00:30+05:30",
    }}])
    item, = feed.items
    assert item.url == "https://example.com/canonical"
    assert item.published_at == "2026-09-15T19:30:30+00:00"
    assert item.published == "2026-09-16"  # existing display date stays intact
    assert "Link: " + item.url in feed.text


def test_yahoo_flat_article_retains_full_utc_publication_and_url(monkeypatch):
    stamp = datetime(2026, 9, 15, 12, 30, 17, tzinfo=UTC)
    feed = _yahoo_feed(monkeypatch, [{"title": "Flat", "summary": "Body",
                                      "link": "https://example.com/flat",
                                      "providerPublishTime": stamp.timestamp()}])
    item, = feed.items
    assert item.url == "https://example.com/flat"
    assert item.published_at == stamp.isoformat()
    assert item.published == "2026-09-15"


def test_yahoo_undated_live_item_keeps_publication_unknown(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    feed = _yahoo_feed(monkeypatch, [{"title": "Undated", "link": "https://example.com/u"}],
                       start=today, end=today)
    item, = feed.items
    assert item.published_at is None and item.published == ""
    assert item.url == "https://example.com/u"


def test_alpha_vantage_retains_raw_text_when_every_article_is_eligible(monkeypatch):
    payload = {"items": "1", "feed": [_article("known", "20260915T123017")]}
    raw = json.dumps(payload, indent=2)
    monkeypatch.setattr(av_news, "get_news", lambda *args: raw)
    feed = av_news.get_news_feed("NVDA", "2026-09-15", "2026-09-16")
    item, = feed.items
    assert feed.text == raw and not feed.unavailable
    assert item.url == "https://example.com/known"
    assert item.published_at == "2026-09-15T12:30:17+00:00"
    assert item.published == "2026-09-15"


@pytest.mark.parametrize("as_text", [False, True])
def test_alpha_vantage_filters_items_and_rendered_text_to_the_inclusive_window(monkeypatch, as_text):
    articles = [_article("before", "20260914T235959"),
                _article("start", "20260915T000000"),
                _article("end", "20260916T235959"),
                _article("after", "20260917T000000"),
                _article("unknown", ""), _article("bad", "20260230T120000"), "not an article"]
    payload = {"items": str(len(articles)), "feed": articles, "sentiment_score_definition": "kept metadata"}
    raw = json.dumps(payload) if as_text else payload
    monkeypatch.setattr(av_news, "get_news", lambda *args: raw)
    feed = av_news.get_news_feed("NVDA", "2026-09-15", "2026-09-16")
    assert [i.title for i in feed.items] == ["start", "end"]
    rendered = json.loads(feed.text)
    assert rendered["feed"] == articles[1:3]
    assert rendered["items"] == "2"
    assert rendered["sentiment_score_definition"] == "kept metadata"
    assert not feed.unavailable
    assert (payload["items"], payload["feed"]) == ("7", articles)  # no caller payload mutation


def test_alpha_vantage_never_promotes_unknown_publication_to_the_fetch_time(monkeypatch):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    monkeypatch.setattr(av_news, "get_news", lambda *args: {"feed": [_article("unknown", "")]})
    feed = av_news.get_news_feed("NVDA", today, today)
    assert not feed.items and json.loads(feed.text)["feed"] == []
    assert feed.unavailable
    assert "publication" in json.loads(feed.text)["coverage_reason"]


@pytest.mark.parametrize("stamp", ["20260917T000000", "20260914T235959", ""])
def test_alpha_vantage_rejected_articles_are_a_coverage_gap_not_no_news(monkeypatch, stamp):
    monkeypatch.setattr(av_news, "get_news", lambda *args: {"feed": [_article("rejected", stamp)]})
    feed = av_news.get_news_feed("NVDA", "2026-09-15", "2026-09-16")
    assert feed.unavailable and not feed.items
    payload = json.loads(feed.text)
    assert payload["feed"] == [] and payload["coverage_status"] == "unavailable"
    assert "not evidence of no news" in payload["coverage_reason"]


@pytest.mark.parametrize("raw", ["not json", "null", "[]", "{}",
                                  '{"Information": "rate limited"}',
                                  '{"Error Message": "invalid API call"}',
                                  '{"feed": null}', '{"feed": {}}'])
def test_alpha_vantage_invalid_or_error_response_is_unavailable(monkeypatch, raw):
    monkeypatch.setattr(av_news, "get_news", lambda *args: raw)
    feed = av_news.get_news_feed("NVDA", "2026-09-15", "2026-09-16")
    assert feed.unavailable and not feed.items
    assert feed.text == raw


def test_alpha_vantage_actual_empty_response_is_a_success(monkeypatch):
    raw = '{ "items": "0", "feed": [] }'
    monkeypatch.setattr(av_news, "get_news", lambda *args: raw)
    feed = av_news.get_news_feed("NVDA", "2026-09-15", "2026-09-16")
    assert not feed.unavailable and not feed.items and feed.text == raw


def test_alpha_vantage_legacy_get_news_keeps_its_raw_response(monkeypatch):
    raw = '{"feed": [{"title": "outside", "time_published": "20260917T000000"}]}'
    monkeypatch.setattr(av_news, "_make_api_request", lambda *args: raw)
    assert av_news.get_news("NVDA", "2026-09-15", "2026-09-16") == raw
