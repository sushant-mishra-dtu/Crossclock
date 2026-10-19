"""C04: count offline source reuse and verify exact fixture evidence preservation.

Run ``python scripts/benchmark_c04.py --output <summary.json>`` from the checkout.
Two workloads, each with sharing off and on: a seven-read router workload, and
C01's real-graph fixture (``TradingAgentsGraph.propagate`` with C01's scripted
model). Every source and model answer is synthetic. Network transports are
blocked, no paid model is constructed, and no source sleeps or latency
measurements are used. Runtime state lives in a temporary folder, so the summary
names no local path. The default output lives under ignored results; updating
the tracked summary is explicit.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (ROOT / "results" / "benchmarks" / "c04" /
                  datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") / "summary.json")
# Config keys that hold runtime paths; hashed as placeholders, as C01 records them.
PATH_KEYS = ("results_dir", "data_cache_dir", "memory_log_path", "india_db_path",
             "screener_db_path")
TICKERS = ("NVDA", "RELIANCE.NS")
REPETITIONS = 3
START_DATE = "2026-10-05"
END_DATE = "2026-10-12"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _plain(value):
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=_plain, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require(condition, reason):
    """Validation must remain active when Python runs with optimization enabled."""
    if not condition:
        raise RuntimeError(f"C04 offline validation failed: {reason}")


@contextmanager
def isolated_environment():
    """Point application paths at a temporary folder, ignoring personal .env settings.

    Yields that folder; it is removed afterwards.
    """
    with tempfile.TemporaryDirectory(prefix="c04-offline-") as directory:
        runtime = Path(directory)
        with _environment(runtime):
            yield runtime


@contextmanager
def _environment(runtime):
    environment = {key: "" for key in os.environ if key.startswith("TRADINGAGENTS_")}
    environment.update({
        "HOME": str(runtime), "USERPROFILE": str(runtime),
        "TRADINGAGENTS_INDIA_DB": str(runtime / "unused_india.db"),
        "TRADINGAGENTS_SCREENER_DB": str(runtime / "unused_screener.db"),
        "TRADINGAGENTS_CACHE_DIR": str(runtime / "cache"),
        "TRADINGAGENTS_RESULTS_DIR": str(runtime / "results"),
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(runtime / "memory.md"),
    })
    # tradingagents imports load_dotenv from this module. Replacing it before
    # package import keeps local vendor/model settings out of this fixture run.
    with patch.dict(os.environ, environment), patch("dotenv.load_dotenv", return_value=False):
        yield


@contextmanager
def offline_network():
    """Block both Python clients and yfinance's separate libcurl transport."""
    def refused(*args, **kwargs):
        raise OSError("C04 offline fixture refused a network request")

    with ExitStack() as stack:
        for name in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, name, refused))
        stack.enter_context(patch.object(socket, "getaddrinfo", refused))
        from curl_cffi import requests as curl_requests

        stack.enter_context(patch.object(curl_requests.Session, "request", refused))
        stack.enter_context(patch.object(curl_requests.AsyncSession, "request", refused))
        yield


def fixture_for(ticker):
    """Invented, reusable fixture facts; .invalid links are never fetched."""
    from tradingagents.dataflows.feed import Feed, FeedItem

    articles = [
        {
            "title": "Synthetic revenue result", "text": "Revenue increased 10%.",
            "url": f"https://fixture.invalid/{ticker}/revenue",
            "published_at": "2026-10-05T08:00:00Z", "available_at": "2026-10-05T08:01:00Z",
        },
        {
            "title": "Synthetic margin result", "text": "Operating margin was 20%.",
            "url": f"https://fixture.invalid/{ticker}/margin",
            "published_at": "2026-10-12T09:00:00Z", "available_at": "2026-10-12T09:01:00Z",
        },
    ]
    items = tuple(FeedItem(
        source="news", text=f"{article['text']} URL: {article['url']}",
        title=article["title"], published=article["published_at"], author="C04 synthetic fixture",
        url=article["url"], published_at=article["published_at"],
    ) for article in articles)
    text = f"## Synthetic {ticker} news {START_DATE}..{END_DATE}\n\n" + "\n\n".join(
        f"### {article['title']}\nPublished: {article['published_at']}\n"
        f"Available: {article['available_at']}\nURL: {article['url']}\n{article['text']}"
        for article in articles
    )
    # Known answers are arithmetic over invented inputs, not market estimates:
    # closes 100 -> 102 give a 2% return; price 102 / EPS 2 gives P/E 51.
    return {
        "ticker": ticker, "asset_type": "stock", "start_date": START_DATE, "end_date": END_DATE,
        "price": f"{ticker} prices {START_DATE}..{END_DATE}: closes=100,102; return=2%",
        "fundamentals": f"{ticker} fundamentals as-of {END_DATE}: EPS=2; PE=51",
        "feed": Feed(text, items), "articles": articles,
    }


