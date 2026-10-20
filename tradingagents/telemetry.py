"""Small, request-scoped timing traces. Never store prompts or response bodies.

Durations use a monotonic clock. UTC timestamps locate events in time; offsets
can be compared even if the system clock changes. Instrumentation is opt-in for
library callers via ``use_trace`` and enabled by the local web analysis jobs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

_trace: ContextVar[RunTrace | None] = ContextVar("research_trace", default=None)
_span: ContextVar[tuple[str, str] | None] = ContextVar("research_span", default=None)
_LABEL = re.compile(r"^[\w .:/+@,=()\-]{1,200}$", re.ASCII)
_ATTRIBUTES = frozenset({
    "node", "model", "provider", "operation", "source", "category", "outcome",
    "transport", "coverage", "http_status", "retry_count", "attempt", "delay_seconds",
    "cache", "cache_status", "input_tokens", "output_tokens", "cached_input_tokens",
    "reasoning_tokens", "usage_status", "error_type", "callback_run_id",
    "parent_callback_run_id", "method", "status", "count", "coalesced", "case",
    "regime", "repetition", "fixture", "supported", "reason", "billable_input_tokens",
    "cost_usd", "estimated_cost_usd", "pricing_version", "currency", "max_calls", "finish_reason",
    "mode", "deadline_seconds", "attempt_timeout_seconds",
})
_CONFIG_KEYS = frozenset({
    "llm_provider", "deep_think_provider", "quick_think_provider", "deep_think_llm",
    "quick_think_llm", "analysts", "max_debate_rounds", "max_risk_discuss_rounds",
    "max_tool_rounds", "max_recur_limit", "llm_max_retries", "output_language",
    "jev_enabled", "checkpoint_enabled", "openai_reasoning_effort", "anthropic_effort",
    "google_thinking_level", "max_output_tokens", "max_tokens", "temperature", "data_vendors",
    "tool_vendors", "version", "mode", "measurement_kind", "spending_cap_usd",
    "jev_model", "jev_claim_check", "source_revision", "research_mode",
})
_WORKLOAD_KEYS = frozenset({
    "ticker", "symbol", "symbols", "trade_date", "asset_type", "analysts", "case",
    "regime", "repetition", "fixture_sha256", "corpus_sha256", "source_revision",
    "source_dirty", "configuration_sha256", "workload_sha256", "mode", "entrypoint",
})


def _value(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        # Labels only; URLs, query strings and credential assignments are not labels.
        if "://" in value or re.search(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]", value):
            return "redacted"
        return value if _LABEL.fullmatch(value) else "redacted"
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value[:50]]
    return None


def safe_attributes(attributes=None):
    return {k: _value(v) for k, v in (attributes or {}).items() if k in _ATTRIBUTES}


def safe_configuration(configuration=None):
    result = {}
    for key, value in (configuration or {}).items():
        if key in _CONFIG_KEYS:
            if key in {"data_vendors", "tool_vendors"} and isinstance(value, dict):
                result[key] = {str(k): _value(v) for k, v in value.items()
                               if isinstance(k, str) and _LABEL.fullmatch(k)}
            else:
                result[key] = _value(value)
        elif key in {"backend_url", "quick_backend_url", "deep_backend_url",
                     "quick_think_backend_url", "deep_think_backend_url"} and value:
            # Identify the endpoint without leaking userinfo, queries or path tokens.
            try:
                url = urlsplit(str(value))
                origin = f"{url.scheme}://{url.hostname}"
                if url.port:
                    origin += f":{url.port}"
                result[key + "_origin"] = origin if url.scheme in {"http", "https"} else None
                result[key + "_sha256"] = hashlib.sha256(str(value).encode()).hexdigest()
            except ValueError:
                result[key + "_origin"] = None
    return result


def _utc():
    return datetime.now(UTC).isoformat()


def current_trace():
    return _trace.get()


def current_span():
    trace = current_trace()
    value = _span.get()
    return value[1] if trace and value and value[0] == trace.run_id else None


@contextmanager
def use_trace(trace):
    token = _trace.set(trace)
    span_token = _span.set((trace.run_id, trace.root_span_id) if trace else None)
    try:
        yield trace
    finally:
        _span.reset(span_token)
        _trace.reset(token)


class RunTrace:
    def __init__(self, run_id=None, configuration=None, workload=None):
        self.run_id = str(run_id or uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.run_id):
            raise ValueError("run_id must be a safe identifier")
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self.started_at_utc = _utc()
        self.configuration = safe_configuration(configuration)
        self.workload = {k: _value(v) for k, v in (workload or {}).items() if k in _WORKLOAD_KEYS}
        self._spans = {}
        self._events = []
        self._milestones = {}
        self._gaps = set()
        self._status = "running"
        self._finished = None
        self._finished_utc = None
        self.root_span_id = self.start_span("run", "research")

    def _offset(self):
        return round((time.monotonic() - self._started) * 1000, 3)

    @property
    def current_span_id(self):
        return current_span() or self.root_span_id

    @property
    def current_span_attributes(self):
        with self._lock:
            return dict(self._spans.get(self.current_span_id, {}).get("attributes", {}))

    @property
    def current_span_kind(self):
        with self._lock:
            return self._spans.get(self.current_span_id, {}).get("kind")

    def start_span(self, kind, name, attributes=None, parent_id=None):
        with self._lock:
            span_id = uuid4().hex
            parent = parent_id or current_span()
            if parent not in self._spans:
                parent = getattr(self, "root_span_id", None)
            self._spans[span_id] = {
                "span_id": span_id, "parent_id": parent,
                "kind": _value(kind), "name": _value(name), "start_ms": self._offset(),
                "started_at_utc": _utc(), "end_ms": None, "duration_ms": None,
                "status": "running", "attributes": safe_attributes(attributes),
            }
            return span_id

    def end_span(self, span_id, status="ok", attributes=None):
        with self._lock:
            span = self._spans.get(span_id)
            if not span or span["end_ms"] is not None:
                return
            span["end_ms"] = self._offset()
            span["duration_ms"] = round(span["end_ms"] - span["start_ms"], 3)
            span["status"] = _value(status)
            span["attributes"].update(safe_attributes(attributes))

    def bind_span(self, span_id):
        """Bind a synchronous callback span until its matching end callback."""
        return _span.set((self.run_id, span_id))

    def reset_span(self, token):
        _span.reset(token)

    @contextmanager
    def activate_span(self, span_id):
        token = self.bind_span(span_id)
        try:
            yield span_id
        finally:
            self.reset_span(token)

    @contextmanager
    def scope(self, kind, name, attributes=None):
        span_id = self.start_span(kind, name, attributes)
        token = _span.set((self.run_id, span_id))
        try:
            yield span_id
        except BaseException as exc:
            self.end_span(span_id, "error", {"error_type": type(exc).__name__})
            raise
        else:
            self.end_span(span_id)
        finally:
            _span.reset(token)

    def event(self, name, attributes=None):
        with self._lock:
            self._events.append({"name": _value(name), "at_utc": _utc(),
                                 "offset_ms": self._offset(), "span_id": self.current_span_id,
                                 "attributes": safe_attributes(attributes)})

    def milestone(self, name):
        with self._lock:
            self._milestones.setdefault(_value(name), {"at_utc": _utc(), "offset_ms": self._offset()})

    def gap(self, name):
        with self._lock:
            self._gaps.add(_value(name))

    def finish(self, status):
        with self._lock:
            if self._finished is not None:
                return
            for span_id, span in self._spans.items():
                if span_id != self.root_span_id and span["end_ms"] is None:
                    self.end_span(span_id, "incomplete")
                    self._gaps.add("unfinished_span_outcome")
            self.end_span(self.root_span_id, status)
            self._status = _value(status)
            self._finished = self._offset()
            self._finished_utc = _utc()
            self.milestone("finished")

    def snapshot(self):
        with self._lock:
            spans = list(self._spans.values())
            llms = [s for s in spans if s["kind"] == "llm"]
            invocations = [s for s in spans if s["kind"] == "llm_invocation"]
            invocation_ids = {s["span_id"] for s in invocations}

            def inside_invocation(span):
                parent = span["parent_id"]
                seen = set()
                while parent in self._spans and parent not in seen:
                    if parent in invocation_ids:
                        return True
                    seen.add(parent)
                    parent = self._spans[parent]["parent_id"]
                return False

            # Existing UI llm_calls counts callback generations. Outer retry
            # generations are attempts within one public model invocation.
            metrics = {
                "logical_llm_invocations": len(invocations) + sum(not inside_invocation(s) for s in llms),
                "llm_generation_attempts": len(llms),
                "observed_http_attempts": sum(s["kind"] == "transport_attempt" and s["status"] != "not_sent" for s in spans),
                "refused_http_sends": sum(s["kind"] == "transport_attempt" and s["status"] == "not_sent" for s in spans),
                "jev_logical_calls": sum(s["kind"] == "jev" for s in spans),
                "reported_input_tokens": sum(s["attributes"].get("input_tokens") or 0 for s in llms),
                "reported_generation_tokens": sum(s["attributes"].get("output_tokens") or 0 for s in llms),
                "reported_cached_input_tokens": sum(s["attributes"].get("cached_input_tokens") or 0 for s in llms),
                "llm_usage_missing_calls": sum(s["attributes"].get("usage_status") != "reported" for s in llms),
            }
            if "quick_worker_transport_usage_not_observed" in self._gaps:
                # Spawned workers have no callback/transport observation in this process.
                for key in ("logical_llm_invocations", "llm_generation_attempts",
                            "observed_http_attempts", "refused_http_sends", "reported_input_tokens",
                            "reported_generation_tokens", "reported_cached_input_tokens",
                            "llm_usage_missing_calls"):
                    metrics[key] = None
            return copy.deepcopy({
                "schema_version": 1, "run_id": self.run_id, "root_span_id": self.root_span_id,
                "configuration": self.configuration, "workload": self.workload,
                "status": self._status, "started_at_utc": self.started_at_utc,
                "finished_at_utc": self._finished_utc,
                "duration_ms": self._finished if self._finished is not None else self._offset(),
                "spans": spans, "events": self._events, "metrics": metrics,
                "milestones": self._milestones, "telemetry_gaps": sorted(self._gaps),
                "cost": {"amount": None, "currency": None, "status": "unknown",
                         "reason": "pricing_not_configured"},
            })

    def persist(self, path):
        target = Path(path)
        if target.suffix != ".json":
            target = target / f"{self.run_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".trace-", suffix=".json", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot(), handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target
