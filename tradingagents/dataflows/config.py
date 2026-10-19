from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from copy import deepcopy

import tradingagents.default_config as default_config
from tradingagents.dataflows.evidence import (
    EvidenceSession,
    current_evidence,
    evidence_scope,
    set_evidence,
)

# Use default config but allow it to be overridden
_config: dict | None = None

# The config of the run in progress. A graph binds its own for the length of a
# run, so the data tools it calls read that graph's vendors even when several
# graphs share a process. LangGraph carries the context into tool calls.
_run_config: ContextVar[dict | None] = ContextVar("tradingagents_run_config", default=None)


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        _config = deepcopy(default_config.DEFAULT_CONFIG)


def _merge(base: dict, config: dict) -> dict:
    """Merge ``config`` into ``base``: dict-valued keys one level deep, scalars replaced."""
    for key, value in deepcopy(config).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def set_config(config: dict):
    """Update the configuration with custom values.

    Dict-valued keys (e.g. ``data_vendors``) are merged one level deep so a
    partial update like ``{"data_vendors": {"core_stock_apis": "alpha_vantage"}}``
    keeps the other nested keys from the default; scalar keys are replaced.
    """
    initialize_config()
    _merge(_config, config)


@contextmanager
def run_config(config: dict, *, fresh_evidence: bool = False):
    """Serve ``config``, over the defaults, to every read made inside the block.

    ``fresh_evidence`` marks the start of a research run: it gets its own
    evidence session even inside a caller's scope with an equal config.
    """
    merged = _merge(deepcopy(default_config.DEFAULT_CONFIG), config)
    session = _evidence_for(merged, fresh_evidence)
    token = _run_config.set(merged)
    try:
        with evidence_scope(session):
            yield
    finally:
        _run_config.reset(token)


def _evidence_for(config: dict, fresh: bool = False) -> EvidenceSession:
    """Nested work in the same run shares evidence; other runs and configs don't."""
    active = current_evidence()
    if not fresh and active is not None and _run_config.get() == config:
        return active
    return EvidenceSession(
        ttl_seconds=config.get("evidence_ttl_seconds"),
        max_concurrency=config.get("evidence_max_concurrency", 2),
        max_entries=config.get("evidence_max_entries", 256),
    )


def run_config_context(config: dict, *, fresh_evidence: bool = False) -> Context:
    """A context serving ``config``, over the defaults, to every read run in it.

    For a run that yields between steps, where a ``run_config`` block would
    stay open in the caller's context while it holds the results.
    ``fresh_evidence`` is as for :func:`run_config`.
    """
    merged = _merge(deepcopy(default_config.DEFAULT_CONFIG), config)
    session = _evidence_for(merged, fresh_evidence)
    context = copy_context()
    context.run(_run_config.set, merged)
    context.run(set_evidence, session)
    return context


def get_config() -> dict:
    """Get the configuration of the run in progress, else the process-wide one."""
    scoped = _run_config.get()
    if scoped is not None:
        return deepcopy(scoped)
    if _config is None:
        initialize_config()
    return deepcopy(_config)


initialize_config()
