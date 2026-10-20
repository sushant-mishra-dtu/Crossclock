"""Quick cannot enter decision writers or report unobserved worker usage as zero."""
import json

import pytest


def test_partial_writer_rejects_poisoned_quick_before_creating_files(tmp_path):
    from tradingagents.reporting import write_partial_report

    with pytest.raises(ValueError, match="Quick|decision"):
        write_partial_report({"research_mode": "quick", "final_rating": "BUY"},
                             "AAPL", tmp_path / "bad", {"state": "partial"})
    assert not (tmp_path / "bad").exists()


def test_quick_memory_is_rejected_before_any_read_or_write(tmp_path):
    from tradingagents.memory import TradingMemoryLog

    with pytest.raises(ValueError, match="Standard|standard"):
        TradingMemoryLog({"research_mode": "quick", "memory_log_path": str(tmp_path / "bad" / "memory.md")})
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("mode", ["quick", "deep"])
def test_backtest_job_rejects_nonstandard_before_queueing(mode):
    from cli.webui.jobs import BacktestJob

    with pytest.raises(ValueError, match="Standard|standard|Deep"):
        BacktestJob(["AAPL"], ["2026-10-16"], {"research_mode": mode}, ["market"])


def test_job_snapshot_persisted_telemetry_and_discovery_keep_quick_mode(tmp_path, monkeypatch):
    from cli.webui.jobs import AnalysisJob, list_saved_reports
    from tests.test_c06_quick import _fixture_phase
    from tradingagents import quick

    actual = quick.run_quick
    monkeypatch.setattr(quick, "run_quick", lambda *a, **k: actual(*a, **k, phase_runner=_fixture_phase([])))
    job = AnalysisJob("AAPL", "2026-10-16", "stock", [],
                      {"research_mode": "quick", "results_dir": str(tmp_path)})
    job.start().wait(10)
    assert not job.active
    snapshot = job.snapshot()
    assert snapshot["mode"]["mode"] == "quick"
    assert snapshot["stats"]["tokens_in"] is None
    assert snapshot["stats"]["tokens_out"] is None
    assert snapshot["stats"]["tool_calls"] is None
    trace = json.loads(job.trace_path.read_text(encoding="utf-8"))
    assert trace["metrics"]["observed_http_attempts"] is None
    assert trace["metrics"]["reported_input_tokens"] is None
    assert trace["configuration"]["research_mode"] == "quick"
    report = list_saved_reports(tmp_path)[0]
    assert (report.kind, report.ticker, report.trade_date) == ("quick", "AAPL", "2026-10-16")


def test_queued_quick_removed_without_runner_or_artifacts(tmp_path):
    from cli.webui.jobs import CANCELLED, AnalysisJob

    job = AnalysisJob("AAPL", "2026-10-16", "stock", [],
                      {"research_mode": "quick", "results_dir": str(tmp_path)})
    job.drop("Removed while waiting")
    assert job.status == CANCELLED
    assert job.completion["decision_eligible"] is False
    assert job.rating is None
    assert not list(tmp_path.rglob("quick_brief.md"))


def test_poisoned_checkpoint_is_rejected_before_resuming(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_checkpoint_lifecycle import _bare_graph
    from tradingagents.graph import trading_graph as module

    graph = _bare_graph(str(tmp_path))
    monkeypatch.setattr(module, "checkpoint_step", lambda *a: 1)
    monkeypatch.setattr(graph.workflow, "compile", lambda **k: SimpleNamespace(
        get_state=lambda *a: SimpleNamespace(values={"research_mode": "quick", "final_rating": "BUY"})))
    try:
        with pytest.raises(ValueError, match="Quick|decision"):
            graph.begin_checkpoint("AAPL", "2026-10-16")
    finally:
        graph.end_checkpoint()


def test_standard_report_records_mode_without_inventing_it_for_legacy(tmp_path):
    from tests.test_reporting import _state
    from tradingagents.reporting import write_report_tree

    path = write_report_tree(_state(), "AAPL", tmp_path, settings={"research_mode": "standard"},
                             completion={"state": "complete", "checks": {}, "gaps": [], "missing": []})
    assert json.loads((path.parent / "run_metadata.json").read_text())["research_mode"] == "standard"
    assert json.loads((path.parent / "run_status.json").read_text())["mode"] == "standard"
    assert "Research mode: standard" in path.read_text()


def test_empty_graph_config_validates_the_same_effective_defaults_before_setup(monkeypatch):
    from tradingagents.graph import trading_graph as module

    monkeypatch.setattr(module, "DEFAULT_CONFIG", {**module.DEFAULT_CONFIG, "research_mode": "quick"})
    monkeypatch.setattr(module, "create_tier_client", lambda *a, **k: pytest.fail("initialized a model"))
    with pytest.raises(ValueError, match="Standard"):
        module.TradingAgentsGraph(config={})


def test_queue_view_accepts_partial_standard_budget():
    from cli.webui.jobs import AnalysisJob, AnalysisQueue, JobRegistry

    job = AnalysisJob("AAPL", "2026-10-16", "stock", ["market"], {
        "research_mode": "standard", "run_budgets": {"standard": {"attempt_timeout_seconds": 5}}})
    registry = JobRegistry()
    registry.add(job)
    queue = AnalysisQueue(registry)
    queue._order.append(job.id)
    assert queue.status()["runs"][0]["budgetSeconds"] == 1200


def test_stopped_quick_job_preserves_completed_phase_status(tmp_path, monkeypatch):
    from cli.webui.jobs import AnalysisJob
    from tests.test_c06_quick import _fixture_phase
    from tradingagents import quick
    from tradingagents.run_control import RunCancelled

    fixture = _fixture_phase([])

    def phase(target, args, **kwargs):
        if target.__name__ == "synthesize":
            raise RunCancelled("fixture stop")
        return fixture(target, args, **kwargs)

    actual = quick.run_quick
    monkeypatch.setattr(quick, "run_quick", lambda *a, **kw: actual(*a, **kw, phase_runner=phase))
    job = AnalysisJob("AAPL", "2026-10-16", "stock", [], {"research_mode": "quick", "results_dir": str(tmp_path)})
    job.start().wait(10)
    assert job.snapshot()["agent_status"] == {"Acquisition": "completed", "Synthesis": "stopped", "Checks": "pending"}
