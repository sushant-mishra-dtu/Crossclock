"""Live-runner budget and destination guards are exercised without any network."""

import importlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_live_graph_c01 as live_runner
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


def test_continuation_registers_exact_remaining_pairs_and_cumulative_cap():
    # The recorded 15 October invocation, with that day's prior reservation.
    args = live_runner.parse_arguments(["--execute", "--pairs", "2", "--start-repetition", "2",
                                       "--max-nvidia-attempts", "192", "--jev-cap-usd", "0.90",
                                       "--prior-jev-reserved-usd", "0.088080384"])
    plan = live_runner.execution_plan(args)
    assert plan["registered_pair_indices"] == [2, 3]
    assert plan["registered_runs"] == 4 and plan["nvidia_max_http_attempts"] == 192
    assert plan["registered_run_order"] == [
        {"mode": "cold_fixture_cache", "repetition": 2},
        {"mode": "warm_fixture_cache", "repetition": 2},
        {"mode": "cold_fixture_cache", "repetition": 3},
        {"mode": "warm_fixture_cache", "repetition": 3},
    ]
    assert args.jev_cap_usd + args.prior_jev_reserved_usd == Decimal("0.988080384")
    assert plan["jev_authorized_total_cap_usd"] == 1.0
    assert plan["timing_protocol"]["research_end"].startswith("persisted_report")


def test_defaults_carry_forward_every_prior_jev_reservation():
    args = live_runner.parse_arguments(["--execute"])
    assert args.prior_jev_reserved_usd == Decimal("0.173408256")
    assert args.jev_cap_usd + args.prior_jev_reserved_usd <= Decimal("1")
    with pytest.raises(SystemExit):
        live_runner.parse_arguments(["--execute", "--jev-cap-usd", "0.90"])


@pytest.mark.parametrize("flags", [
    [], ["--pairs", "0"], ["--pairs", "4"], ["--start-repetition", "0"],
    ["--pairs", "2", "--start-repetition", "3"], ["--max-nvidia-attempts", "0"],
    ["--max-nvidia-attempts", "193"], ["--jev-cap-usd", "NaN"], ["--jev-cap-usd", "Infinity"],
    ["--jev-cap-usd", "-0.1"], ["--jev-cap-usd", "0.001"], ["--jev-cap-usd", "not-a-decimal"],
    ["--jev-cap-usd", "0.98"], ["--prior-jev-reserved-usd", "-0.01"],
    ["--prior-jev-reserved-usd", "NaN"], ["--prior-jev-reserved-usd", "1.0"],
    ["--nvidia-timeout-seconds", "0"], ["--nvidia-timeout-seconds", "600"],
])
def test_invalid_execution_or_spending_selection_is_refused_before_execution(flags):
    argv = [] if not flags else ["--execute", *flags]
    with pytest.raises(SystemExit) as error:
        live_runner.parse_arguments(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("header", ["true", "TRUE", "1", "enabled"])
def test_jev_long_context_header_cannot_invalidate_reservation(header):
    request = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone",
                            headers={"TypeSafe-Danger-Zone-Long-Context": header})
    with pytest.raises(SendGuardStopped):
        check_destination(request, "api.typesafe.ai", "/v1/systemone")


def test_selected_pairs_persist_each_cold_cache_before_warm(monkeypatch, tmp_path):
    calls = []
    fixture = load_fixture()

    def fake_measure(fixture, mode, repetition, directory, cache, nvidia, jev, keys, attempt_id,
                     nvidia_timeout_seconds):
        assert nvidia_timeout_seconds == 180
        calls.append((mode, repetition))
        run_id = f"live-{attempt_id}-{mode}-{repetition:02d}"
        (directory / run_id).mkdir()
        if mode == "warm_fixture_cache":
            persisted = json.loads((directory / f"live-{attempt_id}-cold_fixture_cache-{repetition:02d}" /
                                    "fixture_cache.json").read_text())
            assert persisted["entries"] == cache
            assert persisted["cache_sha256"] == live_runner.fingerprint(cache)
        else:
            assert cache is None
        result = {"run_id": run_id, "mode": mode, "repetition": repetition, "status": "completed",
                  "wall_ms": 1.0, "configuration_sha256": "matched-configuration"}
        return result, {"fixture-key": fixture["source_answer"]}

    monkeypatch.setattr(live_runner, "measure_run", fake_measure)
    stop = threading.Event()
    samples = live_runner.run_pairs(fixture, tmp_path, [2, 3], SendBudget(stop, 192),
        SendBudget(stop, 100, JEV_ATTEMPT_RESERVATION, Decimal("0.90")), ("unused", "unused"), "attempt12", 180)
    assert calls == [("cold_fixture_cache", 2), ("warm_fixture_cache", 2),
                     ("cold_fixture_cache", 3), ("warm_fixture_cache", 3)]
    assert len({sample["run_id"] for sample in samples}) == 4
    assert all("attempt12" in sample["run_id"] for sample in samples)
    assert json.loads((tmp_path / "samples.json").read_text()) == samples


