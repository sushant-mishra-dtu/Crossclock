"""Quick through its real spawned workers (docs/adr/0004 section 6).

The other Quick tests replace the worker with an in-process stand-in. Here the
coordinator starts real worker processes, sources go through the real router,
evidence session and acquisition capture, and the synthesis goes through the
real LangChain/OpenAI SDK to a fake provider on loopback, which counts sends.
The vendor fixture is installed inside each worker, because a spawned process
does not inherit the parent's monkeypatches. No external network is used.
"""
import json
import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tradingagents.quick import bounded_call, run_quick
from tradingagents.quick_sources import acquire

DAY = "2026-10-16"
PRICES = ("Date,Open,High,Low,Close,Volume\n"
          "2026-10-14,99,101,98,100.0,1000\n2026-10-15,100,102,99,101.5,1200\n")
SECRET = "provider-body-must-not-surface"
SUMMARY = {"observations": [{"text": "The latest close was 101.5.", "evidence_ids": ["E2"]}],
           "gaps": [], "questions": ["What explains the latest move?"]}


def _fixture_vendors():
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed, FeedItem

    def prices(symbol, start_date, end_date):
        return PRICES

    def news(ticker, start_date, end_date):
        item = FeedItem("news", "Apple said demand was steady.", title="Apple update",
                        author="Fixture Wire", url="https://example.test/apple",
                        published_at="2026-10-15T09:30:00+00:00")
        return Feed("Apple update: Apple said demand was steady.", (item,))

    router.VENDOR_METHODS["get_stock_data"] = {"yfinance": prices}
    router.VENDOR_METHODS["get_news_feed"] = {"yfinance": news}


def _acquire_with_fixture_vendors(emit, config, ticker, trade_date):
    """Runs inside the spawned worker, in place of the real vendors only."""
    _fixture_vendors()
    return acquire(emit, config, ticker, trade_date)


def _real_workers(target, args, **kwargs):
    return bounded_call(_acquire_with_fixture_vendors if target is acquire else target, args, **kwargs)


class _Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = "ok"
        self.requests = []
        self.on_request = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        server.requests.append((self.path, body))
        if server.on_request:
            server.on_request()
        if server.mode == "fail":
            self._reply(500, {"error": {"message": SECRET, "type": "server_error"}})
        elif server.mode == "hang":
            time.sleep(10)
        else:
            self._reply(200, {
                "id": "chatcmpl-fixture", "object": "chat.completion", "created": 0,
                "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps(SUMMARY)}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            })


@pytest.fixture
def provider(monkeypatch):
    server = _Provider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A test value for the loopback fake only; the spawned workers inherit it.
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-key")
    yield server
    server.shutdown()
    server.server_close()


def _config(tmp_path, server):
    return {"results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
            "llm_provider": "openai", "quick_think_provider": None,
            "backend_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "quick_think_llm": "gpt-4.1-mini"}


def _saved_text(folder):
    return "".join(p.read_text(encoding="utf-8") for p in folder.iterdir()
                   if p.suffix in (".md", ".json", ".html"))


def test_real_workers_save_a_sourced_brief_after_one_provider_send(tmp_path, provider):
    from tradingagents.brief import report_brief

    result = run_quick("AAPL", DAY, config=_config(tmp_path, provider), phase_runner=_real_workers)

    assert result.completion["state"] == "complete_with_gaps", result.completion
    assert [path for path, _ in provider.requests] == ["/v1/chat/completions"]
    body = provider.requests[0][1]
    assert body["model"] == "gpt-4.1-mini"
    assert not body.get("tools")
    assert 0 < (body.get("max_completion_tokens") or body.get("max_tokens")) <= 1200
    assert multiprocessing.active_children() == []

    sources = {e["tool"]: e for e in result.state["quick_sources"]}
    price, news = sources["get_stock_data"], sources["get_news_feed"]
    assert price["coverage"] == "available" and "101.5" in price["text"]
    assert price["acquisitions"][0]["source"] == "yfinance"
    assert price["acquisitions"][0]["fetched_at"]
    assert news["acquisitions"][0]["items"][0]["url"] == "https://example.test/apple"
    assert sources["local_facts"]["coverage"] == "unavailable"

    folder = result.path.parent
    names = {p.name for p in folder.iterdir()}
    assert {"quick_brief.md", "quick_brief.html", "quick_data.json", "evidence.json",
            "run_status.json"} <= names
    assert not names & {"complete_report.md", "partial_report.md"}
    assert not list(tmp_path.rglob("full_states_log_*.json"))

    brief = report_brief(folder, "quick", results_root=tmp_path / "results")
    assert brief["rating"]["final"] is None
    observation, = brief["quick"]["observations"]
    assert observation["evidence_ids"] == ["E2"]
    assert observation["figures"][0]["found"]


def test_provider_failure_is_one_send_and_a_provider_stop_without_its_message(tmp_path, provider):
    provider.mode = "fail"
    result = run_quick("AAPL", DAY, config=_config(tmp_path, provider), phase_runner=_real_workers)

    assert len(provider.requests) == 1  # no SDK or outer-loop retry
    completion = result.completion
    assert completion["state"] == "partial"
    assert completion["stop_reason"] == "provider_error"
    failure = completion["provider_failure"]
    assert failure["error_class"] == "server"
    assert failure["attempts"] == 1
    assert (failure["provider"], failure["model"]) == ("openai", "gpt-4.1-mini")
    assert SECRET not in json.dumps(completion)
    assert SECRET not in _saved_text(result.path.parent)
    assert any(e["coverage"] == "available" for e in result.state["quick_sources"])
    assert multiprocessing.active_children() == []


def test_stop_during_generation_ends_the_worker_after_its_single_send(tmp_path, provider):
    stop = threading.Event()
    provider.mode, provider.on_request = "hang", stop.set

    result = run_quick("AAPL", DAY, config=_config(tmp_path, provider), cancel=stop,
                       phase_runner=_real_workers)

    assert result.completion["stop_reason"] == "cancelled"
    assert result.completion["state"] == "partial"
    assert result.completion["provider_failure"] is None
    assert multiprocessing.active_children() == []
    time.sleep(0.5)
    assert len(provider.requests) == 1
