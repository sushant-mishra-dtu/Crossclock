"""What each analyst read, kept for the report's brief and source drawer (docs/adr/0003).

Every tool result an analyst receives, and every feed the Sentiment Analyst
fetches before its model runs, becomes an evidence entry: who read it, with
which tool and arguments, which vendor answered for which window and when, and
the hash of the text. Gaps are entries too: an unavailable answer is what the
brief lists as a missing input.

The texts go to a content-addressed store under the results folder, written
once, never into graph state or checkpoints. Entries travel as the analysts'
``evidence`` output, keyed by analyst and position, so a resumed run keeps the
entries of the stages it reuses.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import ToolMessage

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.evidence import request_window
from tradingagents.dataflows.feed import Feed

logger = logging.getLogger(__name__)

AVAILABLE, EMPTY, UNAVAILABLE, NO_DATA, WITHHELD = (
    "available", "empty", "unavailable", "no_data", "withheld")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_FIELDS = frozenset({"tool", "arguments", "content_sha256", "coverage", "acquisitions"})

# Router answers met while a read is being made, and reads made by a node itself.
_acquisitions: ContextVar[list | None] = ContextVar("evidence_acquisitions", default=None)
_reads: ContextVar[list | None] = ContextVar("evidence_reads", default=None)


# --- The text store ----------------------------------------------------------

def store_dir(config: Mapping | None = None) -> Path | None:
    """Where the run's source texts go: ``<results_dir>/evidence``."""
    root = (get_config() if config is None else config).get("results_dir")
    return Path(root) / "evidence" if root else None


def store_text(text: str, root) -> str:
    """Write ``text`` once under its SHA-256 and return the hash.

    Bytes, not text mode, so Windows never turns a newline into CRLF and the
    file always hashes to its name.
    """
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    root = Path(root)
    path = root / f"{digest}.txt"
    if path.exists():
        return digest
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.replace(tmp, path)
        except PermissionError:
            if not path.exists():   # another writer holds the same bytes open: theirs will do
                raise
    finally:
        with suppress(OSError):
            os.unlink(tmp)
    return digest


def read_text(root, digest: str) -> str | None:
    """The stored text for ``digest``, or None when missing or no longer matching its hash."""
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return None
    try:
        data = (Path(root) / f"{digest}.txt").read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        return None
    return data.decode("utf-8")


# --- Coverage ----------------------------------------------------------------

def coverage_of(text) -> str:
    """What an answer says about its data, from the router's and adapters' own wording."""
    if not isinstance(text, str) or not text.strip():
        return EMPTY
    head = text.lstrip()
    if head.startswith("DATA_UNAVAILABLE:"):
        return UNAVAILABLE
    if head.startswith("NO_DATA_AVAILABLE:"):
        return NO_DATA
    if "withheld for this date" in head.lower():
        return WITHHELD
    return AVAILABLE


# --- Router answers ----------------------------------------------------------

def _items(value) -> list[dict]:
    return [{"title": item.title, "publisher": item.author, "url": item.url or None,
             "published_at": item.published_at} for item in value.items]


def acquisition(source: str, operation: str, request: Mapping | None, outcome: str,
                value=None, fetched_at: str | None = None, request_sha256: str | None = None) -> dict:
    """One vendor answer (or failure) as an entry records it.

    ``request_sha256`` is the run's evidence-session identity of the request
    (C04), when the answer went through the session.
    """
    start, end, as_of = request_window(request or {})
    record = {
        "source": source, "operation": operation, "outcome": outcome, "request_sha256": request_sha256,
        "fetched_at": fetched_at or (datetime.now(UTC).isoformat() if outcome == "ok" else None),
        "window_start": start, "window_end": end, "as_of": as_of,
    }
    if isinstance(value, Feed):
        record["items"] = _items(value)
    return record


def note_acquisition(source: str, operation: str, request: Mapping | None, outcome: str,
                     value=None, fetched_at: str | None = None, request_sha256: str | None = None) -> None:
    """Called by the router for each vendor it asks; does nothing outside a read."""
    sink = _acquisitions.get()
    if sink is not None:
        sink.append(acquisition(source, operation, request, outcome, value, fetched_at, request_sha256))


