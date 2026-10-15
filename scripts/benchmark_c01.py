"""C01: replay the real graph offline, plus controlled trace attribution cases.

Run from the checkout with ``python scripts/benchmark_c01.py``. Every response
is synthetic and outbound network calls are refused. No paid model is invoked.
Raw traces, semantic outputs and graph exports are retained in results/benchmarks.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from contextvars import copy_context
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "c01" / "workload.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_fixture(path=FIXTURE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextmanager
def offline_network():
    """Refuse both Python socket clients and yfinance's libcurl transport."""
    def refused(*args, **kwargs):
        raise OSError("C01 offline fixture refused an outbound network request")

    with ExitStack() as stack:
        for name in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, name, refused))
        stack.enter_context(patch.object(socket, "getaddrinfo", refused))
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(curl_requests.Session, "request", refused))
            stack.enter_context(patch.object(curl_requests.AsyncSession, "request", refused))
        yield


class FixtureSource:
    """Cache and optional coalescing exist only in this benchmark fixture."""

    def __init__(self, fixture, trace, cache=None, coalesce=False):
        self.fixture = fixture
        self.trace = trace
        self.cache = copy.deepcopy(cache or {})
        self.coalesce = coalesce
        self.lock = threading.Lock()
        self.pending: dict[str, Future] = {}
        self.metrics = {"requests": 0, "fetches": 0, "cache_hits": 0, "coalesced": 0,
                        "retries": 0, "rate_limits": 0, "missing": 0}

    def _count(self, name):
        with self.lock:
            self.metrics[name] += 1

    def fetch(self, method, *args, **kwargs):
        # Invocation arguments are hashed rather than written to trace attributes.
        key = fingerprint({"method": method, "args": args, "kwargs": kwargs})
        self._count("requests")
        with self.trace.scope("fixture_request", method, {"operation": method}):
            with self.lock:
                hit = key in self.cache
                value = self.cache.get(key)
                if hit:
                    self.metrics["cache_hits"] += 1
                pending = self.pending.get(key) if self.coalesce and not hit else None
                owner = pending is None
                if self.coalesce and not hit and owner:
                    pending = Future()
                    self.pending[key] = pending
                if not owner:
                    self.metrics["coalesced"] += 1
            if hit:
                self.trace.event("fixture_cache", {"cache_status": "hit", "operation": method})
                return value
            if not owner:
                with self.trace.scope("queue_wait", "fixture_coalesced_wait"):
                    return pending.result(timeout=5)
            try:
                with self.trace.scope("source_fetch", method,
                                      {"source": "c01_fixture", "cache_status": "miss"}):
                    self._count("fetches")
                    time.sleep(self.fixture["latencies_ms"]["source"] / 1000)
                    value = self.fixture["source_answer"].format(method=method)
                with self.lock:
                    self.cache[key] = value
                if pending is not None:
                    pending.set_result(value)
                return value
            except BaseException as exc:
                if pending is not None:
                    pending.set_exception(exc)
                raise


def _scripted_model(fixture):
    # Lazy imports keep provenance/aggregation helpers usable without graph deps.
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field

    class ScriptedModel(BaseChatModel):
        tools: tuple = ()
        calls: list = Field(default_factory=list)

        @property
        def _llm_type(self):
            return "c01-scripted"

        def bind_tools(self, tools, **kwargs):
            return self.model_copy(update={"tools": tuple(tools)})

        def with_structured_output(self, schema, **kwargs):
            raise NotImplementedError

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls.append(1)
            time.sleep(fixture["latencies_ms"]["model"] / 1000)
            if self.tools and not isinstance(messages[-1], ToolMessage):
                workload = fixture["workload"]
                args = {"symbol": workload["symbol"], "ticker": workload["symbol"],
                        "curr_date": workload["trade_date"], "start_date": "2026-01-02",
                        "end_date": workload["trade_date"], "indicator": "rsi",
                        "topic": "Synthetic macro", "freq": "quarterly"}
                calls = [{"name": tool.name, "id": f"fixture_call_{i}",
                          "args": {k: v for k, v in args.items()
                                   if k in tool.tool_call_schema.model_json_schema()["properties"]}}
                         for i, tool in enumerate(self.tools)]
                message = AIMessage(content="", tool_calls=calls)
            else:
                message = AIMessage(content=fixture["model_answer"])
            return ChatResult(generations=[ChatGeneration(message=message)])

    return ScriptedModel()


