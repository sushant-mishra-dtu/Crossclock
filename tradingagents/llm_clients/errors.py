"""Provider failures, classified for a run's status, never with their message text.

A run that ends on a provider error says which kind it was (a timeout, a rate
limit, a server error, a dropped connection, or a rejected key or quota), not
the provider's message: messages can echo prompts, account details or keys.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from langchain_core import exceptions as _lc

# LangChain's provider-neutral classes, where the installed langchain-core has them.
_LC_CLASSES = tuple((getattr(_lc, name, None), kind) for name, kind in (
    ("ModelTimeoutError", "timeout"),
    ("ModelRateLimitError", "rate_limit"),
    ("ModelAPIError", "server"),
    ("ModelConnectionError", "connection"),
    ("ModelAuthenticationError", "auth"),
    ("ModelPermissionDeniedError", "auth"),
))
_CONNECTION_NAMES = frozenset({"RemoteProtocolError", "ReadError", "WriteError", "ConnectError",
                               "ProtocolError", "NetworkError"})


def _classify(error: BaseException) -> str | None:
    for cls, kind in _LC_CLASSES:
        if cls is not None and isinstance(error, cls):
            return kind
    name = type(error).__name__
    if "Timeout" in name:  # SDK APITimeoutError, httpx ReadTimeout/ConnectTimeout
        return "timeout"
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 429:
            return "rate_limit"
        if status == 408:
            return "timeout"
        if status >= 500:
            return "server"
        if status in (401, 402, 403):
            return "auth"
        return None
    if "Connection" in name or name in _CONNECTION_NAMES:
        return "connection"
    return None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 6:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def provider_error_class(exc: BaseException) -> str | None:
    """``timeout``, ``rate_limit``, ``server``, ``connection`` or ``auth`` for a
    provider failure that asking again in other words cannot fix; None for any
    other error, a bad request included."""
    for error in _chain(exc):
        kind = _classify(error)
        if kind is not None:
            return kind
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """The wait a provider asked for in its Retry-After header, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        millis = headers.get("retry-after-ms")
        if millis is not None:
            return max(float(millis) / 1000, 0.0)
        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(float(value), 0.0)
        except ValueError:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max((when - datetime.now(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
