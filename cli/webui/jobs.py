"""Background runs and on-disk history for the web UI.

Nothing here knows about HTTP. A run executes on a worker thread and writes
into a ``MessageBuffer`` under a lock; the page takes a snapshot under the same
lock on each refresh, so a long run never blocks the browser and a page reload
does not lose it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cli.display import ANALYST_ORDER, AnalystWallTimeTracker, MessageBuffer, process_chunk
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import is_review, parse_rating, run_rating
from tradingagents.backtest import run_backtest, summarize
from tradingagents.completion import (
    PARTIAL,
    STOP_CANCELLED,
    STOP_ERROR,
    STOP_MISSING,
    IncompleteResearch,
    describe_gaps,
    describe_stop,
    is_complete,
    provider_failure_for,
    run_completion,
    stop_reason_for,
)
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.memory import TradingMemoryLog
from tradingagents.reporting import report_dir, write_partial_report
from tradingagents.run_control import (
    DeadlineExceeded,
    RunBudget,
    RunControl,
    RunInterrupted,
    use_control,
)
from tradingagents.telemetry import RunTrace, use_trace

PENDING, RUNNING, DONE, FAILED, CANCELLED = "pending", "running", "done", "failed", "cancelled"

# Team layout of the pipeline, in the order the graph runs it.
TEAMS = {
    "Analyst Team": ["Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"],
    **MessageBuffer.FIXED_AGENTS,
}

SECTION_TITLES = {
    "market_report": "Market Analyst",
    "sentiment_report": "Sentiment Analyst",
    "news_report": "News Analyst",
    "fundamentals_report": "Fundamentals Analyst",
    "investment_plan": "Research Team",
    "trader_investment_plan": "Trader",
    "final_trade_decision": "Risk & Portfolio Management",
}


# The five section headings write_report_tree emits. Agent reports carry their
# own "## " headings, so only these may split a saved report.
_SECTION_RE = re.compile(r"^## ((?:I|II|III|IV|V)\. .+)$", re.MULTILINE)


# The analysis whose graph is running in this context. LangGraph runs nodes on
# pool threads but copies the context into them, so this reaches the LLM calls.
_current_job: ContextVar[AnalysisJob | None] = ContextVar("webui_analysis_job", default=None)


class _JobLogHandler(logging.Handler):
    """Puts LLM-client warnings (a provider retry) in the run's activity log.

    A transient-error retry can back off for minutes; without this the page
    would show a run that has silently stopped moving.
    """

    def emit(self, record: logging.LogRecord) -> None:
        job = _current_job.get()
        if job is not None:
            job._log(record.getMessage())


logging.getLogger("tradingagents.llm_clients").addHandler(_JobLogHandler(logging.WARNING))


def _checkpoint_values(graph, args) -> dict:
    """The state a resumed run's checkpoint holds, or {} when it cannot be read."""
    try:
        return dict(graph.graph.get_state(args.get("config", {})).values or {})
    except Exception:  # a resume without readable state still runs; it only reports less
        return {}
# A failed run's traceback goes to the server's terminal; the API shows only the error itself.
log = logging.getLogger(__name__)


