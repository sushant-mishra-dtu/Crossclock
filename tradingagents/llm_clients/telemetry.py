"""Observe existing SDK HTTPX clients without changing their transport policy.

The SDK client handles are inspected defensively, while the hooks themselves
use HTTPX's public event-hooks interface. No request content, URL, headers, or
exception text is retained. One hook per client serves concurrent run contexts.
"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import httpx

from tradingagents.telemetry import current_trace

_pending_attempts: ContextVar[list | None] = ContextVar("telemetry_http_attempts", default=None)
_call_metadata: ContextVar[dict | None] = ContextVar("telemetry_http_metadata", default=None)
_REQUEST_EXTENSION = "tradingagents_telemetry_attempt"
_hook_lock = threading.Lock()


def _client_types():
    # Jev is optional and uses the separately packaged httpx2 API.
    try:
        import httpx2
    except ImportError:
        return (httpx.Client,), (httpx.AsyncClient,)
    return (httpx.Client, httpx2.Client), (httpx.AsyncClient, httpx2.AsyncClient)


def _request_hook(request):
    trace = current_trace()
    if trace is None:
        return
    transport = "httpx2" if type(request).__module__.startswith("httpx2.") else "httpx"
    parent = trace.current_span_attributes
    attributes = {"transport": transport,
                  **{key: parent[key] for key in ("provider", "model") if key in parent},
                  **(_call_metadata.get() or {})}
    # This SDK-defined counter describes actual SDK retries, rather than an
    # estimate based on a logical LangChain call. Do not retain other headers.
    retry_count = request.headers.get("x-stainless-retry-count") or request.headers.get("x-typesafe-retry-count")
    if retry_count is not None and retry_count.isdigit():
        attributes["retry_count"] = int(retry_count)
        if int(retry_count):
            trace.gap("sdk_retry_wait_unobserved")
    span = trace.start_span("transport_attempt", "httpx.send", attributes)
    entry = {"trace": trace, "span": span, "complete": False}
    request.extensions[_REQUEST_EXTENSION] = entry
    pending = _pending_attempts.get()
    if pending is not None:
        pending.append(entry)


def _response_hook(response):
    entry = response.request.extensions.pop(_REQUEST_EXTENSION, None)
    if entry is None:
        return
    entry["complete"] = True
    entry["trace"].end_span(
        entry["span"],
        status="ok" if response.status_code < 400 else "error",
        attributes={"http_status": response.status_code, "coverage": "response_headers"},
    )


async def _async_request_hook(request):
    _request_hook(request)


async def _async_response_hook(response):
    _response_hook(response)


def mark_transport_not_sent(request, reason):
    """A pre-send guard refused this dispatch after the request hook ran."""
    entry = request.extensions.pop(_REQUEST_EXTENSION, None)
    if entry is None:
        return
    entry["complete"] = True
    entry["trace"].end_span(entry["span"], status="not_sent",
                            attributes={"outcome": "guard_refused", "reason": reason, "coverage": "not_sent"})


def observe_http_client(client) -> bool:
    """Install idempotent hooks on an existing HTTPX/HTTPX2 client."""
    sync_types, async_types = _client_types()
    if not isinstance(client, sync_types + async_types):
        return False
    hooks = client.event_hooks
    request_hook, response_hook = (
        (_async_request_hook, _async_response_hook)
        if isinstance(client, async_types)
        else (_request_hook, _response_hook)
    )
    # LangChain pools default clients, and callers may share custom clients.
    # Idempotency preserves existing hooks and prevents double counting.
    with _hook_lock:
        if request_hook not in hooks.setdefault("request", []):
            hooks["request"].append(request_hook)
        if response_hook not in hooks.setdefault("response", []):
            hooks["response"].append(response_hook)
    return True


def observe_sdk_transport(llm, provider: str, *, sdk: str = "openai"):
    """Attach observers to the clients LangChain already constructed.

    If an SDK changes its client shape, inference continues unchanged and the
    run records a coverage gap instead of claiming an observed HTTP attempt.
    """
    observed = 0
    if sdk in {"openai", "anthropic"}:
        names = ("root_client", "root_async_client") if sdk == "openai" else ("_client", "_async_client")
        for name in names:
            try:
                sdk_client = getattr(llm, name, None)
                if observe_http_client(getattr(sdk_client, "_client", None)):
                    observed += 1
            except Exception:
                # Observation must never make a previously usable model fail.
                continue
    try:
        object.__setattr__(llm, "_telemetry_provider", provider)
        object.__setattr__(llm, "_telemetry_transport_coverage", "httpx_hooks" if observed == 2 else "partial" if observed else "unsupported")
    except (AttributeError, TypeError):
        # Constructor stubs and custom objects without attributes remain valid.
        pass
    return llm


@contextmanager
def transport_call(llm):
    trace = current_trace()
    if trace is None:
        yield
        return
    coverage = getattr(llm, "_telemetry_transport_coverage", "unconfigured")
    provider = getattr(llm, "_telemetry_provider", "unknown")
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or "unknown"
    if coverage != "httpx_hooks":
        trace.gap(f"{provider}_transport_{coverage}")
    metadata_token = _call_metadata.set({"provider": provider, "model": model})
    pending = []
    pending_token = _pending_attempts.set(pending)
    try:
        yield
    finally:
        for entry in pending:
            if not entry["complete"]:
                # HTTPX has no error hook. Count the observed send but do not
                # invent its outcome or duration from the eventual SDK error.
                entry["trace"].end_span(entry["span"], status="incomplete", attributes={"coverage": "response_missing"})
                entry["trace"].gap("httpx_response_missing")
        _pending_attempts.reset(pending_token)
        _call_metadata.reset(metadata_token)


def observed_generation(method):
    """Cover structured/bound calls, preserving the provider class hierarchy."""
    @wraps(method)
    def generate(self, *args, **kwargs):
        with transport_call(self):
            return method(self, *args, **kwargs)
    return generate


def observed_async_generation(method):
    @wraps(method)
    async def generate(self, *args, **kwargs):
        with transport_call(self):
            return await method(self, *args, **kwargs)
    return generate


def observed_invocation(method):
    """One public invocation can include multiple outer generation retries."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return method(self, *args, **kwargs)
        with trace.scope("llm_invocation", "invoke", {
            "provider": getattr(self, "_telemetry_provider", "openai"),
            "model": self.model_name, "operation": "invoke",
        }):
            return method(self, *args, **kwargs)
    return invoke
