"""Bounded NVIDIA component probe on synthetic input; requires --execute.

At most six HTTP sends compare fresh versus reused SDK clients. SDK and
transport retries are disabled. This does not execute the research graph.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = "nvidia/nemotron-3-super-120b-a12b"
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_ATTEMPTS = 6
MAX_TOKENS = 256
STATE = "SYNTHETIC C01 FIXTURE. Example Company revenue rose from 100 to 110 units. Its debt stayed at 20 units."
MESSAGES = [
    {"role": "system", "content": "Use only the supplied synthetic state. Answer in one sentence."},
    {"role": "user", "content": STATE + "\nDid revenue increase, and did debt change?"},
]
EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}


class AttemptGuard:
    def __init__(self, maximum=MAX_ATTEMPTS):
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= MAX_ATTEMPTS:
            raise ValueError("NVIDIA probe permits at most six HTTP sends")
        self.maximum = maximum
        self.attempts = 0
        self._lock = threading.Lock()

    def reserve(self):
        with self._lock:
            if self.attempts >= self.maximum:
                raise RuntimeError("NVIDIA component probe exhausted its HTTP send allowance")
            self.attempts += 1
            return self.attempts


def numerical_quota_headers(headers):
    """Retain only numeric x-ratelimit metadata, never auth/request headers."""
    result = {}
    for key, value in headers.items():
        name = key.lower()
        text = value.strip()
        if (re.fullmatch(r"x-ratelimit-[a-z0-9-]{1,80}", name)
                and re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,12})?", text)):
            number = Decimal(text)
            result[name] = int(number) if number == number.to_integral_value() else float(number)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--execute", action="store_true",
                        help="Explicitly authorize this bounded six-send component replay")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("Use --execute to enable up to six live NVIDIA HTTP sends")

    import httpx
    from dotenv import dotenv_values
    from openai import OpenAI

    from scripts.benchmark_c01 import provenance, write_json
    from tradingagents.llm_clients.telemetry import observe_http_client, transport_call
    from tradingagents.telemetry import RunTrace, current_trace, use_trace

    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    key = dotenv_values(args.env_file).get("NVIDIA_API_KEY")
    if not key:
        parser.error("The selected env file has no NVIDIA_API_KEY")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = args.output_dir or ROOT / "results" / "benchmarks" / f"c01-nvidia-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    guard = AttemptGuard()
    configuration = {"provider": "nvidia", "model": MODEL, "max_tokens": MAX_TOKENS,
                     "max_retries": 0, "transport_retries": 0, "temperature": 0.0,
                     "extra_body": EXTRA_BODY, "max_http_sends": MAX_ATTEMPTS}
    corpus_hash = hashlib.sha256(json.dumps(MESSAGES, sort_keys=True).encode()).hexdigest()
    samples = []

    class GuardedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            if (request.method != "POST" or request.url.scheme != "https"
                    or request.url.host != "integrate.api.nvidia.com"
                    or request.url.path != "/v1/chat/completions"):
                raise RuntimeError("Unexpected NVIDIA component probe destination")
            attempt = guard.reserve()
            trace = current_trace()
            with trace.scope("budgeted_transport", "nvidia_http_send", {
                "provider": "nvidia", "model": MODEL, "attempt": attempt,
            }) as span:
                response = self.inner.handle_request(request)
                response.read()
                trace.end_span(span, "ok" if response.status_code < 400 else "error", {
                    "http_status": response.status_code, "coverage": "response_body",
                })
                return response

        def close(self):
            self.inner.close()

    @contextmanager
    def client():
        with httpx.Client(transport=GuardedTransport(), timeout=30,
                          follow_redirects=False) as http_client:
            observe_http_client(http_client)
            with OpenAI(api_key=key, base_url=BASE_URL, max_retries=0,
                        timeout=30, http_client=http_client) as sdk:
                yield sdk

    def measure(active_client, regime, repetition):
        trace = RunTrace(configuration={"llm_provider": "nvidia", "quick_think_llm": MODEL,
            "max_tokens": MAX_TOKENS, "llm_max_retries": 0, "temperature": 0.0,
            "measurement_kind": "live_component"},
            workload={"case": "nvidia_synthetic_probe", "regime": regime,
                      "repetition": repetition, "corpus_sha256": corpus_hash})
        trace.gap("dollar_cost_unknown")
        trace.gap("ui_render_unobserved")
        sample = {"run_id": trace.run_id, "regime": regime, "repetition": repetition,
                  "cost_usd": None, "first_useful_result_ms": None}
        metadata = SimpleNamespace(model_name=MODEL, _telemetry_provider="nvidia",
                                   _telemetry_transport_coverage="httpx_hooks")
        previous_attempts = guard.attempts
        started = time.monotonic()
        with use_trace(trace):
            try:
                with trace.scope("llm", "nvidia_chat", {"provider": "nvidia", "model": MODEL}) as span:
                    with transport_call(metadata):
                        raw = active_client.chat.completions.with_raw_response.create(
                            model=MODEL, messages=MESSAGES, max_tokens=MAX_TOKENS,
                            temperature=0, stream=False, extra_body=EXTRA_BODY)
                        output_path = directory / f"{trace.run_id}-response.json"
                        body = raw.http_response.content
                        output_path.write_bytes(body)
                        sample.update(response_path=output_path.name,
                                      response_sha256=hashlib.sha256(body).hexdigest())
                        response = raw.parse()
                    # Response bodies belong only in local raw benchmark artifacts.
                    usage = response.usage
                    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
                    attributes = {"usage_status": "reported" if usage is not None else "missing",
                                  "input_tokens": usage.prompt_tokens if usage else None,
                                  "output_tokens": usage.completion_tokens if usage else None,
                                  "cached_input_tokens": cached}
                    trace.end_span(span, "ok", attributes)
                    if usage is None:
                        trace.gap("llm_token_usage_missing")
                    if cached is None:
                        trace.gap("cached_input_token_usage_missing")
                    quota = numerical_quota_headers(raw.headers)
                    sample.update(status="ok", model=response.model, **attributes,
                                  finish_reason=response.choices[0].finish_reason if response.choices else None,
                                  quota_headers=quota, quota_status="reported" if quota else "unknown",
                                  response_path=output_path.name)
                    trace.milestone("generated_report")
            except Exception as exc:
                # API errors can include response text or credentials. Retain only type.
                sample.update(status="error", error_type=type(exc).__name__, quota_status="unknown")
                error_response = getattr(exc, "response", None)
                if error_response is not None:
                    body = error_response.content
                    output_path = directory / f"{trace.run_id}-error-response.txt"
                    output_path.write_bytes(body)
                    quota = numerical_quota_headers(error_response.headers)
                    sample.update(response_path=output_path.name,
                                  response_sha256=hashlib.sha256(body).hexdigest(),
                                  http_status=error_response.status_code,
                                  quota_headers=quota, quota_status="reported" if quota else "unknown")
            sample["wall_ms"] = round((time.monotonic() - started) * 1000, 3)
            sample["http_attempts"] = guard.attempts - previous_attempts
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
    summary = {"schema_version": 1, "measurement_kind": "live_component_on_synthetic_input",
               "configuration": configuration, "model": MODEL, "corpus_sha256": corpus_hash,
               "configuration_sha256": hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest(),
               **provenance(), "http_attempts": guard.attempts, "max_http_sends": MAX_ATTEMPTS,
               "cost_usd": None, "cost_status": "unknown_no_verified_dollar_price",
               "first_useful_result_ms": None, "samples": samples,
               "scope": "NVIDIA chat component only; synthetic input; no full research timing",
               "connection_regimes": "Fresh SDK client per cold sample; one reused client across paired samples. First reused sample may establish its connection."}
    write_json(directory / "summary.json", summary)
    print(json.dumps({"summary_path": str(directory / "summary.json"),
                      "http_attempts": guard.attempts, "samples": len(samples),
                      "cost_status": summary["cost_status"]}))
    return 0 if len(samples) == MAX_ATTEMPTS and all(s["status"] == "ok" for s in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
