"""The trial ledger: every backtest summary and learn evaluation, counted.

A result picked as the best of many configurations looks better than it is, so
each one is appended to ``<results_dir>/trials.jsonl`` with a hash of its
configuration and the window of data it scored. The deflated Sharpe ratio takes
its N from the ledger: the distinct configurations tried on overlapping data.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.trials import (
    TrialCount,
    config_hash,
    count_trials,
    ledger_path,
    question_set_version,
    record_trial,
)

JAN = ("2026-01-05", "2026-02-02")


@pytest.mark.unit
def test_the_ledger_is_one_file_in_the_results_directory(tmp_path):
    assert ledger_path(tmp_path) == tmp_path / "trials.jsonl"
    record_trial(tmp_path / "results", "backtest", {"cost_bps": 30}, JAN)
    assert (tmp_path / "results" / "trials.jsonl").is_file()


@pytest.mark.unit
def test_a_line_holds_the_kind_the_configuration_its_hash_and_the_window(tmp_path):
    record_trial(tmp_path, "backtest", {"cost_bps": 30}, JAN, run_id="sweep1", periods=6, sharpe=0.25)

    [line] = ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert entry["kind"] == "backtest"
    assert entry["config"] == {"cost_bps": 30}
    assert entry["config_hash"] == config_hash({"cost_bps": 30})
    assert entry["window"] == list(JAN)
    assert (entry["run_id"], entry["periods"], entry["sharpe"]) == ("sweep1", 6, 0.25)
    assert entry["time"]


@pytest.mark.unit
def test_two_runs_of_one_configuration_are_one_configuration_and_two_runs(tmp_path):
    config = {"models": {"deep_think_llm": "m1"}, "cost_bps": 30}
    assert record_trial(tmp_path, "backtest", config, JAN) == TrialCount(configurations=1, runs=1)
    # The same settings in another order are the same configuration.
    again = dict(reversed(list(config.items())))
    assert record_trial(tmp_path, "backtest", again, JAN) == TrialCount(configurations=1, runs=2)


@pytest.mark.unit
def test_a_changed_question_set_is_another_configuration(tmp_path):
    from tradingagents import report_features as rf

    base = {"holdout": 0.25, "l2_grid": [0.3, 1.0, 3.0, 10.0, 30.0]}
    full = {**base, "question_set": question_set_version(rf.QUESTIONS)}
    record_trial(tmp_path, "learn", full, JAN)
    assert record_trial(tmp_path, "learn", full, JAN) == TrialCount(1, 2)

    fewer = {**base, "question_set": question_set_version(rf.QUESTIONS[:-1])}
    assert record_trial(tmp_path, "learn", fewer, JAN) == TrialCount(2, 3)


@pytest.mark.unit
def test_a_reworded_question_is_a_new_version_of_the_set():
    from tradingagents import report_features as rf

    overbought = next(q for q in rf.QUESTIONS if q.id == "overbought")
    reworded = rf.FeatureQuestion("overbought", "market", rf._noul(
        "Is `instrument` overbought, as `report` tells it?",
        true=overbought.question.criteria["true"], false=overbought.question.criteria["false"]))
    swapped = tuple(reworded if q.id == "overbought" else q for q in rf.QUESTIONS)

    assert question_set_version(rf.QUESTIONS) == question_set_version(tuple(rf.QUESTIONS))
    assert question_set_version(swapped) != question_set_version(rf.QUESTIONS)


@pytest.mark.unit
def test_the_hash_follows_the_settings_not_their_order():
    assert config_hash({"a": 1, "b": [1, 2]}) == config_hash({"b": [1, 2], "a": 1})
    assert config_hash({"a": 1, "b": [1, 2]}) != config_hash({"a": 1, "b": [2, 1]})
    assert config_hash({"cost_bps": None}) != config_hash({"cost_bps": 0})
    assert config_hash({"cost_bps": 30}) != config_hash({"cost_bps": 12})


@pytest.mark.unit
def test_only_trials_of_the_same_kind_on_overlapping_data_are_counted(tmp_path):
    record_trial(tmp_path, "backtest", {"a": 1}, ("2026-01-05", "2026-02-02"))
    record_trial(tmp_path, "backtest", {"a": 2}, ("2026-03-02", "2026-04-06"))  # later, apart
    record_trial(tmp_path, "learn", {"a": 3}, ("2026-01-05", "2026-04-06"))     # another kind
    record_trial(tmp_path, "backtest", {"a": 4}, ("2026-02-02", "2026-02-23"))  # shares 2026-02-02

    assert count_trials(tmp_path, "backtest", ("2026-01-19", "2026-02-09")) == TrialCount(2, 2)
    assert count_trials(tmp_path, "backtest", ("2026-02-02", "2026-02-02")) == TrialCount(2, 2)
    assert count_trials(tmp_path, "backtest", ("2026-02-24", "2026-03-01")) == TrialCount(0, 0)
    assert count_trials(tmp_path, "backtest", ("2025-01-01", "2026-12-31")) == TrialCount(3, 3)
    assert count_trials(tmp_path, "learn", ("2026-04-06", "2026-05-04")) == TrialCount(1, 1)


@pytest.mark.unit
def test_a_run_with_no_scored_data_is_logged_but_overlaps_nothing(tmp_path):
    record_trial(tmp_path, "backtest", {"a": 1}, None)

    assert len(ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()) == 1
    assert count_trials(tmp_path, "backtest", ("2025-01-01", "2026-12-31")) == TrialCount(0, 0)


@pytest.mark.unit
def test_no_ledger_yet_counts_nothing(tmp_path):
    assert count_trials(tmp_path, "backtest", JAN) == TrialCount(0, 0)
    assert not ledger_path(tmp_path).exists()


@pytest.mark.unit
def test_an_unreadable_line_is_skipped_rather_than_fatal(tmp_path):
    ledger_path(tmp_path).write_text('{"kind": "backtest", "conf\nnot json at all\n\n[1, 2]\n',
                                     encoding="utf-8")

    assert record_trial(tmp_path, "backtest", {"a": 1}, JAN) == TrialCount(1, 1)
