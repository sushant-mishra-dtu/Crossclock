"""Quick enters the CLI before the decision graph or unused model tiers."""

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from cli import main, prefs, prompts, run, selections


def _fail(*args, **kwargs):
    raise AssertionError("A Quick brief entered a Standard-only setup step.")


def _quick_setup(monkeypatch):
    for name in ("TRADINGAGENTS_RESEARCH_MODE", "TRADINGAGENTS_DEEP_THINK_LLM",
                 "TRADINGAGENTS_QUICK_THINK_LLM", "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
                 "TRADINGAGENTS_MAX_RISK_ROUNDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "English")
    monkeypatch.setattr(selections, "fetch_announcements", lambda: [])
    monkeypatch.setattr(selections, "display_announcements", lambda *a: None)
    monkeypatch.setattr(selections, "select_analysts", _fail)
    monkeypatch.setattr(selections, "select_research_depth", _fail)
    monkeypatch.setattr(selections, "select_deep_thinking_agent", _fail)
    monkeypatch.setattr(selections, "parse_analysts", _fail)
    monkeypatch.setattr(selections, "select_shallow_thinking_agent", lambda p, default=None: "quick-model")
    monkeypatch.setitem(selections.DEFAULT_CONFIG, "deep_think_provider", "anthropic")
    checked = []
    monkeypatch.setattr(selections, "ensure_api_key", lambda p: checked.append(p))
    return checked


def test_mode_option_reaches_the_runner(monkeypatch):
    received = {}
    monkeypatch.setattr(main, "run_analysis", lambda **kwargs: received.update(kwargs))
    result = CliRunner().invoke(main.app, ["--mode", "quick", "--ticker", "AAPL"])
    assert result.exit_code == 0, result.output
    assert received["flags"]["mode"] == "quick"


@pytest.mark.parametrize("mode", ["deep", "typo", "", "QUICK"])
def test_invalid_mode_is_refused_before_headless_setup_or_key_checks(monkeypatch, capsys, mode):
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(run, "get_user_selections", _fail)
    with pytest.raises(typer.Exit) as exc:
        run.run_analysis(flags={"mode": mode})
    assert exc.value.exit_code == 1
    output = capsys.readouterr().out
    assert "mode" in output and "No terminal" not in output


def test_quick_has_no_analyst_depth_or_deep_model_gaps(monkeypatch):
    _quick_setup(monkeypatch)
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "quick-model")
    flags = {"mode": "quick", "ticker": "AAPL", "date": "2026-10-16", "save": True, "show": False}
    assert selections.unattended_gaps(flags) == []
    monkeypatch.delenv("TRADINGAGENTS_QUICK_THINK_LLM")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "unused-deep")
    assert selections.unattended_gaps(flags) == ["TRADINGAGENTS_QUICK_THINK_LLM"]


def test_quick_setup_ignores_unused_specialists_and_only_checks_used_provider(monkeypatch):
    checked = _quick_setup(monkeypatch)
    chosen = selections._prompt_selections({}, {"mode": "quick", "ticker": "AAPL",
                                                "date": "2026-10-16", "analysts": "invalid-unused"})
    assert chosen["research_mode"] == "quick"
    assert chosen["analysts"] == []
    assert chosen["quick_think_llm"] == "quick-model"
    assert checked == ["openai"]


def test_quick_with_split_provider_checks_only_the_synthesis_tier(monkeypatch):
    checked = _quick_setup(monkeypatch)
    monkeypatch.setitem(selections.DEFAULT_CONFIG, "quick_think_provider", "google")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "gemini-fixture")
    selections._prompt_selections({}, {"mode": "quick", "ticker": "AAPL", "date": "2026-10-16"})
    assert checked == ["google"]


@pytest.mark.parametrize("explicit,env,remembered,expected", [
    ("standard", "quick", "quick", "standard"),
    (None, "quick", "standard", "quick"),
    (None, None, "quick", "quick"),
    (None, None, "obsolete", "standard"),
    (None, None, None, "standard"),
])
def test_mode_precedence(monkeypatch, explicit, env, remembered, expected):
    monkeypatch.delenv("TRADINGAGENTS_RESEARCH_MODE", raising=False)
    if env is not None:
        monkeypatch.setenv("TRADINGAGENTS_RESEARCH_MODE", env)
    assert selections.resolve_mode({"mode": explicit}, {"research_mode": remembered}) == expected