def graph_case(fixture, trace, directory, cache=None):
    """Run TradingAgentsGraph itself; replace external boundaries only."""
    import pandas as pd

    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.agents import context
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot
    from tradingagents.default_config import _ENV_OVERRIDES, build_default_config
    from tradingagents.graph import trading_graph

    source = FixtureSource(fixture, trace, cache)
    model = _scripted_model(fixture)
    stats = StatsCallbackHandler()
    # Rebuild built-in settings without a developer's TRADINGAGENTS_* overlay.
    environment = {name: "" for name in os.environ if name.startswith("TRADINGAGENTS_")}
    environment.update(dict.fromkeys(_ENV_OVERRIDES, ""))
    with patch.dict(os.environ, environment):
        config = build_default_config()
    config.update(copy.deepcopy(fixture["configuration"]))
    config.update(results_dir=str(directory / "graph_results"),
                  data_cache_dir=str(directory / "application_cache"),
                  memory_log_path=str(directory / "memory.md"),
                  india_db_path=str(directory / "unused_india.db"),
                  screener_db_path=str(directory / "unused_screener.db"))
    prices = fixture["ohlcv"]
    frame = pd.DataFrame({
        "Date": pd.bdate_range(end=fixture["workload"]["trade_date"],
                              periods=prices["business_days"]),
        "Open": prices["open"], "High": prices["high"], "Low": prices["low"],
        "Close": prices["close"], "Volume": prices["volume"],
    })

    class Client:
        def get_llm(self):
            return model

    with ExitStack() as patches:
        for method, vendors in router.VENDOR_METHODS.items():
            for vendor in vendors:
                def answer(*args, _method=method, **kwargs):
                    return source.fetch(_method, *args, **kwargs)
                patches.enter_context(patch.dict(vendors, {vendor: answer}))

        def ohlcv(*args, **kwargs):
            source.fetch("ohlcv", *args, **kwargs)
            return frame.copy()

        patches.enter_context(patch.object(snapshot, "load_ohlcv", ohlcv))
        for name in ("fetch_stocktwits_messages", "fetch_reddit_posts"):
            patches.enter_context(patch.object(sentiment_analyst, name,
                                              lambda *a, **k: fixture["social_answer"]))
        patches.enter_context(patch.object(yahoo_market.yf, "Ticker",
                                           lambda _: type("Ticker", (), {
                                               "info": {"longName": "Synthetic C01 Company"}})()))
        patches.enter_context(patch.object(trading_graph, "create_tier_client",
                                           lambda *a, **k: Client()))
        context._identity.cache_clear()
        try:
            with trace.scope("setup", "fixture_graph_initialization"):
                graph = trading_graph.TradingAgentsGraph(
                    selected_analysts=fixture["workload"]["analysts"], config=config,
                    callbacks=[stats])
            state, rating = graph.propagate(fixture["workload"]["symbol"],
                                          fixture["workload"]["trade_date"])
            trace.milestone("graph_returned")
            graph.save_reports(state, fixture["workload"]["symbol"], directory / "reports")
        finally:
            context._identity.cache_clear()
    keys = ("market_report", "sentiment_report", "news_report", "fundamentals_report",
            "investment_plan", "trader_investment_plan", "final_trade_decision")
    output = {"rating": rating, "reports": {key: state[key] for key in keys}}
    metrics = {**source.metrics, "scripted_model_calls": len(model.calls),
               "callback_stats": stats.get_stats()}
    # Local paths are normalized so the captured complete configuration can be matched.
    captured = dict(config)
    for key in ("results_dir", "data_cache_dir", "memory_log_path", "india_db_path",
                "screener_db_path"):
        captured[key] = f"<run_directory>/{key}"
    write_json(directory / "configuration.json", captured)
    return output, metrics, source.cache, captured


class FixtureCancelled(Exception):
    def __init__(self, output, metrics):
        super().__init__("Cancelled at the deterministic fixture boundary")
        self.output = output
        self.metrics = metrics


