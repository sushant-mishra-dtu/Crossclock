"""Live-runner budget and destination guards are exercised without any network."""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from scripts.benchmark_live_graph_c01 import (
    JEV_ATTEMPT_RESERVATION,
    SendBudget,
    SendGuardStopped,
    check_destination,
    graph_node_paths,
    guarded_transport,
    load_fixture,
    registered_config,
)

pytestmark = pytest.mark.unit


def test_reservations_cannot_overspend_during_concurrent_sends():
    stop = threading.Event()
    budget = SendBudget(stop, 100, Decimal("0.1"), Decimal("0.7"))
    def reserve(_):
        try:
            budget.reserve()
            return True
        except SendGuardStopped:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(reserve, range(40)))
    assert sum(accepted) == budget.attempts == 7
    assert budget.reserved_usd == Decimal("0.7") and stop.is_set()


def test_provider_attempts_and_cost_reservations_share_cancellation():
    stop = threading.Event()
    nvidia = SendBudget(stop, 2)
    jev = SendBudget(stop, 10, JEV_ATTEMPT_RESERVATION, Decimal("0.98"))
    nvidia.reserve()
    jev.reserve()
    nvidia.stop("http_429")
    with pytest.raises(SendGuardStopped):
        jev.reserve()
    assert nvidia.attempts == jev.attempts == 1
    assert jev.reserved_usd == JEV_ATTEMPT_RESERVATION


@pytest.mark.parametrize("url", [
    "http://integrate.api.nvidia.com/v1/chat/completions",
    "https://other.example/v1/chat/completions",
    "https://integrate.api.nvidia.com/v1/responses",
    "https://integrate.api.nvidia.com/v1/chat/completions?token=private",
    "https://private:password@integrate.api.nvidia.com/v1/chat/completions",
    "https://integrate.api.nvidia.com:8443/v1/chat/completions",
])
def test_only_exact_official_model_destination_is_permitted(url):
    with pytest.raises(SendGuardStopped):
        check_destination(httpx.Request("POST", url), "integrate.api.nvidia.com", "/v1/chat/completions")
    check_destination(httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions"),
                      "integrate.api.nvidia.com", "/v1/chat/completions")


def test_live_configuration_preserves_default_graph_caps(monkeypatch, tmp_path):
    from tradingagents.default_config import build_default_config
    monkeypatch.setenv("TRADINGAGENTS_MAX_TOOL_ROUNDS", "2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "10")
    config = registered_config(load_fixture(), tmp_path)
    monkeypatch.delenv("TRADINGAGENTS_MAX_TOOL_ROUNDS")
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS")
    defaults = build_default_config()
    for name in ("max_tool_rounds", "max_debate_rounds", "max_risk_discuss_rounds", "max_recur_limit"):
        assert config[name] == defaults[name]
    assert config["max_tool_rounds"] != load_fixture()["configuration"]["max_tool_rounds"]
    assert config["llm_max_retries"] == 0 and config["jev_enabled"] is True


def test_parallel_nodes_are_separate_from_serialized_stage_durations():
    snapshot = {"spans": [
        {"kind": "graph_node", "name": "Market Analyst", "duration_ms": 50},
        {"kind": "graph_node", "name": "News Analyst", "duration_ms": 30},
        {"kind": "graph_node", "name": "Memory Log", "duration_ms": 20},
        {"kind": "graph_node", "name": "Trader", "duration_ms": 10},
        {"kind": "llm", "name": "nested_call", "duration_ms": 45},
    ]}
    paths = graph_node_paths(snapshot)
    assert paths["parallel_analyst_max_path_ms"] == 50
    assert paths["parallel_memory_path_ms"] == 20
    assert paths["serialized_stage_ms"] == {"Trader": 10}


def test_refused_transport_dispatch_is_marked_not_sent():
    from tradingagents.llm_clients.telemetry import observe_http_client, transport_call
    from tradingagents.telemetry import RunTrace, use_trace

    budget = SendBudget(threading.Event(), 1)
    budget.reserve()  # The one authorized send was already consumed.
    transport = guarded_transport(httpx, budget, "integrate.api.nvidia.com", "/v1/chat/completions", "nvidia", "offline-model")
    transport.inner.close()
    sent = []
    transport.inner = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200))
    trace = RunTrace()
    metadata = SimpleNamespace(_telemetry_provider="nvidia", model="offline-model", _telemetry_transport_coverage="httpx_hooks")
    with httpx.Client(transport=transport) as client:
        observe_http_client(client)
        with use_trace(trace), transport_call(metadata), pytest.raises(SendGuardStopped):
            client.post("https://integrate.api.nvidia.com/v1/chat/completions")
    trace.finish("failed")
    attempts = [span for span in trace.snapshot()["spans"] if span["kind"] == "transport_attempt"]
    assert len(attempts) == 1 and attempts[0]["status"] == "not_sent"
    assert sent == [] and budget.attempts == 1
    assert "httpx_response_missing" not in trace.snapshot()["telemetry_gaps"]
