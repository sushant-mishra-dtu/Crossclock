"""A small signal learned from settled decisions: did the decision beat its benchmark?

Fit 5 in docs/jev-use-cases.md. ``report_features`` turns each decision's
reports into numeric columns; this fits an L2-regularised logistic regression of
"alpha > 0" on them and asks whether they predict better than the rating alone,
and, given price columns (``price_features``), better than models with no LLM in
them: price only, and the rating plus price.
The result is a probability, so it can sit next to the rating as a calibrated
signal, but only once it has beaten the rating on decisions it never saw. Each
model is scored by log loss and by the Brier score, and the full model's
held-out predictions are binned into a reliability table, since log loss alone
does not show whether the probabilities are calibrated.

Decisions are ordered in time and their holding windows overlap, so every split
is chronological: the latest analysis dates are held out, the rest are
cross-validated walk-forward, and a decision trains a model only if its outcome
was known before the first date that model is tested on (its resolution date is
earlier). A shuffled split would train on outcomes from the test period.

numpy only: the data is a few dozen to a few hundred rows, and a linear model
with a penalty chosen by cross-validation is what that supports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from math import comb

import numpy as np

# Below these, the held-out block and the folds are too small to say anything.
MIN_DECISIONS = 40
MIN_DATES = 6
MIN_TRAIN = 10
N_FOLDS = 4
L2_GRID = (0.3, 1.0, 3.0, 10.0, 30.0)
# Combinatorial purged CV: the dev dates in N_GROUPS blocks, every K_TEST of them a
# test set. A decision starting within EMBARGO_DAYS after a test block's last outcome
# does not train: one step of the usual weekly grid, whose reports were written
# knowing the test block's prices.
N_GROUPS = 6
K_TEST = 2
EMBARGO_DAYS = 7
# A question whose columns barely move across decisions cannot explain outcomes
# (the cookbook's cut-off, on each column's own scale).
FLAT_SD = 0.05
WORST_SHOWN = 5


class NotEnoughData(ValueError):
    pass


def check_size(dates: Sequence[str]) -> None:
    n, n_dates = len(dates), len(set(dates))
    if n < MIN_DECISIONS or n_dates < MIN_DATES:
        raise NotEnoughData(
            f"Learning needs at least {MIN_DECISIONS} settled decisions over at least "
            f"{MIN_DATES} analysis dates, to train on the earlier ones and test on the "
            f"later ones; there are {n} over {n_dates}."
        )


@dataclass
class Dataset:
    X: np.ndarray            # decisions x columns; NaN where a document was missing
    y: np.ndarray            # 1.0 when the decision beat its benchmark
    alpha: np.ndarray
    dates: list[str]
    resolved: list[str]      # when each outcome became known
    columns: list[str]
    labels: list[str]

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, float]], columns: Sequence[str],
                  alpha: Sequence[float], dates: Sequence[str], resolved: Sequence[str],
                  labels: Sequence[str]) -> Dataset:
        X = np.array([[row.get(c, np.nan) for c in columns] for row in rows],
                     dtype=float).reshape(len(rows), len(columns))
        alpha = np.asarray(alpha, dtype=float)
        return cls(X, (alpha > 0).astype(float), alpha, list(dates), list(resolved),
                   list(columns), list(labels))

    def indices(self, names: Sequence[str]) -> list[int]:
        return [self.columns.index(n) for n in names]


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y: Sequence[float], p: Sequence[float]) -> float:
    """Mean squared error of the probabilities: 0 is perfect, 0.25 a constant coin flip.

    Unlike log loss it is bounded (a confident miss costs at most 1), so a few
    sharp misses do not dominate it.
    """
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    return float(np.mean((p - y) ** 2))


def reliability(y: Sequence[float], p: Sequence[float],
                bins: int = 5) -> list[tuple[float, float, int, float, float]]:
    """(low, high, count, mean p, share of y = 1) for each non-empty bin of p.

    The bins cut [0, 1] into ``bins`` equal widths, each closed on the left; the
    last is also closed on the right, so p = 1 falls in it. A calibrated model's
    share matches its mean p in every bin.
    """
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    edges = np.arange(bins + 1) / bins
    which = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, bins - 1)
    rows = []
    for k in range(bins):
        in_bin = which == k
        if in_bin.any():
            rows.append((float(edges[k]), float(edges[k + 1]), int(in_bin.sum()),
                         float(p[in_bin].mean()), float(y[in_bin].mean())))
    return rows


def column_stats(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mean and standard deviation of each column over its present values (0 if none)."""
    present = ~np.isnan(X)
    count = np.maximum(present.sum(axis=0), 1)
    mean = np.where(present, X, 0.0).sum(axis=0) / count
    var = (np.where(present, X - mean, 0.0) ** 2).sum(axis=0) / count
    return mean, np.sqrt(var)