@dataclass
class AnalysisJob:
    """One single-ticker analysis running on its own thread."""

    ticker: str
    trade_date: str
    asset_type: str
    analysts: list[str]
    config: dict
    portfolio: object = None
    id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    status: str = PENDING
    error: str | None = None
    rating: str | None = None
    report_path: Path | None = None
    final_state: dict | None = None
    started: float = 0.0
    finished: float | None = None
    judgments: dict | None = None  # the Sentiment Analyst's Jev judgments, once made
    submitted: float = 0.0
    trace_path: Path | None = None
    telemetry_error: str | None = None
    # What the run finished, checked and stopped on (tradingagents.completion).
    completion: dict | None = None
    partial_report_path: Path | None = None

    def __post_init__(self):
        self.analysts = [a for a in ANALYST_ORDER if a in set(self.analysts)]
        if not self.analysts:
            raise ValueError("Pick at least one analyst.")
        # Fails here, at submission, on a misconfigured research budget.
        RunBudget.from_config(self.config)
        self.buffer = MessageBuffer(max_length=500)
        self.buffer.init_for_analysis(self.analysts)
        self.stats = StatsCallbackHandler()
        self.lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.control: RunControl | None = None
        self.trace: RunTrace | None = None
        self._queue_span: str | None = None
        self._trace_finished = False

    def mark_submitted(self) -> None:
        """Start the monotonic run clock when the request enters the scheduler."""
        if self.trace is not None:
            return
        self.submitted = time.time()
        self.trace = RunTrace(run_id=self.id, configuration=self.config, workload={
            "ticker": self.ticker, "trade_date": self.trade_date,
            "asset_type": self.asset_type, "analysts": self.analysts,
            "entrypoint": "webui",
        })
        self.stats.trace = self.trace
        self.trace.milestone("submitted")
        self.trace.gap("first_useful_result_render_unobserved")
        self.trace.gap("final_report_validation_unobserved")
        self._queue_span = self.trace.start_span("queue", "queue_wait")

    @property
    def label(self) -> str:
        return f"{self.ticker} · {self.trade_date}"

    @property
    def active(self) -> bool:
        return self.status in (PENDING, RUNNING)

    def start(self) -> AnalysisJob:
        self.mark_submitted()
        self.trace.end_span(self._queue_span)
        self._queue_span = None
        self.trace.milestone("started")
        self.started = time.time()
        # The deadline counts from here: queue time is measured, not charged.
        self.control = RunControl.from_config(self.config, cancel_event=self._cancel)
        budget = self.control.budget
        self.trace.event("run_budget", {"mode": budget.mode, "deadline_seconds": budget.deadline_seconds,
                                        "attempt_timeout_seconds": budget.attempt_timeout_seconds})
        self.status = RUNNING
        self._thread = threading.Thread(target=self._run, name=f"analysis-{self.id}", daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        """Stop: retry waits end and no model request starts from now on; a request
        already in flight finishes or times out, and the run stops after it. What
        it finished is kept as a partial report, and a checkpointed run can resume."""
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the run's thread ends (at once if it never started)."""
        if self._thread is not None:
            self._thread.join(timeout)

    def drop(self, reason: str) -> None:
        """Cancel a run that never started (a queued one taken off the queue)."""
        with self.lock:
            self.buffer.add_message("System", reason)
            self.status = CANCELLED
            self.completion = run_completion({}, self.analysts, stop_reason=STOP_CANCELLED)
            self.finished = time.time()
        self._finish_trace()

    def _finish_trace(self) -> None:
        """Trace failures never change the analysis result or hide its report."""
        if self._trace_finished:
            return
        self.mark_submitted()
        if self._queue_span is not None:
            self.trace.end_span(self._queue_span, status="cancelled")
            self._queue_span = None
        if self.status in {CANCELLED, FAILED}:
            self.trace.milestone(self.status)
        self.trace.finish({DONE: "ok", FAILED: "error", CANCELLED: "cancelled"}.get(
            self.status, self.status))
        self._trace_finished = True
        try:
            self.trace_path = self.trace.persist(
                Path(self.config.get("results_dir", "results")) / "telemetry" /
                f"{safe_ticker_component(self.id)}.json")
        except Exception as exc:
            self.telemetry_error = type(exc).__name__
            self.trace.gap("trace_persistence_failed")
            log.warning("Telemetry for analysis %s could not be saved (%s)",
                        self.id, self.telemetry_error)

    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started if self.started else 0.0

    def snapshot(self) -> dict:
        """A consistent copy of everything the page shows."""
        with self.lock:
            return {
                "status": self.status,
                "agent_status": dict(self.buffer.agent_status),
                "sections": dict(self.buffer.report_sections),
                "messages": list(self.buffer.messages),
                "tool_calls": list(self.buffer.tool_calls),
                "stats": self.stats.get_stats(),
                "elapsed": self.elapsed(),
                "rating": self.rating,
                "error": self.error,
                "report_path": self.report_path,
                "judgments": self.judgments,
                "submitted": self.submitted,
                "trace_path": self.trace_path,
                "telemetry_error": self.telemetry_error,
                "telemetry": self.trace.snapshot() if self.trace is not None else None,
                "completion": self.completion,
                "partial_report_path": self.partial_report_path,
                # Time left, a retry being waited out and requests in flight, while it runs.
                "control": self.control.snapshot() if self.control is not None and self.active else None,
            }

    def _log(self, text: str) -> None:
        with self.lock:
            self.buffer.add_message("System", text)

    def _run(self) -> None:
        with use_trace(self.trace):
            self._run_traced()

    def _run_traced(self) -> None:
        # Imported here so a missing provider SDK surfaces as this run's error.
        graph = None
        job_token = _current_job.set(self)
        # What the run has finished so far; kept as a partial report if it stops.
        final_state: dict = {}
        reused: set[str] = set()
        try:
            # Every model call, retry wait and HTTP request of the run sees this
            # control: Stop and the deadline reach them all (docs/adr/0001).
            with use_control(self.control):
                with self.trace.scope("setup", "graph_setup"):
                    from tradingagents.graph.trading_graph import TradingAgentsGraph

                    self._log(f"Analyzing {self.ticker} on {self.trade_date} with: {', '.join(self.analysts)}")
                    if self.portfolio is not None:  # the block the decision agents read
                        self._log(self.portfolio.render(self.ticker))
                    graph = TradingAgentsGraph(
                        self.analysts, config=self.config, debug=False, callbacks=[self.stats]
                    )
                plan = build_analyst_execution_plan(self.analysts)
                tracker = AnalystWallTimeTracker(plan)
                # The analysts start together.
                with self.lock:
                    for spec in plan.specs:
                        self.buffer.update_agent_status(spec.agent_node, "in_progress")
                for spec in plan.specs:
                    tracker.mark_started(spec.key)

                with self.trace.scope("setup", "run_state_and_checkpoint"):
                    init_state = graph.create_run_state(
                        self.ticker, self.trade_date, self.asset_type, self.portfolio
                    )
                    args = graph.propagator.get_graph_args(callbacks=[self.stats])
                    tid = graph.begin_checkpoint(self.ticker, self.trade_date, self.asset_type, self.portfolio)
                    if tid is not None:
                        args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid
                        if getattr(graph, "_resuming", False):
                            self._log("Resuming the saved checkpoint for this ticker and date")
                            # The stages the checkpoint already has are reused, not rerun.
                            final_state.update(_checkpoint_values(graph, args))
                            reused = {stage for stage, status in
                                      run_completion(final_state, self.analysts)["stages"].items()
                                      if status == "done"}

                try:
                    with self.trace.scope("graph", "research_graph"):
                        for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                            self.control.check()
                            with self.lock:
                                process_chunk(self.buffer, chunk, wall_time_tracker=tracker, messages=messages)
                                if chunk and chunk.get("sentiment_judgments"):
                                    self.judgments = chunk["sentiment_judgments"]
                            # Reports are available on the backend; their arrival
                            # does not acknowledge a sourced result rendered by a browser.
                            if chunk:
                                if any(chunk.get(spec.report_key) for spec in plan.specs):
                                    self.trace.milestone("generated_report")
                                final_state.update(chunk)
                        self.control.check()
                    completion = run_completion(final_state, self.analysts, reused_stages=reused,
                                                deadline_seconds=self.control.budget.deadline_seconds)
                    if not is_complete(completion):
                        # The graph ended with a stage that wrote nothing: no decision
                        # is recorded, and the checkpoint goes so that running it again
                        # starts fresh instead of resuming at the same empty end.
                        graph.clear_checkpoint_on_success(
                            self.ticker, self.trade_date, self.asset_type, self.portfolio)
                        raise IncompleteResearch(completion)
                    with self.trace.scope("persistence", "decision_and_checkpoint"):
                        graph.record_decision(self.ticker, self.trade_date, final_state)
                        graph.clear_checkpoint_on_success(
                            self.ticker, self.trade_date, self.asset_type, self.portfolio
                        )
                finally:
                    graph.end_checkpoint()

                rating = run_rating(final_state)
                report_path = graph.save_reports(final_state, self.ticker, completion=completion)
                self.trace.milestone("persisted_report")

            with self.lock:
                for agent in self.buffer.agent_status:
                    self.buffer.update_agent_status(agent, "completed")
                for section in self.buffer.report_sections:
                    if final_state.get(section):
                        self.buffer.update_report_section(section, final_state[section])
                self.buffer.add_message("System", tracker.format_summary())
                for gap in describe_gaps(completion):
                    self.buffer.add_message("System", f"Gap: {gap}.")
                if is_review(rating):
                    self.buffer.add_message(
                        "System", "The final decision has no tradeable rating (none could "
                        "be read, or the claim check sent it to review); it is logged for "
                        "review rather than as a position.")
                self.final_state = final_state
                self.rating = rating
                self.report_path = report_path
                self.completion = completion
                self.status = DONE
        except (RunInterrupted, Exception) as exc:
            self._stopped(exc, graph, final_state, reused)
        finally:
            self.finished = time.time()
            _current_job.reset(job_token)
            self._finish_trace()

    def _stopped(self, exc: BaseException, graph, final_state: dict, reused: set[str]) -> None:
        """Record why the run stopped, and keep what it finished as a partial report."""
        deadline = self.control.budget.deadline_seconds if self.control is not None else None
        if isinstance(exc, IncompleteResearch):
            completion, reason = exc.completion, STOP_MISSING
        else:
            # Whatever a step raised as the user stopped it, the run was stopped.
            reason = (STOP_CANCELLED if self._cancel.is_set() and not isinstance(exc, DeadlineExceeded)
                      else stop_reason_for(exc))
            with self.lock:
                active = [agent for agent, status in self.buffer.agent_status.items() if status == "in_progress"]
            noted = self.control.provider_failure if self.control is not None else None
            completion = run_completion(final_state, self.analysts, stop_reason=reason,
                                        provider_failure=provider_failure_for(exc, noted),
                                        active_stages=active, reused_stages=reused, deadline_seconds=deadline)
        self.trace.event("run_stopped", {"reason": reason, "error_type": type(exc).__name__})
        if reason == STOP_ERROR:
            log.error("Analysis %s of %s failed", self.id, self.ticker, exc_info=exc)
        partial = None
        if completion["state"] == PARTIAL and self.config.get("save_partial_reports", True):
            try:
                with self.trace.scope("persistence", "partial_report"):
                    settings = graph.run_settings() if hasattr(graph, "run_settings") else None
                    partial = write_partial_report(
                        final_state, self.ticker,
                        report_dir(self.config.get("results_dir", "results"), self.ticker, "_partial"),
                        completion, settings=settings)
                self.trace.milestone("persisted_partial_report")
            except Exception as save_exc:  # the run's status stands even if its partial report cannot be written
                log.warning("Partial report for analysis %s could not be saved (%s)",
                            self.id, type(save_exc).__name__)
        with self.lock:
            self.completion = completion
            self.partial_report_path = partial
            if reason == STOP_CANCELLED:
                self.buffer.add_message("System", "Stopped by user")
                self.status = CANCELLED
            else:
                self.error = f"{type(exc).__name__}: {exc}" if reason == STOP_ERROR else describe_stop(completion)
                self.buffer.add_message("System", f"Failed: {self.error}")
                self.status = FAILED
            if partial is not None:
                self.buffer.add_message(
                    "System", f"What it finished is saved as a partial report, with no rating: {partial.parent}")


@dataclass
class BacktestJob:
    """A grid backtest on its own thread; progress is read from its decision log."""

    tickers: list[str]
    dates: list[str]
    config: dict
    analysts: list[str]
    asset_type: str = "stock"
    portfolio: object = None
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    status: str = PENDING
    error: str | None = None
    result: object = None
    started: float = 0.0
    finished: float | None = None
    initial_logged: int = 0

    def __post_init__(self):
        self.run_id = safe_ticker_component(self.run_id)
        self.current: tuple[str, str] | None = None  # (ticker, date) of the running cell
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def id(self) -> str:
        return f"backtest_{self.run_id}"

    @property
    def label(self) -> str:
        return f"Backtest {self.run_id} · {', '.join(self.tickers)}"

    @property
    def active(self) -> bool:
        return self.status in (PENDING, RUNNING)

    @property
    def total_cells(self) -> int:
        return len(self.tickers) * len(self.dates)

    @property
    def log_path(self) -> Path:
        return backtest_dir(self.config) / self.run_id / "trading_memory.md"

    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started if self.started else 0.0

    def cells_logged(self) -> int:
        return len(TradingMemoryLog({"memory_log_path": str(self.log_path)}).load_entries())

    def cancel(self) -> None:
        """Start no further cell; the running one finishes and is logged."""
        self._cancel.set()

    @property
    def stopping(self) -> bool:
        return self._cancel.is_set() and self.active

    def start(self) -> BacktestJob:
        self.started = time.time()
        # Cells a resumed sweep already had; the time-left estimate counts only new ones.
        self.initial_logged = self.cells_logged()
        self.status = RUNNING
        self._thread = threading.Thread(target=self._run, name=self.id, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.result = run_backtest(
                self.tickers, self.dates, self.config, asset_type=self.asset_type,
                portfolio=self.portfolio, selected_analysts=self.analysts, run_id=self.run_id,
                on_cell=self._on_cell, should_stop=self._cancel.is_set,
            )
            self.status = CANCELLED if self.result.stopped else DONE
        except Exception as exc:
            log.exception("Backtest %s failed", self.id)
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = FAILED
        finally:
            self.current = None
            self.finished = time.time()

    def _on_cell(self, ticker: str, date: str) -> None:
        self.current = (ticker, date)


class JobRegistry:
    """Every run started from this UI server, newest first."""

    def __init__(self):
        self._jobs: dict[str, object] = {}
        self._lock = threading.Lock()

    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id):
        return self._jobs.get(job_id)

    def of_type(self, cls) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if isinstance(j, cls)]
        return sorted(jobs, key=lambda j: j.started, reverse=True)


