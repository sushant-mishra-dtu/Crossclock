"""Offline contracts for SDK attempts, data outcomes, caches and Jev threads."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import openai
import pytest

from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents import jev
from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import sec_edgar
from tradingagents.llm_clients import openai_client
from tradingagents.llm_clients.telemetry import observe_sdk_transport, transport_call
from tradingagents.telemetry import RunTrace, current_trace, use_trace

pytestmark = pytest.mark.unit


def _spans(trace, kind):
    return [span for span in trace.snapshot()["spans"] if span["kind"] == kind]


def _completion():
    return {"id": "offline", "object": "chat.completion", "created": 1,
            "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "private response text"}}]}


def test_real_sdk_retries_are_http_attempts_under_one_logical_parent(monkeypatch):
    monkeypatch.setattr(openai._base_client.time, "sleep", lambda _: None)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "secret=private-error"}})
        return httpx.Response(200, json=_completion())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        sdk = openai.OpenAI(api_key="private-credential", http_client=http, max_retries=1,
                            base_url="https://offline.example/private-path?token=private-url")
        llm = SimpleNamespace(root_client=sdk, root_async_client=None, model_name="offline-model")
        observe_sdk_transport(llm, "openai")
        observe_sdk_transport(llm, "openai")  # Shared client observers never accumulate.
        trace = RunTrace()
        with use_trace(trace), trace.scope("llm", "logical_call") as parent, transport_call(llm):
            sdk.chat.completions.create(model="offline-model", messages=[{"role": "user", "content": "private prompt text"}])
        trace.finish("done")
    attempts = _spans(trace, "transport_attempt")
    assert len(attempts) == len(calls) == 2
    assert {span["parent_id"] for span in attempts} == {parent}
    assert [span["attributes"]["retry_count"] for span in attempts] == [0, 1]
    assert [span["attributes"]["http_status"] for span in attempts] == [500, 200]
    assert "sdk_retry_wait_unobserved" in trace.snapshot()["telemetry_gaps"]
    saved = json.dumps(trace.snapshot())
    assert all(secret not in saved for secret in ["private-credential", "private-path", "private-url", "private-error", "private prompt text", "private response text"])


def test_connection_failures_keep_observed_send_count_and_unknown_outcomes(monkeypatch):
    monkeypatch.setattr(openai._base_client.time, "sleep", lambda _: None)

    def fail(request):
        raise httpx.ConnectError("credential-bearing-url-private", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        sdk = openai.OpenAI(api_key="test-key", http_client=http, max_retries=1)
        llm = SimpleNamespace(root_client=sdk, root_async_client=None, model="offline-model")
        observe_sdk_transport(llm, "openai")
        trace = RunTrace()
        with use_trace(trace), transport_call(llm), pytest.raises(openai.APIConnectionError):
            sdk.chat.completions.create(model="offline-model", messages=[])
        trace.finish("error")
    assert len(_spans(trace, "transport_attempt")) == 2
    assert {span["status"] for span in _spans(trace, "transport_attempt")} == {"incomplete"}
    assert "httpx_response_missing" in trace.snapshot()["telemetry_gaps"]
    assert "credential-bearing" not in json.dumps(trace.snapshot())


def test_outer_retry_wait_is_separate_from_actual_sdk_requests(monkeypatch):
    monkeypatch.setattr(openai_client, "_TRANSIENT_BACKOFF_SECONDS", (10,))
    waits = []
    monkeypatch.setattr(openai_client, "_wait_or_cancelled", lambda delay: waits.append(delay) or False)
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "offline failure"}}) if len(calls) == 1 else httpx.Response(200, json=_completion())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        llm = openai_client.NormalizedChatOpenAI(model="offline-model", api_key="test-key",
                                                 http_client=http, max_retries=0)
        observe_sdk_transport(llm, "openai")
        trace = RunTrace()
        with use_trace(trace):
            assert llm.invoke("private prompt text", config={"callbacks": [StatsCallbackHandler(trace)]}).content == "private response text"
        trace.finish("done")
    assert waits == [10]
    assert len(_spans(trace, "retry_wait")) == 1
    assert len(_spans(trace, "transport_attempt")) == 2
    assert len(_spans(trace, "llm_invocation")) == 1
    generations = _spans(trace, "llm")
    assert len(generations) == 2
    assert {span["parent_id"] for span in _spans(trace, "transport_attempt")} == {span["span_id"] for span in generations}


@pytest.mark.parametrize("failure,status", [
    (NoMarketDataError("TEST", "TEST", "private no-data detail"), "no_data"),
    (VendorUnavailableError("private service error"), "unavailable"),
])
def test_source_outcomes_and_cache_access_are_distinct(monkeypatch, tmp_path, failure, status):
    monkeypatch.setattr(sec_edgar, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(sec_edgar, "_fetch_json", lambda url: {"value": 42})
    monkeypatch.setattr(router, "get_vendor", lambda *args: "primary,secondary")

    def unavailable():
        raise failure

    monkeypatch.setitem(router.VENDOR_METHODS, "get_balance_sheet", {
        "primary": unavailable,
        "secondary": lambda: sec_edgar._cached_json("https://private.example?token=secret", "facts.json"),
    })
    trace = RunTrace()
    with use_trace(trace):
        assert router.route_to_vendor("get_balance_sheet") == {"value": 42}
        assert router.route_to_vendor("get_balance_sheet") == {"value": 42}
    trace.finish("done")
    sources = _spans(trace, "source")
    assert [span["status"] for span in sources] == [status, "ok", status, "ok"]
    caches = [event for event in trace.snapshot()["events"] if event["name"] == "cache_access"]
    assert [event["attributes"]["cache_status"] for event in caches] == ["miss", "hit"]
    assert [event["span_id"] for event in caches] == [sources[1]["span_id"], sources[3]["span_id"]]
    assert _spans(trace, "transport_attempt") == []  # Mocking a source is not an HTTP observation.
    assert "private" not in json.dumps(trace.snapshot())


def test_shared_http_client_isolates_concurrent_runs():
    barrier = threading.Barrier(2)

    def respond(request):
        barrier.wait(timeout=5)
        return httpx.Response(200, json=_completion())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        sdk = openai.OpenAI(api_key="test-key", http_client=http, max_retries=0)
        llm = SimpleNamespace(root_client=sdk, root_async_client=None, model="offline-model")
        observe_sdk_transport(llm, "openai")

        def run(label):
            trace = RunTrace(run_id=label)
            with use_trace(trace), trace.scope("llm", label) as parent, transport_call(llm):
                sdk.chat.completions.create(model="offline-model", messages=[])
            trace.finish("done")
            return trace, parent

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["first", "second"]))
    for trace, parent in results:
        attempts = _spans(trace, "transport_attempt")
        assert len(attempts) == 1 and attempts[0]["parent_id"] == parent


def test_jev_worker_contexts_preserve_parent_and_isolate_concurrent_runs():
    barrier = threading.Barrier(4)

    class FakeJev:
        def system_one(self, *, state, questions):
            barrier.wait(timeout=5)
            assert current_trace().run_id == state
            return SimpleNamespace(model="jev-offline", usage={"input_tokens": 3, "output_tokens": 1}, answers={})

    def run(label):
        trace = RunTrace(run_id=label)
        with use_trace(trace), trace.scope("node", label) as parent:
            assert len(jev.ask_each(FakeJev(), [label, label], {})) == 2
        trace.finish("done")
        return trace, parent

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["first", "second"]))
    for trace, parent in results:
        calls = _spans(trace, "jev")
        assert len(calls) == 2 and {span["parent_id"] for span in calls} == {parent}
        assert all(span["attributes"]["input_tokens"] == 3 for span in calls)
        assert "jev_transport_attempts_unobserved" in trace.snapshot()["telemetry_gaps"]
        assert _spans(trace, "transport_attempt") == []


def test_unsupported_provider_records_gap_without_fabricated_attempt():
    llm = observe_sdk_transport(SimpleNamespace(model="offline-model"), "google", sdk="google")
    trace = RunTrace()
    with use_trace(trace), transport_call(llm):
        pass
    trace.finish("done")
    assert "google_transport_unsupported" in trace.snapshot()["telemetry_gaps"]
    assert _spans(trace, "transport_attempt") == []


def test_jev_sdk_httpx2_retries_are_observed_under_one_logical_call():
    typesafe = pytest.importorskip("typesafe_sdk")
    httpx2 = pytest.importorskip("httpx2")
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(503, json={"error": {"message": "private service error"}})
        return httpx2.Response(200, json={"model": "jev-offline", "usage": {"input_tokens": 6, "output_tokens": 2},
                                         "answers": {"q": {"type": "noul", "noul": 0.9}}})

    sdk = typesafe.TypeSafeClient(api_key="private-key", model="jev-offline",
                                  retry=typesafe.RetryPolicy(max_retries=1, backoff_initial=0),
                                  transport=httpx2.MockTransport(respond))
    trace = RunTrace()
    with use_trace(trace), jev._ObservedJevClient(sdk) as client:
        client.system_one("private state", {"q": typesafe.Noul(instructions="private question")}, model="jev-offline")
    trace.finish("done")
    calls = _spans(trace, "jev")
    attempts = _spans(trace, "transport_attempt")
    assert len(calls) == 1 and len(attempts) == len(requests) == 2
    assert {span["parent_id"] for span in attempts} == {calls[0]["span_id"]}
    assert [span["attributes"]["http_status"] for span in attempts] == [503, 200]
    assert attempts[1]["attributes"]["retry_count"] == 1
    assert calls[0]["attributes"]["input_tokens"] == 6
    assert "jev_transport_attempts_unobserved" not in trace.snapshot()["telemetry_gaps"]
    assert "private" not in json.dumps(trace.snapshot())