@dataclass
class LogisticModel:
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray      # intercept first

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = np.nan_to_num((X - self.mean) / self.scale)
        return _sigmoid(self.weights[0] + Z @ self.weights[1:])


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float) -> LogisticModel:
    """Newton's method on the L2-penalised log loss, over standardised columns.

    A missing value is set to the column's training mean, i.e. 0 once
    standardised, so it moves the prediction neither way.
    """
    mean, sd = column_stats(X)
    scale = np.where(sd > 1e-6, sd, 1.0)
    Z = np.nan_to_num((X - mean) / scale)

    A = np.hstack([np.ones((len(y), 1)), Z])
    penalty = np.eye(A.shape[1]) * l2
    penalty[0, 0] = 1e-6  # the intercept is not shrunk, only kept finite
    base = np.clip(y.mean(), 1e-3, 1 - 1e-3)
    w = np.zeros(A.shape[1])
    w[0] = np.log(base / (1 - base))
    for _ in range(100):
        p = _sigmoid(A @ w)
        gradient = A.T @ (p - y) + penalty @ w
        hessian = A.T @ (A * (p * (1 - p))[:, None]) + penalty
        step = np.linalg.solve(hessian, gradient)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return LogisticModel(mean, scale, w)


# ---------------------------------------------------------------------------
# Chronological splits
# ---------------------------------------------------------------------------


def holdout_split(dates: Sequence[str], resolved: Sequence[str], fraction: float):
    """Dev rows, the rows the holdout model trains on, and the held-out rows.

    The held-out rows are the last ``fraction`` of analysis dates. The training
    rows are the dev rows whose outcome was known before the first of them.
    """
    if not 0 < fraction < 1:
        raise ValueError(f"the holdout must be a fraction between 0 and 1, got {fraction}")
    days = sorted(set(dates))
    first = days[-min(len(days) - 1, max(1, round(len(days) * fraction)))]
    dates, resolved = np.asarray(dates), np.asarray(resolved)
    dev = np.flatnonzero(dates < first)
    return dev, dev[resolved[dev] < first], np.flatnonzero(dates >= first)


