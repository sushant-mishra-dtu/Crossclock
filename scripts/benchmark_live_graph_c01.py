"""Registered C01 live graph sample on the frozen CC0 synthetic source corpus.

Execution requires --execute. The graph and its default round/tool caps remain
unchanged; only external source/model boundaries and per-run file locations are
configured. Each model send is guarded before it can reach the fixed endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import os
import statistics
import sys
import threading
import time
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_c01 import (  # noqa: E402 - script execution needs the checkout on sys.path
    FIXTURE,
    FixtureSource,
    fingerprint,
    load_fixture,
    provenance,
    write_json,
)

NVIDIA_MODEL = "nvidia/nemotron-3-super-120b-a12b"
JEV_MODEL = "jev-1.13.0"
NVIDIA_MAX_ATTEMPTS = 96
JEV_CAP_USD = Decimal("0.98")
JEV_MAX_INPUT_TOKENS = 65_536
JEV_INPUT_USD_PER_MILLION = Decimal("0.042")
JEV_ATTEMPT_RESERVATION = Decimal(JEV_MAX_INPUT_TOKENS) * JEV_INPUT_USD_PER_MILLION / Decimal(1_000_000)
MODEL_BODY_OVERRIDE = {"chat_template_kwargs": {"enable_thinking": False}}
ANALYST_NODES = frozenset({"Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"})
REPORT_KEYS = ("market_report", "sentiment_report", "news_report", "fundamentals_report",
               "investment_plan", "trader_investment_plan", "final_trade_decision")


class SendGuardStopped(RuntimeError):
    """A fixed destination, attempt limit, or shared cost reservation was refused."""


class SendBudget:
    """Thread-safe reservation that includes failed sends and never refunds them."""

    def __init__(self, stop_event, max_attempts, cost_per_attempt=Decimal("0"), cap_usd=None):
        self.stop_event = stop_event
        self.max_attempts = max_attempts
        self.cost_per_attempt = Decimal(str(cost_per_attempt))
        self.cap_usd = Decimal(str(cap_usd)) if cap_usd is not None else None
        if not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if not self.cost_per_attempt.is_finite() or self.cost_per_attempt < 0:
            raise ValueError("reservation must be finite and non-negative")
        if self.cap_usd is not None and (not self.cap_usd.is_finite() or self.cap_usd <= 0):
            raise ValueError("cap must be finite and positive")
        self.lock = threading.Lock()
        self.attempts = 0
        self.reserved_usd = Decimal("0")
        self.reason = None

    def reserve(self):
        with self.lock:
            if self.stop_event.is_set():
                raise SendGuardStopped("shared benchmark cancellation is set")
            next_cost = self.reserved_usd + self.cost_per_attempt
            if self.attempts >= self.max_attempts or (self.cap_usd is not None and next_cost > self.cap_usd):
                self.reason = "send_budget_exhausted"
                self.stop_event.set()
                raise SendGuardStopped("send budget exhausted before HTTP send")
            self.attempts += 1
            self.reserved_usd = next_cost

    def stop(self, reason):
        with self.lock:
            self.reason = reason
            self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return {"http_attempts": self.attempts, "max_attempts": self.max_attempts,
                    "reserved_worst_case_usd": float(self.reserved_usd),
                    "cap_usd": float(self.cap_usd) if self.cap_usd is not None else None,
                    "stopped": self.stop_event.is_set(), "stop_reason": self.reason}


def check_destination(request, host, path):
    url = request.url
    if (request.method != "POST" or url.scheme != "https" or url.host != host or url.path != path
            or url.port not in (None, 443) or url.query or url.username or url.password):
        raise SendGuardStopped("unexpected benchmark destination refused")


def guarded_transport(http_module, budget, host, path, provider, model):
    """Build an SDK-compatible transport; no response bodies enter trace data."""
    class GuardedTransport(http_module.BaseTransport):
        def __init__(self):
            self.inner = http_module.HTTPTransport(retries=0)

        def handle_request(self, request):
            from tradingagents.llm_clients.telemetry import mark_transport_not_sent
            try:
                check_destination(request, host, path)
            except SendGuardStopped:
                budget.stop("unexpected_destination")
                mark_transport_not_sent(request, "unexpected_destination")
                raise
            try:
                budget.reserve()
            except SendGuardStopped:
                mark_transport_not_sent(request, "send_budget_or_cancellation")
                raise
            from tradingagents.telemetry import current_trace
            trace = current_trace()
            try:
                if trace is None:
                    response = self.inner.handle_request(request)
                    response.read()
                else:
                    with trace.scope("budgeted_transport", "guarded_model_send", {
                        "provider": provider, "model": model, "coverage": "response_body",
                    }) as span:
                        response = self.inner.handle_request(request)
                        response.read()
                        trace.end_span(span, "ok" if response.status_code < 400 else "error",
                                       {"http_status": response.status_code})
            except Exception:
                budget.stop("transport_failure")
                raise
            # Supplying the application's cancel_event prevents its slow outer
            # retry layer from waiting after a throttle/quota/auth failure.
            if response.status_code in (401, 402, 403, 429):
                budget.stop(f"http_{response.status_code}")
            return response

        def close(self):
            self.inner.close()

    return GuardedTransport()


def registered_config(fixture, run_directory):
    from dotenv import dotenv_values, find_dotenv
    names = set(os.environ)
    for filename in (".env", ".env.enterprise"):
        names.update(dotenv_values(find_dotenv(filename, usecwd=True)))
    blank = {name: "" for name in names if name.startswith("TRADINGAGENTS_")}
    with patch.dict(os.environ, blank):
        # Blank settings before the first package import: __init__ loads env
        # files, and malformed/local settings must not affect registration.
        from tradingagents.default_config import _ENV_OVERRIDES, build_default_config
        with patch.dict(os.environ, dict.fromkeys(_ENV_OVERRIDES, "")):
            config = build_default_config()
    # Retain default tool/debate/risk caps. The offline fixture's abbreviated
    # graph configuration is not a live baseline setting.
    config.update(llm_provider="nvidia", quick_think_provider="nvidia", deep_think_provider="nvidia",
                  quick_think_llm=NVIDIA_MODEL, deep_think_llm=NVIDIA_MODEL,
                  backend_url="https://integrate.api.nvidia.com/v1",
                  quick_think_backend_url=None, deep_think_backend_url=None,
                  temperature=1.0, max_tokens=2048, llm_max_retries=0,
                  checkpoint_enabled=False, jev_enabled=True, jev_model=JEV_MODEL,
                  output_language="English")
    config["data_vendors"] = copy.deepcopy(fixture["configuration"]["data_vendors"])
    config["tool_vendors"] = copy.deepcopy(fixture["configuration"].get("tool_vendors", {}))
    for key, filename in (("results_dir", "graph_results"), ("data_cache_dir", "application_cache"),
                          ("memory_log_path", "memory.md"), ("india_db_path", "unused_india.db"),
                          ("screener_db_path", "unused_screener.db")):
        config[key] = str(run_directory / filename)
    return config


def normalized_config(config):
    result = dict(config)
    for key in ("results_dir", "data_cache_dir", "memory_log_path", "india_db_path", "screener_db_path"):
        result[key] = f"<run_directory>/{key}"
    result["model_extra_body"] = copy.deepcopy(MODEL_BODY_OVERRIDE)
    result["llm_request_timeout_seconds"] = 60
    result["jev_request_timeout_seconds"] = 30
    result["jev_sdk_max_retries"] = 0
    return result


@contextmanager
def synthetic_boundaries(fixture, source):
    """Freeze every external data boundary used by the production stock graph."""
    import pandas as pd
    import requests

    from tradingagents.agents import context
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import router
    from tradingagents.dataflows.feed import Feed
    from tradingagents.dataflows.vendors.yahoo import snapshot

    prices = fixture["ohlcv"]
    frame = pd.DataFrame({"Date": pd.bdate_range(end=fixture["workload"]["trade_date"],
        periods=prices["business_days"]), "Open": prices["open"], "High": prices["high"],
        "Low": prices["low"], "Close": prices["close"], "Volume": prices["volume"]})

    def refused(*args, **kwargs):
        raise SendGuardStopped("external data network is outside the registered corpus")

    with ExitStack() as patches:
        patches.enter_context(patch.object(requests.sessions.Session, "request", refused))
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            pass
        else:
            patches.enter_context(patch.object(curl_requests.Session, "request", refused))
            patches.enter_context(patch.object(curl_requests.AsyncSession, "request", refused))
        for method, vendors in router.VENDOR_METHODS.items():
            for vendor in vendors:
                def answer(*args, _method=method, **kwargs):
                    value = source.fetch(_method, *args, **kwargs)
                    return Feed(value) if _method == "get_news_feed" else value
                patches.enter_context(patch.dict(vendors, {vendor: answer}))

        def ohlcv(*args, **kwargs):
            source.fetch("ohlcv", *args, **kwargs)
            return frame.copy()

        def identity(*args, **kwargs):
            source.fetch("identity", *args, **kwargs)
            return {"longName": "Synthetic C01 Company", "quoteType": "EQUITY"}

        patches.enter_context(patch.object(snapshot, "load_ohlcv", ohlcv))
        patches.enter_context(patch.object(context, "get_company_profile", identity))
        for name in ("fetch_stocktwits_messages", "fetch_reddit_posts", "fetch_stocktwits_feed", "fetch_reddit_feed"):
            def social(*args, _name=name, **kwargs):
                source.fetch(_name, *args, **kwargs)
                return Feed(fixture["social_answer"]) if _name.endswith("_feed") else fixture["social_answer"]
            patches.enter_context(patch.object(sentiment_analyst, name, social))
        context._identity.cache_clear()
        try:
            yield
        finally:
            context._identity.cache_clear()


def graph_measurement(fixture, source, trace, directory, config, nvidia_budget, jev_budget, nvidia_key, jev_key):
    import httpx
    import httpx2
    import typesafe_sdk

    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.graph import trading_graph
    from tradingagents.llm_clients import factory
    from tradingagents.llm_clients.openai_client import cancel_event

    stats = StatsCallbackHandler(trace)
    actual_type_safe_client = typesafe_sdk.TypeSafeClient
    actual_tier_client = factory.create_tier_client

    def bounded_jev_client(*args, **kwargs):
        kwargs.update(api_key=jev_key, model=JEV_MODEL, base_url="https://api.typesafe.ai",
                      retry=typesafe_sdk.RetryPolicy(max_retries=0), timeout=30,
                      transport=guarded_transport(httpx2, jev_budget, "api.typesafe.ai", "/v1/systemone", "typesafe", JEV_MODEL))
        return actual_type_safe_client(*args, **kwargs)

    def refuse_async(request):
        from tradingagents.llm_clients.telemetry import mark_transport_not_sent
        nvidia_budget.stop("unregistered_async_model_send")
        mark_transport_not_sent(request, "unregistered_async_model_send")
        raise SendGuardStopped("unregistered async model send refused")

    with ExitStack() as resources:
        http = resources.enter_context(httpx.Client(timeout=60,
            transport=guarded_transport(httpx, nvidia_budget, "integrate.api.nvidia.com", "/v1/chat/completions", "nvidia", NVIDIA_MODEL)))
        async_http = httpx.AsyncClient(transport=httpx.MockTransport(refuse_async))
        resources.callback(lambda: asyncio.run(async_http.aclose()))

        def tier_client(active_config, tier, **extra):
            client = actual_tier_client(active_config, tier, api_key=nvidia_key,
                                        timeout=60, http_client=http, http_async_client=async_http, **extra)
            # extra_body is a supported ChatOpenAI field. Setting it on the
            # production model avoids changing general provider passthrough.
            original_get_llm = client.get_llm
            def get_llm():
                model = original_get_llm()
                model.extra_body = copy.deepcopy(MODEL_BODY_OVERRIDE)
                return model
            client.get_llm = get_llm
            return client

        resources.enter_context(patch.object(typesafe_sdk, "TypeSafeClient", bounded_jev_client))
        resources.enter_context(patch.object(trading_graph, "create_tier_client", tier_client))
        # jev_client checks this switch before constructing the SDK client;
        # the key lives only in the subprocess environment and model clients.
        resources.enter_context(patch.dict(os.environ, {"TYPESAFE_API_KEY": jev_key, "NVIDIA_API_KEY": nvidia_key}))
        token = cancel_event.set(nvidia_budget.stop_event)
        resources.callback(cancel_event.reset, token)
        with synthetic_boundaries(fixture, source):
            with trace.scope("setup", "live_graph_initialization"):
                graph = trading_graph.TradingAgentsGraph(selected_analysts=fixture["workload"]["analysts"],
                                                         config=config, callbacks=[stats])
            graph_json = graph.graph.get_graph().to_json()
            write_json(directory / "graph.json", graph_json)
            state, rating = graph.propagate(fixture["workload"]["symbol"], fixture["workload"]["trade_date"])
            trace.milestone("graph_returned")
            graph.save_reports(state, fixture["workload"]["symbol"], directory / "reports")
            return {"rating": rating, "reports": {key: state.get(key) for key in REPORT_KEYS}}, stats.get_stats()


def graph_node_paths(snapshot):
    spans = [span for span in snapshot["spans"] if span["kind"] == "graph_node"
             and span["duration_ms"] is not None]
    by_node = defaultdict(float)
    for span in spans:
        by_node[span["name"]] += span["duration_ms"]
    analyst = {name: duration for name, duration in by_node.items() if name in ANALYST_NODES}
    serialized = {name: duration for name, duration in by_node.items() if name not in ANALYST_NODES and name != "Memory Log"}
    return {"top_level_node_ms": dict(by_node), "parallel_analyst_max_path_ms": max(analyst.values(), default=None),
            "parallel_memory_path_ms": by_node.get("Memory Log"),
            "serialized_stage_ms": serialized,
            "semantics": "Per-node inclusive spans. Parallel analysts and memory overlap; do not add their durations."}


def measure_run(fixture, mode, repetition, directory, cache, nvidia_budget, jev_budget, keys):
    from tradingagents.telemetry import RunTrace, use_trace

    run_id = f"live-graph-{mode}-{repetition:02d}"
    run_dir = directory / run_id
    run_dir.mkdir(exist_ok=False)
    config = registered_config(fixture, run_dir)
    captured = normalized_config(config)
    trace = RunTrace(run_id=run_id, configuration=captured,
                     workload={**fixture["workload"], "case": "graph", "regime": mode, "repetition": repetition,
                               "corpus_sha256": fingerprint(fixture), "configuration_sha256": fingerprint(captured)})
    trace.gap("ui_render_unobserved")
    source = FixtureSource(fixture, trace, cache)
    before_nvidia, before_jev = nvidia_budget.snapshot(), jev_budget.snapshot()
    output, callback_stats = {}, {}
    status = "completed"
    started = time.monotonic()
    with use_trace(trace), trace.scope("benchmark", "live_graph", {"regime": mode, "fixture": True}):
        if nvidia_budget.stop_event.is_set():
            status = "not_started"
            output = {"error_type": "SendGuardStopped", "reason": "shared_budget_or_provider_cancellation"}
        else:
            try:
                output, callback_stats = graph_measurement(fixture, source, trace, run_dir, config,
                                                          nvidia_budget, jev_budget, *keys)
            except Exception as exc:
                status = "failed"
                output = {"error_type": type(exc).__name__}
                # Save safe failure metadata only: exception messages can contain
                # credential-bearing URLs or response content.
                write_json(run_dir / "failure.json", output)
    trace.finish(status)
    snapshot = trace.snapshot()
    jev_calls = [span for span in snapshot["spans"] if span["kind"] == "jev"]
    reported = [span["attributes"]["input_tokens"] for span in jev_calls
                if isinstance(span["attributes"].get("input_tokens"), (int, float))]
    estimate = sum(Decimal(str(tokens)) * JEV_INPUT_USD_PER_MILLION / Decimal(1_000_000) for tokens in reported)
    after_nvidia, after_jev = nvidia_budget.snapshot(), jev_budget.snapshot()
    milestones = snapshot["milestones"]
    record = {"run_id": run_id, "mode": mode, "repetition": repetition, "status": status,
              "wall_ms": round((time.monotonic() - started) * 1000, 3),
              "available_generated_report_ms": milestones.get("generated_report", {}).get("offset_ms"),
              "graph_returned_ms": milestones.get("graph_returned", {}).get("offset_ms"),
              "first_useful_result_ms": None, "render_observation": "unobserved",
              "configuration_sha256": fingerprint(captured), "output_sha256": fingerprint(output),
              "workload_sha256": fingerprint(fixture["workload"]), "corpus_sha256": fingerprint(fixture),
              "nvidia_http_attempts": after_nvidia["http_attempts"] - before_nvidia["http_attempts"],
              "nvidia_cost": {"amount": None, "status": "unknown_trial_quota"},
              "jev_http_attempts": after_jev["http_attempts"] - before_jev["http_attempts"],
              "jev_reserved_worst_case_usd": after_jev["reserved_worst_case_usd"] - before_jev["reserved_worst_case_usd"],
              "jev_reported_estimated_cost_usd": float(estimate),
              "jev_usage_status": "reported" if len(reported) == len(jev_calls) else "partial_or_missing",
              "source_metrics": source.metrics, "callback_stats": callback_stats,
              "node_paths": graph_node_paths(snapshot), "telemetry_gaps": snapshot["telemetry_gaps"],
              "trace_path": f"{run_id}/trace.json", "output_path": f"{run_id}/output.json"}
    trace.persist(run_dir / "trace.json")
    write_json(run_dir / "configuration.json", captured)
    write_json(run_dir / "output.json", output)
    write_json(run_dir / "sample.json", record)
    return record, source.cache


def summarize(samples):
    groups = []
    for mode in ("cold_fixture_cache", "warm_fixture_cache"):
        members = [sample for sample in samples if sample["mode"] == mode]
        complete = [sample for sample in members if sample["status"] == "completed"]
        nodes = defaultdict(list)
        for sample in complete:
            for name, duration in sample["node_paths"]["top_level_node_ms"].items():
                nodes[name].append(duration)
        contributors = sorted(({"node": name, "median_inclusive_ms": statistics.median(values), "n": len(values)}
                               for name, values in nodes.items()), key=lambda row: row["median_inclusive_ms"], reverse=True)
        walls = [sample["wall_ms"] for sample in complete]
        parallel = [sample["node_paths"]["parallel_analyst_max_path_ms"] for sample in complete
                    if sample["node_paths"]["parallel_analyst_max_path_ms"] is not None]
        groups.append({"mode": mode, "registered_n": len(members), "completed_n": len(complete),
                       "statuses": {status: sum(member["status"] == status for member in members)
                                    for status in sorted({member["status"] for member in members})},
                       "median_wall_ms": statistics.median(walls) if walls else None,
                       "median_parallel_analyst_max_path_ms": statistics.median(parallel) if parallel else None,
                       "top_three_latency_contributors": contributors[:3],
                       "tail": "unestimated: registered n=3 per regime"})
    return groups


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run the registered paid/trial model calls")
    parser.add_argument("--nvidia-env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--jev-env-file", type=Path, default=ROOT.parent / "129BOB" / ".env")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required for live calls")
    from dotenv import dotenv_values
    nvidia_key = dotenv_values(args.nvidia_env_file).get("NVIDIA_API_KEY")
    jev_key = dotenv_values(args.jev_env_file).get("TYPESAFE_API_KEY")
    if not nvidia_key or not jev_key:
        parser.error("Both selected env files must contain their provider key")
    # SDK debug logs can expose request/response bodies. Raw model reports are
    # stored only in their explicitly named synthetic-output artifacts.
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in ("openai", "httpx", "httpx2", "typesafe_sdk", "tradingagents"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    fixture = load_fixture()
    directory = (args.output_dir or ROOT / "results" / "benchmarks" /
                 f"c01-live-graph-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}").resolve()
    directory.mkdir(parents=True, exist_ok=False)
    stop = threading.Event()
    nvidia_budget = SendBudget(stop, NVIDIA_MAX_ATTEMPTS)
    jev_budget = SendBudget(stop, int(JEV_CAP_USD // JEV_ATTEMPT_RESERVATION), JEV_ATTEMPT_RESERVATION, JEV_CAP_USD)
    metadata = provenance()
    config = normalized_config(registered_config(fixture, directory / "registration"))
    registration = {"schema_version": 1, "benchmark_id": "C01", "measurement_kind": "live_graph_on_synthetic_sources",
        "registered_at_utc": datetime.now(UTC).isoformat(), **metadata,
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), "corpus_sha256": fingerprint(fixture),
        "fixture_license": fixture["license"], "workload": fixture["workload"], "configuration": config,
        "configuration_sha256": fingerprint(config), "workload_sha256": fingerprint(fixture["workload"]),
        "registered_pairs": 3, "registered_runs": 6, "nvidia_max_http_attempts": NVIDIA_MAX_ATTEMPTS,
        "jev_cap_usd": float(JEV_CAP_USD), "jev_max_input_tokens_per_attempt": JEV_MAX_INPUT_TOKENS,
        "jev_input_usd_per_million": float(JEV_INPUT_USD_PER_MILLION),
        "jev_price_source": "https://docs.typesafe.ai/models", "jev_pricing_checked_date": "2026-10-14",
        "prior_jev_component_probe_max_usd": 0.016515072,
        "source_transformations": {"identity_long_name": "Synthetic C01 Company", "feed_items": [],
                                   "feed_unavailable": False, "ohlcv": "business-day expansion of frozen scalar OHLCV"},
        "cold_warm_scope": "Fixture response cache only; fresh graph, model client connections, and memory each run.",
        "tail_semantics": "n=3 per regime; no tail performance claim", "first_useful_result_ms": None,
        "render_observation": "unobserved", "failure_policy": "retain failures; cancel all new sends after throttle/auth/quota/transport failure/budget guard"}
    write_json(directory / "registration.json", registration)
    samples = []
    for repetition in range(1, 4):
        cold, cache = measure_run(fixture, "cold_fixture_cache", repetition, directory, None,
                                  nvidia_budget, jev_budget, (nvidia_key, jev_key))
        samples.append(cold)
        print(json.dumps({"run_id": cold["run_id"], "status": cold["status"], "wall_ms": cold["wall_ms"],
                          "nvidia_http_attempts": nvidia_budget.attempts, "jev_reserved_usd": float(jev_budget.reserved_usd)}), flush=True)
        warm, _ = measure_run(fixture, "warm_fixture_cache", repetition, directory, cache,
                              nvidia_budget, jev_budget, (nvidia_key, jev_key))
        samples.append(warm)
        print(json.dumps({"run_id": warm["run_id"], "status": warm["status"], "wall_ms": warm["wall_ms"],
                          "nvidia_http_attempts": nvidia_budget.attempts, "jev_reserved_usd": float(jev_budget.reserved_usd)}), flush=True)
        write_json(directory / "samples.json", samples)
    summary = {**registration, "samples": samples, "groups": summarize(samples),
               "nvidia_budget": nvidia_budget.snapshot(), "jev_budget": jev_budget.snapshot(),
               "jev_reported_estimated_cost_usd": sum(sample["jev_reported_estimated_cost_usd"] for sample in samples),
               "nvidia_cost": {"amount": None, "status": "unknown_trial_quota"},
               "interpretation": "Real model latency on invented evidence; not live market accuracy or uncapped production workload.",
               "phase_semantics": "Top-level graph_node medians only. Maximum parallel analyst path is separate from serialized stages; overlaps are not summed."}
    write_json(directory / "summary.json", summary)
    lines = ["# C01 live graph on frozen synthetic evidence", "", summary["interpretation"], "",
             "UI render / first useful result are unobserved. Source cache regimes are interleaved with fresh memory.", "",
             "| Regime | Completed / registered | Median wall ms | Maximum analyst path median ms |", "|---|---:|---:|---:|"]
    for group in summary["groups"]:
        lines.append(f"| {group['mode']} | {group['completed_n']} / {group['registered_n']} | {group['median_wall_ms']} | {group['median_parallel_analyst_max_path_ms']} |")
        for row in group["top_three_latency_contributors"]:
            lines.append(f"\n{row['node']}: median inclusive {row['median_inclusive_ms']:.1f} ms across {row['n']} completed runs.")
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"summary_path": str(directory / "summary.json"), "completed_runs": sum(s["status"] == "completed" for s in samples),
                      "nvidia_budget": nvidia_budget.snapshot(), "jev_budget": jev_budget.snapshot()}), flush=True)
    return 0 if all(sample["status"] == "completed" for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
