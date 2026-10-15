"""Small live Jev transport probe, with a budget enforced before every send.

This is a component measurement on synthetic input, not a research baseline.
The rate/context assumptions are dated in docs/benchmarks/c01.md. Credentials
are loaded in memory from an ignored env file; only response metadata is logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = "jev-1.13.0"
INPUT_PRICE_PER_MILLION = Decimal("0.042")
MAX_INPUT_TOKENS = 65_536
MAX_ATTEMPTS = 6
STATE = "SYNTHETIC C01 FIXTURE. Example Company revenue rose from 100 to 110 units. Its debt stayed at 20 units."
QUESTIONS = {"revenue_increased": {"type": "noul", "instructions": "Does the supplied state say revenue increased?"}}


class ProbeBudget:
    def __init__(self, cap_usd="1.00", max_attempts=MAX_ATTEMPTS):
        self.cap = Decimal(str(cap_usd))
        if not self.cap.is_finite() or not Decimal("0") < self.cap <= Decimal("1"):
            raise ValueError("Jev cap must be positive and no more than the authorized US$1")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("Probe permits at most six HTTP attempts")
        self.max_attempts = max_attempts
        self.attempts = 0
        self.reserved = Decimal("0")

    def reserve(self):
        cost = Decimal(MAX_INPUT_TOKENS) * INPUT_PRICE_PER_MILLION / Decimal(1_000_000)
        if self.attempts >= self.max_attempts or self.reserved + cost > self.cap:
            raise RuntimeError("Jev probe budget exhausted before HTTP send")
        self.attempts += 1
        self.reserved += cost


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--cap-usd", default="1.00")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    budget = ProbeBudget(args.cap_usd)

    import httpx2
    from dotenv import dotenv_values
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    from tradingagents.agents.jev import _ObservedJevClient
    from tradingagents.telemetry import RunTrace, use_trace

    # SDK debug mode can include request bodies; disable it for this probe.
    logging.getLogger("typesafe_sdk").setLevel(logging.CRITICAL)
    key = dotenv_values(args.env_file).get("TYPESAFE_API_KEY")
    if not key:
        parser.error("The selected env file has no Jev key")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = args.output_dir or ROOT / "results" / "benchmarks" / f"c01-jev-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    samples = []
    workload_hash = hashlib.sha256(json.dumps({"state": STATE, "questions": QUESTIONS},
                                             sort_keys=True).encode()).hexdigest()

    class BudgetTransport(httpx2.BaseTransport):
        def __init__(self):
            self.inner = httpx2.HTTPTransport(retries=0)

        def handle_request(self, request):
            if request.url.host != "api.typesafe.ai" or request.url.path != "/v1/systemone":
                raise RuntimeError("Unexpected benchmark destination")
            budget.reserve()
            from tradingagents.telemetry import current_trace
            trace = current_trace()
            with trace.scope("budgeted_transport", "jev_http_send", {"provider": "typesafe", "model": MODEL}) as span:
                response = self.inner.handle_request(request)
                # Include body transfer; no request/response content in the trace.
                response.read()
                trace.end_span(span, "ok" if response.status_code < 400 else "error",
                               {"http_status": response.status_code, "coverage": "response_body"})
                return response

        def close(self):
            self.inner.close()

    def client():
        return _ObservedJevClient(TypeSafeClient(api_key=key, model=MODEL,
            base_url="https://api.typesafe.ai", retry=RetryPolicy(max_retries=0),
            timeout=30, transport=BudgetTransport()))

    def measure(active_client, regime, repetition):
        trace = RunTrace(configuration={"jev_model": MODEL, "llm_max_retries": 0,
            "spending_cap_usd": float(budget.cap), "measurement_kind": "live_component"},
            workload={"case": "jev_synthetic_probe", "regime": regime, "repetition": repetition,
                      "corpus_sha256": workload_hash})
        started = time.monotonic()
        sample = {"run_id": trace.run_id, "regime": regime, "repetition": repetition}
        with use_trace(trace):
            try:
                response = active_client.system_one(state=STATE, questions=QUESTIONS)
                usage = response.usage
                sample.update(status="ok", model=response.model,
                    input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    estimated_cost_usd=float(Decimal(usage.input_tokens) * INPUT_PRICE_PER_MILLION / Decimal(1_000_000)))
            except Exception as exc:
                sample.update(status="error", error_type=type(exc).__name__)
            sample["wall_ms"] = round((time.monotonic() - started) * 1000, 3)
            trace.finish(sample["status"])
        trace.persist(directory / f"{trace.run_id}.json")
        samples.append(sample)
        return sample["status"] == "ok"

    with client() as reused:
        for repetition in range(1, 4):
            with client() as fresh:
                if not measure(fresh, "fresh_client", repetition):
                    break
            if not measure(reused, "reused_client", repetition):
                break
    from scripts.benchmark_c01 import provenance

    summary = {"measurement_kind": "live_component_on_synthetic_input", "model": MODEL, **provenance(),
        "corpus_sha256": workload_hash, "price_source": "https://docs.typesafe.ai/models",
        "pricing_checked_date": "2026-10-14", "input_usd_per_million": float(INPUT_PRICE_PER_MILLION),
        "max_input_tokens_per_attempt": MAX_INPUT_TOKENS, "max_attempts": MAX_ATTEMPTS,
        "authorized_cap_usd": float(budget.cap), "http_attempts": budget.attempts,
        "reserved_worst_case_usd": float(budget.reserved),
        "reported_estimated_cost_usd": sum(s.get("estimated_cost_usd", 0) for s in samples),
        "cost_status": "reported_usage_estimate" if all(s["status"] == "ok" for s in samples) else "partial_usage_estimate",
        "samples": samples, "scope": "Jev transport only; no data cache or full research timing"}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary_path": str(directory / "summary.json"),
        "http_attempts": budget.attempts, "samples": len(samples),
        "reported_estimated_cost_usd": summary["reported_estimated_cost_usd"],
        "reserved_worst_case_usd": float(budget.reserved)}))
    return 0 if len(samples) == MAX_ATTEMPTS and all(s["status"] == "ok" for s in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