def walk_forward(dates: Sequence[str], resolved: Sequence[str], rows: np.ndarray,
                 n_folds: int = N_FOLDS) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window folds over ``rows``: test each block of dates on the ones before.

    The dates are cut into ``n_folds + 1`` consecutive blocks; fold k tests block
    k and trains on the earlier rows whose outcome was known before block k began.
    """
    dates, resolved = np.asarray(dates), np.asarray(resolved)
    days = sorted(set(dates[rows]))
    folds = []
    for block in np.array_split(np.asarray(days), n_folds + 1)[1:]:
        if not len(block):
            continue
        first, last = block[0], block[-1]
        test = rows[(dates[rows] >= first) & (dates[rows] <= last)]
        train = rows[(dates[rows] < first) & (resolved[rows] < first)]
        folds.append((train, test))
    return folds


def _trainable(y: np.ndarray) -> bool:
    return len(y) >= MIN_TRAIN and 0 < y.sum() < len(y)


def cross_validate(ds: Dataset, cols: Sequence[int], folds, l2: float):
    """Log loss of out-of-fold predictions, and the predictions (NaN where none)."""
    out = np.full(len(ds.y), np.nan)
    for train, test in folds:
        model = fit_logistic(ds.X[np.ix_(train, cols)], ds.y[train], l2)
        out[test] = model.predict(ds.X[np.ix_(test, cols)])
    scored = ~np.isnan(out)
    return log_loss(ds.y[scored], out[scored]), out


# ---------------------------------------------------------------------------
# Combinatorial purged CV and the probability of backtest overfitting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    train: np.ndarray
    test: np.ndarray
    groups: tuple[int, ...]   # the test blocks
    test_blocks: np.ndarray   # the block of each test row


def combinatorial_purged_splits(dates: Sequence[str], resolved: Sequence[str], rows: np.ndarray,
                                n_groups: int = N_GROUPS, k_test: int = K_TEST,
                                embargo_days: int = EMBARGO_DAYS) -> list[Split]:
    """Every choice of ``k_test`` of ``n_groups`` blocks of dates as the test set
    (López de Prado, Advances in Financial Machine Learning, ch. 12).

    The distinct dates of ``rows`` are cut into consecutive blocks. A row trains
    unless its interval, from its analysis date to its resolution date, overlaps a
    test block's window: from the block's first analysis date to its last
    resolution date, plus ``embargo_days``. Rows after a test block may train,
    unlike in ``walk_forward``, so the purge covers both sides.
    """
    rows = np.asarray(rows)
    start = np.asarray(dates, dtype="datetime64[D]")[rows]
    end = np.asarray(resolved, dtype="datetime64[D]")[rows]
    days = np.unique(start)
    if len(days) < n_groups:
        raise ValueError(f"{n_groups} blocks need at least {n_groups} dates, got {len(days)}")
    block_of = np.zeros(len(rows), dtype=int)
    for g, block in enumerate(np.array_split(days, n_groups)):
        block_of[np.isin(start, block)] = g
    embargo = np.timedelta64(embargo_days, "D")
    splits = []
    for chosen in combinations(range(n_groups), k_test):
        in_test = np.isin(block_of, chosen)
        clear = ~in_test
        for g in chosen:
            low, high = start[block_of == g].min(), end[block_of == g].max() + embargo
            clear &= (start > high) | (end < low)
        splits.append(Split(rows[clear], rows[in_test], chosen, block_of[in_test]))
    return splits


@dataclass
class CpcvFit:
    in_sample: np.ndarray       # per split: log loss on the training rows the model was fitted to
    out_of_sample: np.ndarray   # per split: log loss on its test rows
    predictions: list[np.ndarray]  # per split: P for its test rows


def cpcv_fit(ds: Dataset, splits: Sequence[Split], cols: Sequence[int], l2: float) -> CpcvFit:
    """Fit one configuration on each split's training rows and score both sides."""
    in_sample, out_of_sample, predictions = [], [], []
    for s in splits:
        model = fit_logistic(ds.X[np.ix_(s.train, cols)], ds.y[s.train], l2)
        p = model.predict(ds.X[np.ix_(s.test, cols)])
        in_sample.append(log_loss(ds.y[s.train], model.predict(ds.X[np.ix_(s.train, cols)])))
        out_of_sample.append(log_loss(ds.y[s.test], p))
        predictions.append(p)
    return CpcvFit(np.array(in_sample), np.array(out_of_sample), predictions)


def backtest_paths(splits: Sequence[Split], predictions: Sequence[np.ndarray], n_rows: int) -> np.ndarray:
    """Paths x rows of out-of-sample predictions, NaN where a path has none.

    The j-th split (in order) that tests a block gives that block's predictions to
    path j, so with every split each path predicts every row once: C(n - 1, k - 1)
    paths for n blocks taken k at a time.
    """
    seen: dict[int, int] = {}
    pieces = []
    for s, p in zip(splits, predictions, strict=True):
        for g in s.groups:
            path = seen[g] = seen.get(g, -1) + 1
            own = s.test_blocks == g
            pieces.append((path, s.test[own], p[own]))
    paths = np.full((max(seen.values(), default=-1) + 1, n_rows), np.nan)
    for path, rows, p in pieces:
        paths[path, rows] = p
    return paths


def probability_of_overfitting(in_sample, out_of_sample) -> float:
    """PBO by CSCV (Bailey, Borwein, López de Prado and Zhu, 2014), from losses
    (lower is better), splits x configurations.

    In each split the configuration with the lowest in-sample loss is ranked out
    of sample, 1 the worst to N the best (ties share their ranks); its relative
    rank w = rank / (N + 1) overfits when logit(w) <= 0, i.e. w <= 0.5. PBO is
    the share of splits where it does.
    """
    in_sample, out_of_sample = np.asarray(in_sample, dtype=float), np.asarray(out_of_sample, dtype=float)
    n_configs = in_sample.shape[1]
    if n_configs < 2 or not len(in_sample):
        raise ValueError("the probability of backtest overfitting needs two configurations and a split")
    overfit = 0
    for ins, oos in zip(in_sample, out_of_sample, strict=True):
        best = int(np.argmin(ins))
        rank = 1 + np.sum(oos > oos[best]) + (np.sum(oos == oos[best]) - 1) / 2
        overfit += rank / (n_configs + 1) <= 0.5
    return float(overfit / len(in_sample))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class ModelScore:
    name: str
    l2: float | None
    cv_loss: float
    holdout_loss: float
    cv_brier: float          # Brier score of the same predictions as the log losses
    holdout_brier: float
    favoured: tuple[float, int] | None = None   # mean held-out alpha and count, p above base rate
    rest: tuple[float, int] | None = None
    # Combinatorial purged CV at the model's L2, over its backtest paths: median, 10th, 90th percentile.
    paths_loss: tuple[float, float, float] | None = None
    paths_brier: tuple[float, float, float] | None = None