class QueueFull(ValueError):
    pass


class AnalysisQueue:
    """Analyses run one at a time, in the order they were queued.

    A run started from the Analyze page starts at once, as before; runs queued
    together (the Screens page's "Analyze with agents") wait here, so ten picks
    cost one run's worth of concurrency, not ten. One worker thread takes the
    next run when the last ends, and exits when the queue is empty. A queued run
    can be taken off before it starts; the running one stops after its current
    graph step, like any run.
    """

    def __init__(self, registry: JobRegistry, max_waiting: int = 10, history: int = 50):
        self.registry = registry
        self.max_waiting = max_waiting
        self._waiting: list[AnalysisJob] = []
        self._order: list[str] = []  # every queued run's id, oldest first (the last ``history``)
        self._history = history
        self.current: AnalysisJob | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def submit(self, jobs: list[AnalysisJob]) -> list[str]:
        with self._lock:
            if len(self._waiting) + len(jobs) > self.max_waiting:
                room = self.max_waiting - len(self._waiting)
                raise QueueFull(f"The queue holds at most {self.max_waiting} waiting runs; "
                                f"{room} more can be added now.")
            for job in jobs:
                job.status = PENDING
                job.mark_submitted()
                self.registry.add(job)
                self._waiting.append(job)
                self._order.append(job.id)
            del self._order[:-self._history]
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, name="analysis-queue", daemon=True)
                self._worker.start()
        return [j.id for j in jobs]

    def cancel(self, job_id: str) -> bool:
        """Take a waiting run off the queue, or stop the running one. False if neither."""
        with self._lock:
            job = next((j for j in self._waiting if j.id == job_id), None)
            if job is not None:
                self._waiting.remove(job)
            running = self.current if self.current is not None and self.current.id == job_id else None
        if job is not None:
            job.drop("Taken off the queue before it started")
            return True
        if running is not None:
            running.cancel()
            return True
        return False

    def cancel_all(self) -> int:
        with self._lock:
            waiting, self._waiting = self._waiting, []
            running = self.current
        for job in waiting:
            job.drop("Taken off the queue before it started")
        if running is not None:
            running.cancel()
        return len(waiting) + (running is not None)

    def holds(self, job_id: str) -> bool:
        with self._lock:
            return any(j.id == job_id for j in self._waiting)

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._waiting:
                    self.current = None
                    self._worker = None
                    return
                job = self._waiting.pop(0)
                self.current = job
            if job._cancel.is_set():  # stopped in the moment between leaving the queue and starting
                job.drop("Stopped before it started")
                continue
            job.start()
            job.wait()

    def status(self) -> dict:
        with self._lock:
            ids = list(self._order)
            waiting = [j.id for j in self._waiting]
            current = self.current.id if self.current is not None else None
        runs = []
        for job_id in ids:
            job = self.registry.get(job_id)
            if isinstance(job, AnalysisJob):
                runs.append({"id": job.id, "ticker": job.ticker, "date": job.trade_date, "status": job.status,
                             "rating": job.rating, "elapsed": job.elapsed(),
                             "position": waiting.index(job.id) + 1 if job.id in waiting else None})
        return {"current": current, "waiting": len(waiting), "max": self.max_waiting, "runs": runs}


