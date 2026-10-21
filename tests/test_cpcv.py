"""Combinatorial purged cross-validation and the probability of backtest overfitting (Phase R, R5).

One walk-forward path gives one score. Combinatorial purged CV (López de Prado,
Advances in Financial Machine Learning, ch. 12) cuts the dev dates into blocks and
tests on every choice of k of them, which gives a distribution of out-of-sample
scores over several backtest paths. A training decision whose outcome window
touches a test block is purged, and so is one starting within an embargo after
it; decisions after a test block may train, unlike in walk-forward. The
probability of backtest overfitting (PBO; Bailey, Borwein, López de Prado and
Zhu, 2014, CSCV) uses the same splits: the share of them where the
configuration that is best in-sample ranks in the bottom half out of sample.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from itertools import combinations
from math import comb

import numpy as np
import pytest

from tests.test_outcome_model import PRICE, QUESTIONS, _dataset, _days, _later, _momentum_dataset
from tradingagents import outcome_model as om


def _blocks(dates, rows, n_groups):
    """Each row's block, worked out here rather than by the code under test."""
    days = sorted({dates[i] for i in rows})
    block_of = {d: g for g, block in enumerate(np.array_split(np.array(days), n_groups)) for d in block}
    return {i: block_of[dates[i]] for i in rows}


def _shift(day, days):
    return (date.fromisoformat(day) + timedelta(days=int(days))).isoformat()


# ---------------------------------------------------------------------------
# The splits
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("n_groups, k_test", [(6, 2), (5, 1), (8, 3)])
def test_every_choice_of_test_blocks_is_one_split(n_groups, k_test):
    dates = [d for d in _days(24) for _ in range(3)]
    rows = np.arange(len(dates))
    blocks = _blocks(dates, rows, n_groups)

    splits = om.combinatorial_purged_splits(dates, [_later(d) for d in dates], rows,
                                            n_groups=n_groups, k_test=k_test)

    assert len(splits) == comb(n_groups, k_test)
    assert [s.groups for s in splits] == list(combinations(range(n_groups), k_test))
    for s in splits:
        assert sorted(s.test) == [i for i in rows if blocks[i] in s.groups]
        assert [int(b) for b in s.test_blocks] == [blocks[i] for i in s.test]
        assert not set(s.train) & set(s.test)
    # Each block is tested C(n - 1, k - 1) times, once on each backtest path.
    tested = [g for s in splits for g in s.groups]
    assert all(tested.count(g) == comb(n_groups - 1, k_test - 1) for g in range(n_groups))


@pytest.mark.unit
@pytest.mark.parametrize("seed", range(20))
def test_no_training_row_overlaps_a_test_block_or_its_embargo(seed):
    rng = np.random.default_rng(seed)
    days = sorted({_shift("2026-01-01", d) for d in rng.choice(400, size=30, replace=False)})
    dates = [d for d in days for _ in range(int(rng.integers(1, 5)))]
    resolved = [_shift(d, rng.integers(1, 31)) for d in dates]  # holding windows of 1 to 30 days
    rows = np.arange(len(dates))
    blocks = _blocks(dates, rows, 6)

    for s in om.combinatorial_purged_splits(dates, resolved, rows, embargo_days=7):
        windows = []
        for g in s.groups:
            members = [i for i in rows if blocks[i] == g]
            windows.append((min(dates[i] for i in members),
                            _shift(max(resolved[i] for i in members), 7)))
        clear = [i for i in rows if blocks[i] not in s.groups
                 and all(dates[i] > hi or resolved[i] < lo for lo, hi in windows)]
        # Every training row is clear of each test block and its embargo, and every clear row trains.
        assert sorted(s.train) == clear


@pytest.mark.unit
def test_decisions_after_a_test_block_train_unlike_walk_forward():
    dates = [d for d in _days(24) for _ in range(3)]
    rows = np.arange(len(dates))
    first_two = next(s for s in om.combinatorial_purged_splits(dates, [_later(d) for d in dates], rows)
                     if s.groups == (0, 1))
    last_test = max(dates[i] for i in first_two.test)
    assert any(dates[i] > last_test for i in first_two.train)


