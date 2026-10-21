"""Quick's untrusted worker, source freshness and provider-send boundaries."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from tradingagents.modes import require_decision_state
from tradingagents.quick import run_quick, validate_summary
from tradingagents.quick_sources import acquire, synthesize


def summary(text="Revenue was 100 crore."):
    return {"observations": [{"text": text, "evidence_ids": ["E1"]}], "gaps": [], "questions": []}


@pytest.mark.parametrize("text", ["BUY", "SELL.", "HOLD", "REVIEW", "Strong Buy", "Recommendation: SELL",
                                  "Action = HOLD", "I recommend buying.", "Overweight", "Bullish",
                                  "The final rating is REVIEW.", "My recommendation is REVIEW."])
def test_standalone_ratings_are_rejected_in_every_generated_field(text):
    for field in ("observations", "gaps", "questions"):
        result = summary(text if field == "observations" else "Revenue was 100 crore.")
        if field != "observations":
            result[field] = [text]
        assert not validate_summary(result, {"E1": "Revenue was 100 crore."})["valid"]


@pytest.mark.parametrize("mode", ["quick", "deep", "other", None, False])
def test_unknown_explicit_modes_cannot_cross_decision_writer_boundary(mode):
    with pytest.raises(ValueError):
        require_decision_state({"research_mode": mode})


def _phase(target, args, *, on_event=None, **kwargs):
    if target.__name__ == "acquire":
        on_event({"type": "evidence", "tool": "local_facts", "text": "Revenue was 100 crore.",
                  "coverage": "available", "arguments": {"as_of": "2026-10-16"}, "acquisitions": []})
        return None
    return {"summary": summary(), "usage": {}}


def test_unobserved_worker_usage_and_cost_remain_unknown(tmp_path):
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)}, phase_runner=_phase)
    assert result.state["usage"] is None
    assert result.state["cost"] is None
    assert result.state["generation_sends"] is None
    assert result.state["generation_attempts"] == 1


def test_no_save_and_no_html_are_honored(tmp_path):
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path / "absent")},
                       phase_runner=_phase, save=False)
    assert result.path is None
    assert not (tmp_path / "absent").exists()
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)},
                       phase_runner=_phase, html=False)
    assert result.path.exists()
    assert not result.path.with_suffix(".html").exists()


def test_keyboard_interrupt_keeps_obtained_sources(tmp_path):
    def interrupted(target, args, **kwargs):
        _phase(target, args, **kwargs)
        raise KeyboardInterrupt

    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)}, phase_runner=interrupted)
    assert result.path.exists()
    assert result.completion["state"] == "partial"
    assert result.completion["stop_reason"] == "cancelled"


def test_real_acquisition_scope_routes_only_one_price_and_news_window(monkeypatch):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed, FeedItem
    from tradingagents.dataflows.vendors.india import store

    monkeypatch.setattr(store, "open_existing", lambda *a: None)
    calls = []

    def routed(tool, ticker, start, end):
        calls.append((tool, ticker, start, end))
        if tool == "get_stock_data":
            return "Date,Close\n2026-10-16,100\n"
        return Feed("unused", tuple(FeedItem("news", "body" * 2000, title=f"Item {i}",
                    published_at=f"2026-10-{16 - i:02}T12:00:00+00:00") for i in range(12)))

    monkeypatch.setattr(router, "route_to_vendor", routed)
    events = []
    acquire(events.append, {"results_dir": "unused", "data_cache_dir": "unused"}, "AAPL", "2026-10-16")
    assert calls == [("get_stock_data", "AAPL", "2026-09-16", "2026-10-16"),
                     ("get_news_feed", "AAPL", "2026-10-09", "2026-10-16")]
    assert len(events) == 3
    assert events[0]["coverage"] == "unavailable"
    assert len(events[2]["text"]) <= 24000
    assert "Item 8" not in events[2]["text"]
    assert "truncated" in events[2]["text"].lower()


@pytest.mark.parametrize("csv", ["Date,Close\n2026-09-20,90\n", "Date,Close\n2026-10-17,100\n",
                                  "Date,Close\nnot-dated,100\n"])
def test_stale_future_or_undated_prices_are_withheld(monkeypatch, csv):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed
    from tradingagents.dataflows.vendors.india import store

    monkeypatch.setattr(store, "open_existing", lambda *a: None)
    monkeypatch.setattr(router, "route_to_vendor", lambda tool, *a: csv if tool == "get_stock_data" else Feed(""))
    events = []
    acquire(events.append, {}, "AAPL", "2026-10-16")
    assert events[1]["coverage"] != "available"
    assert "100" not in events[1]["text"]


def test_a_cited_closing_price_traces_to_its_price_row(monkeypatch):
    # The figure checks read "99,101.5" in a CSV row as one thousands-grouped
    # number, so no price from a comma-separated window could ever be traced.
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed
    from tradingagents.dataflows.vendors.india import store

    monkeypatch.setattr(store, "open_existing", lambda *a: None)
    csv = "Date,Open,High,Low,Close,Volume\n2026-10-14,99,101,98,100.0,1000\n2026-10-15,100,102,99,101.5,1200\n"
    monkeypatch.setattr(router, "route_to_vendor", lambda tool, *a: csv if tool == "get_stock_data" else Feed(""))
    events = []
    acquire(events.append, {}, "AAPL", "2026-10-16")
    prices = events[1]
    assert prices["coverage"] == "available"
    assert "Close" in prices["text"] and "1200" in prices["text"]
    cited = {"observations": [{"text": "The latest close was 101.5.", "evidence_ids": ["E2"]}],
             "gaps": [], "questions": []}
    checked = validate_summary(cited, {"E2": prices["text"]})
    assert checked["valid"]
    assert checked["observations"][0]["figures"][0]["found"]
    assert checked["gaps"] == []


def test_generation_attempt_cap_is_a_provider_timeout(tmp_path):
    def capped(target, args, **kwargs):
        if target.__name__ == "synthesize":
            raise TimeoutError("Quick phase reached its time limit.")
        return _phase(target, args, **kwargs)

    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)}, phase_runner=capped)
    completion = result.completion
    assert completion["state"] == "partial"
    assert completion["stop_reason"] == "provider_error"
    assert completion["provider_failure"]["error_class"] == "timeout"
    assert completion["provider_failure"]["attempts"] == 1
    assert "provider failure" in completion["detail"]


def test_generation_cut_by_the_run_budget_is_the_deadline(tmp_path):
    import time

    from tradingagents.modes import mode_config
    from tradingagents.run_control import RunControl

    control = RunControl.from_config(mode_config({"research_mode": "quick"}))
    control.deadline = time.monotonic() + 5.3  # leaves 0.3 s before the 5 s reserve

    def slow(target, args, *, timeout, **kwargs):
        if target.__name__ == "synthesize":
            time.sleep(timeout + 0.05)
            raise TimeoutError("Quick phase reached its time limit.")
        return _phase(target, args, **kwargs)

    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)}, control=control,
                       phase_runner=slow)
    assert result.completion["stop_reason"] == "deadline"
    assert result.completion["provider_failure"] is None


@pytest.mark.parametrize("provider,sdk_attempts", [("google", 1), ("openai", 0), ("anthropic", 0), ("azure", 0)])
def test_synthesis_requests_no_sdk_retries_and_bounded_tokens(monkeypatch, provider, sdk_attempts):
    from tradingagents import llm_clients

    calls = []

    def create(config, tier, **kwargs):
        calls.append((config, tier, kwargs))
        return SimpleNamespace(get_llm=lambda: SimpleNamespace(invoke=lambda messages:
            SimpleNamespace(content=json.dumps(summary()), usage_metadata=None)))

    monkeypatch.setattr(llm_clients, "create_tier_client", create)
    result = synthesize(lambda e: None, {"llm_provider": provider, "max_tokens": 9000,
                        "llm_max_retries": 9}, "AAPL", "2026-10-16", "[E1]\nRevenue was 100 crore.")
    assert len(calls) == 1
    config, tier, kwargs = calls[0]
    assert tier == "quick"
    assert config["max_tokens"] == 1200
    assert config["llm_max_retries"] == 0
    assert kwargs["max_retries"] == sdk_attempts
    assert result["usage"] is None


def test_oversized_model_text_is_rejected_before_parsing_or_storage(monkeypatch):
    from tradingagents import llm_clients

    monkeypatch.setattr(llm_clients, "create_tier_client", lambda *a, **k:
        SimpleNamespace(get_llm=lambda: SimpleNamespace(invoke=lambda messages:
            SimpleNamespace(content=" " * 30000, usage_metadata=None))))
    with pytest.raises(ValueError, match="output|large|limit"):
        synthesize(lambda e: None, {}, "AAPL", "2026-10-16", "[E1]\n100")


def test_local_financials_require_eligible_filing_dates(monkeypatch):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed
    from tradingagents.dataflows.vendors.india import statements, store

    row = {"isin": "test"}
    conn = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: row), close=lambda: None)
    monkeypatch.setattr(store, "open_existing", lambda *a: conn)
    monkeypatch.setattr(router, "route_to_vendor", lambda tool, *a: Feed("") if tool == "get_news_feed" else "")
    facts = SimpleNamespace(empty=False, basis="consolidated", quarterly={"2026-06-30": {"sales": 100}},
        annual={}, balance={}, cashflow={}, filings=[{"filed_at": "unknown", "period_end": "2026-06-30"}])
    monkeypatch.setattr(statements, "load_statements", lambda *a, **k: facts)
    events = []
    acquire(events.append, {}, "TEST.NS", date(2026, 10, 16).isoformat())
    assert events[0]["coverage"] != "available"
    assert "100" not in events[0]["text"]


def test_azure_constructor_receives_output_cap_and_zero_retries(monkeypatch):
    from tradingagents.llm_clients import azure_client

    captured = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(azure_client, "NormalizedAzureChatOpenAI", FakeChat)
    azure_client.AzureOpenAIClient("m", max_tokens=600, max_retries=0).get_llm()
    assert captured["max_tokens"] == 600
    assert captured["max_retries"] == 0


def test_bedrock_zero_retries_reaches_botocore_configuration(monkeypatch):
    import sys

    from tradingagents.llm_clients import bedrock_client

    captured = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(bedrock_client, "_bedrock_class", lambda: FakeChat)
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kw: SimpleNamespace(**kw)))
    bedrock_client.BedrockClient("m", max_tokens=1200, max_retries=0, timeout=40).get_llm()
    assert captured["config"].retries == {"total_max_attempts": 1, "mode": "standard"}
    assert captured["config"].read_timeout == 40
    assert captured["max_tokens"] == 1200


def test_only_evidence_in_the_bounded_packet_can_be_cited(tmp_path):
    packets = []

    def phase(target, args, *, on_event=None, **kwargs):
        if target.__name__ == "acquire":
            for _number in (1, 2):
                on_event({"type": "evidence", "tool": "local_facts", "text": "x" * 24000,
                          "coverage": "available", "arguments": {}, "acquisitions": []})
            return None
        packets.append(args[-1])
        invalid = summary()
        invalid["observations"][0]["evidence_ids"] = ["E2"]
        return {"summary": invalid}

    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)}, phase_runner=phase)
    assert len(packets[0]) <= 24000
    assert "truncated" in packets[0]
    assert "[E2]" not in packets[0]
    assert result.completion["state"] == "partial"
    assert result.state["quick_summary"] is None


def test_direct_synthesis_rejects_large_input_before_client_construction(monkeypatch):
    from tradingagents import llm_clients

    monkeypatch.setattr(llm_clients, "create_tier_client", lambda *a, **k: pytest.fail("constructed a client"))
    with pytest.raises(ValueError, match="input limit"):
        synthesize(lambda e: None, {}, "AAPL", "2026-10-16", "x" * 24001)


def test_crypto_asset_kind_survives_quick_output(tmp_path):
    result = run_quick("BTC-USD", "2026-10-16", config={"results_dir": str(tmp_path)},
                       phase_runner=_phase, asset_type="crypto")
    assert result.state["asset_type"] == "crypto"
    with pytest.raises(ValueError, match="asset_type"):
        run_quick("BTC-USD", "2026-10-16", config={"results_dir": str(tmp_path)}, asset_type="unknown")
