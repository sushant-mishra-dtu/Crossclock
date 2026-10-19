"""C04's offline benchmark: source counts on a router workload and on the real graph."""

import json
import tempfile
from collections import Counter

import pytest

from scripts import benchmark_c04 as benchmark

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def summary():
    return benchmark.benchmark()


def test_router_workload_acquires_each_source_once_per_run(summary):
    # Each pair runs seven logical reads: price twice, fundamentals twice, news
    # text twice and the news feed once. Without sharing that is 7 acquisitions
    # (get_news has its own vendor call); with it, 3 (price, fundamentals, one
    # feed serving both news views). 2 tickers x 3 repetitions = 6 pairs.
    assert summary["totals"] == {
        "baseline_logical_reads": 42, "shared_logical_reads": 42,
        "baseline_source_acquisitions": 42, "shared_source_acquisitions": 18,
        "pairs_with_identical_answers": 6,
    }


def test_real_graph_shares_the_one_repeated_news_window_and_changes_no_answer(summary):
    graph = summary["graph"]
    baseline, shared = graph["baseline"], graph["shared"]
    # C01's fixture graph makes 13 source requests. Without Jev the Sentiment
    # Analyst reads get_news over the 7 days to the trade date, and the scripted
    # News Analyst asks for the same window: the only repeat, so 13 -> 12.
    assert (baseline["source_acquisitions"], shared["source_acquisitions"]) == (13, 12)
    removed = Counter(baseline["source_counts_by_operation"])
    removed.subtract(shared["source_counts_by_operation"])
    assert +removed == {"get_news": 2} and -removed == {"get_news_feed": 1}
    # Every routed read still happens and returns the same answer.
    assert baseline["logical_reads"] == shared["logical_reads"] == 12
    assert baseline["reads"] == shared["reads"]
    assert baseline["rating"] == shared["rating"]
    assert graph["reads_identical"] is True


def test_summary_carries_no_local_path_and_reruns_identically(summary):
    text = json.dumps(summary)
    for path in (benchmark.ROOT, tempfile.gettempdir()):
        assert json.dumps(str(path))[1:-1] not in text
    assert benchmark.benchmark() == summary
