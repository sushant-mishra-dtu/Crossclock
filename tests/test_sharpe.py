"""The probabilistic and deflated Sharpe ratios (Bailey and López de Prado).

PSR (2012, "The Sharpe Ratio Efficient Frontier"): the probability that the true
Sharpe ratio is above a benchmark, given the estimate, the number of periods T
and the skew and kurtosis of the returns:

    PSR(SR*) = Phi((SR - SR*) * sqrt(T - 1) / sqrt(1 - skew * SR + (kurtosis - 1) / 4 * SR^2))

DSR (2014, "The Deflated Sharpe Ratio", Journal of Portfolio Management 40(5)):
PSR against the Sharpe ratio the best of N trials with no skill is expected to
reach, sqrt(V) * ((1 - g) * z(1 - 1/N) + g * z(1 - 1/(N e))), g Euler's constant.

Kurtosis here is not excess kurtosis: 3 for normal returns.
"""

from __future__ import annotations

import math
from itertools import pairwise
from statistics import NormalDist

import pytest

from tradingagents.sharpe import (
    EULER_GAMMA,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    sharpe_stats,
)

PHI = NormalDist().cdf


@pytest.mark.unit
def test_the_moments_of_a_hand_series():
    # r = 0, 0, 0, 0.04: mean 0.01; deviations -0.01 (three times) and +0.03.
    # Each moment divides by T = 4:
    #   m2 = (3 * 1e-4 + 9e-4) / 4 = 3e-4,   sd = sqrt(3e-4) = 0.01 * sqrt(3)
    #   m3 = (3 * -1e-6 + 27e-6) / 4 = 6e-6, skew = 6e-6 / (3e-4)^1.5 = 2 / sqrt(3)
    #   m4 = (3 * 1e-8 + 81e-8) / 4 = 21e-8, kurtosis = 21e-8 / (3e-4)^2 = 7 / 3
    #   Sharpe = 0.01 / (0.01 * sqrt(3)) = 1 / sqrt(3)
    stats = sharpe_stats([0.0, 0.0, 0.0, 0.04])
    assert stats.periods == 4
    assert stats.sharpe == pytest.approx(1 / math.sqrt(3))
    assert stats.skew == pytest.approx(2 / math.sqrt(3))
    assert stats.kurtosis == pytest.approx(7 / 3)


@pytest.mark.unit
def test_the_probabilistic_sharpe_ratio_of_the_hand_series():
    # SR = 1/sqrt(3), skew = 2/sqrt(3), kurtosis = 7/3, T = 4 (above):
    #   1 - skew * SR + (kurtosis - 1) / 4 * SR^2 = 1 - 2/3 + (4/3) / 4 * (1/3) = 4/9
    #   z = SR * sqrt(T - 1) / sqrt(4/9) = (1/sqrt(3)) * sqrt(3) * 3/2 = 1.5
    #   PSR(0) = Phi(1.5) = 0.93319 (normal table)
    stats = sharpe_stats([0.0, 0.0, 0.0, 0.04])
    psr = probabilistic_sharpe(stats.sharpe, stats.periods, stats.skew, stats.kurtosis)
    assert psr == pytest.approx(PHI(1.5))
    assert psr == pytest.approx(0.93319, abs=1e-5)


@pytest.mark.unit
def test_a_benchmark_above_zero_lowers_the_probability():
    # Normal returns (skew 0, kurtosis 3), SR 0.25 over T = 37:
    #   1 + (3 - 1) / 4 * 0.0625 = 1.03125
    #   PSR(0)   = Phi(0.25 * 6 / sqrt(1.03125)) = Phi(1.47710)
    #   PSR(0.1) = Phi(0.15 * 6 / sqrt(1.03125)) = Phi(0.88626)
    assert probabilistic_sharpe(0.25, 37, 0.0, 3.0) == pytest.approx(PHI(1.5 / math.sqrt(1.03125)))
    assert probabilistic_sharpe(0.25, 37, 0.0, 3.0, benchmark=0.1) == pytest.approx(
        PHI(0.9 / math.sqrt(1.03125)))


