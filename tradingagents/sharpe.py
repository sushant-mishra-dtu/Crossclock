"""The probabilistic and deflated Sharpe ratios (Bailey and López de Prado).

A Sharpe ratio estimated from a few dozen periods is noisy, and the best of
several configurations tried on the same data is biased upward: some of them
look good by chance alone. Two corrections, written out here because the
project carries no statistics package beyond the standard library:

- The probabilistic Sharpe ratio (PSR; Bailey and López de Prado, "The Sharpe
  Ratio Efficient Frontier", Journal of Risk 15(2), 2012) is the probability that
  the true Sharpe ratio is above a benchmark, given the estimate, the number of
  periods T, and the skew and kurtosis of the returns:

      PSR(SR*) = Phi((SR - SR*) * sqrt(T - 1) / sqrt(1 - skew * SR + (kurtosis - 1) / 4 * SR^2))

- The deflated Sharpe ratio (DSR; Bailey and López de Prado, "The Deflated Sharpe
  Ratio", Journal of Portfolio Management 40(5), 2014) is the PSR against the
  Sharpe ratio the best of N trials is expected to reach when none has any skill:

      SR0 = sqrt(V) * ((1 - g) * z(1 - 1/N) + g * z(1 - 1/(N e)))

  where g is the Euler-Mascheroni constant, z the standard normal quantile and V
  the variance of the trials' Sharpe ratios. With no V given it is the variance
  of an estimate whose true value is 0, 1 / (T - 1), so the DSR says whether the
  result beats the best of N tries of chance alone. Trials that share data are
  correlated, so they are fewer independent tries than N, and the deflation errs
  on the strict side.

Kurtosis is not excess kurtosis: 3 for normal returns. Every moment divides by T,
the moments of the series as observed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

EULER_GAMMA = 0.5772156649015329

_NORMAL = NormalDist()


@dataclass(frozen=True)
class SharpeStats:
    periods: int
    sharpe: float     # mean over standard deviation, per period
    skew: float
    kurtosis: float   # 3 for normal returns


def sharpe_stats(returns: Sequence[float]) -> SharpeStats:
    """Sharpe ratio, skew and kurtosis of a series of period returns.

    Raises ``ValueError`` for fewer than two returns or returns that never vary,
    which have no Sharpe ratio.
    """
    values = [float(r) for r in returns]
    n = len(values)
    if n < 2:
        raise ValueError(f"a Sharpe ratio needs at least two periods, got {n}")
    mean = math.fsum(values) / n
    deviations = [r - mean for r in values]
    m2 = math.fsum(d * d for d in deviations) / n
    # Equal returns can leave float noise in the variance rather than an exact 0.
    if m2 <= (1e-12 * max(abs(r) for r in values)) ** 2:
        raise ValueError("the returns do not vary, so they have no Sharpe ratio")
    m3 = math.fsum(d ** 3 for d in deviations) / n
    m4 = math.fsum(d ** 4 for d in deviations) / n
    return SharpeStats(periods=n, sharpe=mean / math.sqrt(m2), skew=m3 / m2 ** 1.5,
                       kurtosis=m4 / (m2 * m2))


def probabilistic_sharpe(sharpe: float, periods: int, skew: float, kurtosis: float,
                         benchmark: float = 0.0) -> float:
    """The probability that the true Sharpe ratio is above ``benchmark``."""
    if periods < 2:
        raise ValueError(f"a Sharpe ratio needs at least two periods, got {periods}")
    spread = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe * sharpe
    # Never negative for real moments (kurtosis >= 1 + skew^2); zero only for a
    # two-valued series at one exact Sharpe ratio, where the estimate has no spread.
    if spread <= 0:
        return 1.0 if sharpe > benchmark else 0.0
    return _NORMAL.cdf((sharpe - benchmark) * math.sqrt(periods - 1) / math.sqrt(spread))


def expected_max_sharpe(trials: int, variance: float) -> float:
    """The Sharpe ratio the best of ``trials`` tries without skill is expected to reach.

    One trial is expected to reach 0, its true value; the formula, an
    approximation for large N, does not hold there (z(0) is minus infinity).
    """
    if trials < 1:
        raise ValueError(f"the number of trials must be at least 1, got {trials}")
    if trials == 1:
        return 0.0
    return math.sqrt(variance) * ((1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / trials)
                                  + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (trials * math.e)))


def deflated_sharpe(sharpe: float, periods: int, skew: float, kurtosis: float, trials: int,
                    variance: float | None = None) -> float:
    """The PSR against the expected best of ``trials`` tries; ``variance`` defaults to 1 / (T - 1)."""
    if periods < 2:
        raise ValueError(f"a Sharpe ratio needs at least two periods, got {periods}")
    if variance is None:
        variance = 1 / (periods - 1)
    return probabilistic_sharpe(sharpe, periods, skew, kurtosis,
                                benchmark=expected_max_sharpe(trials, variance))
