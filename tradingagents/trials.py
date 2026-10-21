"""The trial ledger: every backtest summary and learn evaluation, one line each.

A result picked as the best of several configurations tried on the same data
looks better than it is (What survives honest evaluation?, arXiv 2608.27734), so
it has to be deflated by how many were tried, and that count has to be kept as
the work goes rather than remembered afterwards. Each ``backtest`` summary and
each ``learn`` evaluation appends one JSON line to ``<results_dir>/trials.jsonl``
(``TRADINGAGENTS_RESULTS_DIR``) holding its configuration, a hash of it, and the
window of analysis dates it scored. Counting the lines of one kind whose window
overlaps a result's own gives N(configurations), the distinct hashes, which the
deflated Sharpe ratio uses, and N(runs), every line including repeats.

The file is append-only plain text, so it can be read, kept or cleared by hand.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEDGER_NAME = "trials.jsonl"

Window = tuple[str, str]  # first and last analysis date, YYYY-MM-DD


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


@dataclass(frozen=True)
class TrialCount:
    configurations: int   # distinct configuration hashes
    runs: int             # every line, repeats of a configuration included

    def describe(self) -> str:
        return f"{_plural(self.configurations, 'configuration')}, {_plural(self.runs, 'run')}"


def ledger_path(results_dir) -> Path:
    return Path(results_dir) / LEDGER_NAME


def config_hash(config: Mapping[str, Any]) -> str:
    """A short hash of the settings, the same whatever order they were given in."""
    text = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def question_set_version(questions: Sequence[Any]) -> str:
    """A hash of each question's id, document and full wording: any change is a new version."""
    return config_hash({"questions": [[q.id, q.document, q.question.model_dump(mode="json")]
                                      for q in questions]})


def record_trial(results_dir, kind: str, config: Mapping[str, Any], window: Window | None,
                 **details: Any) -> TrialCount:
    """Append one trial and count the trials of its kind on data overlapping its window.

    ``details`` (the run id, a score) are kept on the line but are not part of the
    configuration. A trial with no window (nothing was scored) is kept but
    overlaps nothing.
    """
    entry = {"kind": kind, "time": datetime.now().isoformat(timespec="seconds"),
             "config_hash": config_hash(config), "config": dict(config),
             "window": list(window) if window else None, **details}
    path = ledger_path(results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return count_trials(results_dir, kind, window)


def count_trials(results_dir, kind: str, window: Window | None) -> TrialCount:
    """Configurations and runs of ``kind`` whose analysis dates overlap ``window``."""
    if window is None:
        return TrialCount(0, 0)
    hashes = [entry["config_hash"] for entry in _entries(ledger_path(results_dir))
              if entry.get("kind") == kind and _overlaps(entry.get("window"), window)]
    return TrialCount(configurations=len(set(hashes)), runs=len(hashes))


def _entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if isinstance(entry, dict) and isinstance(entry.get("config_hash"), str):
            entries.append(entry)
        else:  # a line cut short or edited by hand is not a trial
            logger.warning("Skipping unreadable line %d of the trial ledger %s", number, path)
    return entries


def _overlaps(window: Any, other: Window) -> bool:
    try:
        first, last = (str(day) for day in window)
    except (TypeError, ValueError):
        return False
    return first <= other[1] and other[0] <= last
