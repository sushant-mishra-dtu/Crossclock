"""Logical call counters and content-free LangChain trace callbacks."""

import threading
from collections import OrderedDict
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

from tradingagents.telemetry import RunTrace, current_trace

_GRAPH_NODES = frozenset({
    "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
    "Memory Log", "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
    "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager",
})


def _token_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage(response: LLMResult) -> dict[str, Any]:
    """Read provider usage without inspecting generated text or prompt content."""
    usage = None
    finish_reason = None
    try:
        generation = response.generations[0][0]
        message = getattr(generation, "message", None)
        if isinstance(message, AIMessage):
            usage = message.usage_metadata
            finish_reason = (message.response_metadata or {}).get("finish_reason")
        if finish_reason is None:
            finish_reason = (getattr(generation, "generation_info", None) or {}).get("finish_reason")
    except (AttributeError, IndexError, TypeError):
        pass
    if not usage:
        output = getattr(response, "llm_output", None) or {}
        usage = output.get("token_usage") or output.get("usage") or {}
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_token_details") or usage.get("prompt_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    tokens_in = _token_count(usage.get("input_tokens", usage.get("prompt_tokens")))
    tokens_out = _token_count(usage.get("output_tokens", usage.get("completion_tokens")))
    cached = _token_count(details.get("cache_read", details.get("cached_tokens",
                          usage.get("cache_read_input_tokens"))))
    return {
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cached_input_tokens": cached,
        "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
        "usage_status": "missing" if tokens_in is None and tokens_out is None else (
            "partial" if tokens_in is None or tokens_out is None else "reported"),
    }


