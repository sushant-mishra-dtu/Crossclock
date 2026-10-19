"""Bounded, request-scoped reuse of successful rendered source results.

This cache does not establish publication or knowledge time. Vendor adapters
remain responsible for their date filters and coverage verdicts. It bounds
concurrent acquisitions and checks Stop/deadline while callers wait; an already
running vendor call must enforce its own transport timeout.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, wait
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from typing import Any

from tradingagents.dataflows.feed import Feed
from tradingagents.run_control import current_control
from tradingagents.telemetry import current_trace

_POLL_SECONDS = 0.05
_DEFAULT_TTLS = {
    "price": 60.0, "core_stock_apis": 60.0, "technical_indicators": 60.0,
    "news": 60.0, "news_data": 60.0,
    "financial": 300.0, "fundamental_data": 300.0,
}
_INSTRUMENT = re.compile(r"^[A-Za-z0-9_.^=+\-]{1,100}$")
_API_NOTICE_FIELDS = frozenset({"Information", "Note", "Error Message"})


@dataclass(frozen=True)
class EvidenceRecord:
    """Acquisition provenance for rendered output, with unknown fields explicit.

    A content hash identifies the adapter's result, not an original document.
    The optional context fields copy only explicit request values. No timestamp,
    reporting period or monetary unit is inferred from the rendered body.
    """

    source: str
    operation: str
    request_sha256: str
    content_sha256: str
    fetched_at: str
    parser_version: str = "rendered-v1"
    coverage: str = "available"
    instrument: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    as_of: str | None = None
    source_url: str | None = None
    published_at: str | None = None
    available_at: str | None = None
    reporting_period: str | None = None
    currency: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class _Cached:
    value: Any
    completed: float
    record: EvidenceRecord


def _positive(name, value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _limit(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _json_default(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def _sha256(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=_json_default, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_control():
    control = current_control()
    if control is not None:
        control.check()


def _observe(source, operation, status):
    trace = current_trace()
    if trace is not None:
        trace.event("evidence_cache", {
            "source": source, "operation": operation, "cache_status": status,
        })


def _payload_has_data(value):
    """Reject empty payloads and standalone vendor notice envelopes.

    A data response may legitimately contain a Note beside its figures. Only
    an envelope made entirely of the known notice fields is an API notice.
    A nonempty feed envelope with zero articles remains a valid answer.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        notice = (bool(value) and value.keys() <= _API_NOTICE_FIELDS
                  and all(item is None or isinstance(item, str) for item in value.values()))
        return bool(value) and not notice
    if isinstance(value, (list, tuple, set, frozenset)):
        return bool(value)
    return True


def _eligible(value):
    if isinstance(value, Feed):
        return not value.unavailable and _eligible(value.text)
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if not text or (
            text.startswith(("DATA_UNAVAILABLE:", "NO_DATA_AVAILABLE:"))
            or "withheld for this date" in lowered
            or (text.startswith("<") and "unavailable" in lowered)
        ):
            return False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return True  # Plain rendered source text, including tables/CSV.
        return _payload_has_data(payload)
    return _payload_has_data(value)


def _request_date(request, *keys):
    for key in keys:
        value = request.get(key)
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                continue
            if parsed.isoformat() == value:
                return value
    return None


def request_window(request) -> tuple[str | None, str | None, str | None]:
    """(window start, window end, as-of date) a request states, each None when it states none."""
    return (_request_date(request, "start_date", "window_start"),
            _request_date(request, "end_date", "window_end"),
            _request_date(request, "as_of", "as_of_date", "curr_date", "trade_date"))


def _record(source, operation, request_sha256, request, value):
    instrument = next((request.get(key) for key in ("instrument", "symbol", "ticker")
                       if isinstance(request.get(key), str)
                       and _INSTRUMENT.fullmatch(request[key])), None)
    window_start, window_end, as_of = request_window(request)
    return EvidenceRecord(
        source=source, operation=operation, request_sha256=request_sha256,
        content_sha256=_sha256(value), fetched_at=datetime.now(UTC).isoformat(),
        instrument=instrument, window_start=window_start, window_end=window_end, as_of=as_of,
    )


