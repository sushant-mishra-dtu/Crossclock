"""Deadlines and stops for one research run (docs/adr/0001).

A run's budget comes from its research mode (``run_budgets`` in the config).
Whoever starts a run (the web job, the CLI, ``propagate``, a backtest cell)
puts a ``RunControl`` in the context. LangGraph copies the context into its
worker threads, so the model clients' retry waits and the HTTP hooks on every
provider request see the same control: after Stop or the deadline, nothing
waits and nothing more is sent.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class RunInterrupted(BaseException):  # noqa: N818 — named for what happened, like KeyboardInterrupt
    """The run was stopped or ran out of time.

    A BaseException, as ``asyncio.CancelledError`` is, so the broad
    ``except Exception`` fallbacks around optional checks (the claim check, Jev
    judgments, structured output) cannot swallow a stop and carry on.
    """

    reason = "interrupted"


class RunCancelled(RunInterrupted):
    reason = "cancelled"


class DeadlineExceeded(RunInterrupted):
    reason = "deadline"


# The fields a mode's budget needs. ``default_config`` holds the values.
_BUDGET_FIELDS = ("deadline_seconds", "attempt_timeout_seconds", "retry_backoff_seconds",
                  "max_retry_after_seconds", "min_attempt_seconds")


def _positive(name: str, value, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"run budget {name} must be a positive number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class RunBudget:
    """How long one run may take, and how its model calls wait and retry."""

    mode: str
    deadline_seconds: float | None  # research start to persisted report; None: no deadline
    attempt_timeout_seconds: float  # one HTTP attempt
    retry_backoff_seconds: tuple[float, ...]  # waits before each retry of a model call
    max_retry_after_seconds: float  # longest server-requested wait honoured
    min_attempt_seconds: float  # a retry needs at least this long left after its wait

    @classmethod
    def from_config(cls, config: dict | None) -> RunBudget:
        """The budget of ``config``'s research mode; fields it leaves out take the defaults."""
        from tradingagents.default_config import DEFAULT_CONFIG

        config = config or {}
        mode = config.get("research_mode") or DEFAULT_CONFIG["research_mode"]
        defaults = DEFAULT_CONFIG["run_budgets"].get(mode, {})
        budgets = config.get("run_budgets") or DEFAULT_CONFIG["run_budgets"]
        if mode not in budgets and mode not in DEFAULT_CONFIG["run_budgets"]:
            raise ValueError(f"no run budget for research mode {mode!r}")
        values = {**defaults, **(budgets.get(mode) or {})}
        missing = [name for name in _BUDGET_FIELDS if name not in values]
        if missing:
            raise ValueError(f"run budget for {mode!r} is missing {', '.join(missing)}")
        backoff = tuple(_positive("retry_backoff_seconds", v) for v in values["retry_backoff_seconds"])
        return cls(
            mode=mode,
            deadline_seconds=_positive("deadline_seconds", values["deadline_seconds"], allow_none=True),
            attempt_timeout_seconds=_positive("attempt_timeout_seconds", values["attempt_timeout_seconds"]),
            retry_backoff_seconds=backoff,
            max_retry_after_seconds=_positive("max_retry_after_seconds", values["max_retry_after_seconds"]),
            min_attempt_seconds=_positive("min_attempt_seconds", values["min_attempt_seconds"]),
        )


def attempt_timeout_seconds(config: dict | None) -> float:
    """The per-request timeout model clients are built with for ``config``'s mode."""
    return RunBudget.from_config(config).attempt_timeout_seconds


def capped_schedule(schedule: tuple[float, ...], limit: int | None) -> tuple[float, ...]:
    """``schedule`` cut to ``limit`` retries, or extended with its last wait past its length."""
    if limit is None:
        return tuple(schedule)
    if limit <= len(schedule):
        return tuple(schedule[:limit])
    return (*schedule, *(schedule[-1:] or (0,)) * (limit - len(schedule)))