class FixtureSources:
    """No fixture cache: every call here is one observed source acquisition."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.counts = Counter()

    def _check(self, ticker, start_date=START_DATE, end_date=END_DATE):
        require(ticker == self.fixture["ticker"], "unexpected source ticker")
        require((start_date, end_date) == (START_DATE, END_DATE), "unexpected source window")

    def price(self, symbol, start_date, end_date):
        self._check(symbol, start_date, end_date)
        self.counts["get_stock_data"] += 1
        return self.fixture["price"]

    def fundamentals(self, ticker, as_of_date=None):
        self._check(ticker)
        require(as_of_date == END_DATE, "unexpected fundamentals as-of date")
        self.counts["get_fundamentals"] += 1
        return self.fixture["fundamentals"]

    def news(self, ticker, start_date, end_date):
        self._check(ticker, start_date, end_date)
        self.counts["get_news"] += 1
        return self.fixture["feed"].text

    def feed(self, ticker, start_date, end_date):
        self._check(ticker, start_date, end_date)
        self.counts["get_news_feed"] += 1
        return self.fixture["feed"]

    def implementations(self):
        return {name: {"yfinance": implementation} for name, implementation in (
            ("get_stock_data", self.price), ("get_fundamentals", self.fundamentals),
            ("get_news", self.news), ("get_news_feed", self.feed),
        )}


def workload(ticker):
    """Seven sequential analyst reads, including both public news shapes."""
    from tradingagents.dataflows.router import route_to_vendor

    return [
        route_to_vendor("get_stock_data", ticker, START_DATE, END_DATE),
        route_to_vendor("get_stock_data", ticker, START_DATE, END_DATE),
        route_to_vendor("get_fundamentals", ticker, END_DATE),
        route_to_vendor("get_fundamentals", ticker, END_DATE),
        route_to_vendor("get_news", ticker, START_DATE, END_DATE),
        route_to_vendor("get_news_feed", ticker, START_DATE, END_DATE),
        route_to_vendor("get_news", ticker, START_DATE, END_DATE),
    ]


def measure_case(fixture, config, sharing):
    from tradingagents.dataflows import config as config_module, router
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.evidence import current_evidence

    require(current_evidence() is None, "case inherited another run's evidence session")
    source = FixtureSources(fixture)
    scope = run_config(config) if sharing else nullcontext()
    with patch.object(config_module, "_config", copy.deepcopy(config)), \
            patch.dict(router.VENDOR_METHODS, source.implementations()), scope:
        answers = workload(fixture["ticker"])
        session = current_evidence()
        require((session is not None) == sharing, "run scope did not bind isolated evidence")
        records = [] if session is None else [asdict(record) for record in session.records()]
    require(current_evidence() is None, "case leaked its evidence session")

    expected = [fixture["price"], fixture["price"], fixture["fundamentals"],
                fixture["fundamentals"], fixture["feed"].text, fixture["feed"], fixture["feed"].text]
    require(answers == expected, "fixture evidence changed or was lost")
    expected_counts = {"get_stock_data": 1, "get_fundamentals": 1, "get_news_feed": 1} if sharing else {
        "get_stock_data": 2, "get_fundamentals": 2, "get_news": 2, "get_news_feed": 1,
    }
    require(dict(source.counts) == expected_counts, "source acquisition counts do not match policy")
    require(len(records) == (3 if sharing else 0), "unexpected evidence record inventory")
    if sharing:
        record_answers = {"get_stock_data": fixture["price"], "get_fundamentals": fixture["fundamentals"],
                          "get_news_feed": fixture["feed"]}
        require({record["operation"] for record in records} == set(record_answers),
                "evidence operations do not match acquisitions")
        for record in records:
            require(record["source"] == "yfinance", "evidence names the wrong source")
            require(record["content_sha256"] == fingerprint(record_answers[record["operation"]]),
                    "evidence content hash does not match the returned fixture")
            require(len(record["request_sha256"]) == 64, "missing evidence request hash")
            # Acquisition time is real wall time, not article publication time,
            # and the request hash covers the run's cache/database paths. Omit
            # both volatile fields so a rerun anywhere gives the same summary.
            record.pop("fetched_at")
            record.pop("request_sha256")
    counts = {operation: source.counts[operation] for operation in (
        "get_stock_data", "get_fundamentals", "get_news", "get_news_feed")}
    return {
        "sharing": sharing, "logical_reads": len(answers),
        "source_acquisitions": sum(counts.values()), "source_counts_by_operation": counts,
        "source_counts_by_kind": {"price": counts["get_stock_data"],
                                  "fundamentals": counts["get_fundamentals"],
                                  "news": counts["get_news"] + counts["get_news_feed"]},
        "answer_sha256": [fingerprint(answer) for answer in answers],
        "workload_answer_sha256": fingerprint(answers), "news_article_count": len(answers[5].items),
        "coverage_preserved": True,
        "evidence_records": sorted(records, key=lambda record: record["operation"]),
    }


def portable(config):
    """The configuration with its runtime paths as placeholders, for hashing."""
    return {**config, **{key: f"<runtime>/{key}" for key in PATH_KEYS if key in config}}


def graph_case(runtime, sharing):
    """C01's real-graph fixture once, counting source requests and routed answers.

    Every vendor call counts as one acquisition, whatever C01's fixture source
    does with it. Its ``get_news_feed`` answers with the ``get_news`` text, which
    is Yahoo's own contract (``get_news_yfinance`` returns
    ``get_news_feed_yfinance(...).text``), so one acquisition serving both views
    cannot change an answer. Sharing off replaces the run's evidence session
    with none, which the router treats as the ordinary path.
    """
    from scripts import benchmark_c01 as c01
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed
    from tradingagents.telemetry import RunTrace

    fixture = c01.load_fixture()
    # No artificial delays: this counts requests and compares answers, not time.
    fixture["latencies_ms"] = dict.fromkeys(fixture["latencies_ms"], 0)
    acquisitions, reads = Counter(), []

    class Source(c01.FixtureSource):
        def fetch(self, method, *args, **kwargs):
            acquisitions[method] += 1
            if method == "get_news_feed":
                return Feed(super().fetch("get_news", *args, **kwargs))
            return super().fetch(method, *args, **kwargs)

    retrieve_from_source = router._retrieve_from_source

    def recorded(impl_func, source, method, category, args, kwargs):
        value = retrieve_from_source(impl_func, source, method, category, args, kwargs)
        reads.append({"operation": method, "source": source,
                      "request_sha256": fingerprint([list(args), kwargs]),
                      "answer_sha256": fingerprint(value)})
        return value

    evidence = router.current_evidence if sharing else (lambda: None)
    with patch.object(c01, "FixtureSource", Source), \
            patch.object(router, "_retrieve_from_source", recorded), \
            patch.object(router, "current_evidence", evidence):
        output, _, _, captured = c01.graph_case(
            fixture, RunTrace(), runtime / ("graph-shared" if sharing else "graph-baseline"))
    counts = dict(sorted(acquisitions.items()))
    return {
        "sharing": sharing, "rating": output["rating"],
        "source_acquisitions": sum(counts.values()), "source_counts_by_operation": counts,
        "logical_reads": len(reads), "reads": sorted(reads, key=canonical_json),
    }, fixture, captured


def benchmark():
    with isolated_environment() as runtime, offline_network():
        from tradingagents.default_config import build_default_config

        config = build_default_config()
        config.update(data_vendors={"core_stock_apis": "yfinance", "fundamental_data": "yfinance",
                                   "news_data": "yfinance"}, tool_vendors={})
        fixtures = [fixture_for(ticker) for ticker in TICKERS]
        pairs = []
        for fixture in fixtures:
            for repetition in range(1, REPETITIONS + 1):
                baseline = measure_case(fixture, config, sharing=False)
                shared = measure_case(fixture, config, sharing=True)
                require(baseline["answer_sha256"] == shared["answer_sha256"], "matched pair changed answers")
                pairs.append({"ticker": fixture["ticker"], "repetition": repetition,
                              "baseline": baseline, "shared": shared})
        graph_baseline, graph_fixture, graph_config = graph_case(runtime, sharing=False)
        graph_shared, _, _ = graph_case(runtime, sharing=True)
        require(graph_baseline["reads"] == graph_shared["reads"], "the graph's routed answers changed")
        require(graph_baseline["rating"] == graph_shared["rating"], "the graph's rating changed")
        return {
            "schema_version": 2, "benchmark_id": "C04", "status": "fixture_tested",
            "measurement_kind": "offline_router_and_graph_source_counts", "fixture_version": "c04-v2",
            "interpretation": "Synthetic source-count and exact-evidence checks only; no latency, speedup, cost, real-source coverage or model-quality claim.",
            "design": {"tickers": list(TICKERS), "repetitions_per_ticker": REPETITIONS,
                       "matched_pairs": len(pairs), "execution": "sequential baseline then shared; fresh scope per pair",
                       "network": "blocked Python sockets and curl_cffi", "model_calls": 0,
                       "artificial_source_delays": False, "fixture_rights": "invented CC0 test content",
                       "omitted_volatile_fields": ["evidence_records[].fetched_at",
                                                   "evidence_records[].request_sha256"]},
            "configuration_sha256": fingerprint(portable(config)),
            "source_configuration": {"data_vendors": config["data_vendors"], "tool_vendors": {}},
            "request_hash_scope": "Configuration hashes use placeholders for runtime paths. Evidence request hashes cover the run's cache/database paths, so they are checked but not recorded.",
            "fixtures": [{**fixture, "feed": asdict(fixture["feed"])} for fixture in fixtures],
            "fixture_sha256": fingerprint(fixtures), "pairs": pairs,
            "graph": {
                "workload": "C01's real-graph fixture: TradingAgentsGraph.propagate with C01's scripted model, four analysts, Jev off",
                "fixture_sha256": fingerprint(graph_fixture),
                "configuration_sha256": fingerprint(graph_config),
                "acquisition_definition": "One vendor-function call, counted before C01's fixture source applies its own response cache; OHLCV loads included",
                "baseline": graph_baseline, "shared": graph_shared,
                "reads_identical": True,
            },
            "totals": {
                "baseline_logical_reads": sum(pair["baseline"]["logical_reads"] for pair in pairs),
                "shared_logical_reads": sum(pair["shared"]["logical_reads"] for pair in pairs),
                "baseline_source_acquisitions": sum(pair["baseline"]["source_acquisitions"] for pair in pairs),
                "shared_source_acquisitions": sum(pair["shared"]["source_acquisitions"] for pair in pairs),
                "pairs_with_identical_answers": len(pairs),
            },
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Summary JSON path; default is under ignored results/benchmarks.")
    arguments = parser.parse_args(argv)
    summary = benchmark()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    totals, graph = summary["totals"], summary["graph"]
    print(f"C04 offline: {summary['design']['matched_pairs']} matched pairs; "
          f"{totals['baseline_source_acquisitions']} -> {totals['shared_source_acquisitions']} "
          "synthetic source acquisitions; all answers preserved.")
    print(f"C01 graph fixture: {graph['baseline']['source_acquisitions']} -> "
          f"{graph['shared']['source_acquisitions']} source acquisitions; "
          f"{graph['shared']['logical_reads']} routed answers unchanged.")
    print(f"Summary: {arguments.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