def _saved_cold(directory, fixture, configuration_sha256, repetition=2):
    entries = {"fixture-key": fixture["source_answer"]}
    run = directory / "earlier-attempt" / f"live-earlier-cold_fixture_cache-{repetition:02d}"
    run.mkdir(parents=True)
    cache_hash = live_runner.fingerprint(entries)
    (run / "fixture_cache.json").write_text(json.dumps({
        "schema_version": 1, "corpus_sha256": live_runner.fingerprint(fixture),
        "configuration_sha256": configuration_sha256, "cache_sha256": cache_hash, "entries": entries}))
    (run / "sample.json").write_text(json.dumps({
        "run_id": run.name, "mode": "cold_fixture_cache", "repetition": repetition, "status": "completed",
        "configuration_sha256": configuration_sha256, "fixture_cache_sha256": cache_hash}))
    return run / "fixture_cache.json"


def test_warm_seed_finishes_an_earlier_pair_before_new_pairs(monkeypatch, tmp_path):
    fixture = load_fixture()
    seed = live_runner.load_warm_seed(_saved_cold(tmp_path, fixture, "matched-configuration"),
                                      fixture, "matched-configuration")
    assert seed["repetition"] == 2
    assert seed["metadata"]["cold_session"] == "earlier-attempt"
    args = live_runner.parse_arguments(["--execute", "--pairs", "1", "--start-repetition", "3",
                                       "--warm-seed", "unused-until-main", "--nvidia-timeout-seconds", "180"])
    plan = live_runner.execution_plan(args, seed)
    assert plan["registered_runs"] == 3 and plan["registered_pair_indices"] == [3]
    assert [(row["mode"], row["repetition"]) for row in plan["registered_run_order"]] == [
        ("warm_fixture_cache", 2), ("cold_fixture_cache", 3), ("warm_fixture_cache", 3)]
    assert "entries" not in json.dumps(plan)
    calls = []

    def fake_measure(fixture, mode, repetition, directory, cache, nvidia, jev, keys, attempt_id,
                     nvidia_timeout_seconds):
        calls.append((mode, repetition, cache))
        run_id = f"live-{attempt_id}-{mode}-{repetition:02d}"
        (directory / run_id).mkdir()
        return ({"run_id": run_id, "mode": mode, "repetition": repetition, "status": "completed", "wall_ms": 1.0,
                 "configuration_sha256": "matched-configuration"}, {"cold-key": "value"})

    monkeypatch.setattr(live_runner, "measure_run", fake_measure)
    output = tmp_path / "completion"
    output.mkdir()
    stop = threading.Event()
    samples = live_runner.run_pairs(fixture, output, [3], SendBudget(stop, 10),
        SendBudget(stop, 10, JEV_ATTEMPT_RESERVATION, Decimal("0.20")), ("unused", "unused"), "attempt34", 180, seed)
    assert [(mode, repetition) for mode, repetition, _ in calls] == [
        ("warm_fixture_cache", 2), ("cold_fixture_cache", 3), ("warm_fixture_cache", 3)]
    assert calls[0][2] == seed["entries"] and calls[1][2] is None and calls[2][2] == {"cold-key": "value"}
    seeded = samples[0]
    assert seeded["input_fixture_cache_sha256"] == seed["metadata"]["cache_sha256"]
    assert seeded["input_fixture_cache_source"] == {"cold_session": "earlier-attempt",
                                                    "cold_run_id": "live-earlier-cold_fixture_cache-02"}
    assert samples[2]["input_fixture_cache_sha256"] == samples[1]["fixture_cache_sha256"]
    with pytest.raises(ValueError, match="also registered"):
        live_runner.execution_plan(live_runner.parse_arguments(
            ["--execute", "--pairs", "2", "--start-repetition", "2", "--warm-seed", "x"]), seed)