@dataclass
class Combinatorial:
    n_groups: int
    k_test: int
    embargo_days: int
    splits: int          # C(n_groups, k_test)
    used: int            # the splits with enough training decisions of both outcomes
    paths: int
    configurations: int  # every model at every L2 of its grid
    pbo: float | None    # None with under two splits or configurations


@dataclass
class QuestionScore:
    id: str
    gain: float      # dev CV log loss without the question, minus with it
    flat: bool


@dataclass
class Evaluation:
    decisions: int
    dates: int
    holdout_dates: tuple[str, str]
    holdout_rows: int
    train_rows: int
    base_rate: float
    models: list[ModelScore]
    questions: list[QuestionScore] = field(default_factory=list)
    worst: list[tuple[str, float, float]] = field(default_factory=list)  # label, p, alpha
    # The full model's held-out predictions by bin: low, high, count, mean p, share that beat.
    reliability: list[tuple[float, float, int, float, float]] = field(default_factory=list)
    price_columns: list[str] = field(default_factory=list)   # the no-LLM baseline's columns, if any
    price_missing: dict[str, int] = field(default_factory=dict)  # column -> decisions without it
    cpcv: Combinatorial | None = None   # None when the dev dates are too few to cut into blocks
    dev_dates: int = 0

    def model(self, name: str) -> ModelScore:
        return next(m for m in self.models if m.name == name)

    def render(self) -> str:
        first, last = self.holdout_dates
        span = first if first == last else f"{first} to {last}"
        lines = [
            f"Settled decisions: {self.decisions} over {self.dates} analysis dates. "
            f"Held out {span}: {self.holdout_rows} decisions. Trained on the "
            f"{self.train_rows} settled before {first}, of which {self.base_rate:.0%} "
            "beat the benchmark.",
            "",
            "Scores of P(beats the benchmark), lower is better: log loss (0.693 is a coin "
            "flip) and the Brier score, the mean squared error of P (0.250 is a coin flip):",
        ]
        for m in self.models:
            line = (f"- {m.name}: log loss dev CV {m.cv_loss:.3f}, held out {m.holdout_loss:.3f}; "
                    f"Brier dev CV {m.cv_brier:.3f}, held out {m.holdout_brier:.3f}")
            if m.l2 is not None:
                line += f" (L2 {m.l2:g})"
            if m.favoured and m.rest:
                line += (f"; held-out alpha {m.favoured[0]:+.2%} (n={m.favoured[1]}) where it "
                         f"favours the stock vs {m.rest[0]:+.2%} (n={m.rest[1]}) elsewhere")
            lines.append(line)
        if self.price_columns:
            lines += ["", f"Price only and rating + price use no LLM: {', '.join(self.price_columns)}, "
                      "computed from the daily closes before each analysis date (returns over 20, 60 "
                      "and 120 bars, the 20-day volatility of log returns, the distance to the 50- and "
                      "200-day averages). A gain from the Jev features counts only if it beats them too."]
            if all(self.price_missing.get(c) == self.decisions for c in self.price_columns):
                lines.append("No price history was found for any decision, so the price models are "
                             "the base rate and the rating.")
            elif self.price_missing:
                missing = ", ".join(f"{c} missing for {n} of {self.decisions} decisions"
                                    for c, n in self.price_missing.items())
                lines.append(f"Some decisions have no price history, or too little of it (each gap "
                             f"is filled with its training mean): {missing}.")
        if self.cpcv:
            c = self.cpcv
            used = "" if c.used == c.splits else (f", {c.used} of them with enough training "
                                                  "decisions of both outcomes")
            lines += ["", f"Combinatorial purged CV on the dev dates: {c.n_groups} blocks of dates, every "
                      f"{c.k_test} of them a test set ({c.splits} splits{used}; {c.paths} backtest paths). "
                      "A training decision whose outcome window touches a test block, or that starts "
                      f"within {c.embargo_days} days after one, is left out. Out-of-sample scores per "
                      "path at each model's L2, median [10th to 90th percentile]:"]
            for m in self.models:
                if m.paths_loss and m.paths_brier:
                    (loss, low, high), (b, b_low, b_high) = m.paths_loss, m.paths_brier
                    lines.append(f"- {m.name}: log loss {loss:.3f} [{low:.3f} to {high:.3f}], "
                                 f"Brier {b:.3f} [{b_low:.3f} to {b_high:.3f}]")
            if c.pbo is not None:
                lines.append(f"Probability of backtest overfitting: {c.pbo:.2f}, over the {c.configurations} "
                             "model-and-L2 configurations: the share of the splits where the one with the "
                             "best in-sample log loss ranks in the bottom half out of sample (CSCV; Bailey, "
                             "Borwein, López de Prado and Zhu, 2014). Near 0 the in-sample choice holds up; "
                             "near 0.5 or above it is no better than chance.")
        elif self.dev_dates:
            lines += ["", f"Combinatorial purged CV needs at least {N_GROUPS} dev dates; there are "
                      f"{self.dev_dates}, so it was skipped."]
        if self.reliability:
            lines += ["", f"Reliability of {self.models[-1].name} on the held-out dates, by "
                      "predicted P (empty bins left out); a calibrated model's share that beat "
                      "matches its mean P. A few dozen held-out decisions make these bins coarse:"]
            lines += [f"- P {lo:.1f} to {hi:.1f}: {n} decisions, mean P {mean_p:.2f}, {beat:.0%} beat"
                      for lo, hi, n, mean_p, beat in self.reliability]
        if self.questions:
            lines += ["", "Questions, by how much dropping each one raises the dev CV log "
                      "loss of the full model. Keep one only while this is above 0; a "
                      "small gain is often noise:"]
            for q in self.questions:
                verdict = ("flat: drop" if q.flat else
                           "keep" if q.gain > 0 else "drop: no worse without it")
                lines.append(f"- {q.id}: {q.gain:+.4f} ({verdict})")
        if self.worst:
            lines += ["", "Worst-predicted dev decisions, the cases to read when proposing "
                      "new questions:"]
            lines += [f"- {label}: P(beats) {p:.2f}, alpha {alpha:+.1%}"
                      for label, p, alpha in self.worst]
        lines += ["", "One sampling of each report and a few dozen decisions make these "
                  "figures noisy. Trust a gain only when it holds on the held-out dates and "
                  "again on a longer or wider sweep."]
        return "\n".join(lines)


