# ADR 0002: Shared evidence and bounded retrieval (C04)

Status: **Implemented and fixture tested** on 19 October 2026 (Asia/Calcutta).
Code reviewed by Claude Code on 20 October 2026: no correctness defect (see
[Review](#review-20-october-2026)). No design was approved in advance, unlike
[ADR 0001](0001-bounded-execution-and-run-status.md). Task: C04 in the
[plan](../plan.md) and [progress tracker](../progress.md). Evidence class:
source reviewed and fixture tested. No provider or data vendor was called.

## Context

| Finding | Evidence |
| --- | --- |
| A run fetched the same source view once per caller. Without Jev, the Sentiment Analyst reads `get_news` over the seven days to the trade date, and a News Analyst asking for the same window fetched it again; with Jev, `get_news_feed` and `get_news` were two acquisitions of one Yahoo answer (`get_news_yfinance` returns `get_news_feed_yfinance(...).text`) | [Sentiment analyst](../../tradingagents/agents/analysts/sentiment_analyst.py), [Yahoo news](../../tradingagents/dataflows/vendors/yahoo/news.py) |
| Alpha Vantage feed items were not trimmed to the requested window, an unknown publication time became an empty display date, and a body that is not a feed (a rate-limit notice) read as "no news" | [Alpha Vantage news](../../tradingagents/dataflows/vendors/alpha_vantage/news.py) |
| A feed item kept a display date only: no source URL and no full publication time, so a later source drawer (C05) could not show either | [Feed](../../tradingagents/dataflows/feed.py) |
| The plan asks for request-scoped shared futures, no cached outages, per-category freshness and bounded per-provider concurrency, extending the existing Python design | [Plan](../plan.md#shared-evidence-and-incremental-research-design) |

## Decision

### 1. One evidence session per research run

`EvidenceSession` (`tradingagents/dataflows/evidence.py`) holds a run's completed
results (an LRU of 256 entries), its in-flight requests and its per-source
permits. `run_config` and `run_config_context` bind one in a context variable.
`TradingAgentsGraph.propagate` and `stream_run` pass `fresh_evidence=True`, so
every graph run starts its own session, even inside a caller's scope with an
equal configuration (a backtest sweep, a web job). A nested `run_config` with the
same configuration inside a run (settlement) shares the run's session; any other
configuration gets its own. Nothing outside a run scope is shared: the public
vendor functions and the router outside runs behave as before.

### 2. What is shared, and the request identity

Router methods `get_stock_data`, `get_news`, `get_news_feed`, `get_fundamentals`,
`get_balance_sheet`, `get_cashflow` and `get_income_statement`. Indicators, global
news, insider transactions, macro data, prediction markets, OHLCV snapshot loads
and the StockTwits/Reddit fetchers are not shared.

A request is identified by source (vendor), operation, category, the call's
arguments bound to the vendor's signature with defaults applied (so positional,
keyword and default forms match), the normalized instrument, and the
source-relevant configuration (vendors, article limit, data paths, asset type,
data revision). The tools clamp dates to the run's trade date before routing,
so the cached request is always the clamped one.

Inside a run, `get_news` is served from the vendor's `get_news_feed`, and the text
view is the feed's text. For Yahoo this is exact. For Alpha Vantage the run's text
is the window-filtered feed of section 6, not the raw response.

### 3. Coalescing, failure and Stop

The first caller of a request owns it; identical concurrent callers wait on a
shared future, checking the run's Stop and deadline every 50 ms, and each caller
gets its own deep copy. An exception reaches every waiter and is not stored, so
the next call retries. An already running vendor call relies on its own transport
timeout; the session cannot interrupt it.

### 4. What is never reused

Router sentinels (`DATA_UNAVAILABLE`, `NO_DATA_AVAILABLE`), feeds marked
unavailable, coverage-gap markers, withheld historical profiles, empty values and
payloads made only of vendor notice fields (`Information`, `Note`,
`Error Message`). A genuinely empty feed (`{"feed": []}`) and data carrying a
`Note` beside its figures are valid answers and are reused.

### 5. Freshness and limits

Reuse expires a set time after the acquisition completes: 60 s for price and news,
300 s for financial statements and fundamentals, 60 s for anything else. A
negative clock age is never reused. Each source has at most two acquisitions in
flight per run. Optional configuration keys override these: `evidence_ttl_seconds`
(a number, or a mapping by category), `evidence_max_entries` and
`evidence_max_concurrency`. They are read with defaults and are not in
`default_config`, so C04 does not change C01's configuration hash.

### 6. Evidence records and news provenance

Each retained result has an immutable `EvidenceRecord`: source, operation,
request hash, content hash, fetch time (UTC), parser version, coverage, and the
instrument, window and as-of date copied from the request. Source URL,
publication and availability time, reporting period, currency and unit exist
and stay `None`: nothing is inferred from the rendered body. The content hash
identifies the adapter's rendered result, not an original document. Records are
in memory for the run's length only; persisting them and showing them is C05.

`FeedItem` gains `url` and `published_at` (full UTC ISO time; unknown stays
`None`, never the fetch time). Yahoo items carry the canonical link and the
publication time. Alpha Vantage feed items and their rendered text keep only
articles published inside the inclusive window; an unreadable or non-feed body is
unavailable; a feed whose every article is rejected is an explicit coverage gap
("not evidence of no news"). Alpha Vantage's own `get_news` keeps its raw
response.

## Offline evidence

105 tests: `test_evidence_session.py` (61: coalescing, freshness, copies,
unusable results, failure, permits, Stop and deadline, LRU, records, trace
events, scopes), `test_c04_evidence.py` (20: router and graph paths, isolation
between runs and configurations, clamped tool dates, recovery after failure and
after unavailable news), `test_c04_news_provenance.py` (21) and
`test_benchmark_c04.py` (3). `scripts/benchmark_c04.py` writes
[the summary](../benchmarks/c04-summary.json):

| Workload | Without sharing | With sharing | Answers |
| --- | --- | --- | --- |
| Seven-read router workload (price x2, fundamentals x2, news text x2, feed x1), NVDA and RELIANCE.NS, 3 repetitions each | 42 acquisitions | 18 | All 42 identical |
| C01's real-graph fixture (`propagate`, scripted model, four analysts, Jev off) | 13 | 12 | All 12 routed answers identical; same rating |

The first workload is built to overlap and shows the most a run can save; the
second is the real graph's wiring, where the one repeat is the news window shared
by the Sentiment and News Analysts. Planted bugs: letting a graph run inherit
the caller's session, giving the news text its own acquisition, caching an
unavailable feed and recording path-dependent request hashes each fail at least
one test. Dropping the vendor settings from the request key fails none, because
a session never spans two configurations; that key field is defence in depth.

## Not in C04

- No latency or cost claim. The fixtures have no source delays, and how often a
  live model repeats a request is unmeasured: C01's raw live traces are not on
  this machine, and a new live run needs its own allowance.
- The two-per-source cap can add queueing in the parallel analyst stage; not
  measured.
- No reuse across runs, no persisted records and no change-based invalidation
  (C08). Publication times reach feed items, not the evidence records.

## Review (20 October 2026)

Claude Code reviewed C04 as it stands on `main` (`4ab64a3`: the code from
`07220d2` and the PR #4 close-out), as the owner asked on 20 October. Source
reviewed; the C04 tests passed in the full offline gate on PR #9's tree (2,386
passed). No live call.

**No correctness defect found.** Checked:

- **Session.** The first caller owns a request. Waiters check Stop and the
  deadline every 50 ms and each get a deep copy. A failure reaches every waiter
  and is never stored. An ineligible answer goes to the callers waiting on it
  but is not retained. The permit is released on every path, including a Stop
  between taking the permit and starting the load.
- **No deadlock.** The two-per-source cap cannot deadlock through nesting: no
  vendor function routes back through the router, and a waiter holds no permit.
- **Isolation.** Every Standard entry point (CLI, web job, backtest cell, the
  benchmark scripts) reaches `propagate` or `stream_run`, which start a fresh
  session. A nested `run_config` shares only with an equal configuration. The
  Quick runner (C06) starts its own.
- **Request identity.** Arguments are bound to the vendor's signature with
  defaults. Inside a run, `get_news` is served by the same vendor's
  `get_news_feed`, whose signature matches for Yahoo and Alpha Vantage.
- **Hashing and outages.** Every shared vendor method returns text or a `Feed`,
  so the JSON content hash (which refuses NaN) cannot turn a good answer into a
  failure. The Yahoo adapters raise on an empty or failed fetch rather than
  return prose, so an outage is not reused as an answer.
- **News provenance.** Alpha Vantage items and text are trimmed to the inclusive
  window using UTC-aware times. A notice or non-feed body is unavailable. Yahoo
  items carry the link and the UTC publication time.

Low notes; no change proposed:

- A coalesced waiter re-raises the owner's exception object and records the
  shared outcome as its own acquisition. Each read's provenance is accurate from
  that read's point of view.
- `record_for` runs after `fetch`. If the entry expired in between and another
  thread fetched again, the fetch time shown would be the newer one. That needs
  an expiry within microseconds of the read.

**Outside C04 (a C03 gap).** When the Jev extra is not installed but
`TYPESAFE_API_KEY` is set, the Sentiment Analyst screens social posts through
`post_screen.system_one`: raw HTTP, up to three attempts of 15 s with
`time.sleep` backoff, on pool threads that do not copy the run's context. Those
sends ignore Stop and the deadline, contrary to
[ADR 0001](0001-bounded-execution-and-run-status.md). `jev.py` already copies the
context for each request. The same fix, plus a control check and an
interruptible wait in that retry loop, belongs with C03, test-first.

The questions for the owner below remain open.

## Questions for the owner

1. Keep the per-run scope, or allow reuse across runs (for example a backtest
   sweep) within the freshness limits?
2. Are 60 s (price, news) and 300 s (financials) right, given runs of 6-7
   minutes in C01? Within one run, a repeat after the limit fetches again.
3. Keep the two-per-source concurrency cap until a live run measures its cost?