class StatsCallbackHandler(BaseCallbackHandler):
    """Count logical calls; retain callback ancestry without tracing content.

    SDK/network attempts are observed separately. A callback run UUID represents
    one logical call even if both model-start hooks are delivered for it.
    """

    run_inline = True
    _CALLBACK_HISTORY = 1024

    def __init__(self, trace: RunTrace | None = None) -> None:
        super().__init__()
        self.trace = trace
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cached_input_tokens = 0
        self._cached_usage_reported = 0
        self.usage_missing = 0
        # Wrappers alias their nearest stage: timing them as graph nodes would
        # double-count inclusive stage durations.
        self._parents: dict[str, str] = {}
        self._spans: dict[str, tuple[RunTrace, str]] = {}
        self._seen: set[tuple[str, str]] = set()
        # Keep recent terminal IDs for duplicate delivery, rather than retaining
        # every callback UUID across a reusable handler's lifetime.
        self._ended: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._nodes: dict[str, str | None] = {}
        self._bindings: dict[str, Any] = {}

    def _active_trace(self) -> RunTrace | None:
        return self.trace or current_trace()

    def _parent(self, trace: RunTrace, kwargs: dict[str, Any]) -> str | None:
        parent = kwargs.get("parent_run_id")
        return self._parents.get(str(parent)) if parent is not None else None

    @staticmethod
    def _attributes(kwargs: dict[str, Any]) -> dict[str, Any]:
        metadata = kwargs.get("metadata") or {}
        invocation = kwargs.get("invocation_params") or {}
        return {
            "callback_run_id": str(kwargs["run_id"]) if kwargs.get("run_id") else None,
            "parent_callback_run_id": str(kwargs["parent_run_id"]) if kwargs.get("parent_run_id") else None,
            "node": metadata.get("langgraph_node"),
            "model": metadata.get("ls_model_name") or invocation.get("model") or invocation.get("model_name"),
            "provider": metadata.get("ls_provider"),
        }

    def on_chain_start(self, serialized: dict[str, Any] | None, inputs: Any, **kwargs: Any) -> None:
        trace, run_id = self._active_trace(), kwargs.get("run_id")
        if trace is None or run_id is None:
            return
        name = kwargs.get("name") or (serialized or {}).get("name")
        with self._lock:
            key = str(run_id)
            if key in self._parents or ("chain", key) in self._ended:
                return
            parent = self._parent(trace, kwargs)
            parent_key = str(kwargs.get("parent_run_id"))
            if name in _GRAPH_NODES and self._nodes.get(parent_key) != name:
                span = trace.start_span("graph_node", name, self._attributes(kwargs), parent_id=parent)
                self._spans[key] = (trace, span)
                self._parents[key] = span
                self._nodes[key] = name
                self._bindings[key] = trace.bind_span(span)
            else:
                self._parents[key] = parent or trace.current_span_id
                self._nodes[key] = self._nodes.get(parent_key)

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:
        trace = self._active_trace()
        if trace is not None and isinstance(outputs, dict) and any(outputs.get(key) for key in (
            "market_report", "sentiment_report", "news_report", "fundamentals_report",
        )):
            trace.milestone("generated_report")
        self._end("chain", kwargs)

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        self._end("chain", kwargs, status="error", attributes={"error_type": type(error).__name__})

    def _start(self, kind: str, name: str, kwargs: dict[str, Any]) -> None:
        with self._lock:
            run_id = kwargs.get("run_id")
            key = str(run_id) if run_id is not None else None
            if key is not None:
                if (kind, key) in self._seen or (kind, key) in self._ended:
                    return
                self._seen.add((kind, key))
            if kind == "llm":
                self.llm_calls += 1
            else:
                self.tool_calls += 1
            trace = self._active_trace()
            if trace is not None and key is not None:
                attributes = self._attributes(kwargs)
                node = self._nodes.get(str(kwargs.get("parent_run_id")))
                if node is not None:
                    attributes["node"] = node
                parent = self._parent(trace, kwargs)
                # The public invoke wrapper owns outer retries. Callback
                # ancestry supplies stage labels, but cannot replace this
                # active invocation as the generation attempt's parent.
                if kind == "llm" and trace.current_span_kind == "llm_invocation":
                    parent = trace.current_span_id
                span = trace.start_span(kind, name, attributes,
                                        parent_id=parent)
                self._spans[key] = (trace, span)
                self._parents[key] = span
                self._nodes[key] = node
                self._bindings[key] = trace.bind_span(span)
            elif trace is not None:
                trace.gap("callback_run_id_missing")

    def _end(self, kind: str, kwargs: dict[str, Any], status: str = "ok",
             attributes: dict[str, Any] | None = None) -> bool:
        with self._lock:
            run_id = kwargs.get("run_id")
            key = str(run_id) if run_id is not None else None
            if key is not None:
                if (kind, key) in self._ended:
                    return False
                self._seen.discard((kind, key))
                self._ended[(kind, key)] = None
                if len(self._ended) > self._CALLBACK_HISTORY:
                    self._ended.popitem(last=False)
                owned = self._spans.pop(key, None)
                self._parents.pop(key, None)
                self._nodes.pop(key, None)
                if owned is not None:
                    trace, span = owned
                    trace.end_span(span, status=status, attributes=attributes)
                    token = self._bindings.pop(key, None)
                    if token is not None:
                        try:
                            trace.reset_span(token)
                        except ValueError:
                            trace.gap("callback_context_changed")
            return True

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        self._start("llm", "model", kwargs)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[Any]],
                            **kwargs: Any) -> None:
        self._start("llm", "model", kwargs)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = _usage(response)
        if not self._end("llm", kwargs, attributes=usage):
            return
        with self._lock:
            self.tokens_in += usage["input_tokens"] or 0
            self.tokens_out += usage["output_tokens"] or 0
            self.cached_input_tokens += usage["cached_input_tokens"] or 0
            self._cached_usage_reported += usage["cached_input_tokens"] is not None
            self.usage_missing += usage["usage_status"] != "reported"
        trace = self._active_trace()
        if trace is not None:
            if usage["usage_status"] != "reported":
                trace.gap("llm_token_usage_missing")
            if usage["cached_input_tokens"] is None:
                trace.gap("cached_input_token_usage_missing")

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        if not self._end("llm", kwargs, status="error", attributes={"error_type": type(error).__name__,
                                                                     "usage_status": "missing"}):
            return
        with self._lock:
            self.usage_missing += 1
        trace = self._active_trace()
        if trace is not None:
            trace.gap("failed_llm_token_usage_missing")

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        self._start("tool", (serialized or {}).get("name") or "tool", kwargs)

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self._end("tool", kwargs)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self._end("tool", kwargs, status="error", attributes={"error_type": type(error).__name__})

    def get_stats(self) -> dict[str, Any]:
        """Return current statistics."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cached_input_tokens": self.cached_input_tokens if (
                    self.llm_calls > 0 and self._cached_usage_reported == self.llm_calls
                ) else None,
                "usage_missing": self.usage_missing,
            }