def _alpha_split(alpha: np.ndarray, p: np.ndarray, base: float):
    favoured = p > base + 1e-9
    if not favoured.any() or favoured.all():
        return None, None
    return ((float(alpha[favoured].mean()), int(favoured.sum())),
            (float(alpha[~favoured].mean()), int((~favoured).sum())))


def evaluate(ds: Dataset, questions: Mapping[str, Sequence[str]], baseline: Sequence[str],
             holdout: float = 0.25, price: Sequence[str] = ()) -> Evaluation:
    """Score the base rate, the baseline columns, and baseline plus every question.

    With ``price`` columns, two models with no LLM in them are scored as well:
    price only, and the baseline plus price. The L2 penalty of each model is
    chosen by walk-forward cross-validation on the dev dates, and each model is
    then scored once on the held-out dates.
    """
    check_size(ds.dates)
    dev, train, test = holdout_split(ds.dates, ds.resolved, holdout)
    folds = [(tr, te) for tr, te in walk_forward(ds.dates, ds.resolved, dev) if _trainable(ds.y[tr])]
    if len(folds) < 2 or not _trainable(ds.y[train]):
        raise NotEnoughData(
            "Too few settled decisions before the held-out dates, or all of one outcome, "
            "to cross-validate; widen the sweep or hold out less."
        )

    base_cols = ds.indices(baseline)
    all_cols = base_cols + ds.indices([c for cols in questions.values() for c in cols])
    base_rate = float(ds.y[train].mean())
    price_cols = ds.indices(price)
    specs = [("base rate", []), ("rating", base_cols)]
    if price_cols:
        specs += [("price only", price_cols), ("rating + price", base_cols + price_cols)]
    models, fitted = [], []
    # The full model stays last: its predictions and penalty are read below.
    for name, cols in (*specs, ("rating + Jev features", all_cols)):
        grid = L2_GRID if cols else (1.0,)
        l2, cv, oof = min(((l2, *cross_validate(ds, cols, folds, l2)) for l2 in grid),
                          key=lambda fit: fit[1])
        fitted.append((cols, grid, l2))
        model = fit_logistic(ds.X[np.ix_(train, cols)], ds.y[train], l2)
        p = model.predict(ds.X[np.ix_(test, cols)])
        favoured, rest = _alpha_split(ds.alpha[test], p, base_rate) if cols else (None, None)
        oof_rows = ~np.isnan(oof)
        models.append(ModelScore(name, l2 if cols else None, cv, log_loss(ds.y[test], p),
                                 cv_brier=brier(ds.y[oof_rows], oof[oof_rows]),
                                 holdout_brier=brier(ds.y[test], p),
                                 favoured=favoured, rest=rest))
    calibration = reliability(ds.y[test], p)  # p: the last model's, the full one

    full_l2 = models[-1].l2
    full_cv, oof = cross_validate(ds, all_cols, folds, full_l2)
    scored = []
    for qid, cols in questions.items():
        own = set(ds.indices(cols))
        without = [c for c in all_cols if c not in own]
        gain = cross_validate(ds, without, folds, full_l2)[0] - full_cv
        flat = bool(np.all(column_stats(ds.X[np.ix_(dev, sorted(own))])[1] < FLAT_SD))
        scored.append(QuestionScore(qid, gain, flat))
    scored.sort(key=lambda q: q.gain, reverse=True)

    predicted = np.flatnonzero(~np.isnan(oof))
    row_loss = [(-np.log(np.clip(oof[i] if ds.y[i] else 1 - oof[i], 1e-6, 1)), i) for i in predicted]
    worst = [(ds.labels[i], float(oof[i]), float(ds.alpha[i]))
             for _, i in sorted(row_loss, reverse=True)[:WORST_SHOWN]]

    dev_dates = len({ds.dates[i] for i in dev})
    cpcv = _combinatorial(ds, dev, fitted, models) if dev_dates >= N_GROUPS else None

    held = [ds.dates[i] for i in test]
    return Evaluation(
        decisions=len(ds.y), dates=len(set(ds.dates)), holdout_dates=(min(held), max(held)),
        holdout_rows=len(test), train_rows=len(train), base_rate=base_rate,
        models=models, questions=scored, worst=worst, reliability=calibration,
        price_columns=list(price),
        price_missing={c: n for c, i in zip(price, price_cols, strict=True)
                       if (n := int(np.isnan(ds.X[:, i]).sum()))},
        cpcv=cpcv, dev_dates=dev_dates,
    )


