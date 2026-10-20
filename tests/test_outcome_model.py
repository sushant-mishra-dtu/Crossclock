"""The fit 5 model: a logistic signal scored on chronological, purged splits.

Decisions overlap in time, so a split that trains on an outcome not yet known
at the test date would flatter the model. These pin the splits, the fit, and
the evaluation's verdicts on synthetic data with a known answer.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tradingagents import outcome_model as om


def _days(n, start=date(2026, 1, 5), step=7):
    return [(start + timedelta(days=step * i)).isoformat() for i in range(n)]


def _later(day, days=7):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_last_dates_are_held_out_and_training_stops_at_what_was_known():
    days = _days(8)
    dates = [d for d in days for _ in range(3)]
    resolved = [_later(d) for d in dates]  # each outcome is known a week later

    dev, train, test = om.holdout_split(dates, resolved, 0.25)

    assert {dates[i] for i in test} == set(days[-2:])
    assert {dates[i] for i in dev} == set(days[:-2])
    # The week before the holdout settled on its first day, so it cannot train.
    assert {dates[i] for i in train} == set(days[:-3])


@pytest.mark.unit
def test_walk_forward_folds_train_only_on_outcomes_known_before_the_test_block():
    days = _days(10)
    dates = [d for d in days for _ in range(2)]
    resolved = [_later(d, 10) for d in dates]
    rows = np.arange(len(dates))

    folds = om.walk_forward(dates, resolved, rows, n_folds=4)

    assert len(folds) == 4
    tested = []
    for train, test in folds:
        first = min(dates[i] for i in test)
        assert all(dates[i] < first and resolved[i] < first for i in train)
        tested += [dates[i] for i in test]
    assert sorted(set(tested)) == days[2:]  # the first block only ever trains
    assert len(folds[-1][0]) > len(folds[0][0])  # the window expands


@pytest.mark.unit
def test_a_holdout_outside_zero_and_one_is_rejected():
    with pytest.raises(ValueError, match="between 0 and 1"):
        om.holdout_split(_days(10), _days(10), 1.0)


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_fit_finds_the_direction_of_a_predictor_and_ignores_a_missing_value():
    rng = np.random.default_rng(0)
    x = rng.normal(size=200)
    y = (x + rng.normal(scale=0.5, size=200) > 0).astype(float)
    X = np.column_stack([x, np.full(200, 3.0)])  # a constant column must not break it

    model = om.fit_logistic(X, y, l2=1.0)

    assert model.weights[1] > 1.0
    p = model.predict(np.array([[2.0, 3.0], [-2.0, 3.0], [np.nan, 3.0]]))
    assert p[0] > 0.9 and p[1] < 0.1
    # A missing value is the training mean, which here is near zero.
    assert abs(p[2] - model.predict(np.array([[x.mean(), 3.0]]))[0]) < 1e-9


@pytest.mark.unit
def test_with_no_columns_the_model_is_the_base_rate():
    y = np.array([1.0] * 30 + [0.0] * 10)
    p = om.fit_logistic(np.empty((40, 0)), y, l2=1.0).predict(np.empty((5, 0)))
    assert np.allclose(p, 0.75, atol=1e-3)


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


def _dataset(n_days=16, per_day=4, seed=1):
    """Decisions where one question predicts the outcome and the rating does not."""
    rng = np.random.default_rng(seed)
    dates = [d for d in _days(n_days) for _ in range(per_day)]
    n = len(dates)
    signal = rng.uniform(0, 4, size=n)
    alpha = (signal - 2) / 50 + rng.normal(scale=0.01, size=n)
    rows = [{"rating": 1.0, "review": 0.0, "signal": s, "signal_sd": 0.3,
             "noise": float(rng.uniform()), "flat": 0.5}
            for s in signal]
    columns = ["rating", "review", "signal", "signal_sd", "noise", "flat"]
    return om.Dataset.from_rows(
        rows, columns, alpha, dates, [_later(d) for d in dates],
        [f"T{i} {d} Buy" for i, d in enumerate(dates)],
    )


QUESTIONS = {"signal": ("signal", "signal_sd"), "noise": ("noise",), "flat": ("flat",)}


@pytest.mark.unit
def test_an_informative_question_beats_the_rating_on_the_held_out_dates():
    ev = om.evaluate(_dataset(), QUESTIONS, baseline=("rating", "review"))

    base, rating, full = (ev.model(n) for n in ("base rate", "rating", "rating + Jev features"))
    assert full.holdout_loss < rating.holdout_loss - 0.1
    assert full.holdout_loss < base.holdout_loss - 0.1
    assert full.cv_loss < rating.cv_loss
    # Where the model favours the stock, the stock did better.
    assert full.favoured[0] > full.rest[0]
    assert rating.favoured is None  # every decision is a Buy, so the rating favours none
    assert ev.holdout_dates == (_days(16)[-4], _days(16)[-1])


@pytest.mark.unit
def test_the_question_that_carries_the_signal_ranks_first_and_a_constant_one_is_flat():
    ev = om.evaluate(_dataset(), QUESTIONS, baseline=("rating", "review"))

    by_id = {q.id: q for q in ev.questions}
    assert ev.questions[0].id == "signal"
    assert by_id["signal"].gain > 0.05
    assert by_id["flat"].flat and not by_id["signal"].flat
    assert by_id["flat"].gain == pytest.approx(0.0, abs=1e-6)


@pytest.mark.unit
def test_the_worst_predicted_decisions_are_listed_for_proposing_new_questions():
    ev = om.evaluate(_dataset(), QUESTIONS, baseline=("rating", "review"))

    assert len(ev.worst) == om.WORST_SHOWN
    label, p, alpha = ev.worst[0]
    assert label.startswith("T")
    assert (p > 0.5) != (alpha > 0)  # the worst miss is a wrong call

    text = ev.render()
    assert "rating + Jev features" in text
    assert "signal: +" in text and "(keep)" in text
    assert "flat: drop" in text
    assert "Worst-predicted dev decisions" in text


@pytest.mark.unit
def test_too_few_decisions_is_refused_before_any_fit():
    ds = _dataset(n_days=5, per_day=4)
    with pytest.raises(om.NotEnoughData, match="at least 40 settled decisions"):
        om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))


@pytest.mark.unit
def test_a_dev_period_with_one_outcome_only_cannot_be_cross_validated():
    ds = _dataset()
    ds.y[:] = 1.0
    with pytest.raises(om.NotEnoughData, match="cross-validate"):
        om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))


# ---------------------------------------------------------------------------
# Calibration: Brier score and reliability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_brier_score_is_the_mean_squared_error_of_the_probability():
    # (0.8 - 1)^2 = 0.04 and (0.3 - 0)^2 = 0.09, so the mean is 0.13 / 2 = 0.065.
    assert om.brier([1, 0], [0.8, 0.3]) == pytest.approx(0.065)
    y = np.array([1.0, 0.0, 0.0, 1.0, 1.0])
    assert om.brier(y, np.full(5, 0.5)) == pytest.approx(0.25)  # a coin flip
    assert om.brier(y, y) == 0.0


@pytest.mark.unit
def test_reliability_bins_the_probabilities_and_skips_empty_bins():
    p = [0.9, 0.05, 0.35, 1.0, 0.2, 0.6, 0.15, 0.85]
    y = [0, 0, 0, 1, 1, 1, 0, 1]

    rows = om.reliability(y, p, bins=5)

    # Bins of width 0.2, each closed on the left; the last also holds p = 1.
    # [0, 0.2):   0.05, 0.15            -> mean 0.10,   none beat
    # [0.2, 0.4): 0.2 (on the edge), 0.35 -> mean 0.275,  1 of 2
    # [0.4, 0.6): empty, left out
    # [0.6, 0.8): 0.6 (on the edge)     -> mean 0.6,    1 of 1
    # [0.8, 1]:   0.9, 1.0, 0.85        -> mean 2.75/3, 2 of 3
    expected = [(0.0, 0.2, 2, 0.10, 0.0), (0.2, 0.4, 2, 0.275, 0.5),
                (0.6, 0.8, 1, 0.6, 1.0), (0.8, 1.0, 3, 2.75 / 3, 2 / 3)]
    assert len(rows) == len(expected)
    for row, want in zip(rows, expected, strict=True):
        assert row[:3] == want[:3]
        assert row[3:] == pytest.approx(want[3:])

    # Two bins: [0, 0.5) holds 0.05, 0.15, 0.2, 0.35 (1 of 4 beat), [0.5, 1] the rest (3 of 4).
    low, high = om.reliability(y, p, bins=2)
    assert low == pytest.approx((0.0, 0.5, 4, 0.1875, 0.25))
    assert high == pytest.approx((0.5, 1.0, 4, 0.8375, 0.75))


@pytest.mark.unit
def test_the_log_losses_are_unchanged_by_the_calibration_scores():
    # Pinned on main 4ab64a3, before the Brier score and reliability were added.
    ev = om.evaluate(_dataset(), QUESTIONS, baseline=("rating", "review"))

    pinned = {
        "base rate": (None, 0.6869211491470799, 0.6884955712837247),
        "rating": (0.3, 0.6869211491470799, 0.6884955712837247),
        "rating + Jev features": (0.3, 0.3238875982474984, 0.07068757806909279),
    }
    assert [m.name for m in ev.models] == list(pinned)
    for m in ev.models:
        l2, cv, held = pinned[m.name]
        assert m.l2 == l2
        assert m.cv_loss == pytest.approx(cv, abs=1e-9)
        assert m.holdout_loss == pytest.approx(held, abs=1e-9)
    gains = {q.id: q.gain for q in ev.questions}
    assert gains == pytest.approx({"signal": 0.36716775355597014,
                                   "noise": 0.005643795155139686, "flat": 0.0}, abs=1e-9)


def _folds_and_split(ds):
    dev, train, test = om.holdout_split(ds.dates, ds.resolved, 0.25)
    folds = [(tr, te) for tr, te in om.walk_forward(ds.dates, ds.resolved, dev)
             if om._trainable(ds.y[tr])]
    return folds, train, test


@pytest.mark.unit
def test_the_base_rate_scores_the_brier_of_its_training_share():
    ds = _dataset()
    ev = om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))
    folds, train, test = _folds_and_split(ds)

    # With no columns, each fold predicts the share of its training rows that beat,
    # and the held-out model the share of all training rows (the base rate).
    oof = np.full(len(ds.y), np.nan)
    for tr, te in folds:
        oof[te] = ds.y[tr].mean()
    scored = ~np.isnan(oof)
    base = ev.model("base rate")
    assert base.cv_brier == pytest.approx(np.mean((oof[scored] - ds.y[scored]) ** 2), abs=1e-6)
    assert base.holdout_brier == pytest.approx(np.mean((ev.base_rate - ds.y[test]) ** 2), abs=1e-6)


@pytest.mark.unit
def test_the_brier_scores_and_reliability_come_from_the_predictions_the_log_losses_score():
    ds = _dataset()
    ev = om.evaluate(ds, QUESTIONS, baseline=("rating", "review"))
    folds, train, test = _folds_and_split(ds)
    full = ev.model("rating + Jev features")
    cols = ds.indices(["rating", "review", "signal", "signal_sd", "noise", "flat"])

    cv, oof = om.cross_validate(ds, cols, folds, full.l2)
    scored = ~np.isnan(oof)
    assert full.cv_loss == pytest.approx(cv)
    assert full.cv_brier == pytest.approx(om.brier(ds.y[scored], oof[scored]))

    p = om.fit_logistic(ds.X[np.ix_(train, cols)], ds.y[train], full.l2).predict(
        ds.X[np.ix_(test, cols)])
    assert full.holdout_loss == pytest.approx(om.log_loss(ds.y[test], p))
    assert full.holdout_brier == pytest.approx(om.brier(ds.y[test], p))
    assert len(ev.reliability) == len(om.reliability(ds.y[test], p))
    for row, want in zip(ev.reliability, om.reliability(ds.y[test], p), strict=True):
        assert row == pytest.approx(want)
    assert sum(n for _, _, n, _, _ in ev.reliability) == ev.holdout_rows
    # The signal separates the outcomes, so the full model beats the base rate on both scores.
    assert full.holdout_brier < ev.model("base rate").holdout_brier - 0.1


def _calibrated_dataset(n_days=40, per_day=25, seed=3):
    """Outcomes drawn with a known probability of one column, so the fit is calibrated."""
    rng = np.random.default_rng(seed)
    dates = [d for d in _days(n_days) for _ in range(per_day)]
    x = rng.normal(size=len(dates))
    beat = rng.uniform(size=len(dates)) < 1 / (1 + np.exp(-1.5 * x))
    rows = [{"rating": 1.0, "review": 0.0, "signal": float(v)} for v in x]
    return om.Dataset.from_rows(
        rows, ["rating", "review", "signal"], np.where(beat, 0.01, -0.01), dates,
        [_later(d) for d in dates], [f"T{i} {d} Buy" for i, d in enumerate(dates)],
    )


@pytest.mark.unit
def test_a_model_of_outcomes_drawn_with_its_probability_lies_near_the_diagonal():
    ev = om.evaluate(_calibrated_dataset(), {"signal": ("signal",)}, baseline=("rating", "review"))

    assert ev.holdout_rows == 250
    assert sum(n for _, _, n, _, _ in ev.reliability) == 250
    assert len(ev.reliability) >= 4
    for lo, hi, n, mean_p, beat in ev.reliability:
        assert lo <= mean_p <= hi
        if n >= 20:  # about 0.1 standard error at most; a loose tolerance
            assert abs(beat - mean_p) < 0.15, (lo, hi, n, mean_p, beat)


@pytest.mark.unit
def test_the_render_gives_the_brier_scores_and_a_reliability_table():
    ev = om.evaluate(_dataset(), QUESTIONS, baseline=("rating", "review"))

    text = " ".join(ev.render().split())
    assert "Brier" in text and "0.250 is a coin flip" in text and "0.693 is a coin flip" in text
    for m in ev.models:
        assert (f"{m.name}: log loss dev CV {m.cv_loss:.3f}, held out {m.holdout_loss:.3f}; "
                f"Brier dev CV {m.cv_brier:.3f}, held out {m.holdout_brier:.3f}") in text
    assert "Reliability of rating + Jev features on the held-out dates" in text
    assert "coarse" in text
    for lo, hi, n, mean_p, beat in ev.reliability:
        assert f"- P {lo:.1f} to {hi:.1f}: {n} decisions, mean P {mean_p:.2f}, {beat:.0%} beat" in text
