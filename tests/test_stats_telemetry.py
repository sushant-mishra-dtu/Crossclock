"""Callback attribution and missing usage without provider requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from uuid import uuid4

import pytest
from langchain_core.callbacks import CallbackManager
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from cli.stats_handler import StatsCallbackHandler
from tradingagents.telemetry import RunTrace, current_span, use_trace

pytestmark = pytest.mark.unit


def _response(usage=None):
    return LLMResult(generations=[[ChatGeneration(message=AIMessage(
        content="private generated report", usage_metadata=usage))]])


def test_callbacks_preserve_stage_ancestry_and_count_one_logical_model_call():
    trace = RunTrace()
    stats = StatsCallbackHandler(trace)
    stage, wrapper, model = uuid4(), uuid4(), uuid4()
    with use_trace(trace):
        stats.on_chain_start(None, {"private": "document"}, run_id=stage,
                             name="Market Analyst", metadata={"langgraph_node": "Market Analyst"})
        stage_span = current_span()
        stats.on_chain_start(None, {}, run_id=wrapper, parent_run_id=stage,
                             name="agent", metadata={"langgraph_node": "agent"})
        params = {"run_id": model, "parent_run_id": wrapper,
                  "metadata": {"langgraph_node": "agent", "ls_model_name": "test-model",
                               "ls_provider": "test-provider", "api_key": "private-secret"}}
        stats.on_chat_model_start({}, [["private prompt"]], **params)
        model_span = current_span()
        stats.on_llm_start({}, ["private prompt"], **params)
        stats.on_llm_end(_response({"input_tokens": 11, "output_tokens": 7,
                                   "total_tokens": 18, "input_token_details": {"cache_read": 4}}),
                         run_id=model)
        stats.on_llm_end(_response(), run_id=model)  # duplicate end cannot add usage
        assert current_span() == stage_span
        stats.on_chain_end({"market_report": "private report text"}, run_id=wrapper)
        stats.on_chain_end({}, run_id=stage)
        assert current_span() == trace.root_span_id
    data = trace.snapshot()
    [node] = [s for s in data["spans"] if s["kind"] == "graph_node"]
    [llm] = [s for s in data["spans"] if s["kind"] == "llm"]
    assert llm["span_id"] == model_span
    assert llm["parent_id"] == node["span_id"]
    assert llm["attributes"]["node"] == "Market Analyst"
    assert llm["attributes"]["cached_input_tokens"] == 4
    assert llm["attributes"]["model"] == "test-model"
    assert stats.get_stats() == {"llm_calls": 1, "tool_calls": 0, "tokens_in": 11,
                                 "tokens_out": 7, "cached_input_tokens": 4, "usage_missing": 0}
    assert "generated_report" in data["milestones"]
    assert "first_useful_result" not in data["milestones"]
    assert "private" not in json.dumps(data)


def test_missing_usage_and_tool_errors_are_explicit_and_redacted():
    trace = RunTrace()
    stats = StatsCallbackHandler(trace)
    model, tool = uuid4(), uuid4()
    with use_trace(trace):
        stats.on_llm_start({}, ["private prompt"], run_id=model)
        stats.on_llm_end(LLMResult(generations=[]), run_id=model)
        stats.on_tool_start({"name": "get_news"}, "https://private.example/?key=secret", run_id=tool)
        stats.on_tool_error(RuntimeError("private source failure"), run_id=tool)
    data = trace.snapshot()
    [llm] = [s for s in data["spans"] if s["kind"] == "llm"]
    [source] = [s for s in data["spans"] if s["kind"] == "tool"]
    assert llm["attributes"]["usage_status"] == "missing"
    assert llm["attributes"]["input_tokens"] is None
    assert source["status"] == "error"
    assert source["attributes"]["error_type"] == "RuntimeError"
    assert stats.get_stats()["usage_missing"] == 1
    assert stats.get_stats()["cached_input_tokens"] is None
    assert "llm_token_usage_missing" in data["telemetry_gaps"]
    assert "private" not in json.dumps(data)


def test_provider_usage_fallback_and_failed_calls_keep_existing_counters():
    stats = StatsCallbackHandler()
    model = uuid4()
    stats.on_llm_start({}, ["prompt"], run_id=model)
    stats.on_llm_end(LLMResult(generations=[], llm_output={"token_usage": {
        "prompt_tokens": 13, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 9},
    }}), run_id=model)
    failed = uuid4()
    stats.on_llm_start({}, ["prompt"], run_id=failed)
    stats.on_llm_error(TimeoutError("failure"), run_id=failed)
    assert stats.get_stats() == {"llm_calls": 2, "tool_calls": 0, "tokens_in": 13,
                                 "tokens_out": 2, "cached_input_tokens": None, "usage_missing": 1}
    assert stats.cached_input_tokens == 9  # the reported subtotal remains available


def test_concurrent_callback_bindings_restore_each_worker_context():
    trace = RunTrace()
    stats = StatsCallbackHandler(trace)

    def worker(name):
        stage, model = uuid4(), uuid4()
        initial = current_span()
        stats.on_chain_start(None, {}, name=name, run_id=stage)
        stats.on_llm_start({}, ["prompt"], run_id=model, parent_run_id=stage)
        stats.on_llm_end(_response(), run_id=model)
        stats.on_chain_end({}, run_id=stage)
        assert current_span() == initial

    with use_trace(trace), ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(copy_context().run, worker, name)
                 for name in ("Market Analyst", "News Analyst")]
        for task in tasks:
            task.result()
    nodes = {s["span_id"]: s["name"] for s in trace.snapshot()["spans"] if s["kind"] == "graph_node"}
    llms = [s for s in trace.snapshot()["spans"] if s["kind"] == "llm"]
    assert len(llms) == 2
    assert {nodes[s["parent_id"]] for s in llms} == {"Market Analyst", "News Analyst"}


@pytest.mark.parametrize("tracing", [False, True])
def test_reused_callback_handler_releases_ancestry_and_bounds_completed_ids(tracing):
    stats = StatsCallbackHandler()
    response = _response()
    calls = stats._CALLBACK_HISTORY + 5
    for _ in range(calls):
        trace = RunTrace() if tracing else None
        with use_trace(trace) if tracing else nullcontext():
            stage, wrapper, model = uuid4(), uuid4(), uuid4()
            stats.on_chain_start(None, {}, name="Market Analyst", run_id=stage)
            stats.on_chain_start(None, {}, name="agent", run_id=wrapper, parent_run_id=stage)
            stats.on_llm_start({}, ["prompt"], run_id=model, parent_run_id=wrapper)
            stats.on_llm_end(response, run_id=model)
            stats.on_chain_end({}, run_id=wrapper)
            stats.on_chain_end({}, run_id=stage)
        assert not stats._parents and not stats._nodes
        assert not stats._spans and not stats._bindings and not stats._seen
    assert stats.get_stats()["llm_calls"] == calls
    assert len(stats._ended) <= stats._CALLBACK_HISTORY


def test_public_retry_invocation_owns_generations_with_graph_callback_ancestry(monkeypatch):
    import httpx

    from tradingagents.llm_clients import openai_client
    from tradingagents.llm_clients.telemetry import observe_sdk_transport

    monkeypatch.setattr(openai_client, "_TRANSIENT_BACKOFF_SECONDS", (10,))
    monkeypatch.setattr(openai_client, "_wait_or_cancelled", lambda _: False)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={"error": {"message": "private failure"}})
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "private report"}}],
        })

    trace = RunTrace()
    stats = StatsCallbackHandler(trace)
    stage = uuid4()
    # The same handler is configured on the model and inherited from the
    # graph, as in TradingAgentsGraph. Its UUID counters still count once.
    manager = CallbackManager(handlers=[stats], inheritable_handlers=[stats], parent_run_id=stage)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        llm = openai_client.NormalizedChatOpenAI(model="offline-model", api_key="test-key",
            http_client=http, max_retries=0, callbacks=[stats])
        observe_sdk_transport(llm, "openai")
        with use_trace(trace):
            stats.on_chain_start(None, {}, name="Market Analyst", run_id=stage)
            llm.invoke("private prompt", config={"callbacks": manager})
            stats.on_chain_end({}, run_id=stage)
    trace.finish("ok")
    data = trace.snapshot()
    [invocation] = [s for s in data["spans"] if s["kind"] == "llm_invocation"]
    generations = [s for s in data["spans"] if s["kind"] == "llm"]
    attempts = [s for s in data["spans"] if s["kind"] == "transport_attempt"]
    assert len(generations) == len(attempts) == len(requests) == 2
    assert {s["parent_id"] for s in generations} == {invocation["span_id"]}
    assert {s["parent_id"] for s in attempts} == {s["span_id"] for s in generations}
    assert {s["attributes"]["node"] for s in generations} == {"Market Analyst"}
    assert stats.get_stats()["llm_calls"] == 2
    assert data["metrics"]["logical_llm_invocations"] == 1
    assert data["metrics"]["llm_generation_attempts"] == 2
    assert "private" not in json.dumps(data)


@pytest.mark.parametrize("message_reason,generation_reason,expected", [
    ("stop", None, "stop"), ("length", None, "length"), (None, "length", "length"),
])
def test_finish_reason_records_capped_output_without_response_content(
    message_reason, generation_reason, expected,
):
    trace = RunTrace()
    stats = StatsCallbackHandler(trace)
    model = uuid4()
    response = LLMResult(generations=[[ChatGeneration(
        message=AIMessage(content="private output", response_metadata={
            "finish_reason": message_reason, "private_content": "private metadata"}),
        generation_info={"finish_reason": generation_reason},
    )]])
    with use_trace(trace):
        stats.on_chat_model_start({}, [["private prompt"]], run_id=model)
        stats.on_llm_end(response, run_id=model)
    [llm] = [s for s in trace.snapshot()["spans"] if s["kind"] == "llm"]
    assert llm["attributes"]["finish_reason"] == expected
    assert "private" not in json.dumps(trace.snapshot())