def controlled_case(case, mode, fixture, trace):
    """Isolated scenarios test attribution, not application optimizations."""
    source = FixtureSource(fixture, trace, coalesce=mode == "coalesced")
    output = {}
    if case == "duplicate_requests":
        count = fixture["controls"]["duplicate_requests"]
        barrier = threading.Barrier(count)

        def request():
            barrier.wait(timeout=5)
            return source.fetch("duplicate", "same_input")

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(copy_context().run, request) for _ in range(count)]
            output["responses"] = [future.result(timeout=5) for future in futures]
    elif case == "slow_source":
        with trace.scope("queue_wait", "fixture_queue"):
            time.sleep(fixture["latencies_ms"]["queue"] / 1000)
        with trace.scope("source_fetch", "fixture_slow_source", {"source": "c01_fixture"}):
            time.sleep(fixture["latencies_ms"]["slow_source"] / 1000)
        source._count("fetches")
        with trace.scope("model_inference", "fixture_model"):
            time.sleep(fixture["latencies_ms"]["model"] / 1000)
        output["answer"] = fixture["model_answer"]
        trace.milestone("generated_report")
    elif case == "rate_limit":
        for attempt in range(fixture["controls"]["rate_limit_attempts"]):
            with trace.scope("source_fetch", "fixture_rate_limited_source",
                             {"attempt": attempt + 1, "http_status": 429,
                              "source": "c01_fixture"}):
                source._count("fetches")
                source._count("rate_limits")
                trace.event("fixture_http_response", {"http_status": 429})
            source._count("retries")
            with trace.scope("retry_backoff", "fixture_retry", {"attempt": attempt + 1}):
                time.sleep(fixture["latencies_ms"]["retry_backoff"] / 1000)
        output["answer"] = source.fetch("recovered_source")
    elif case == "missing_data":
        with trace.scope("source_fetch", "fixture_missing_source", {"source": "c01_fixture"}):
            source._count("fetches")
            source._count("missing")
            trace.event("fixture_missing_data", {"source": "fixture_missing_source"})
        output = {"answer": None, "limitation": "Synthetic source has no data", "rating": "REVIEW"}
    elif case == "cancellation":
        output["partial_evidence"] = source.fetch("before_cancel")
        trace.event("cancellation_requested", {"source": "fixture"})
        with trace.scope("cancellation", "fixture_cancelled_before_model"):
            raise FixtureCancelled({**output, "cancelled": True, "rating": None},
                                   {**source.metrics, "model_calls_after_cancel": 0})
    else:
        raise ValueError(f"Unknown C01 controlled case: {case}")
    trace.milestone("evidence_available")
    return output, source.metrics


def phase_durations(snapshot):
    """Inclusive span durations; parallel/nested totals must not be added."""
    grouped = defaultdict(list)
    for span in snapshot.get("spans", []):
        if span.get("duration_ms") is not None:
            grouped[span["kind"]].append(span["duration_ms"])
    return {kind: {"count": len(values), "inclusive_total_ms": round(sum(values), 3),
                   "max_span_ms": max(values)}
            for kind, values in sorted(grouped.items())}


def run_record(case, mode, repetition, fixture, directory, cache=None):
    from tradingagents.telemetry import RunTrace, use_trace

    run_id = f"{case}-{mode}-{repetition:02d}"
    run_dir = directory / run_id
    # Avoid overwriting prior evidence, or reading a prior benchmark's state.
    run_dir.mkdir(parents=True, exist_ok=False)
    trace = RunTrace(run_id=run_id, configuration=fixture["configuration"],
                     workload=fixture["workload"])
    trace.gap("ui_render_unobserved")
    output, metrics, warmed, configuration = {}, {}, {}, fixture["configuration"]
    status = "completed"
    started = time.monotonic()
    try:
        with use_trace(trace), trace.scope("benchmark", case, {"regime": mode, "fixture": True}):
            if case == "graph":
                output, metrics, warmed, configuration = graph_case(fixture, trace, run_dir, cache)
            else:
                output, metrics = controlled_case(case, mode, fixture, trace)
    except FixtureCancelled as exc:
        status = "cancelled"
        output, metrics = exc.output, exc.metrics
    except Exception as exc:
        status = "failed"
        output = {"error_type": type(exc).__name__}
        write_json(run_dir / "failure.json", output)
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    wall_ms = (time.monotonic() - started) * 1000
    trace.finish(status)
    trace.persist(run_dir / "trace.json")
    write_json(run_dir / "output.json", output)
    snapshot = trace.snapshot()
    milestones = snapshot.get("milestones", {})
    available = next((milestones[key]["offset_ms"] for key in
                      ("generated_report", "graph_returned")
                      if key in milestones), None)
    record = {"run_id": run_id, "case": case, "mode": mode, "repetition": repetition,
              "status": status, "wall_ms": round(wall_ms, 3),
              "available_report_ms": available, "first_useful_result_ms": None,
              "available_evidence_ms": milestones.get("evidence_available", {}).get("offset_ms"),
              "render_observation": "unobserved",
              "output_sha256": fingerprint(output), "configuration_sha256": fingerprint(configuration),
              "workload_sha256": fingerprint(fixture["workload"]),
              "trace_path": str((run_dir / "trace.json").relative_to(directory)),
              "output_path": str((run_dir / "output.json").relative_to(directory)),
              "phase_durations": phase_durations(snapshot), "metrics": metrics}
    return record, warmed