def _spread(values: Sequence[float]) -> tuple[float, float, float] | None:
    """Median, 10th and 90th percentile, or None for no values."""
    if not len(values):
        return None
    median, low, high = np.percentile(values, [50, 10, 90])
    return float(median), float(low), float(high)


def _combinatorial(ds: Dataset, dev: np.ndarray, fitted, models: list[ModelScore]) -> Combinatorial:
    """Combinatorial purged CV of the dev rows: each model's backtest-path scores at
    the L2 walk-forward chose for it (set on its ModelScore), and the PBO of every
    model at every L2 of its grid. Splits too small to fit are left out."""
    all_splits = combinatorial_purged_splits(ds.dates, ds.resolved, dev)
    splits = [s for s in all_splits if _trainable(ds.y[s.train])]
    in_sample, out_of_sample = [], []
    for (cols, grid, chosen), model in zip(fitted, models, strict=True):
        for l2 in grid:
            fit = cpcv_fit(ds, splits, cols, l2)
            in_sample.append(fit.in_sample)
            out_of_sample.append(fit.out_of_sample)
            if l2 != chosen:
                continue
            losses, briers = [], []
            for path in backtest_paths(splits, fit.predictions, len(ds.y)):
                scored = ~np.isnan(path)
                if scored.any():
                    losses.append(log_loss(ds.y[scored], path[scored]))
                    briers.append(brier(ds.y[scored], path[scored]))
            model.paths_loss, model.paths_brier = _spread(losses), _spread(briers)
    pbo = (probability_of_overfitting(np.column_stack(in_sample), np.column_stack(out_of_sample))
           if splits and len(in_sample) > 1 else None)
    return Combinatorial(N_GROUPS, K_TEST, EMBARGO_DAYS, splits=len(all_splits), used=len(splits),
                         paths=comb(N_GROUPS - 1, K_TEST - 1), configurations=len(in_sample), pbo=pbo)
