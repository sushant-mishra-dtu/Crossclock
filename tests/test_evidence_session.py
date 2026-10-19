"""Offline acceptance cases for request-scoped retrieval reuse."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from tradingagents.dataflows.evidence import (
    EvidenceSession,
    current_evidence,
    evidence_scope,
    set_evidence,
)
from tradingagents.dataflows.feed import Feed, FeedItem
from tradingagents.run_control import (
    DeadlineExceeded,
    RunCancelled,
    RunControl,
    use_control,
)
from tradingagents.telemetry import RunTrace, use_trace

pytestmark = pytest.mark.unit

REQUEST = {"instrument": "AAPL", "start_date": "2026-10-12", "end_date": "2026-10-19"}


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _fetch(session, loader, *, request=None, source="fixture", operation="get_news", category="news"):
    return session.fetch(source, operation, REQUEST if request is None else request,
                         loader, category=category)


def _control(clock=None):
    return RunControl.from_config({"run_budgets": {"standard": {"deadline_seconds": 2}}},
                                  clock=clock or Clock())


def _traced_fetch(trace, session, loader, **kwargs):
    with use_trace(trace):
        return _fetch(session, loader, **kwargs)


def _coalescing_trace(monkeypatch):
    """Observe the public event before releasing a deliberately held loader."""
    trace, waiting = RunTrace(), threading.Event()
    real_event = trace.event

    def event(name, attributes=None):
        real_event(name, attributes)
        if name == "evidence_cache" and attributes.get("cache_status") == "coalesced":
            waiting.set()

    monkeypatch.setattr(trace, "event", event)
    return trace, waiting


def test_concurrent_requests_share_one_known_answer(monkeypatch):
    session = EvidenceSession()
    entered, release = threading.Event(), threading.Event()
    trace, waiting = _coalescing_trace(monkeypatch)
    expected = Feed("Known headline", (FeedItem("news", "Revenue rose 12%", title="Quarter"),))
    calls = []

    def load():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return expected

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_traced_fetch, trace, session, load)
        assert entered.wait(3)
        follower = pool.submit(_traced_fetch, trace, session, load)
        try:
            assert waiting.wait(3)
        finally:
            release.set()
        assert owner.result(timeout=3) == follower.result(timeout=3) == expected
    assert calls == [1]
    assert len(session.records()) == 1


@pytest.mark.parametrize("category,ttl", [("price", 60), ("news", 60), ("financial", 300),
                                         ("core_stock_apis", 60), ("news_data", 60),
                                         ("fundamental_data", 300)])
def test_source_specific_freshness_is_measured_after_completion(category, ttl):
    clock, calls = Clock(), []
    session = EvidenceSession(clock=clock)

    def load():
        calls.append(1)
        clock.now += 10  # acquisition time must not consume the reuse interval
        return {"answer": len(calls)}

    assert _fetch(session, load, category=category) == {"answer": 1}
    clock.now = 10 + ttl - 0.001
    assert _fetch(session, load, category=category) == {"answer": 1}
    clock.now = 10 + ttl
    assert _fetch(session, load, category=category) == {"answer": 2}
    assert len(calls) == 2


def test_negative_clock_age_is_not_reusable():
    clock, calls = Clock(10), []
    session = EvidenceSession(clock=clock)
    _fetch(session, lambda: calls.append(1) or "first")
    clock.now = 9
    assert _fetch(session, lambda: calls.append(1) or "second") == "second"
    assert calls == [1, 1]


def test_explicit_ttl_replaces_category_default():
    clock, calls = Clock(), []
    session = EvidenceSession(ttl_seconds=2, clock=clock)
    _fetch(session, lambda: calls.append(1) or "answer", category="financial")
    clock.now = 2
    _fetch(session, lambda: calls.append(1) or "answer", category="financial")
    assert calls == [1, 1]


def test_cache_keys_are_order_independent_and_isolate_request_scope():
    session, calls = EvidenceSession(), []

    def load():
        calls.append(1)
        return {"answer": len(calls)}

    assert _fetch(session, load, request={"a": 1, "b": 2}) == {"answer": 1}
    assert _fetch(session, load, request={"b": 2, "a": 1}) == {"answer": 1}
    assert _fetch(session, load, request={"a": 2, "b": 2}) == {"answer": 2}
    assert _fetch(session, load, request={"a": 1, "b": 2}, source="another") == {"answer": 3}
    assert _fetch(session, load, request={"a": 1, "b": 2}, operation="another") == {"answer": 4}
    assert _fetch(EvidenceSession(), load, request={"a": 1, "b": 2}) == {"answer": 5}


def test_return_values_and_loader_payload_are_independently_owned():
    session, payload = EvidenceSession(), {"values": [10, 20]}
    first = _fetch(session, lambda: payload)
    payload["values"].append(999)
    first["values"].append(888)
    second = _fetch(session, lambda: pytest.fail("cache miss"))
    assert second == {"values": [10, 20]}
    second["values"].clear()
    assert _fetch(session, lambda: pytest.fail("cache miss")) == {"values": [10, 20]}


@pytest.mark.parametrize("unusable", [
    None, "", "  ", [], {},
    "{}", "[]", "null", '""',
    '{"Information": "source is unavailable"}',
    '{"Note": "temporary source notice"}',
    '{"Error Message": "invalid API call"}',
    {"Information": "source is unavailable"},
    "DATA_UNAVAILABLE: offline", "NO_DATA_AVAILABLE: invalid instrument",
    "# Company Fundamentals\nProfile fundamentals are withheld for this date.",
    "<Yahoo Finance news unavailable for this window: recent coverage only>",
    Feed("No coverage", unavailable=True),
])
def test_unavailable_and_empty_results_do_not_poison_recovery(unusable):
    session, calls = EvidenceSession(), []
    assert _fetch(session, lambda: calls.append(1) or unusable) == unusable
    assert session.records() == ()
    assert _fetch(session, lambda: calls.append(1) or "recovered") == "recovered"
    assert calls == [1, 1]


def test_valid_empty_feed_is_distinct_from_unavailable():
    session = EvidenceSession()
    silence = Feed("No news found between the requested dates")
    assert _fetch(session, lambda: silence) == silence
    assert _fetch(session, lambda: pytest.fail("valid silence should be reusable")) == silence
    assert session.records()[0].coverage == "available"


def test_json_empty_financial_result_does_not_hide_a_recovered_statement():
    session, calls = EvidenceSession(), []

    def load():
        calls.append(1)
        return "{}" if len(calls) == 1 else '{"annualReports":[{"totalRevenue":"200"}]}'

    assert _fetch(session, load, operation="get_income_statement", category="financial") == "{}"
    assert session.records() == ()
    statement = _fetch(session, load, operation="get_income_statement", category="financial")
    assert "totalRevenue" in statement
    assert _fetch(session, load, operation="get_income_statement", category="financial") == statement
    assert calls == [1, 1]


def test_genuine_empty_news_json_and_data_with_a_note_remain_reusable():
    for value in ('{"feed":[]}', Feed('{"feed":[]}'),
                  '{"facts":{"Revenue":200},"Note":"Values are in USD"}'):
        session = EvidenceSession()
        assert _fetch(session, lambda value=value: value) == value
        assert _fetch(session, lambda: pytest.fail("valid source response was not reused")) == value
        assert len(session.records()) == 1


@pytest.mark.parametrize("failure", [ValueError("source failed"), TimeoutError("source timed out"),
                                    RunCancelled("owner stopped")])
def test_owner_failure_unblocks_followers_and_allows_recovery(monkeypatch, failure):
    session = EvidenceSession(max_concurrency=1)
    trace, waiting = _coalescing_trace(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(3)
        raise failure

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_traced_fetch, trace, session, load)
        assert entered.wait(3)
        follower = pool.submit(_traced_fetch, trace, session, load)
        try:
            assert waiting.wait(3)
        finally:
            release.set()
        for result in (owner, follower):
            with pytest.raises(type(failure)):
                result.result(timeout=3)
    assert session.records() == ()
    assert _fetch(session, lambda: "recovered") == "recovered"


def test_per_source_permits_bound_loaders_and_other_sources_proceed():
    session = EvidenceSession(max_concurrency=2)
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    active, peak, started = 0, 0, 0

    def load():
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            if started == 2:
                entered.set()
        try:
            assert release.wait(3)
            return "known answer"
        finally:
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_fetch, session, load, request={"call": n}) for n in range(4)]
        assert entered.wait(3)
        independent = pool.submit(_fetch, session, lambda: "other", source="other")
        try:
            assert independent.result(timeout=2) == "other"
            assert peak == 2
        finally:
            release.set()
        assert [future.result(timeout=3) for future in futures] == ["known answer"] * 4
    assert peak == 2 and started == 4


@pytest.mark.parametrize("wait_for", ["future", "permit"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_waits_observe_cancel_and_deadline_without_waiting_for_loader(
    monkeypatch, wait_for, stop,
):
    session, clock = EvidenceSession(max_concurrency=1), Clock()
    control = _control(clock)
    trace, waiting = _coalescing_trace(monkeypatch)
    entered, release, follower_started = threading.Event(), threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(3)
        return "known answer"

    def follow():
        with use_control(control), use_trace(trace):
            follower_started.set()
            request = REQUEST if wait_for == "future" else {"different": True}
            return _fetch(session, lambda: pytest.fail("stopped loader must not run"), request=request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_fetch, session, load)
        assert entered.wait(3)
        follower = pool.submit(follow)
        try:
            assert follower_started.wait(3)
            if wait_for == "future":
                assert waiting.wait(3)
            if stop == "cancel":
                control.cancel()
            else:
                clock.now = 3
            with pytest.raises(RunCancelled if stop == "cancel" else DeadlineExceeded):
                follower.result(timeout=1)
        finally:
            release.set()
        assert owner.result(timeout=3) == "known answer"
    # Waiting caller did not cancel the successful owner or poison another key.
    assert _fetch(session, lambda: "second", request={"different": True}) == "second"


def test_entry_limit_does_not_evict_inflight_sharing(monkeypatch):
    session = EvidenceSession(max_entries=1, max_concurrency=2)
    trace, waiting = _coalescing_trace(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return "first"

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_fetch, session, load)
        assert entered.wait(3)
        assert _fetch(session, lambda: "second", request={"key": 2}) == "second"
        assert _fetch(session, lambda: "third", request={"key": 3}) == "third"
        follower = pool.submit(_traced_fetch, trace, session, load)
        try:
            assert waiting.wait(3)
        finally:
            release.set()
        assert owner.result(timeout=3) == follower.result(timeout=3) == "first"
    assert calls == [1]
    assert len(session.records()) <= 1


def test_completed_cache_evicts_least_recently_used_request():
    session = EvidenceSession(max_entries=2)
    assert _fetch(session, lambda: "one", request={"key": 1}) == "one"
    assert _fetch(session, lambda: "two", request={"key": 2}) == "two"
    assert _fetch(session, lambda: pytest.fail("cache miss"), request={"key": 1}) == "one"
    assert _fetch(session, lambda: "three", request={"key": 3}) == "three"
    assert len(session.records()) == 2
    assert _fetch(session, lambda: pytest.fail("recent entry was evicted"), request={"key": 1}) == "one"
    assert _fetch(session, lambda: "two refreshed", request={"key": 2}) == "two refreshed"


def test_owner_stop_after_source_completes_discards_result_and_releases_permit():
    session, control = EvidenceSession(max_concurrency=1), _control()

    def load():
        control.cancel()
        return "late answer"

    with use_control(control), pytest.raises(RunCancelled):
        _fetch(session, load)
    assert session.records() == ()
    assert _fetch(session, lambda: "eligible answer") == "eligible answer"


def test_category_ttl_override_is_copied_and_other_policies_keep_defaults():
    policy, clock = {"news": 1}, Clock()
    session = EvidenceSession(ttl_seconds=policy, clock=clock)
    policy["news"] = 100
    _fetch(session, lambda: "news first")
    _fetch(session, lambda: "financial first", category="financial")
    clock.now = 1
    assert _fetch(session, lambda: "news refreshed") == "news refreshed"
    assert _fetch(session, lambda: pytest.fail("financial expired"), category="financial") == "financial first"


def test_metadata_is_immutable_content_addressed_and_preserves_unknown_times():
    session = EvidenceSession()
    first = _fetch(session, lambda: {"answer": 42})
    assert first == {"answer": 42}
    record = session.records()[0]
    assert record.source == "fixture" and record.operation == "get_news"
    assert len(record.request_sha256) == len(record.content_sha256) == 64
    assert record.fetched_at.endswith("+00:00")
    assert record.parser_version == "rendered-v1" and record.coverage == "available"
    assert record.instrument == "AAPL"
    assert record.window_start == "2026-10-12" and record.window_end == "2026-10-19"
    for field in ("source_url", "published_at", "available_at", "reporting_period", "currency", "unit"):
        assert getattr(record, field) is None
    with pytest.raises(FrozenInstanceError):
        record.coverage = "changed"
    another = EvidenceSession()
    _fetch(another, lambda: {"answer": 42})
    assert another.records()[0].content_sha256 == record.content_sha256


def test_trace_counts_miss_hit_expiry_without_recording_request_or_content():
    clock, trace = Clock(), RunTrace()
    session = EvidenceSession(ttl_seconds=1, clock=clock)
    with use_trace(trace):
        request = {"instrument": "PRIVATE_TICKER", "api_key": "private credential"}
        _fetch(session, lambda: "private source body", request=request)
        _fetch(session, lambda: pytest.fail("cache miss"), request=request)
        clock.now = 1
        _fetch(session, lambda: "private source body", request=request)
    events = [item for item in trace.snapshot()["events"] if item["name"] == "evidence_cache"]
    statuses = [item["attributes"]["cache_status"] for item in events]
    assert statuses == ["miss", "hit", "expired"]
    assert "private" not in str(trace.snapshot()).lower()


def test_context_scopes_restore_parent_and_setter_supports_copied_contexts():
    assert current_evidence() is None
    outer = EvidenceSession()
    with evidence_scope(outer) as active:
        assert active is current_evidence() is outer
        with evidence_scope() as inner:
            assert current_evidence() is inner and inner is not outer
        assert current_evidence() is outer
        with evidence_scope(EvidenceSession()):
            set_evidence(outer)
            assert current_evidence() is outer
    assert current_evidence() is None


@pytest.mark.parametrize("kwargs", [
    {"ttl_seconds": 0}, {"ttl_seconds": -1}, {"ttl_seconds": True},
    {"ttl_seconds": float("nan")}, {"ttl_seconds": float("inf")},
    {"ttl_seconds": {"news": -1}}, {"ttl_seconds": {"news": True}},
    {"max_concurrency": 0}, {"max_concurrency": True}, {"max_concurrency": 1.5},
    {"max_entries": 0}, {"max_entries": True}, {"max_entries": 1.5},
    {"clock": 42},
])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        EvidenceSession(**kwargs)
