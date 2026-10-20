"""C06 web mode validation, persistence and Quick source access, entirely offline."""
from urllib.parse import quote

import pytest

from cli.webui import server
from tests.test_c06_quick import _fixture_phase
from tests.test_webui_server import base, call  # noqa: F401
from tradingagents.quick import run_quick


def test_mode_rejection_precedes_key_model_and_analyst_validation(monkeypatch):
    def unexpected():
        raise AssertionError("provider validation ran before mode validation")
    monkeypatch.setattr(server, "options", unexpected)
    for mode in ("deep", "bogus"):
        with pytest.raises(server.ApiError, match="mode|Deep"):
            server._analysis_jobs({"date": "2026-10-16", "settings": {"mode": mode}}, ["AAPL"])
        with pytest.raises(server.ApiError, match="mode|Deep"):
            server.start_backtest(None, {"settings": {"mode": mode}})
    with pytest.raises(server.ApiError, match="Standard"):
        server.start_backtest(None, {"settings": {"mode": "quick"}})


def test_http_omitted_mode_is_standard_even_with_quick_preferences(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server, "load_last_run", lambda: {"research_mode": "quick"})
    assert server.options()["defaults"]["mode"] == "quick"
    assert server._selections({"provider": "openai", "quick": "q", "deep": "d"})["research_mode"] == "standard"
    with pytest.raises(server.ApiError, match="both models"):
        server._selections({"provider": "openai", "quick": "q"})


def test_quick_request_uses_fixed_policy_and_no_standard_controls(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server, "save_last_run", lambda value: None)
    [job] = server._analysis_jobs({"date": "2026-10-16", "settings": {
        "mode": "quick", "provider": "openai", "quick": "q", "checkpoint": True}}, ["AAPL"])
    assert job.analysts == []
    assert job.config["research_mode"] == "quick"
    assert job.config["checkpoint_enabled"] is False
    assert job.config["run_budgets"]["quick"]["deadline_seconds"] == 60
    with pytest.raises(server.ApiError, match="resume"):
        server._analysis_jobs({"date": "2026-10-16", "resume": True, "settings": {
            "mode": "quick", "provider": "openai", "quick": "q"}}, ["AAPL"])


def test_quick_saved_api_has_no_rating_and_keeps_verified_sources(base, tmp_path):  # noqa: F811
    root = tmp_path / "results"
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(root)},
                       phase_runner=_fixture_phase([]))
    status, rows = call(base, "/api/reports")
    assert status == 200 and len(rows) == 1
    saved = rows[0]
    assert saved["kind"] == "quick" and saved["rating"] is None
    report_id = quote(saved["id"])
    status, detail = call(base, "/api/report?id=" + report_id)
    assert status == 200 and detail["rating"] is None and detail["hasBrief"]
    assert detail["mode"]["mode"] == "quick"
    status, brief = call(base, "/api/report/brief?id=" + report_id)
    assert status == 200 and brief["mode"] == "quick"
    assert brief["rating"]["final"] is None and brief["quick"]["observations"]
    assert brief["quick"]["qualitativeSupport"] == "not_checked"
    digest = brief["entries"][0]["content_sha256"]
    assert call(base, f"/api/report/source?id={report_id}&sha={digest}")[1]["text"]
    assert call(base, f"/api/report/source?id={report_id}&sha={'f' * 64}")[0] == 404
    assert b"No investment decision" in call(base, "/api/report/download?id=" + report_id)[1]
    assert result.path.name == "quick_brief.md"


def test_quick_checks_only_its_effective_provider_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(server.DEFAULT_CONFIG, "quick_think_provider", "ollama")
    selections = server._selections({"mode": "quick", "provider": "openai", "quick": "local-model"})
    assert selections["research_mode"] == "quick"


def test_browser_environment_mode_precedes_remembered_default(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RESEARCH_MODE", "standard")
    monkeypatch.setattr(server, "load_last_run", lambda: {"research_mode": "quick"})
    options = server.options()
    assert options["modeFromEnv"] is True
    assert options["defaults"]["mode"] == "standard"
