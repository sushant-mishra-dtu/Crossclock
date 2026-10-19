"""C03: deadlines, retries, Stop and truthful run status (docs/adr/0001), offline.

No provider is called. Models are scripted, HTTP goes to mock transports, and
retry waits run on a clock the test moves, so a 60-second backoff takes no time.
"""

from __future__ import annotations

import copy
import json
import threading
import time

import anthropic as anthropic_sdk
import httpx
import httpx2
import openai
import pytest
import typer
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from cli.webui import jobs, server
from tests.run_states import LATER_STAGES, complete_state
from tests.test_cli_memory_log import _FakeGraph, _run_cli
from tests.test_graph_end_to_end import (
    TRADE_DATE,
    ScriptedModel,
    _Client,
    offline,  # noqa: F401 — the fixture, used by name
)
from tradingagents import backtest
from tradingagents.agents import claim_check, debate_judgments, structured
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.completion import IncompleteResearch, run_completion
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.llm_clients import openai_client
from tradingagents.llm_clients.anthropic_client import AnthropicClient
from tradingagents.llm_clients.errors import provider_error_class, retry_after_seconds
from tradingagents.llm_clients.factory import build_llm_kwargs, create_tier_client
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI, OpenAIClient
from tradingagents.llm_clients.telemetry import observe_http_client
from tradingagents.memory import TradingMemoryLog
from tradingagents.reporting import write_partial_report, write_report_tree
from tradingagents.run_control import (
    DeadlineExceeded,
    RunBudget,
    RunCancelled,
    RunControl,
    capped_schedule,
    current_control,
    merge_checks,
    recording_checks,
    use_control,
)

pytestmark = pytest.mark.unit


# --- helpers ----------------------------------------------------------------------