def test_interactive_mode_offers_the_remembered_choice_and_remembers_the_answer(monkeypatch):
    _quick_setup(monkeypatch)
    offered = []
    monkeypatch.setattr(selections, "select_research_mode", lambda default=None: offered.append(default) or "quick")
    prefs.save_last_run({"research_mode": "quick"})
    selections.get_user_selections({"ticker": "AAPL", "date": "2026-10-16"})
    assert offered == ["quick"]
    assert prefs.load_last_run()["research_mode"] == "quick"


def test_deep_is_visible_but_disabled_in_the_mode_menu(monkeypatch):
    seen = {}
    def select(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(ask=lambda: "quick")
    monkeypatch.setattr(prompts.questionary, "select", select)
    assert prompts.select_research_mode("quick") == "quick"
    assert seen["default"] == "quick"
    deep = next(c for c in seen["choices"] if c.value == "deep")
    assert deep.disabled and "planned" in str(deep.title).lower()


def test_quick_config_needs_no_deep_model_or_depth_and_pins_the_policy():
    config = run._build_run_config({"research_mode": "quick", "quick_think_llm": "quick-model",
                                    "llm_provider": "openai", "backend_url": None}, checkpoint=None)
    assert config["research_mode"] == "quick"
    assert config["checkpoint_enabled"] is False
    assert config["run_budgets"]["quick"]["deadline_seconds"] == 60
    assert config["llm_max_retries"] == 0


def test_quick_rejects_explicit_checkpoint_resume():
    with pytest.raises(ValueError, match="Quick.*resume|Quick.*checkpoint"):
        run._build_run_config({"research_mode": "quick"}, checkpoint=True)


def test_quick_dispatch_precedes_graph_and_prints_the_brief(monkeypatch, tmp_path, capsys):
    from tradingagents import quick

    monkeypatch.setattr(run, "get_user_selections", lambda flags=None: {
        "research_mode": "quick", "ticker": "AAPL", "analysis_date": "2026-10-16",
        "asset_type": "stock", "quick_think_llm": "q", "llm_provider": "openai", "backend_url": None})
    monkeypatch.setattr(run, "TradingAgentsGraph", _fail)
    monkeypatch.setattr(run, "build_analyst_execution_plan", _fail)
    result = SimpleNamespace(state={"quick_sources": [], "quick_summary": None},
                             completion={"state": "complete_with_gaps", "decision_eligible": False},
                             path=tmp_path / "quick_brief.md", settings={})
    received = []
    monkeypatch.setattr(quick, "run_quick", lambda *a, **kw: received.append((a, kw)) or result)
    monkeypatch.setattr(quick, "quick_markdown", lambda *a: "A sourced Quick observation.")
    run.run_analysis(flags={"mode": "quick", "save": True, "show": True, "html": False})
    assert received[0][0] == ("AAPL", "2026-10-16")
    assert received[0][1]["save"] is True and received[0][1]["html"] is False
    output = capsys.readouterr().out
    assert "A sourced Quick observation" in output
    assert "No investment decision" in output
    assert "60" in output and "cost" in output.lower()


def test_cli_quick_partial_is_saved_and_exits_unsuccessfully(monkeypatch, tmp_path, capsys):
    from tradingagents import quick

    monkeypatch.setattr(run, "get_user_selections", lambda flags=None: {
        "research_mode": "quick", "ticker": "AAPL", "analysis_date": "2026-10-16",
        "quick_think_llm": "q", "llm_provider": "openai", "backend_url": None})
    monkeypatch.setattr(run, "TradingAgentsGraph", _fail)
    result = SimpleNamespace(state={}, completion={"state": "partial", "stop_reason": "deadline",
                                                    "detail": "The source reached its deadline."},
                             path=tmp_path / "quick_brief.md", settings={})
    monkeypatch.setattr(quick, "run_quick", lambda *a, **kw: result)
    with pytest.raises(typer.Exit) as exc:
        run.run_analysis(flags={"mode": "quick", "save": True, "show": False})
    assert exc.value.exit_code == 1
    output = capsys.readouterr().out
    assert "partial" in output.lower() and "quick_brief.md" in output


def test_cli_backtest_pins_standard_even_when_default_mode_is_quick(monkeypatch, tmp_path):
    monkeypatch.setitem(main.DEFAULT_CONFIG, "research_mode", "quick")
    received = []
    monkeypatch.setattr(main, "run_backtest", lambda *a, **kw: received.append(a[2]) or SimpleNamespace(
        cells_run=0, skipped=0, log_path=tmp_path, run_id="fixture", failures=[], settlement_failures=[]))
    monkeypatch.setattr(main, "summarize", lambda r, **kw: SimpleNamespace(render=lambda: "fixture"))
    monkeypatch.setattr(main, "record_summary", lambda *a, **kw: None)  # a stub summary, not a trial
    result = CliRunner().invoke(main.app, ["backtest", "AAPL", "--start", "2026-10-15", "--end", "2026-10-16"])
    assert result.exit_code == 0, result.output
    assert received[0]["research_mode"] == "standard"
    assert main.DEFAULT_CONFIG["research_mode"] == "quick"


@pytest.mark.parametrize("save,html", [(True, True), (True, False), (False, False)])
def test_cli_quick_fixture_run_keeps_its_artifact_kind_and_save_controls(monkeypatch, tmp_path, save, html):
    from tests.test_c06_quick import _fixture_phase
    from tradingagents import quick

    monkeypatch.setitem(run.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    monkeypatch.setitem(run.DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(run, "get_user_selections", lambda flags=None: {
        "research_mode": "quick", "ticker": "AAPL", "analysis_date": "2026-10-16",
        "asset_type": "crypto", "quick_think_llm": "fixture", "llm_provider": "openai", "backend_url": None})
    monkeypatch.setattr(run, "TradingAgentsGraph", _fail)
    actual = quick.run_quick
    calls = []
    monkeypatch.setattr(quick, "run_quick", lambda *a, **kw: actual(
        *a, **kw, phase_runner=_fixture_phase(calls)))
    result = run.run_analysis(flags={"mode": "quick", "save": save, "show": False, "html": html})
    assert calls == ["acquire", "synthesize"]
    assert result.completion["decision_eligible"] is False
    assert result.state["asset_type"] == "crypto"
    assert bool(list(tmp_path.rglob("quick_brief.md"))) is save
    assert bool(list(tmp_path.rglob("quick_brief.html"))) is (save and html)
    assert not list(tmp_path.rglob("complete_report*"))
    assert not list(tmp_path.rglob("full_states_log*"))


def test_headless_reload_uses_the_remembered_quick_mode_without_a_mode_prompt(monkeypatch):
    checked = _quick_setup(monkeypatch)
    prefs.save_last_run({"research_mode": "quick"})
    monkeypatch.setattr(selections.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(selections, "select_research_mode", _fail)
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "fixture")
    flags = {"ticker": "AAPL", "date": "2026-10-16", "save": True, "show": False}
    assert selections.unattended_gaps(flags) == []
    chosen = selections.get_user_selections(flags)
    assert chosen["research_mode"] == "quick" and chosen["analysts"] == []
    assert checked == ["openai"]


@pytest.mark.parametrize("mode", ["deep", "typo", 3, ["quick"]])
def test_only_available_mode_preferences_are_stored(mode):
    prefs.save_last_run({"research_mode": mode, "llm_provider": "openai"})
    assert "research_mode" not in prefs.load_last_run()


@pytest.mark.parametrize("show", [True, False])
def test_cli_renders_requested_facts_with_provenance_before_synthesis(monkeypatch, show):
    from tradingagents import quick

    printed = []
    monkeypatch.setattr(run.console, "print", lambda *a, **kw: printed.append((" ".join(map(str, a)), kw)))
    monkeypatch.setattr(run, "get_user_selections", lambda flags=None: {
        "research_mode": "quick", "ticker": "AAPL", "analysis_date": "2026-10-16",
        "quick_think_llm": "fixture", "llm_provider": "openai", "backend_url": None})
    monkeypatch.setattr(quick, "quick_markdown", lambda *a: "Fixture final brief.")

    def fixture_run(*args, on_event, **kwargs):
        on_event({"type": "stage", "stage": "Acquisition"})
        text = "[bold red]Revenue was 100 crore.[/bold red]"
        on_event({"type": "evidence", "tool": "local_facts", "text": text, "coverage": "available",
                  "arguments": {"as_of": "2026-10-16"},
                  "acquisitions": [{"source": "local fixture", "fetched_at": "2026-10-16T10:00:00Z"}]})
        # The assertion happens before the synthesis stage can start.
        seen = [(body, kw) for body, kw in printed if text in body]
        assert bool(seen) is show
        if show:
            assert seen[0][1].get("markup") is False
            provenance = " ".join(body for body, _ in printed)
            for value in ("local_facts", "local fixture", "2026-10-16", "2026-10-16T10:00:00Z"):
                assert value in provenance
        on_event({"type": "stage", "stage": "Synthesis"})
        return SimpleNamespace(state={}, completion={"state": "complete_with_gaps"}, path=None, settings={})

    monkeypatch.setattr(quick, "run_quick", fixture_run)
    run.run_analysis(flags={"mode": "quick", "save": False, "show": show})
