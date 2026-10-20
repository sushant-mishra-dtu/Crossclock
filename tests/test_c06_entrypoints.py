"""Mode behavior through real CLI, job, queue and saved-report entrypoints."""
from pathlib import Path

import pytest

from tests.test_c06_quick import _fixture_phase


def test_http_quick_needs_no_deep_model_or_specialist_selection():
    from cli.webui import server

    job = server._analysis_jobs({"date": "2026-10-16", "settings": {
        "mode": "quick", "provider": "openai", "quick": "gpt-6-luna"}}, ["AAPL"])[0]
    assert job.config["research_mode"] == "quick"
    assert job.analysts == []
    assert job.config["checkpoint_enabled"] is False
    assert server._selections({"provider": "openai", "quick": "q", "deep": "d"})["research_mode"] == "standard"


@pytest.mark.parametrize("mode", ["deep", "typo"])
def test_http_invalid_mode_is_rejected_before_job_creation(mode):
    from cli.webui import server

    with pytest.raises(server.ApiError, match="mode|Deep"):
        server._analysis_jobs({"date": "2026-10-16", "settings": {"mode": mode}}, ["AAPL"])


def test_quick_job_uses_runner_and_reloads_without_decision_labels(tmp_path, monkeypatch):
    from cli.webui import server
    from cli.webui.jobs import DONE, AnalysisJob
    from tradingagents import quick

    actual = quick.run_quick
    monkeypatch.setattr(quick, "run_quick", lambda *a, **k: actual(*a, **k, phase_runner=_fixture_phase([])))
    job = AnalysisJob("AAPL", "2026-10-16", "stock", [],
                      {"research_mode": "quick", "results_dir": str(tmp_path)})
    job.start().wait(10)
    assert job.status == DONE
    assert job.rating is None
    assert job.completion["decision_eligible"] is False
    assert server.analysis_detail(job)["mode"]["mode"] == "quick"
    monkeypatch.setitem(server.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    saved = server.reports_list()
    assert len(saved) == 1 and saved[0]["kind"] == "quick" and saved[0]["rating"] is None
    detail = server.report_detail(saved[0]["id"])
    assert detail["hasBrief"] and detail["rating"] is None
    brief = server.report_brief(saved[0]["id"])
    assert brief["mode"] == "quick" and brief["rating"]["final"] is None
    assert brief["quick"]["observations"]


def test_queued_configuration_is_frozen_from_caller_mutations():
    from cli.webui.jobs import AnalysisJob

    config = {"research_mode": "quick", "data_vendors": {"news_data": "yfinance"}}
    job = AnalysisJob("AAPL", "2026-10-16", "stock", [], config)
    config["data_vendors"]["news_data"] = "changed"
    assert job.config["data_vendors"]["news_data"] == "yfinance"


def test_quick_headless_skips_specialist_depth_and_deep_model_requirements(monkeypatch):
    from cli.selections import unattended_gaps

    for name, value in {"TRADINGAGENTS_LLM_PROVIDER": "openai", "TRADINGAGENTS_QUICK_THINK_LLM": "q",
                        "TRADINGAGENTS_OUTPUT_LANGUAGE": "English"}.items():
        monkeypatch.setenv(name, value)
    assert unattended_gaps({"mode": "quick", "ticker": "AAPL", "date": "2026-10-16",
                            "save": True, "show": False}) == []


def test_poisoned_quick_state_cannot_enter_standard_writers(tmp_path):
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.reporting import write_report_tree

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"research_mode": "standard"}
    state = {"research_mode": "quick", "decision_eligible": False,
             "final_rating": "BUY", "final_trade_decision": "BUY"}
    with pytest.raises(ValueError, match="Quick|decision"):
        graph.record_decision("AAPL", "2026-10-16", state)
    with pytest.raises(ValueError, match="Quick|decision"):
        graph._log_state("2026-10-16", state)
    with pytest.raises(ValueError, match="Quick|decision"):
        write_report_tree(state, "AAPL", Path(tmp_path) / "bad", completion={"state": "complete"})
    assert not (tmp_path / "bad").exists()