class Clock:
    """A monotonic clock the test moves."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Event:
    """A Stop event whose waits take no real time: each one moves the clock."""

    def __init__(self, clock):
        self.clock, self._set, self.waits = clock, False, []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if not self._set:
            self.clock.now += seconds
        return self._set


def _control(deadline=1200.0, attempt=180.0, backoff=(10, 20, 40, 60, 60), min_attempt=30.0):
    clock = Clock()
    budget = RunBudget("standard", deadline, attempt, tuple(backoff), 120.0, min_attempt)
    return RunControl(budget, Event(clock), clock), clock


def _server_error(headers=None):
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    return openai.InternalServerError("Internal server error",
                                      response=httpx.Response(500, request=request, headers=headers), body=None)


def _script(monkeypatch, outcomes):
    """Replace the model call under the retry loop with ``outcomes``, one per attempt."""
    calls = []

    def fake_invoke(self, input, config=None, **kwargs):
        calls.append(input)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(BaseChatModel, "invoke", fake_invoke)
    return calls


def _chat_completion():
    return {"id": "offline", "object": "chat.completion", "created": 1, "model": "offline-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "offline answer"}}]}


def _nvidia_llm(monkeypatch, respond, **kwargs):
    """The production NVIDIA client, its HTTP answered by ``respond``."""
    monkeypatch.setenv("NVIDIA_API_KEY", "offline-test-key")
    http = httpx2.Client(transport=httpx2.MockTransport(respond))
    return OpenAIClient("nvidia/nemotron-offline", provider="nvidia", timeout=180,
                        http_client=http, **kwargs).get_llm()


def _job_config(tmp_path, **overrides):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), checkpoint_enabled=False)
    config.update(overrides)
    return config


def _run_job(config, model, monkeypatch, analysts=("market",)):
    """An Analyze-page run on the real graph with ``model`` serving every agent."""
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    job = jobs.AnalysisJob("NVDA", TRADE_DATE, "stock", list(analysts), config).start()
    job.wait(60)
    assert not job._thread.is_alive()
    return job


def _node(run_manager):
    return (run_manager.metadata or {}).get("langgraph_node") if run_manager else None


class NodeModel(ScriptedModel):
    """ScriptedModel that fails, answers nothing, or stops the run at one graph node."""

    at: str | None = None
    action: str = "fail"   # fail | empty | stop
    nodes: list = Field(default_factory=list)   # shared across bound copies

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        node = _node(run_manager)
        self.nodes.append(node)
        if node == self.at and self.action == "fail":
            raise _server_error()
        if node == self.at and self.action == "empty":
            self._count()
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
        if node == self.at and self.action == "stop":
            current_control().cancel()   # as if the user pressed Stop while this agent answered
        return super()._generate(messages, stop, run_manager, **kwargs)


# --- budgets and configuration -------------------------------------------------------

def test_the_standard_budget_is_the_approved_one():
    budget = RunBudget.from_config(DEFAULT_CONFIG)
    assert (budget.mode, budget.deadline_seconds, budget.attempt_timeout_seconds) == ("standard", 1200, 180)
    assert budget.retry_backoff_seconds == (10, 20, 40, 60, 60)
    assert (budget.max_retry_after_seconds, budget.min_attempt_seconds) == (120, 30)
    assert DEFAULT_CONFIG["save_partial_reports"] is True


def test_a_budget_override_keeps_the_other_defaults_and_bad_ones_fail_loudly():
    budget = RunBudget.from_config({"run_budgets": {"standard": {"deadline_seconds": 900}}})
    assert budget.deadline_seconds == 900 and budget.attempt_timeout_seconds == 180
    assert RunBudget.from_config({"run_budgets": {"standard": {"deadline_seconds": None}}}).deadline_seconds is None
    with pytest.raises(ValueError, match="research mode"):
        RunBudget.from_config({"research_mode": "quick"})
    with pytest.raises(ValueError, match="positive"):
        RunBudget.from_config({"run_budgets": {"standard": {"attempt_timeout_seconds": 0}}})


@pytest.mark.parametrize("provider", ["openai", "nvidia", "anthropic", "google", "azure"])
def test_every_provider_gets_the_attempt_timeout(provider):
    assert build_llm_kwargs({"llm_provider": provider})["timeout"] == 180


def test_built_clients_have_a_timeout_instead_of_none(monkeypatch):
    """LangChain passed timeout=None, which these SDKs read as no timeout at all."""
    monkeypatch.setenv("NVIDIA_API_KEY", "offline-test-key")
    config = {**DEFAULT_CONFIG, "llm_provider": "nvidia", "quick_think_llm": "nvidia/nemotron-offline"}
    nvidia = create_tier_client(config, "quick").get_llm()
    assert nvidia.root_client.timeout == 180 and nvidia.root_client.max_retries == 0
    anthropic = AnthropicClient("claude-offline", api_key="offline-test-key", timeout=180).get_llm()
    assert anthropic._client.timeout == 180


def test_retry_schedules_are_capped_or_extended():
    assert capped_schedule((10, 20, 40), None) == (10, 20, 40)
    assert capped_schedule((10, 20, 40), 0) == ()
    assert capped_schedule((10, 20, 40), 2) == (10, 20)
    assert capped_schedule((10, 20, 40), 5) == (10, 20, 40, 40, 40)


def test_a_resume_under_a_longer_deadline_keeps_its_checkpoint():
    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.selected_analysts = ("market",)
    graph.config = {**DEFAULT_CONFIG, "max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    before = graph._run_signature("stock")
    graph.config = {**graph.config, "run_budgets": {"standard": {"deadline_seconds": 3600}},
                    "save_partial_reports": False}
    assert graph._run_signature("stock") == before
    graph.config = {**graph.config, "research_mode": "deep"}
    assert graph._run_signature("stock") != before


# --- one retry owner, bounded by the deadline ----------------------------------------

def test_a_retry_that_would_cross_the_deadline_is_not_made(monkeypatch):
    calls = _script(monkeypatch, [_server_error()] * 6)
    control, _ = _control(deadline=35)   # a 10 s wait would leave 25 s, under the 30 s minimum
    llm = NormalizedChatOpenAI(model="nvidia/nemotron-offline", api_key="k")
    with use_control(control), pytest.raises(DeadlineExceeded) as stopped:
        llm.invoke("hi")
    assert len(calls) == 1 and control.cancel_event.waits == []
    assert isinstance(stopped.value.__cause__, openai.InternalServerError)
    assert control.provider_failure == {"provider": "openai", "model": "nvidia/nemotron-offline",
                                        "error_class": "server", "attempts": 1, "waited_seconds": 0.0}
    assert control.interruption == "deadline"


def test_retries_wait_their_schedule_until_the_deadline_leaves_too_little(monkeypatch):
    calls = _script(monkeypatch, [_server_error()] * 6)
    control, _ = _control(deadline=100)
    llm = NormalizedChatOpenAI(model="m", api_key="k")
    with use_control(control), pytest.raises(DeadlineExceeded):
        llm.invoke("hi")
    # 10 s (90 left), 20 s (70 left), 40 s (30 left); a 60 s wait would cross the deadline.
    assert control.cancel_event.waits == [10, 20, 40]
    assert len(calls) == 4
    assert control.provider_failure["attempts"] == 4 and control.provider_failure["waited_seconds"] == 70


def test_a_wait_the_server_asks_for_is_honoured_up_to_its_cap(monkeypatch):
    _script(monkeypatch, [_server_error({"retry-after": "45"}), _server_error({"retry-after": "500"}),
                          AIMessage(content="ok")])
    control, _ = _control()
    with use_control(control):
        assert NormalizedChatOpenAI(model="m", api_key="k").invoke("hi").content == "ok"
    assert control.cancel_event.waits == [45, 120]


def test_llm_max_retries_caps_the_only_retry_layer(monkeypatch):
    sleeps = []
    monkeypatch.setattr(openai_client.time, "sleep", sleeps.append)
    llm = _nvidia_llm(monkeypatch, lambda request: httpx2.Response(200, json=_chat_completion()), max_retries=2)
    assert llm.max_retries == 0   # the SDK makes no retries of its own
    calls = _script(monkeypatch, [_server_error()] * 5)
    with pytest.raises(openai.InternalServerError):
        llm.invoke("hi")
    assert len(calls) == 3 and sleeps == [10, 20]


def test_sdk_attempts_and_waits_stay_within_the_budget(monkeypatch):
    """Through the real SDK and HTTP stack: every attempt passes the run's guard."""
    seen = []

    def respond(request):
        seen.append((request.headers.get("x-stainless-retry-count"), dict(request.extensions["timeout"])))
        return httpx2.Response(500, json={"error": {"message": "busy"}})

    llm = _nvidia_llm(monkeypatch, respond)
    control, clock = _control(deadline=200)
    started = clock.now
    with use_control(control), pytest.raises(DeadlineExceeded):
        llm.invoke("hi")
    assert control.cancel_event.waits == [10, 20, 40, 60]
    assert clock.now - started == 130 <= 200
    assert len(seen) == 5 and {count for count, _ in seen} == {"0"}   # no SDK-internal retry
    # Each attempt's timeout is the 180 s attempt timeout, cut to the time left.
    assert [timeout["read"] for _, timeout in seen] == [180, 180, 170, 130, 70]
    assert all(len(set(timeout.values())) == 1 for _, timeout in seen)   # connect, write and pool too