def summarize_runs(runs):
    grouped = defaultdict(list)
    for run in runs:
        grouped[(run["case"], run["mode"])].append(run)
    groups = []
    for (case, mode), members in sorted(grouped.items()):
        walls = sorted(run["wall_ms"] for run in members)
        available = [run["available_report_ms"] for run in members
                     if run["available_report_ms"] is not None]
        groups.append({"case": case, "mode": mode, "n": len(members),
                       "statuses": {status: sum(m["status"] == status for m in members)
                                    for status in sorted({m["status"] for m in members})},
                       "median_wall_ms": round(statistics.median(walls), 3),
                       "p95_wall_ms": walls[max(0, math.ceil(0.95 * len(walls)) - 1)],
                       "p95_method": "nearest_rank; exploratory with small n",
                       "median_available_report_ms": round(statistics.median(available), 3)
                       if available else None,
                       "first_useful_result_ms": None,
                       "output_hashes": sorted({m["output_sha256"] for m in members}),
                       "median_inclusive_phase_ms": {
                           kind: round(statistics.median(
                               m.get("phase_durations", {}).get(kind, {}).get("inclusive_total_ms", 0)
                               for m in members), 3)
                           for kind in sorted({kind for m in members
                                               for kind in m.get("phase_durations", {})})}})
    return groups


def provenance():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

    revision = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain")
    files = sorted(path for base in ("tradingagents", "cli", "scripts")
                   for path in (ROOT / base).rglob("*.py") if "__pycache__" not in path.parts)
    source_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in files}
    processor = platform.processor() or None
    memory_bytes = None
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        processor = next((line.split(":", 1)[1].strip()
                          for line in cpuinfo.read_text().splitlines()
                          if line.startswith("model name")), processor)
    if hasattr(os, "sysconf"):
        with suppress(OSError, ValueError):
            memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {"source_revision": revision.decode().strip() if revision else None,
            "source_dirty": bool(dirty) if dirty is not None else None,
            "source_tree_sha256": fingerprint(source_hashes), "source_files_sha256": source_hashes,
            "dependency_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
            if (ROOT / "uv.lock").exists() else None,
            "hardware": {"platform": platform.platform(), "machine": platform.machine(),
                         "processor": processor, "logical_cpus": os.cpu_count(),
                         "python": platform.python_version(), "gpu": "unobserved",
                         "memory_bytes": memory_bytes,
                         "memory_scope": "execution_environment"}}


def benchmark(directory, repetitions=3, fixture_path=FIXTURE):
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    fixture = load_fixture(fixture_path)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = provenance()
    runs = []
    with offline_network():
        for repetition in range(1, repetitions + 1):
            cold, cache = run_record("graph", "cold_fixture_cache", repetition, fixture, directory)
            runs.append(cold)
            warm, _ = run_record("graph", "warm_fixture_cache", repetition, fixture, directory, cache)
            runs.append(warm)
        for case, modes in (("duplicate_requests", ("uncoalesced", "coalesced")),
                            ("slow_source", ("controlled",)), ("rate_limit", ("controlled",)),
                            ("missing_data", ("controlled",)), ("cancellation", ("controlled",))):
            for mode in modes:
                for repetition in range(1, repetitions + 1):
                    run, _ = run_record(case, mode, repetition, fixture, directory)
                    runs.append(run)
    summary = {"schema_version": 1, "benchmark_id": "C01", "measurement_kind": "offline_fixture",
               "created_at_utc": datetime.now(UTC).isoformat(), **metadata,
               "fixture_sha256": hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest(),
               "configuration": fixture["configuration"], "workload": fixture["workload"],
               "configuration_sha256": fingerprint(fixture["configuration"]),
               "workload_sha256": fingerprint(fixture["workload"]),
               "repetitions": repetitions, "runs": runs, "groups": summarize_runs(runs),
               "interpretation": "Synthetic fixture timings; no claim about live speed or quality.",
               "phase_duration_semantics": "Inclusive durations; nested and parallel phases overlap. Do not sum them as wall time.",
               "cold_warm_scope": "Fixture vendor response cache only; fresh graph/model/memory for each run.",
               "first_useful_result_ms": None, "render_observation": "unobserved",
               "live_baseline": {"status": "not_ready_for_measurement",
                                 "executed_by_this_runner": False}}
    write_json(directory / "summary.json", summary)
    lines = ["# C01 offline fixture benchmark", "", summary["interpretation"], "",
             "UI render and first useful result are unobserved. Available report measures generated evidence only.",
             "", "| Case | Mode | n | Median ms | p95 ms |", "|---|---|---:|---:|---:|"]
    lines += [f"| {g['case']} | {g['mode']} | {g['n']} | {g['median_wall_ms']:.3f} | {g['p95_wall_ms']:.3f} |"
              for g in summary["groups"]]
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "results" / "benchmarks" / "c01" /
                        datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args(argv)
    summary = benchmark(args.output_dir, args.repetitions, args.fixture)
    failed = sum(run["status"] == "failed" for run in summary["runs"])
    print(f"C01 offline fixture: {len(summary['runs'])} runs, {failed} failures; "
          f"summary: {args.output_dir.resolve() / 'summary.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