@pytest.mark.unit
def test_the_papers_worked_example():
    """Bailey and López de Prado (2014), the paper's numerical example: an annualised Sharpe
    ratio of 2.5 from five years of daily returns (T = 1,250, skew -3, kurtosis 10),
    the best of N = 100 trials whose Sharpe ratios have an annualised variance of 1/2.
    The paper finds SR0 = 0.1132 and DSR = 0.9004, below 0.95, so it is rejected.

    Per day: SR = 2.5 / sqrt(250) = 0.15811, V = 0.5 / 250 = 0.002.
      SR0 = sqrt(0.002) * ((1 - g) * z(0.99) + g * z(1 - 1/(100 e)))
          = 0.044721 * (0.42278 * 2.32635 + 0.57722 * 2.68021) = 0.044721 * 2.53060 = 0.11317
      1 - (-3) * 0.15811 + (10 - 1) / 4 * 0.15811^2 = 1 + 0.47434 + 0.05625 = 1.53059
      DSR = Phi((0.15811 - 0.11317) * sqrt(1249) / sqrt(1.53059)) = Phi(1.28385) = 0.9004
    """
    sr, variance = 2.5 / math.sqrt(250), 0.5 / 250
    assert expected_max_sharpe(100, variance) == pytest.approx(0.1132, abs=5e-5)
    dsr = deflated_sharpe(sr, 1250, -3.0, 10.0, trials=100, variance=variance)
    assert dsr == pytest.approx(0.9004, abs=5e-5)


@pytest.mark.unit
def test_the_expected_best_of_n_trials_without_skill():
    # One trial: its Sharpe ratio is expected to be 0. More trials: the formula above,
    # e.g. N = 10 with V = 1: (1 - g) * z(0.9) + g * z(1 - 1/(10 e))
    #   = 0.42278 * 1.28155 + 0.57722 * 1.78924 = 1.57460
    assert math.isclose(EULER_GAMMA, 0.5772156649, abs_tol=1e-10)
    assert expected_max_sharpe(1, 0.04) == 0.0
    assert expected_max_sharpe(10, 1.0) == pytest.approx(1.57460, abs=1e-5)
    assert expected_max_sharpe(10, 0.25) == pytest.approx(0.5 * 1.57460, abs=1e-5)  # scales with sqrt(V)


@pytest.mark.unit
@pytest.mark.parametrize("sharpe, periods, skew, kurtosis", [
    (0.3, 20, 0.0, 3.0),
    (2.5 / math.sqrt(250), 1250, -3.0, 10.0),
    (-0.2, 12, 0.5, 4.0),
])
def test_with_one_trial_the_deflated_ratio_is_the_probabilistic_one_against_zero(sharpe, periods, skew, kurtosis):
    assert deflated_sharpe(sharpe, periods, skew, kurtosis, trials=1) == pytest.approx(
        probabilistic_sharpe(sharpe, periods, skew, kurtosis, benchmark=0.0))


@pytest.mark.unit
def test_the_deflated_ratio_falls_as_the_number_of_trials_rises():
    values = [deflated_sharpe(0.4, 30, -0.5, 4.0, trials=n) for n in (1, 2, 3, 5, 10, 100, 1000)]
    assert all(later < earlier for earlier, later in pairwise(values))


@pytest.mark.unit
def test_without_a_variance_the_trials_are_chance_alone():
    """With no V given, each trial's Sharpe ratio is taken to vary as an estimate of
    a true Sharpe ratio of 0 does over T periods: V = 1 / (T - 1). Then
    DSR = Phi((SR * sqrt(T - 1) - E[max of N standard normals]) / sqrt(1 - skew SR + (k - 1)/4 SR^2)).

    SR 0.5, T 26, normal returns, N 10: Phi((0.5 * 5 - 1.57460) / sqrt(1.125)) = Phi(0.87248) = 0.80853.
    """
    assert deflated_sharpe(0.5, 26, 0.0, 3.0, trials=10) == pytest.approx(0.80853, abs=1e-5)
    assert deflated_sharpe(0.5, 26, 0.0, 3.0, trials=10) == pytest.approx(
        deflated_sharpe(0.5, 26, 0.0, 3.0, trials=10, variance=1 / 25))


@pytest.mark.unit
@pytest.mark.parametrize("returns", [[], [0.01], [0.01] * 8])
def test_a_series_too_short_or_flat_has_no_sharpe_ratio(returns):
    with pytest.raises(ValueError):
        sharpe_stats(returns)


@pytest.mark.unit
def test_the_number_of_trials_must_be_at_least_one():
    with pytest.raises(ValueError):
        expected_max_sharpe(0, 1.0)
    with pytest.raises(ValueError):
        deflated_sharpe(0.3, 20, 0.0, 3.0, trials=0)