# --- the guard on every HTTP attempt -------------------------------------------------

@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_no_model_or_jev_request_is_sent_after_stop_or_the_deadline(monkeypatch, stop):
    typesafe = pytest.importorskip("typesafe_sdk")
    sent = []

    def respond(request):
        sent.append(request)
        return httpx2.Response(200, json=_chat_completion())

    nvidia = _nvidia_llm(monkeypatch, respond)
    # The production Anthropic model has the hooks on its SDK client; this SDK client
    # gets the same hooks over a mock transport, so nothing could reach the network.
    assert AnthropicClient("claude-offline", api_key="k").get_llm()._telemetry_transport_coverage == "httpx_hooks"
    anthropic = anthropic_sdk.Anthropic(api_key="offline-test-key",
                                        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
    observe_http_client(anthropic._client)
    jev = typesafe.TypeSafeClient(api_key="offline-test-key", model="jev-offline",
                                  retry=typesafe.RetryPolicy(max_retries=1, backoff_initial=0),
                                  transport=httpx2.MockTransport(respond))
    observe_http_client(jev._http_client)
    control, clock = _control(deadline=60)
    if stop == "cancel":
        control.cancel()
    else:
        clock.now += 61
    expected = RunCancelled if stop == "cancel" else DeadlineExceeded
    message = [{"role": "user", "content": "hi"}]
    with use_control(control):
        # Straight to each SDK, past every check in Python code: the HTTP hook refuses.
        with pytest.raises(expected):
            nvidia.root_client.chat.completions.create(model="nvidia/nemotron-offline", messages=message)
        with pytest.raises(expected):
            anthropic.messages.create(model="claude-offline", max_tokens=5, messages=message)
        with pytest.raises(expected):
            jev.system_one("state", {"q": typesafe.Noul(instructions="q")}, model="jev-offline")
        with pytest.raises(expected):
            nvidia.invoke("hi")
    assert sent == []


def test_a_stop_while_a_request_is_in_flight_sends_nothing_after_it(monkeypatch):
    started, release, sent = threading.Event(), threading.Event(), []

    def respond(request):
        sent.append(request)
        started.set()
        release.wait(5)
        return httpx2.Response(500, json={"error": {"message": "busy"}})

    llm = _nvidia_llm(monkeypatch, respond)
    control = RunControl.from_config(DEFAULT_CONFIG)   # real clock and event
    outcome = {}

    def run():
        try:
            with use_control(control):
                llm.invoke("hi")
        except BaseException as exc:  # noqa: BLE001 — the test reads what the run raised
            outcome["exc"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(5)
    assert control.snapshot()["in_flight"] == 1
    control.cancel()
    stopped = time.monotonic()
    release.set()   # the provider answers (with an error) after the Stop
    worker.join(5)
    assert isinstance(outcome["exc"], RunCancelled)
    assert len(sent) == 1   # no retry after the Stop
    assert time.monotonic() - stopped < 2   # not after the 10 s backoff
    assert control.snapshot()["in_flight"] == 0


# --- fallbacks never swallow a stop or repeat a provider failure ---------------------

class _Jev:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _raise(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


def test_the_claim_check_records_a_failure_and_never_swallows_a_stop(monkeypatch):
    monkeypatch.setattr(claim_check, "jev_client", _Jev)
    decision = "**Rating**: Buy\n\nThe thesis."
    state = {"market_report": "Revenue rose.", "company_of_interest": "NVDA"}
    node = recording_checks(lambda s: {"final_trade_decision": claim_check.check_claims(decision, s)})

    monkeypatch.setattr(claim_check, "run_check", _raise(RuntimeError("Jev is down")))
    assert node(state) == {"final_trade_decision": decision, "run_checks": {"claim_check": "failed"}}
    monkeypatch.setattr(claim_check, "jev_client", lambda: None)
    assert node(state)["run_checks"] == {"claim_check": "unavailable"}

    monkeypatch.setattr(claim_check, "jev_client", _Jev)
    monkeypatch.setattr(claim_check, "run_check", _raise(RunCancelled("stopped")))
    with pytest.raises(RunCancelled):
        node(state)


def test_debate_and_sentiment_judgments_never_swallow_a_stop(monkeypatch):
    from tradingagents.agents import sentiment_judgments

    class Failing(_Jev):
        def __init__(self, exc):
            self.exc = exc

        def system_one(self, *a, **k):
            raise self.exc

    monkeypatch.setattr(debate_judgments, "jev_client", lambda: Failing(RuntimeError("down")))
    node = recording_checks(lambda s: {"score": debate_judgments.new_argument("Bull: a", "Bear: b")})
    assert node({}) == {"score": None, "run_checks": {"debate_judgments": "failed"}}
    monkeypatch.setattr(debate_judgments, "jev_client", lambda: Failing(DeadlineExceeded("late")))
    with pytest.raises(DeadlineExceeded):
        debate_judgments.stronger_side("bull case", "bear case")

    monkeypatch.setattr(sentiment_analyst, "resolve_instrument_identity", lambda ticker: {})
    monkeypatch.setattr(sentiment_judgments, "judge_feeds", _raise(RuntimeError("down")))
    assert sentiment_analyst._judge(_Jev(), {}, "NVDA") is None
    monkeypatch.setattr(sentiment_judgments, "judge_feeds", _raise(RunCancelled("stopped")))
    with pytest.raises(RunCancelled):
        sentiment_analyst._judge(_Jev(), {}, "NVDA")


def test_structured_output_does_not_repeat_a_provider_failure_as_free_text():
    class Fails:
        def __init__(self, exc):
            self.exc = exc

        def invoke(self, prompt):
            raise self.exc

    class Plain:
        calls = 0

        def invoke(self, prompt):
            Plain.calls += 1
            return AIMessage(content="free text")

    with pytest.raises(openai.InternalServerError):
        structured.invoke_structured_or_freetext(Fails(_server_error()), Plain(), "p", str, "Trader")
    assert Plain.calls == 0
    # A malformed answer still falls back, as before.
    assert structured.invoke_structured_or_freetext(Fails(ValueError("bad json")), Plain(), "p", str,
                                                    "Trader") == "free text"


def test_a_backtest_counts_a_timed_out_cell_as_failed_and_ends_on_stop(tmp_path, monkeypatch):
    outcomes = {"2026-01-05": DeadlineExceeded("late"), "2026-01-06": None,
                "2026-01-07": RunCancelled("stopped"), "2026-01-08": None}
    ran, settled = [], []

    class Graph:
        def __init__(self, analysts, config):
            self.memory_log = TradingMemoryLog(config)

        def propagate(self, ticker, date, asset_type="stock", portfolio=None):
            ran.append(date)
            if outcomes[date] is not None:
                raise outcomes[date]

        def settle_pending(self, ticker):
            settled.append(ticker)

    monkeypatch.setattr(backtest, "TradingAgentsGraph", Graph)
    result = backtest.run_backtest(["NVDA"], list(outcomes), {"results_dir": str(tmp_path)})
    assert ran == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert [f[:2] for f in result.failures] == [("NVDA", "2026-01-05")]
    assert result.cells_run == 1 and result.stopped and settled == []


# --- truthful status and retained partial work --------------------------------------

@pytest.mark.usefixtures("offline")
def test_a_provider_failure_mid_run_leaves_a_partial_report_and_no_decision(tmp_path, monkeypatch):
    config = _job_config(tmp_path)
    job = _run_job(config, NodeModel(at="Bear Researcher"), monkeypatch)

    assert job.status == jobs.FAILED and job.rating is None and job.report_path is None
    c = job.completion
    assert (c["state"], c["stop_reason"]) == ("partial", "provider_error")
    assert c["provider_failure"]["error_class"] == "server"
    assert c["stages"]["Market Analyst"] == "done" and c["stages"]["Bull Researcher"] == "done"
    assert "Bear Researcher" in c["missing"] and "Portfolio Manager" in c["missing"]
    assert "provider failure" in job.error and "Internal server error" not in job.error

    partial = job.partial_report_path
    assert partial.name == "partial_report.md" and partial.parent.name.endswith("_partial")
    text = partial.read_text(encoding="utf-8")
    header = text.split("## ", 1)[0]
    assert header.startswith("# Partial Research Report (incomplete): NVDA")
    assert "Run status: PARTIAL. Stopped by a provider failure" in header
    assert not any(line.startswith("- Rating:") for line in header.splitlines())
    assert "### Market Analyst" in text and "### Bull Researcher" in text
    assert json.loads((partial.parent / "run_status.json").read_text())["state"] == "partial"
    assert "Incomplete research" in (partial.parent / "partial_report.html").read_text(encoding="utf-8")

    results = tmp_path / "results"
    assert not list(results.rglob("complete_report.md"))
    assert not list(results.rglob("full_states_log_*.json"))   # the 129BOB importer reads these
    assert TradingMemoryLog(config).load_entries() == []
    [saved] = jobs.list_saved_reports(results)
    assert (saved.kind, saved.ticker, saved.trade_date) == ("partial", "NVDA", TRADE_DATE)
    detail = server.analysis_detail(job)
    assert detail["rating"] is None and detail["partialReportDir"] == str(partial.parent)
    assert detail["completion"]["state"] == "partial" and "provider failure" in detail["completion"]["summary"]


@pytest.mark.usefixtures("offline")
def test_a_stage_that_wrote_nothing_cannot_finish_as_done(tmp_path, monkeypatch):
    config = _job_config(tmp_path)
    job = _run_job(config, NodeModel(at="Bear Researcher", action="empty"), monkeypatch)

    assert job.status == jobs.FAILED
    assert (job.completion["state"], job.completion["stop_reason"]) == ("partial", "missing_output")
    assert job.completion["missing"] == ["Bear Researcher"]
    assert job.error == "Finished without output from Bear Researcher."
    assert TradingMemoryLog(config).load_entries() == []
    assert job.partial_report_path.exists()
    assert not list((tmp_path / "results").rglob("complete_report.md"))


@pytest.mark.usefixtures("offline")
def test_a_claim_check_that_failed_is_named_in_the_report_and_the_api(tmp_path, monkeypatch):
    monkeypatch.setattr(claim_check, "jev_client", _Jev)
    monkeypatch.setattr(claim_check, "run_check", _raise(RuntimeError("Jev is down")))
    job = _run_job(_job_config(tmp_path), ScriptedModel(), monkeypatch)

    assert job.status == jobs.DONE and job.rating == "Overweight"
    assert job.completion["state"] == "complete_with_gaps"
    assert job.completion["checks"]["claim_check"] == "failed"
    header = job.report_path.read_text(encoding="utf-8").split("## ", 1)[0]
    assert "- Run status: complete with gaps:" in header
    assert "Claim check failed to run; the decision's claims are unchecked" in header
    assert "- Rating: Overweight" in header
    assert "Run status" in (job.report_path.parent / "complete_report.html").read_text(encoding="utf-8")
    gaps = server.analysis_detail(job)["completion"]["gaps"]
    assert "Claim check failed to run; the decision's claims are unchecked" in gaps


@pytest.mark.usefixtures("offline")
def test_a_stopped_run_resumes_without_rerunning_its_analysts(tmp_path, monkeypatch):
    config = _job_config(tmp_path, checkpoint_enabled=True)
    first = _run_job(config, NodeModel(at="Bull Researcher", action="stop"), monkeypatch)
    assert first.status == jobs.CANCELLED and first.error is None
    assert first.completion["stop_reason"] == "cancelled"
    assert first.completion["stages"]["Market Analyst"] == "done"
    assert first.partial_report_path.exists()
    assert TradingMemoryLog(config).load_entries() == []

    # A longer deadline is the same run, so its checkpoint resumes.
    longer = {**config, "run_budgets": {"standard": {**config["run_budgets"]["standard"], "deadline_seconds": 1800}}}
    model = NodeModel()
    second = _run_job(longer, model, monkeypatch)

    assert second.status == jobs.DONE and second.rating == "Overweight"
    assert "agent" not in model.nodes   # no analyst model call was repeated
    assert second.completion["stages"]["Market Analyst"] == "reused"
    assert [e["rating"] for e in TradingMemoryLog(config).load_entries()] == ["Overweight"]


@pytest.mark.usefixtures("offline")
def test_traces_record_the_budget_and_stop_without_content(tmp_path, monkeypatch):
    job = _run_job(_job_config(tmp_path), NodeModel(at="Bear Researcher"), monkeypatch)
    trace = json.loads(job.trace_path.read_text(encoding="utf-8"))
    events = {e["name"]: e["attributes"] for e in trace["events"]}
    assert events["run_budget"] == {"mode": "standard", "deadline_seconds": 1200, "attempt_timeout_seconds": 180}
    assert events["run_stopped"]["reason"] == "provider_error"
    assert "persisted_partial_report" in trace["milestones"] and "persisted_report" not in trace["milestones"]
    saved = json.dumps(trace)
    assert "Report." not in saved and "Internal server error" not in saved and "offline-test-key" not in saved


def test_the_cli_saves_a_partial_report_and_exits_when_a_provider_fails(tmp_path, monkeypatch):
    class Failing(_FakeGraph):
        def stream_run(self, graph_input, **kwargs):
            yield [], {"messages": [], "market_report": "M"}
            raise _server_error()

    fake = Failing()
    with pytest.raises(typer.Exit) as stopped:
        _run_cli(monkeypatch, tmp_path, fake)
    assert stopped.value.exit_code == 1
    assert not any(call[0] == "record_decision" for call in fake.calls)
    [partial] = (tmp_path / "results").rglob("partial_report.md")
    assert "Stopped by a provider failure" in partial.read_text(encoding="utf-8")


def test_the_cli_still_raises_an_unclassified_error_after_saving_what_it_had(tmp_path, monkeypatch):
    class Broken(_FakeGraph):
        def stream_run(self, graph_input, **kwargs):
            yield [], {"messages": [], "market_report": "M"}
            raise KeyError("a bug, not a provider failure")

    with pytest.raises(KeyError):
        _run_cli(monkeypatch, tmp_path, Broken())
    assert list((tmp_path / "results").rglob("partial_report.md"))


# --- the completion record and saved artifacts ---------------------------------------

def test_a_debate_turn_with_only_its_label_is_missing():
    state = complete_state(final_trade_decision="**Rating**: Hold")
    assert run_completion(state, ["market"])["state"] == "complete"
    empty_bear = {**LATER_STAGES["investment_debate_state"], "bear_history": "\nBear Analyst: "}
    c = run_completion({**state, "investment_debate_state": empty_bear}, ["market"])
    assert (c["state"], c["stop_reason"], c["missing"]) == ("partial", "missing_output", ["Bear Researcher"])
    assert run_completion({}, ["market"], stop_reason="cancelled")["state"] == "none"


def test_checks_merge_to_the_worst_and_only_failed_or_unavailable_are_gaps():
    assert merge_checks({"debate_judgments": "ran"}, {"debate_judgments": "failed"}) == {"debate_judgments": "failed"}
    assert merge_checks({"debate_judgments": "failed"}, {"debate_judgments": "ran"}) == {"debate_judgments": "failed"}
    state = complete_state(final_trade_decision="x", run_checks={"claim_check": "disabled",
                                                                 "sentiment_judgments": "unavailable"},
                           memory_note="1 past decision(s) could not be settled this run and stay pending.")
    c = run_completion(state, ["market"])
    assert c["state"] == "complete_with_gaps" and c["gaps"] == ["memory", "sentiment_judgments"]


def test_an_incomplete_run_cannot_be_saved_as_a_complete_report(tmp_path):
    partial = run_completion({"market_report": "M"}, ["market"], stop_reason="deadline", deadline_seconds=1200)
    with pytest.raises(ValueError, match="partial"):
        write_report_tree({"market_report": "M"}, "NVDA", tmp_path / "r", completion=partial)
    path = write_partial_report({"market_report": "M", "final_trade_decision": "**Rating**: Buy"},
                                "NVDA", tmp_path / "p", partial, html=False)
    text = path.read_text(encoding="utf-8")
    assert path.name == "partial_report.md" and "Stopped at its 1,200 s deadline" in text
    assert "- Rating:" not in text and not (tmp_path / "p" / "complete_report.md").exists()


def test_propagate_records_no_decision_for_a_run_missing_a_stage(tmp_path):
    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = _job_config(tmp_path)
    graph.selected_analysts = ("market",)
    graph.debug = False
    graph._resuming = False
    graph.propagator = type("P", (), {"get_graph_args": lambda self, callbacks=None: {}})()
    graph.create_run_state = lambda *a, **k: {}
    graph.graph = type("G", (), {"invoke": lambda self, i, **k: {"final_trade_decision": "**Rating**: Buy"}})()
    recorded = []
    graph.record_decision = lambda *a: recorded.append(a)
    graph.clear_checkpoint_on_success = lambda *a: None
    with pytest.raises(IncompleteResearch) as stopped:
        graph._run_graph("NVDA", TRADE_DATE)
    assert recorded == [] and "Market Analyst" in stopped.value.completion["missing"]


@pytest.mark.parametrize("exc, kind", [
    (_server_error(), "server"),
    (openai.RateLimitError("slow", response=httpx.Response(429, request=httpx.Request("POST", "https://x")), body=None),
     "rate_limit"),
    (openai.APITimeoutError(request=httpx.Request("POST", "https://x")), "timeout"),
    (openai.AuthenticationError("no", response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                                body=None), "auth"),
    (httpx.ConnectError("refused"), "connection"),
    (openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                            body=None), None),
    (ValueError("not a provider error"), None),
])
def test_provider_errors_are_classified_without_their_text(exc, kind):
    assert provider_error_class(exc) == kind


def test_retry_after_headers_are_read_in_seconds_or_milliseconds():
    assert retry_after_seconds(_server_error({"retry-after": "7"})) == 7
    assert retry_after_seconds(_server_error({"retry-after-ms": "1500"})) == 1.5
    assert retry_after_seconds(_server_error()) is None