@pytest.mark.parametrize("tamper", ["entries", "corpus", "configuration", "cold_failed", "cold_hash"])
def test_warm_seed_is_refused_unless_every_recorded_hash_matches(tmp_path, tamper):
    fixture = load_fixture()
    path = _saved_cold(tmp_path, fixture, "matched-configuration")
    seed, cold = json.loads(path.read_text()), json.loads(path.with_name("sample.json").read_text())
    if tamper == "entries":
        seed["entries"]["fixture-key"] = "edited"
    elif tamper == "corpus":
        seed["corpus_sha256"] = "other-corpus"
    elif tamper == "cold_failed":
        cold["status"] = "failed"
    elif tamper == "cold_hash":
        cold["fixture_cache_sha256"] = "other-cache"
    path.write_text(json.dumps(seed))
    path.with_name("sample.json").write_text(json.dumps(cold))
    expected = "other-configuration" if tamper == "configuration" else "matched-configuration"
    with pytest.raises(ValueError, match="warm seed refused"):
        live_runner.load_warm_seed(path, fixture, expected)


def test_zero_new_pairs_is_accepted_only_with_a_warm_seed():
    args = live_runner.parse_arguments(["--execute", "--pairs", "0", "--warm-seed", "seed.json"])
    assert args.pairs == 0
    with pytest.raises(SystemExit):
        live_runner.parse_arguments(["--execute", "--pairs", "0"])


def test_recovery_timeout_is_registered_as_a_different_configuration(tmp_path):
    args = live_runner.parse_arguments(["--execute", "--nvidia-timeout-seconds", "180"])
    assert live_runner.execution_plan(args)["nvidia_request_timeout_seconds"] == 180
    config = registered_config(load_fixture(), tmp_path)
    baseline = live_runner.normalized_config(config)
    recovery = live_runner.normalized_config(config, args.nvidia_timeout_seconds)
    assert baseline["llm_request_timeout_seconds"] == 60
    assert recovery["llm_request_timeout_seconds"] == 180
    assert live_runner.fingerprint(baseline) != live_runner.fingerprint(recovery)
    assert {key for key in baseline if baseline[key] != recovery[key]} == {"llm_request_timeout_seconds"}


def test_startup_preserves_execution_identity_without_model_warmup(monkeypatch, tmp_path):
    pytest.importorskip("typesafe_sdk")
    from langchain_core.language_models.chat_models import BaseChatModel

    def no_inference(*args, **kwargs):
        raise AssertionError("dependency startup must not invoke a model")

    monkeypatch.setattr(BaseChatModel, "invoke", no_inference)
    typesafe = importlib.import_module("typesafe_sdk")
    monkeypatch.setattr(typesafe.TypeSafeClient, "system_one", no_inference)
    fixture = load_fixture()
    before_config = live_runner.normalized_config(registered_config(fixture, tmp_path / "before"))
    before_hash = live_runner.fingerprint(before_config)
    before_corpus = live_runner.fingerprint(fixture)
    graph_module = importlib.import_module("tradingagents.graph.trading_graph")
    startup = live_runner.session_startup(tmp_path, "test-attempt")
    after = live_runner.normalized_config(registered_config(fixture, tmp_path / "after"))
    assert live_runner.fingerprint(after) == before_hash
    assert live_runner.fingerprint(fixture) == before_corpus
    assert sys.modules["tradingagents.graph.trading_graph"] is graph_module
    assert startup["warmup_requests"] == 0 and startup["network_policy"] == "refused"
    assert startup["run_id"] == "startup-test-attempt" and startup["duration_ms"] >= 0
    trace = json.loads((tmp_path / startup["trace_path"]).read_text())
    assert trace["status"] == "completed"
    assert not any(span["kind"] in {"llm", "llm_invocation", "jev", "transport_attempt"} for span in trace["spans"])
    assert {span["name"] for span in trace["spans"] if span["kind"] == "startup_import"} == set(live_runner.STARTUP_MODULES)