# --- History on disk -------------------------------------------------------

@dataclass
class SavedReport:
    path: Path
    ticker: str
    trade_date: str | None
    modified: datetime
    # "report" (complete_report.md), "partial" (partial_report.md: a stopped
    # run's finished sections, with no rating) or "state" (full_states_log JSON)
    kind: str

    @property
    def judgments_path(self) -> Path | None:
        """The Sentiment Analyst's saved Jev judgments, when the run made them."""
        if self.kind not in ("report", "partial"):
            return None
        path = self.path.parent / "1_analysts" / "sentiment_judgments.json"
        return path if path.exists() else None

    @property
    def label(self) -> str:
        when = self.trade_date or self.modified.strftime("%Y-%m-%d %H:%M")
        return f"{self.ticker} · {when}"


def backtest_dir(config: dict) -> Path:
    return Path(config["results_dir"]) / "backtest"


def list_saved_reports(results_dir) -> list[SavedReport]:
    """Report trees and state logs under ``results_dir``, newest first."""
    root = Path(results_dir)
    if not root.exists():
        return []
    found = []
    for name, kind in (("complete_report.md", "report"), ("partial_report.md", "partial")):
        for path in root.rglob(name):
            ticker, trade_date = _report_header(path)
            found.append(SavedReport(path, ticker, trade_date, _mtime(path), kind))
    for path in root.rglob("full_states_log_*.json"):
        # <results>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json
        found.append(SavedReport(path, path.parent.parent.name,
                                 path.stem.removeprefix("full_states_log_"), _mtime(path), "state"))
    return sorted(found, key=lambda r: r.modified, reverse=True)


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime)