@pytest.mark.unit
def test_the_embargo_keeps_out_a_leak_the_purge_alone_lets_in():
    """A feature equal to the outcome only in the test rows and in the rows inside the
    embargo after each test block: a model that trains on those rows scores the test
    rows almost perfectly; with the embargo they never train, and it scores as chance."""
    dates = [d for d in _days(24) for _ in range(6)]
    resolved = [_later(d) for d in dates]           # each outcome is known a week later
    y = np.random.default_rng(4).integers(0, 2, size=len(dates)).astype(float)
    rows = np.arange(len(dates))
    blocks = _blocks(dates, rows, 6)

    def held_out_losses(embargo_days):
        losses = []
        for s in om.combinatorial_purged_splits(dates, resolved, rows, embargo_days=embargo_days):
            leak = np.full(len(dates), 0.5)
            leak[s.test] = y[s.test]
            for g in s.groups:
                end = max(resolved[i] for i in rows if blocks[i] == g)
                zone = [i for i in rows if end < dates[i] <= _shift(end, 7)]
                leak[zone] = y[zone]
            if not om._trainable(y[s.train]):
                continue
            model = om.fit_logistic(leak[s.train, None], y[s.train], 0.3)
            losses.append(om.log_loss(y[s.test], model.predict(leak[s.test, None])))
        return losses

    without, with_embargo = held_out_losses(0), held_out_losses(7)
    assert sum(loss < 0.1 for loss in without) >= 8       # the leak trains: near-perfect scores
    assert min(with_embargo) > 0.6                         # it never trains: a coin flip


@pytest.mark.unit
def test_each_backtest_path_predicts_every_dev_row_once():
    dates = [d for d in _days(24) for _ in range(3)]
    rows = np.arange(len(dates))
    splits = om.combinatorial_purged_splits(dates, [_later(d) for d in dates], rows)
    # Predict each test row with its split number, to see which split each path takes it from.
    predictions = [np.full(len(s.test), float(n)) for n, s in enumerate(splits)]

    paths = om.backtest_paths(splits, predictions, len(dates))

    assert paths.shape == (comb(5, 1), len(dates))
    assert not np.isnan(paths).any()
    blocks = _blocks(dates, rows, 6)
    for path in paths:
        for g in range(6):
            used = {path[i] for i in rows if blocks[i] == g}
            assert len(used) == 1 and g in splits[int(used.pop())].groups  # one split per block
    # Across the paths, each block is taken from each of the splits that test it.
    for g in range(6):
        taken = sorted(int(path[next(i for i in rows if blocks[i] == g)]) for path in paths)
        assert taken == [n for n, s in enumerate(splits) if g in s.groups]


# ---------------------------------------------------------------------------
# The probability of backtest overfitting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pbo_of_a_hand_matrix():
    # Losses, lower is better; four splits, three configurations. The best in-sample
    # configuration's rank out of sample, 1 = worst and 3 = best, over N + 1 = 4:
    #   split 1: c0 is best in-sample, worst out of sample: 1/4, logit < 0, overfit
    #   split 2: c1 is best in-sample, the middle out of sample: 2/4, logit = 0, overfit
    #   split 3: c2 is best in-sample, best out of sample: 3/4, not overfit
    #   split 4: c0 is best in-sample, tied best out of sample with c1: (1 + 1 + 0.5)/4, not overfit
    in_sample = [[0.1, 0.2, 0.3], [0.3, 0.1, 0.2], [0.2, 0.3, 0.1], [0.1, 0.3, 0.2]]
    out_of_sample = [[0.3, 0.2, 0.1], [0.1, 0.2, 0.3], [0.3, 0.2, 0.1], [0.1, 0.1, 0.3]]
    assert om.probability_of_overfitting(in_sample, out_of_sample) == 0.5