class RunControl:
    """One run's deadline, Stop and retry state, shared by every thread of the run."""

    def __init__(self, budget: RunBudget, cancel_event: threading.Event | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.budget = budget
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._clock = clock
        self.started = clock()
        self.deadline = None if budget.deadline_seconds is None else self.started + budget.deadline_seconds
        self._lock = threading.Lock()
        self.in_flight = 0
        self.retry: dict | None = None
        self.interruption: str | None = None  # "cancelled" or "deadline", once raised
        self.provider_failure: dict | None = None

    @classmethod
    def from_config(cls, config: dict | None, cancel_event: threading.Event | None = None,
                    clock: Callable[[], float] = time.monotonic) -> RunControl:
        return cls(RunBudget.from_config(config), cancel_event, clock)

    def remaining(self) -> float | None:
        """Seconds left before the deadline (negative once past), or None without one."""
        return None if self.deadline is None else self.deadline - self._clock()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _interrupt(self, exc: RunInterrupted) -> RunInterrupted:
        with self._lock:
            self.interruption = self.interruption or exc.reason
        return exc

    def check(self) -> None:
        """Raise if the run was stopped or its deadline has passed."""
        if self.cancel_event.is_set():
            raise self._interrupt(RunCancelled("The run was stopped."))
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise self._interrupt(DeadlineExceeded(
                f"The run passed its {self.budget.deadline_seconds:g} s deadline."))

    def attempt_timeout(self) -> float:
        """The timeout for an HTTP attempt starting now: the budget's, cut to the time left."""
        remaining = self.remaining()
        timeout = self.budget.attempt_timeout_seconds
        return timeout if remaining is None else max(min(timeout, remaining), 0.001)

    def note_provider_failure(self, failure: dict) -> None:
        """Keep the provider error a run ended on (the first one, if several threads fail)."""
        with self._lock:
            self.provider_failure = self.provider_failure or dict(failure)

    def wait_before_retry(self, delay: float, failure: dict) -> None:
        """Wait ``delay`` seconds before retrying a model call.

        Raises RunCancelled at once on Stop, and DeadlineExceeded without waiting
        when the retry could not get ``min_attempt_seconds`` before the deadline.
        ``failure`` describes the provider error being retried (no message text).
        """
        try:
            self.check()
        except RunInterrupted:
            self.note_provider_failure(failure)
            raise
        remaining = self.remaining()
        if remaining is not None and delay + self.budget.min_attempt_seconds > remaining:
            self.note_provider_failure(failure)
            raise self._interrupt(DeadlineExceeded(
                f"A {delay:g} s retry wait would leave under {self.budget.min_attempt_seconds:g} s "
                f"before the {self.budget.deadline_seconds:g} s deadline."))
        with self._lock:
            self.retry = {**failure, "delay_seconds": delay, "until": self._clock() + delay}
        try:
            if self.cancel_event.wait(delay):
                self.note_provider_failure(failure)
                raise self._interrupt(RunCancelled("The run was stopped during a retry wait."))
        finally:
            with self._lock:
                self.retry = None

    @contextmanager
    def request(self):
        """Count a model or Jev request as in flight while it runs."""
        with self._lock:
            self.in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self.in_flight -= 1

    def snapshot(self) -> dict:
        """What a progress page shows: time left, a retry being waited out, requests in flight."""
        now = self._clock()
        with self._lock:
            retry = None
            if self.retry is not None:
                retry = {k: v for k, v in self.retry.items() if k != "until"}
                retry["remaining_seconds"] = round(max(self.retry["until"] - now, 0.0), 1)
            return {
                "mode": self.budget.mode,
                "deadline_seconds": self.budget.deadline_seconds,
                "remaining_seconds": None if self.deadline is None else round(self.deadline - now, 1),
                "attempt_timeout_seconds": self.budget.attempt_timeout_seconds,
                "in_flight": self.in_flight,
                "retry": retry,
                "stopping": self.cancel_event.is_set(),
                "interruption": self.interruption,
            }


_control: ContextVar[RunControl | None] = ContextVar("run_control", default=None)


def current_control() -> RunControl | None:
    return _control.get()


@contextmanager
def use_control(control: RunControl | None):
    token = _control.set(control)
    try:
        yield control
    finally:
        _control.reset(token)


def set_control(control: RunControl | None):
    """Set the control in the current context (for a ``contextvars.Context.run``)."""
    return _control.set(control)


# --- Optional checks -------------------------------------------------------
# The claim check, Jev judgments and the memory step are optional: a run goes
# on without them. Their outcome is recorded so a report can say what did not
# run, instead of reading the same as one where everything did.

CHECK_RAN, CHECK_FAILED, CHECK_DISABLED, CHECK_UNAVAILABLE, CHECK_SKIPPED = (
    "ran", "failed", "disabled", "unavailable", "skipped")
_CHECK_RANK = {CHECK_RAN: 0, CHECK_SKIPPED: 0, CHECK_DISABLED: 1, CHECK_UNAVAILABLE: 2, CHECK_FAILED: 3}
# Statuses that leave a gap in the research the user meant to run.
GAP_STATUSES = frozenset({CHECK_FAILED, CHECK_UNAVAILABLE})

_checks: ContextVar[dict | None] = ContextVar("run_checks", default=None)


def merge_checks(left: dict | None, right: dict | None) -> dict:
    """State reducer: one status per check, the worst seen (a failed turn outweighs a judged one)."""
    merged = dict(left or {})
    for name, status in (right or {}).items():
        if _CHECK_RANK.get(status, 0) >= _CHECK_RANK.get(merged.get(name), -1):
            merged[name] = status
    return merged


def record_check(name: str, status: str) -> None:
    """Note how an optional check went, for the node that is running it."""
    sink = _checks.get()
    if sink is not None:
        sink.update(merge_checks(sink, {name: status}))


def recording_checks(node):
    """Wrap a graph node so the checks it records reach the state as ``run_checks``."""
    # Only ``state``: LangGraph reads a node's signature to decide what else to pass.
    def recorded(state):
        sink: dict = {}
        token = _checks.set(sink)
        try:
            update = node(state)
        finally:
            _checks.reset(token)
        if sink and isinstance(update, dict):
            update = {**update, "run_checks": merge_checks(update.get("run_checks"), sink)}
        return update

    recorded.__name__ = getattr(node, "__name__", "node")
    recorded.__doc__ = getattr(node, "__doc__", None)
    return recorded