def _report_header(path: Path) -> tuple[str, str | None]:
    """(ticker, analysis date) from a complete report's header.

    Reports written before the header carried the date give None for it, and a
    report with no readable header falls back to its folder name for the ticker.
    """
    ticker, trade_date = path.parent.name.removesuffix("_partial").rsplit("_", 2)[0], None
    try:
        with path.open(encoding="utf-8") as f:
            head = [next(f, "") for _ in range(12)]
    except OSError:
        return ticker, None
    for line in head:
        line = line.removeprefix("- ")  # the header lists what produced the run
        if line.startswith(("# Trading Analysis Report:", "# Partial Research Report (incomplete):")):
            ticker = line.split(":", 1)[1].strip()
        elif line.startswith("Analysis date:"):
            trade_date = line.split(":", 1)[1].strip() or None
    return ticker, trade_date


def load_report_sections(report: SavedReport) -> list[tuple[str, str]]:
    """(title, markdown) sections of a saved report, in pipeline order."""
    if report.kind in ("report", "partial"):
        text = report.path.read_text(encoding="utf-8")
        heads = list(_SECTION_RE.finditer(text))
        # A section ends at the next level-2 heading of any kind, so the Sources
        # appendix after section V (docs/adr/0003) is never read as part of it.
        starts = [m.start() for m in re.finditer(r"^## ", text, re.MULTILINE)]
        ends = [min((s for s in starts if s > h.start()), default=len(text)) for h in heads]
        return [(h.group(1).strip(), text[h.end():end].strip())
                for h, end in zip(heads, ends, strict=True)]
    state = json.loads(report.path.read_text(encoding="utf-8"))
    # Logs written before 0.6 name the Trader's plan trader_investment_decision.
    state.setdefault("trader_investment_plan", state.get("trader_investment_decision"))
    return state_sections(state)