@pytest.mark.unit
def test_pbo_of_noise_is_a_half_and_of_one_real_signal_is_zero():
    rng = np.random.default_rng(0)
    noise_in, noise_out = rng.normal(size=(2000, 10)), rng.normal(size=(2000, 10))
    assert om.probability_of_overfitting(noise_in, noise_out) == pytest.approx(0.5, abs=0.04)

    signal_in, signal_out = noise_in.copy(), noise_out.copy()
    # Configuration 3 has the lowest loss in and out of sample. 10 below, not 5: another
    # standard normal beats a 5-lower one with p ~ 2e-4 per pair, which 2,000 splits do find.
    signal_in[:, 3] -= 10
    signal_out[:, 3] -= 10
    assert om.probability_of_overfitting(signal_in, signal_out) == 0.0


@pytest.mark.unit
def test_pbo_needs_two_configurations():
    with pytest.raises(ValueError):
        om.probability_of_overfitting([[0.1], [0.2]], [[0.1], [0.2]])


def _noise_columns(n_days=30, per_day=6, n_columns=8, seed=1, signal=False):
    rng = np.random.default_rng(seed)
    dates = [d for d in _days(n_days) for _ in range(per_day)]
    X = rng.normal(size=(len(dates), n_columns))
    score = X[:, 0] * 3 if signal else 0
    y = (rng.uniform(size=len(dates)) < 1 / (1 + np.exp(-score))).astype(float)
    columns = [f"c{i}" for i in range(n_columns)]
    rows = [dict(zip(columns, map(float, x), strict=True)) for x in X]
    return om.Dataset.from_rows(rows, columns, np.where(y > 0, 0.01, -0.01), dates,
                                [_later(d) for d in dates], [f"T{i}" for i in range(len(dates))])


def _pbo_of_single_columns(ds):
    rows = np.arange(len(ds.y))
    splits = [s for s in om.combinatorial_purged_splits(ds.dates, ds.resolved, rows)
              if om._trainable(ds.y[s.train])]
    fits = [om.cpcv_fit(ds, splits, [c], 1.0) for c in range(len(ds.columns))]
    return om.probability_of_overfitting(np.column_stack([f.in_sample for f in fits]),
                                         np.column_stack([f.out_of_sample for f in fits]))


@pytest.mark.unit
def test_pbo_over_fitted_noise_configurations_is_no_better_than_chance():
    """Eight configurations of equal size, each one noise column. Choosing among them in
    sample is no better than chance: PBO is at least about 0.5.

    It is not near 0.5 as for fixed strategies (the matrix test above). With fitted models
    the configuration that fits its training rows best is the one that learned the most
    noise, which costs it out of sample, so its rank there is lower than chance. Over seeds
    0-199 single seeds ranged 0.13-1.00 (mean 0.75); the mean of each run of 10 seeds was
    0.57-0.83, so the test takes the mean of seeds 0-9 (0.75).
    """
    pbo = [_pbo_of_single_columns(_noise_columns(seed=seed)) for seed in range(10)]
    assert np.mean(pbo) >= 0.5


@pytest.mark.unit
def test_pbo_with_one_configuration_carrying_the_signal_is_near_zero():
    assert _pbo_of_single_columns(_noise_columns(signal=True)) <= 0.1


# ---------------------------------------------------------------------------
# In evaluate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evaluate_reports_each_models_paths_and_the_pbo_of_its_grid():
    ds = _dataset()
    ev = om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))

    assert (ev.cpcv.n_groups, ev.cpcv.k_test, ev.cpcv.embargo_days) == (6, 2, 7)
    assert ev.cpcv.splits == 15 and ev.cpcv.paths == 5
    assert 0 < ev.cpcv.used <= 15
    assert ev.cpcv.configurations == 1 + 5 + 5   # base rate, and two models over the L2 grid
    for m in ev.models:
        median, low, high = m.paths_loss
        assert low <= median <= high
        assert m.paths_brier[1] <= m.paths_brier[0] <= m.paths_brier[2]
    full, base = ev.model("rating + Jev features"), ev.model("base rate")
    assert full.paths_loss[2] < base.paths_loss[1]   # the signal wins on every path
    assert ev.cpcv.pbo <= 0.2                         # and the in-sample choice holds up

    text = " ".join(ev.render().split())
    assert "Combinatorial purged CV" in text
    for m in ev.models:
        median, low, high = m.paths_loss
        assert f"- {m.name}: log loss {median:.3f} [{low:.3f} to {high:.3f}]" in text
    assert f"Probability of backtest overfitting: {ev.cpcv.pbo:.2f}" in text


