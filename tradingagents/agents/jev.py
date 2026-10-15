"""TypeSafe Jev access for agents that ask typed questions.

Jev (https://docs.typesafe.ai) answers questions about natural-language state
with typed values: the probability of a yes (Noul), a distribution over named
options (Choice), or a position on described levels (Score). Agents use it for
cheap judgments around their LLM calls; the LLMs keep writing the reports, and
arithmetic, dates and counting stay in code.

Jev is optional. ``jev_client`` returns None when the ``jev`` extra is not
installed, ``TYPESAFE_API_KEY`` is unset, or ``jev_enabled`` is off in the
config, and every caller then behaves as it did without Jev.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import cache
from types import SimpleNamespace
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.llm_clients.telemetry import observe_http_client, transport_call
from tradingagents.telemetry import current_trace

logger = logging.getLogger(__name__)

# Requests in flight at once. The service allows 1,200 requests a minute and the
# SDK retries a 429 with backoff, so this bounds latency, not correctness.
MAX_CONCURRENT_REQUESTS = 8

# Characters of state a request may carry. The service rejects a request past
# about 33K input tokens (400 max_tokens_exceeded; measured 2026-09-24 on
# jev-latest, where debate text ran about 3.3 characters a token), so this leaves
# room for text that tokenizes more densely.
MAX_STATE_CHARS = 80_000


def jev_client() -> Any | None:
    """A ``TypeSafeClient`` for the configured model, or None when Jev is off.

    Use the client as a context manager so its HTTP connections are closed.
    """
    config = get_config()
    if not config.get("jev_enabled", True):
        return None
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        return None
    try:
        from typesafe_sdk import TypeSafeClient, TypeSafeError
    except ImportError:
        _warn_sdk_missing()
        return None
    try:
        return _ObservedJevClient(TypeSafeClient(model=config.get("jev_model") or None))
    except TypeSafeError as exc:  # a malformed key or timeout, caught before any request
        logger.warning("Could not create the TypeSafe client (%s); running without Jev", exc)
        return None


@cache
def _warn_sdk_missing() -> None:
    # Once per process: every judged debate turn asks for a client.
    logger.warning(
        "TYPESAFE_API_KEY is set but typesafe-sdk is not installed; running without "
        'Jev. Install it with: pip install "tradingagents[jev]"'
    )


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _observed_system_one(client, *args, **kwargs):
    trace = current_trace()
    if trace is None:
        return client.system_one(*args, **kwargs)
    model = kwargs.get("model") or _field(_field(client, "_config"), "default_model") or get_config().get("jev_model") or "jev-latest"
    try:
        observed = observe_http_client(getattr(client, "_http_client", None))
    except Exception:
        observed = False
    if not observed:
        trace.gap("jev_transport_attempts_unobserved")
    transport = SimpleNamespace(model_name=model, _telemetry_provider="typesafe",
                                _telemetry_transport_coverage="httpx_hooks" if observed else "unsupported")
    with trace.scope("jev", "system_one", {"provider": "typesafe", "model": model}) as span:
        try:
            with transport_call(transport):
                response = client.system_one(*args, **kwargs)
        except Exception as exc:
            trace.gap("jev_failed_call_usage_unknown")
            trace.end_span(span, "error", {"usage_status": "missing", "error_type": type(exc).__name__})
            raise
        attributes = {}
        usage = _field(response, "usage")
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            value = _field(usage, name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                attributes[name] = value
        response_model = _field(response, "model")
        if isinstance(response_model, str):
            attributes["model"] = response_model
        reported = attributes.keys() & {"input_tokens", "output_tokens"}
        attributes["usage_status"] = "reported" if len(reported) == 2 else "partial" if reported else "missing"
        if attributes["usage_status"] == "missing":
            trace.gap("jev_usage_missing")
        trace.end_span(span, "ok", attributes)
        return response


class _ObservedJevClient:
    """Transparent context-manager adapter for every production Jev call."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, *exc):
        return self._client.__exit__(*exc)

    def system_one(self, *args, **kwargs):
        return _observed_system_one(self._client, *args, **kwargs)


def ask_each(client: Any, states: Sequence[Any], questions: Mapping[str, Any]) -> list[Any]:
    """Ask the same ``questions`` about every state, one request per state.

    Requests run concurrently; responses come back in state order. The first
    request that still fails after the SDK's retries raises, so a caller never
    mixes judged and unjudged items without knowing.
    """
    if not states:
        return []
    pool = ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_REQUESTS, len(states)))
    try:
        call = client.system_one if isinstance(client, _ObservedJevClient) else lambda **kwargs: _observed_system_one(client, **kwargs)
        # ThreadPoolExecutor does not propagate ContextVars. Each request needs
        # its own copy: entering one shared Context concurrently is forbidden.
        jobs = [(copy_context(), state) for state in states]
        return list(pool.map(lambda job: job[0].run(call, state=job[1], questions=questions), jobs))
    finally:
        # After a failure, requests not yet started are dropped rather than run.
        pool.shutdown(wait=True, cancel_futures=True)