def state_sections(state: dict) -> list[tuple[str, str]]:
    """Group a final state into the five report sections the CLI prints."""
    def join(parts):
        return "\n\n".join(f"### {name}\n{text}" for name, text in parts if text)

    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    sections = [
        ("I. Analyst Team Reports", join([
            ("Market Analyst", state.get("market_report")),
            ("Sentiment Analyst", state.get("sentiment_report")),
            ("News Analyst", state.get("news_report")),
            ("Fundamentals Analyst", state.get("fundamentals_report")),
        ])),
        ("II. Research Team Decision", join([
            ("Bull Researcher", debate.get("bull_history")),
            ("Bear Researcher", debate.get("bear_history")),
            # Logs written before 0.6 keep the decisions in the debate states.
            ("Research Manager", state.get("investment_plan") or debate.get("judge_decision")),
        ])),
        ("III. Trading Team Plan", join([("Trader", state.get("trader_investment_plan"))])),
        ("IV. Risk Management Team Decision", join([
            ("Aggressive Analyst", risk.get("aggressive_history")),
            ("Conservative Analyst", risk.get("conservative_history")),
            ("Neutral Analyst", risk.get("neutral_history")),
        ])),
        ("V. Portfolio Manager Decision", join([
            ("Portfolio Manager", state.get("final_trade_decision") or risk.get("judge_decision")),
        ])),
    ]
    return [(title, body) for title, body in sections if body]