@pytest.mark.unit
def test_a_models_path_scores_are_its_chosen_l2s_paths_at_the_10th_50th_and_90th_percentile():
    ds = _dataset()
    ev = om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))
    dev, _, _ = om.holdout_split(ds.dates, ds.resolved, 0.25)
    splits = [s for s in om.combinatorial_purged_splits(ds.dates, ds.resolved, dev) if om._trainable(ds.y[s.train])]
    full = ev.model("rating + Jev features")
    cols = ds.indices(["rating", "review", "signal", "signal_sd", "noise", "flat"])

    def path_scores(l2):
        losses, briers = [], []
        for path in om.backtest_paths(splits, om.cpcv_fit(ds, splits, cols, l2).predictions, len(ds.y)):
            scored = ~np.isnan(path)
            losses.append(om.log_loss(ds.y[scored], path[scored]))
            briers.append(om.brier(ds.y[scored], path[scored]))
        return losses, briers

    losses, briers = path_scores(full.l2)
    assert full.paths_loss == pytest.approx(tuple(np.percentile(losses, [50, 10, 90])))
    assert full.paths_brier == pytest.approx(tuple(np.percentile(briers, [50, 10, 90])))
    assert path_scores(30.0)[0] != pytest.approx(losses)  # another L2 would give other figures


@pytest.mark.unit
def test_splits_with_too_few_training_decisions_are_left_out_and_counted():
    # Three decisions a date: purging two blocks' windows leaves some splits under 10 training rows.
    rng = np.random.default_rng(6)
    dates = [d for d in _days(16) for _ in range(3)]
    x = rng.normal(size=len(dates))
    rows = [{"rating": 1.0, "review": 0.0, "signal": float(v)} for v in x]
    ds = om.Dataset.from_rows(rows, ["rating", "review", "signal"], np.where(x + rng.normal(size=len(x)) > 0, 0.01, -0.01),
                              dates, [_later(d) for d in dates], [f"T{i}" for i in range(len(dates))])
    dev, _, _ = om.holdout_split(ds.dates, ds.resolved, 0.25)
    trainable = sum(om._trainable(ds.y[s.train]) for s in om.combinatorial_purged_splits(ds.dates, ds.resolved, dev))

    ev = om.evaluate(ds, {"signal": ("signal",)}, baseline=("rating", "review"))

    assert 0 < trainable < 15
    assert ev.cpcv.used == trainable
    assert f"{trainable} of them with enough training decisions" in " ".join(ev.render().split())


@pytest.mark.unit
def test_on_noise_the_in_sample_best_configuration_overfits():
    """No column carries the outcome: the configuration that fits the training
    decisions best is the most flexible one, and it does worse than most out of sample."""
    rng = np.random.default_rng(2)
    dates = [d for d in _days(24) for _ in range(6)]
    n = len(dates)
    rows = [{"rating": float(rng.choice([-1.0, 1.0])), "review": 0.0,
             **{f"q{i}": float(rng.normal()) for i in range(6)}} for _ in range(n)]
    ds = om.Dataset.from_rows(rows, ["rating", "review", *(f"q{i}" for i in range(6))],
                              rng.normal(scale=0.01, size=n), dates, [_later(d) for d in dates],
                              [f"T{i}" for i in range(n)])

    ev = om.evaluate(ds, {f"q{i}": (f"q{i}",) for i in range(6)}, baseline=("rating", "review"))

    assert ev.cpcv.pbo >= 0.6