class EvidenceSession:
    """One run's completed LRU, shared in-flight futures and provider permits.

    ``max_entries`` bounds completed values and their metadata. In-flight keys
    are kept separately and are never evicted while a caller can be sharing
    them. Expiry starts after acquisition completes. ``ttl_seconds`` can be a
    scalar override or a mapping of category overrides; otherwise price/news
    use 60 seconds and financial data 300. Unlisted categories use 60 seconds.
    """

    def __init__(self, ttl_seconds=None, max_concurrency=2, max_entries=256, clock=None):
        if ttl_seconds is None:
            self._ttl = None
        elif isinstance(ttl_seconds, Mapping):
            self._ttl = {key: _positive("ttl_seconds", value)
                         for key, value in ttl_seconds.items()}
        else:
            self._ttl = _positive("ttl_seconds", ttl_seconds)
        self._max_concurrency = _limit("max_concurrency", max_concurrency)
        self._max_entries = _limit("max_entries", max_entries)
        self._clock = time.monotonic if clock is None else clock
        if not callable(self._clock):
            raise ValueError("clock must be callable")
        self._lock = threading.RLock()
        self._cache: OrderedDict[tuple, _Cached] = OrderedDict()
        self._inflight: dict[tuple, Future] = {}
        self._permits: dict[str, threading.BoundedSemaphore] = {}

    def _ttl_for(self, category):
        default = _DEFAULT_TTLS.get(category, 60.0)
        if isinstance(self._ttl, dict):
            return self._ttl.get(category, default)
        return default if self._ttl is None else self._ttl

    def records(self) -> tuple[EvidenceRecord, ...]:
        """Immutable metadata for successful results currently retained in the LRU."""
        with self._lock:
            return tuple(entry.record for entry in self._cache.values())

    def record_for(self, source: str, operation: str, request: dict, *,
                   category: str) -> EvidenceRecord | None:
        """The retained record for a request, giving when its answer was really fetched."""
        key = (source, operation, category, _sha256(deepcopy(request)))
        with self._lock:
            cached = self._cache.get(key)
            return None if cached is None else cached.record

    def fetch(self, source: str, operation: str, request: dict,
              loader: Callable, *, category: str):
        """Acquire once for identical requests and give every consumer its own copy.

        Keys include source, operation, category and the entire request. The
        caller supplies its view window and relevant configuration in request;
        the session cannot infer date eligibility or vendor configuration.
        """
        _check_control()
        request = deepcopy(request)
        request_hash = _sha256(request)
        key = (source, operation, category, request_hash)
        cached_value = None
        with self._lock:
            cached = self._cache.get(key)
            status = "miss"
            if cached is not None:
                age = self._clock() - cached.completed
                if 0 <= age < self._ttl_for(category):
                    self._cache.move_to_end(key)
                    cached_value = cached
                    status = "hit"
                else:
                    del self._cache[key]
                    status = "expired"
            future = self._inflight.get(key)
            owner = future is None
            if cached_value is None and owner:
                future = Future()
                self._inflight[key] = future
                if source not in self._permits:
                    self._permits[source] = threading.BoundedSemaphore(self._max_concurrency)
            elif cached_value is None:
                status = "coalesced"
        _observe(source, operation, status)
        if cached_value is not None:
            _check_control()
            return deepcopy(cached_value.value)
        if not owner:
            while not future.done():
                _check_control()
                wait((future,), timeout=_POLL_SECONDS)
            _check_control()
            return deepcopy(future.result())

        permit = self._permits[source]
        acquired = False
        try:
            while not acquired:
                _check_control()
                acquired = permit.acquire(timeout=_POLL_SECONDS)
            _check_control()
            value = loader()
            completed = self._clock()
            _check_control()
            stored = deepcopy(value)
            result = deepcopy(stored)
            record = _record(source, operation, request_hash, request, stored) if _eligible(stored) else None
            with self._lock:
                if record is not None:
                    self._cache[key] = _Cached(stored, completed, record)
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
                future.set_result(stored)
                del self._inflight[key]
            return result
        except BaseException as exc:
            with self._lock:
                if not future.done():
                    future.set_exception(exc)
                self._inflight.pop(key, None)
            raise
        finally:
            if acquired:
                permit.release()


_evidence: ContextVar[EvidenceSession | None] = ContextVar("evidence_session", default=None)


def current_evidence() -> EvidenceSession | None:
    return _evidence.get()


def set_evidence(session: EvidenceSession | None):
    """Set a session in the current context, including ``Context.run`` callers."""
    return _evidence.set(session)


@contextmanager
def evidence_scope(session: EvidenceSession | None = None):
    """Bind an explicit session or a fresh isolated one, restoring the parent."""
    active = EvidenceSession() if session is None else session
    token = set_evidence(active)
    try:
        yield active
    finally:
        _evidence.reset(token)