def report_rating(sections: list[tuple[str, str]]) -> str:
    """The Portfolio Manager's rating in a saved report, or REVIEW when unreadable."""
    body = next((b for t, b in sections if t.startswith("V.")), "")
    return parse_rating(body)


def load_judgments(report: SavedReport) -> dict | None:
    path = report.judgments_path
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_backtests(config: dict) -> list[Path]:
    """Backtest run directories that have a decision log, newest first."""
    root = backtest_dir(config)
    if not root.exists():
        return []
    runs = [p for p in root.iterdir() if (p / "trading_memory.md").exists()]
    return sorted(runs, key=lambda p: (p / "trading_memory.md").stat().st_mtime, reverse=True)


def load_decisions(log_path) -> list[dict]:
    """Rows of a decision log, with alpha and raw return as fractions."""
    rows = []
    for e in TradingMemoryLog({"memory_log_path": str(log_path)}).load_entries():
        rows.append({
            "date": e["date"],
            "ticker": e["ticker"],
            "rating": e["rating"],
            "status": "pending" if e["pending"] else "settled",
            "return": _pct(e.get("raw")),
            "alpha": _pct(e.get("alpha")),
            "holding": e.get("holding"),
            "decision": e.get("decision", ""),
            "reflection": e.get("reflection", ""),
        })
    return rows


def backtest_summary(log_path):
    return summarize(TradingMemoryLog({"memory_log_path": str(log_path)}))


def _pct(text) -> float | None:
    try:
        return float(str(text).strip().rstrip("%")) / 100
    except (TypeError, ValueError):
        return None