@contextmanager
def capturing():
    """Collect the router's answers made inside the block, for one read."""
    sink: list = []
    token = _acquisitions.set(sink)
    try:
        yield sink
    finally:
        _acquisitions.reset(token)


# --- Entries -----------------------------------------------------------------

def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def entry(tool: str, arguments: Mapping, value, acquisitions: Iterable[dict] = ()) -> dict:
    """An evidence entry for one read; its text goes to the store."""
    text = value.text if isinstance(value, Feed) else value
    text = text if isinstance(text, str) else str(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stored = False
    root = store_dir()
    if root is not None:
        try:
            store_text(text, root)
            stored = True
        except OSError as exc:   # a full disk must not end the research
            logger.warning("Could not keep the text %s read from %s: %s", tool, root, exc)
    unavailable = isinstance(value, Feed) and value.unavailable
    return {
        "tool": tool, "arguments": _plain(dict(arguments)),
        "content_sha256": digest, "chars": len(text), "stored": stored,
        "coverage": UNAVAILABLE if unavailable else coverage_of(text),
        "acquisitions": list(acquisitions),
    }


def read(tool: str, arguments: Mapping, value, acquisitions: Iterable[dict] = (), *,
         source: str | None = None) -> None:
    """Record a read a node makes itself (the Sentiment Analyst's feeds).

    ``source`` names a fetcher that does not go through the router (StockTwits,
    Reddit); its answer is recorded as that source's acquisition.
    """
    sink = _reads.get()
    if sink is None:
        return
    acquired = list(acquisitions)
    if source is not None:
        failed = isinstance(value, Feed) and value.unavailable
        acquired.append(acquisition(source, tool, arguments, "unavailable" if failed else "ok", value))
    sink.append(entry(tool, arguments, value, acquired))


def _is_entry(value) -> bool:
    return isinstance(value, Mapping) and value.keys() >= _ENTRY_FIELDS


def recorded_tools(tools: Iterable) -> list:
    """Copies of ``tools`` whose results carry their evidence entry as the message artifact.

    The model sees the same names, descriptions and schemas, and the same text:
    an artifact is never sent to it. The entry gets its analyst when the
    analyst's node collects it (``recording_evidence``).
    """
    def recorded(tool):
        func = tool.func

        def run(*args, **kwargs):
            with capturing() as acquisitions:
                text = func(*args, **kwargs)
            return text, entry(tool.name, kwargs, text, acquisitions)

        return tool.model_copy(update={"func": run, "response_format": "content_and_artifact"})

    return [recorded(tool) for tool in tools]


def _key(analyst: str, position: int) -> str:
    return f"{analyst}#{position:03d}"


def recording_evidence(node, analyst: str):
    """Wrap an analyst's node so what it read reaches the state as ``evidence``.

    Tool results come from the artifacts on its private messages; reads the node
    makes itself are collected while it runs. Keys are positions, so writing the
    same reads again on a later turn changes nothing.
    """
    def recorded(state):
        sink: list = []
        token = _reads.set(sink)
        try:
            update = node(state)
        finally:
            _reads.reset(token)
        reads = [m.artifact for m in state.get("messages") or ()
                 if isinstance(m, ToolMessage) and _is_entry(m.artifact)]
        reads += sink
        if reads and isinstance(update, dict):
            found = {_key(analyst, i): {**r, "analyst": analyst, "seq": i} for i, r in enumerate(reads)}
            update = {**update, "evidence": merge_evidence(update.get("evidence"), found)}
        return update

    recorded.__name__ = getattr(node, "__name__", "node")
    recorded.__doc__ = getattr(node, "__doc__", None)
    return recorded


def merge_evidence(left: Mapping | None, right: Mapping | None) -> dict:
    """State reducer: entries by key; a key written again keeps one entry."""
    return {**(left or {}), **(right or {})}


def ordered_entries(evidence: Mapping | None) -> list[dict]:
    """The entries in pipeline order (analyst, then read), numbered E1, E2, ..."""
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

    order = {spec.agent_node: i for i, spec in enumerate(ANALYST_NODE_SPECS.values())}
    entries = sorted((evidence or {}).values(),
                     key=lambda e: (order.get(e.get("analyst"), len(order)), e.get("analyst") or "",
                                    e.get("seq", 0)))
    return [{"id": f"E{i}", **e} for i, e in enumerate(entries, start=1)]