@pytest.mark.unit
def test_too_few_dev_dates_skip_the_combinatorial_cv_and_say_so():
    dates = [d for d in _days(7) for _ in range(8)]        # 5 dev dates after holding out 2
    rng = np.random.default_rng(3)
    x = rng.normal(size=len(dates))
    rows = [{"rating": 1.0, "review": 0.0, "signal": float(v)} for v in x]
    ds = om.Dataset.from_rows(rows, ["rating", "review", "signal"], np.where(x > 0, 0.01, -0.01),
                              dates, [_shift(d, 1) for d in dates], [f"T{i}" for i in range(len(dates))])

    ev = om.evaluate(ds, {"signal": ("signal",)}, baseline=("rating", "review"))

    assert ev.cpcv is None and all(m.paths_loss is None for m in ev.models)
    assert "needs at least 6 dev dates" in " ".join(ev.render().split())


# Pinned on feat/r4-price-baseline 634b494, before the combinatorial CV.
_R4_SCORES = {
    "base rate": (None, 0.7166296625668721, 0.6809806601408864, 0.26159421335302335, 0.24391836768289024),
    "rating": (0.3, 0.7166296625668721, 0.6809806601408864, 0.26159421335302335, 0.24391836768289024),
    "price only": (0.3, 0.3588482872462197, 0.24164448281385767, 0.12189446762689614, 0.0660192190460075),
    "rating + price": (0.3, 0.35884828724621964, 0.2416444828138577, 0.12189446762689614, 0.06601921904600752),
    "rating + Jev features": (3.0, 0.7122492941280503, 0.6671713184606383, 0.25944073287827385, 0.23709617837901642),
}


@pytest.mark.unit
def test_the_walk_forward_holdout_and_price_model_scores_are_unchanged():
    ev = om.evaluate(_momentum_dataset(), {"noise": ("noise",)}, baseline=("rating", "review"), price=PRICE)

    assert [m.name for m in ev.models] == list(_R4_SCORES)
    for m in ev.models:
        l2, cv, held, cv_brier, held_brier = _R4_SCORES[m.name]
        assert m.l2 == l2
        assert (m.cv_loss, m.holdout_loss, m.cv_brier, m.holdout_brier) == pytest.approx(
            (cv, held, cv_brier, held_brier), abs=1e-12)
    assert [(q.id, q.gain) for q in ev.questions] == [("noise", pytest.approx(0.004380368438821836, abs=1e-12))]


@pytest.mark.unit
def test_the_price_models_are_in_the_pbo_grid_and_score_their_paths():
    ev = om.evaluate(_momentum_dataset(), {"noise": ("noise",)}, baseline=("rating", "review"), price=PRICE)

    assert ev.cpcv.configurations == 1 + 4 * 5   # base rate, then four models over the L2 grid
    price, base = ev.model("price only"), ev.model("base rate")
    assert price.paths_loss[2] < base.paths_loss[1]  # momentum carries the outcome on every path


# ---------------------------------------------------------------------------
# The learn command
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_learn_command_prints_the_paths_and_records_the_cv_settings(tmp_path, monkeypatch, jev_config):
    from typer.testing import CliRunner

    import cli.main as m
    from tests.test_jev_report_features import FakeJev, _grid, _write_run
    from tradingagents import report_features as rf

    _write_run(tmp_path / "backtest" / "sweep1", _grid())
    monkeypatch.setitem(m.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    monkeypatch.setattr(rf, "jev_client", lambda: FakeJev())
    monkeypatch.setattr(rf, "load_bars", lambda ticker, through: [])

    result = CliRunner().invoke(m.app, ["learn", "sweep1"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Combinatorial purged CV" in text and "Probability of backtest overfitting" in text
    [entry] = [json.loads(line) for line in (tmp_path / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entry["config"]["cpcv"] == {"groups": 6, "test_groups": 2, "embargo_days": 7}


@pytest.fixture
def jev_config(tmp_path):
    from tradingagents.dataflows.config import set_config
    set_config({"data_cache_dir": str(tmp_path / "cache"), "jev_model": "jev-test-pinned"})
