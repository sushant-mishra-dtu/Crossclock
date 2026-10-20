"""C06 policy boundaries: invalid modes must fail before side effects."""
import copy

import pytest

from tradingagents.default_config import build_default_config
from tradingagents.modes import mode_config, research_mode


def test_legacy_request_is_standard_and_quick_is_a_fixed_independent_policy():
    original = build_default_config()
    before = copy.deepcopy(original)
    assert research_mode({}) == "standard"
    quick = mode_config({**original, "research_mode": "quick", "max_tokens": 9000,
                         "llm_max_retries": 8, "checkpoint_enabled": True})
    assert original == before
    assert quick["research_mode"] == "quick"
    assert quick["max_tokens"] == 1200
    assert quick["llm_max_retries"] == 0
    assert quick["checkpoint_enabled"] is False
    assert quick["run_budgets"]["quick"]["deadline_seconds"] == 60
    assert quick["run_budgets"]["quick"]["retry_backoff_seconds"] == []
    quick["run_budgets"]["standard"]["deadline_seconds"] = 1
    assert original == before


@pytest.mark.parametrize("mode", ["deep", "QUICK", "invalid", False, 9])
def test_unknown_and_unavailable_modes_are_rejected(mode):
    with pytest.raises(ValueError, match="mode|Deep"):
        mode_config({"research_mode": mode})


def test_environment_can_select_mode_but_invalid_values_fail(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RESEARCH_MODE", "quick")
    assert build_default_config()["research_mode"] == "quick"
    monkeypatch.setenv("TRADINGAGENTS_RESEARCH_MODE", "typo")
    with pytest.raises(ValueError, match="mode"):
        build_default_config()


def test_full_graph_rejects_quick_before_initialization(monkeypatch):
    from tradingagents.graph import trading_graph as g

    monkeypatch.setattr(g, "create_tier_client", lambda *a, **k: pytest.fail("constructed full graph"))
    monkeypatch.setattr(g.os, "makedirs", lambda *a, **k: pytest.fail("created a directory"))
    with pytest.raises(ValueError, match="Standard|standard"):
        g.TradingAgentsGraph(config={"research_mode": "quick"})


def test_quick_backtest_rejected_before_directory_or_model_creation(tmp_path):
    from tradingagents.backtest import run_backtest

    folder = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Standard|standard"):
        run_backtest(["AAPL"], ["2026-10-16"], {"research_mode": "quick", "results_dir": str(folder)})
    assert not folder.exists()


def test_quick_preferences_are_validated_and_legacy_depth_is_not_a_mode():
    from cli.prefs import sanitize

    assert sanitize({"research_mode": "quick", "research_depth": 5}, "stock")["research_mode"] == "quick"
    assert "research_mode" not in sanitize({"research_mode": "deep"}, "stock")
    assert "research_mode" not in sanitize({"research_depth": 1}, "stock")


@pytest.mark.parametrize("cap", [100, "100"])
def test_smaller_explicit_or_environment_output_cap_is_preserved(cap):
    assert mode_config({"research_mode": "quick", "max_tokens": cap})["max_tokens"] == 100
