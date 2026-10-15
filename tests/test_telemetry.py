"""Trace privacy, parent attribution and isolation are part of C01's contract."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from tradingagents.telemetry import RunTrace, current_span, current_trace, use_trace


def test_nested_trace_persists_only_metadata(tmp_path):
    trace = RunTrace(configuration={
        "llm_provider": "nvidia", "api_key": "private-key",
        "backend_url": "https://user:password@example.com/v1?key=private-key",
    })
    with use_trace(trace), trace.scope("graph_node", "Market Analyst") as node:
        with trace.scope("llm", "nemotron", {"input_tokens": 7, "prompt": "private-document"}):
            trace.event("usage", {"output_tokens": 3, "document": "private-document"})
        trace.milestone("generated_report")
    trace.finish("completed")
    saved = trace.persist(tmp_path)
    data = json.loads(saved.read_text())
    assert data["configuration"]["backend_url_origin"] == "https://example.com"
    assert data["spans"][2]["parent_id"] == node
    assert all(s["duration_ms"] >= 0 for s in data["spans"])
    assert data["milestones"]["generated_report"]["at_utc"].endswith("+00:00")
    assert "private-key" not in saved.read_text()
    assert "private-document" not in saved.read_text()
    assert "password" not in saved.read_text()
    assert data["cost"]["amount"] is None
    assert current_trace() is None


def test_parallel_contexts_inherit_parent_and_keep_runs_isolated():
    traces = [RunTrace(), RunTrace()]

    def record(trace):
        assert current_trace() is trace
        with trace.scope("source_attempt", "fixture"):
            assert current_span() != trace.root_span_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = []
        for trace in traces:
            with use_trace(trace):
                tasks.append(pool.submit(copy_context().run, record, trace))
        for task in tasks:
            task.result()
    for trace in traces:
        spans = trace.snapshot()["spans"]
        assert len(spans) == 2
        assert spans[1]["parent_id"] == trace.root_span_id


def test_failure_and_unobserved_transport_have_truthful_status():
    trace = RunTrace()
    with use_trace(trace):
        trace.start_span("transport_attempt", "sdk")
        with pytest.raises(ValueError), trace.scope("source_attempt", "fixture"):
            raise ValueError("secret exception body")
    trace.finish("failed")
    trace.finish("completed")
    data = trace.snapshot()
    assert data["status"] == "failed"
    assert data["spans"][1]["status"] == "incomplete"
    assert data["spans"][2]["attributes"]["error_type"] == "ValueError"
    assert "unfinished_span_outcome" in data["telemetry_gaps"]
    assert "secret exception body" not in json.dumps(data)


def test_bind_span_restores_parent_even_with_nested_callback():
    trace = RunTrace()
    with use_trace(trace), trace.scope("graph_node", "Trader") as parent:
        model = trace.start_span("llm", "model")
        token = trace.bind_span(model)
        attempt = trace.start_span("transport_attempt", "http")
        trace.reset_span(token)
        assert current_span() == parent
    assert trace.snapshot()["spans"][-1]["span_id"] == attempt
    assert trace.snapshot()["spans"][-1]["parent_id"] == model
