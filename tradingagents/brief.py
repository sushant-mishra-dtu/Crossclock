"""The brief a saved report opens with, and the texts its source drawer shows (docs/adr/0003).

Assembled in code from what the run saved, with no model call: the report's
header and decision, ``run_status.json``, ``evidence.json``,
``claim_check.json`` and the sentiment judgments. A statement links to the
sources its analyst-report section was written from, and each figure in it is
traced to the source lines that state it at the claim's precision. Neither says
that a source supports the claim: that is not checked here.

Texts are read only from the store ``evidence.json`` names, only inside the
results folder, only for hashes the report lists, and only while the file still
hashes to its name.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from tradingagents.agents.claim_check import figure_stated, figures
from tradingagents.agents.rating import parse_rating
from tradingagents.completion import describe_gaps, describe_stop, is_complete
from tradingagents.dataflows.provenance import AVAILABLE, read_text
from tradingagents.reporting import strip_sources_appendix

# Fields a brief would hold that no run produces yet (owner decision, ADR 0003).
NOT_PRODUCED = ["business overview", "key risks", "next review trigger"]

_ANALYST_OF_REPORT = {"market": "Market Analyst", "sentiment": "Sentiment Analyst",
                      "news": "News Analyst", "fundamentals": "Fundamentals Analyst"}
_VERDICT_ORDER = {"contradicted": 0, "unsupported": 1, "unverified": 2, "supported": 3, "not_checkable": 4}
VERDICT_TEXT = {
    "contradicted": "Contradicted by the analyst reports",
    "unsupported": "Not found in the analyst reports",
    "unverified": "Unverified: settling it takes comparing numbers",
    "supported": "Supported by the analyst reports",
    "not_checkable": "Not checked: not a checkable fact",
}
STATEMENTS_TEXT = {
    "ran": "Checked against the analyst reports by TypeSafe Jev; figures traced in code.",
    "unavailable": "Not checked: Jev is not set up, so no statement is marked supported.",
    "disabled": "Not checked: the claim check is turned off, so no statement is marked supported.",
    "failed": "Not checked: the claim check failed, so no statement is marked supported.",
    "skipped": "Not checked: there was nothing to check the decision against.",
    "not_run": "Not checked: the run stopped before the Portfolio Manager.",
    "not_recorded": "This report was saved before claim checks were kept as data; "
                    "its decision section shows any check that ran.",
}
_COVERAGE_TEXT = {
    "empty": "answered with nothing", "unavailable": "could not be answered",
    "no_data": "had no data", "withheld": "was withheld",
}
_COVERAGE_FIX = {
    "unavailable": "The vendor was throttled or unreachable. Run the analysis again later, "
                   "or choose another vendor in Settings.",
    "no_data": "No configured vendor had data for this symbol and window.",
    "withheld": "Withheld on purpose: this vendor dates the data after the analysis date.",
    "empty": "The vendor's answer was empty.",
}
_CHECK_NAMES = {"claim_check": "Claim check", "sentiment_judgments": "Jev sentiment judgments",
                "debate_judgments": "Jev debate judgments", "memory": "Memory log"}
_CHECK_FIX = {
    "unavailable": "Add a TypeSafe key (TYPESAFE_API_KEY) to the .env file, then run the analysis again.",
    "disabled": "Turned off in Settings; turn Jev back on to run it.",
    "failed": "It failed during the run; the run's log says why. Run the analysis again.",
}
_CHECK_STATUS_TEXT = {"unavailable": "not available", "disabled": "turned off", "failed": "failed"}
_LINE_CHARS = 240
_FOUND_MAX = 5


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _report_file(folder: Path, kind: str) -> Path:
    return folder / ("partial_report.md" if kind == "partial" else "complete_report.md")


# --- Header, run, rating and summary ------------------------------------------

def _header(text: str, instrument: dict) -> dict:
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    title = next((line for line in lines if line.startswith("# ")), "")
    fields = {}
    for line in lines:
        if line.startswith("- "):
            key, _, value = line[2:].partition(": ")
            if key.startswith("TradingAgents "):
                fields["models"] = line[2:]
            else:
                fields[key] = value
    return {
        "ticker": instrument.get("ticker") or title.partition(": ")[2].strip() or None,
        "company": instrument.get("name"), "exchange": instrument.get("exchange"),
        "classification": instrument.get("classification"),
        "nameIsCurrent": instrument.get("name_is_current") == "true",
        "analysisDate": fields.get("Analysis date") or instrument.get("trade_date"),
        "generated": fields.get("Generated"), "models": fields.get("models"),
        "analysts": fields.get("Analysts"), "vendors": fields.get("Data vendors"),
        "memoryNote": fields.get("Memory log"),
    }


def _run(completion: dict | None) -> dict:
    if not completion:
        return {"state": None, "stopReason": None, "summary": None, "gaps": []}
    return {
        "state": completion.get("state"), "stopReason": completion.get("stop_reason"),
        "summary": None if is_complete(completion) else describe_stop(completion),
        "gaps": describe_gaps(completion),
    }


def _decision(folder: Path, report_text: str) -> str:
    try:
        return (folder / "5_portfolio" / "decision.md").read_text(encoding="utf-8")
    except OSError:
        m = re.search(r"^## V\. [^\n]*\n(.*)\Z", report_text, re.M | re.S)
        return m.group(1) if m else ""


def _rating(decision: str, partial: bool) -> dict:
    if partial or not decision.strip():
        return {"final": None, "portfolioManager": None, "partial": partial}
    own = re.search(r"^\*\*Rating\*\*:[^\n]*", decision, re.M)
    return {"final": parse_rating(decision), "portfolioManager": parse_rating(own.group(0)) if own else None,
            "partial": False}


def _field(decision: str, label: str, multiline: bool = False) -> str | None:
    pattern = rf"\*\*{label}\*\*:\s*(.*?)(?:\n\s*\n|\Z)" if multiline else rf"\*\*{label}\*\*:\s*([^\n]*)"
    m = re.search(pattern, decision, re.S)
    value = m.group(1).strip() if m else ""
    return None if not value or value.lower() == "not provided" else value


def _summary(decision: str) -> dict:
    return {
        "executiveSummary": _field(decision, "Executive Summary", multiline=True),
        "timeHorizon": _field(decision, "Time Horizon"), "priceTarget": _field(decision, "Price Target"),
        # Shown only when a source states it; no source records one yet (owner decision).
        "currency": None,
    }


# --- Entries, texts and statements -------------------------------------------

def _store(folder: Path, manifest: dict, results_root) -> Path | None:
    """The manifest's store, when it lies inside the results folder."""
    store = manifest.get("store")
    if not store or results_root is None:
        return None
    path = (folder / store).resolve()
    return path if path.is_relative_to(Path(results_root).resolve()) else None


def _entries(manifest: dict, store: Path | None) -> tuple[list[dict], dict[str, str]]:
    entries = [e for e in manifest.get("entries") or () if isinstance(e, dict) and e.get("id")]
    by_hash: dict[str, list[str]] = {}
    for e in entries:
        by_hash.setdefault(e.get("content_sha256"), []).append(e["id"])
    texts: dict[str, str] = {}
    out = []
    for e in entries:
        digest = e.get("content_sha256")
        if store is not None and digest not in texts:
            text = read_text(store, digest)
            if text is not None:
                texts[digest] = text
        out.append({**e, "sharedWith": [i for i in by_hash.get(digest, []) if i != e["id"]],
                    "textAvailable": digest in texts})
    return out, texts


def _trace(claim: str, entries: list[dict], texts: dict[str, str], lines_cache: dict) -> list[dict]:
    """Each figure in the claim, with the source lines that state it at the claim's precision."""
    traced = []
    for fig in figures(claim):
        found = []
        for e in entries:
            digest = e.get("content_sha256")
            if digest not in texts:
                continue
            if digest not in lines_cache:
                lines_cache[digest] = [(line, figures(line, claim=False)) for line in texts[digest].splitlines()]
            line = next((line for line, stated in lines_cache[digest] if figure_stated(fig, stated)), None)
            if line is not None:
                found.append({"id": e["id"], "line": line.strip()[:_LINE_CHARS]})
        result = {"figure": fig.written, "found": found[:_FOUND_MAX]}
        if len(found) > _FOUND_MAX:
            result["more"] = len(found) - _FOUND_MAX
        traced.append(result)
    return traced


def _statements(check: dict | None, recorded: bool, entries: list[dict], texts: dict[str, str]) -> dict:
    if check is None:
        status = "not_run" if recorded else "not_recorded"
        return {"status": status, "text": STATEMENTS_TEXT[status], "claims": []}
    status = check.get("status") or "not_recorded"
    if status != "ran":
        return {"status": status, "text": STATEMENTS_TEXT.get(status, status), "claims": []}
    lines_cache: dict = {}
    claims = []
    for c in check.get("claims") or ():
        verdict = c.get("verdict")
        section = c.get("section") or None
        analyst = _ANALYST_OF_REPORT.get((section or {}).get("report"))
        checked = verdict != "not_checkable"
        claims.append({
            "claim": c.get("claim", ""), "verdict": verdict, "verdictText": VERDICT_TEXT.get(verdict, verdict),
            "probability": c.get("probability"), "section": section,
            "sources": [e["id"] for e in entries if checked and analyst and e.get("analyst") == analyst],
            "figures": _trace(c.get("claim", ""), entries, texts, lines_cache) if checked else [],
        })
    claims.sort(key=lambda c: _VERDICT_ORDER.get(c["verdict"], len(_VERDICT_ORDER)))
    return {"status": status, "text": STATEMENTS_TEXT["ran"], "review": bool(check.get("review")),
            "reasons": list(check.get("reasons") or ()), "claims": claims}


# --- Headlines, coverage and missing inputs ------------------------------------

def _when(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value).timestamp() if value else float("-inf")
    except ValueError:
        return float("-inf")


def _headlines(entries: list[dict], judgments: dict | None) -> dict:
    material = {item.get("title"): item.get("material")
                for item in (judgments or {}).get("items") or ()
                if item.get("source") == "news" and item.get("title") and item.get("material") is not None}
    seen, items = set(), []
    for e in entries:
        for a in e.get("acquisitions") or ():
            if a.get("outcome") != "ok":
                continue
            for item in a.get("items") or ():
                key = item.get("url") or item.get("title")
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append({"title": item.get("title"), "publisher": item.get("publisher"),
                              "url": item.get("url"), "publishedAt": item.get("published_at"),
                              "entry": e["id"], "material": material.get(item.get("title"))})
    by_material = any(i["material"] is not None for i in items)
    items.sort(key=lambda i: (-(i["material"] if i["material"] is not None else -1) if by_material else 0,
                              -_when(i["publishedAt"])))
    return {"orderedBy": "material" if by_material else "time", "items": items}


def _window(a: dict) -> str | None:
    start, end = a.get("window_start"), a.get("window_end")
    return f"{start} to {end}" if start and end else (start or end)


def _coverage(entries: list[dict]) -> list[dict]:
    rows: dict[str, list] = {}
    for e in entries:
        answered = [a for a in e.get("acquisitions") or () if a.get("outcome") == "ok"]
        first = answered[0] if answered else {}
        rows.setdefault(e.get("analyst") or "Unknown", []).append({
            "id": e["id"], "tool": e.get("tool"),
            "sources": list(dict.fromkeys(a.get("source") for a in answered)),
            "window": _window(first), "asOf": first.get("as_of"), "fetchedAt": first.get("fetched_at"),
            "coverage": e.get("coverage"),
        })
    return [{"analyst": analyst, "entries": items} for analyst, items in rows.items()]


def _missing(entries: list[dict], completion: dict | None) -> list[dict]:
    missing = []
    for e in entries:
        coverage = e.get("coverage")
        if coverage and coverage != AVAILABLE:
            missing.append({"what": f"{e.get('analyst')}: {e.get('tool')} {_COVERAGE_TEXT.get(coverage, coverage)}",
                            "entry": e["id"], "fix": _COVERAGE_FIX.get(coverage)})
    for name, status in ((completion or {}).get("checks") or {}).items():
        if status in _CHECK_FIX:
            what = f"{_CHECK_NAMES.get(name, name)} {_CHECK_STATUS_TEXT[status]}"
            if name == "claim_check":
                what += ": the decision's statements are unchecked"
            missing.append({"what": what, "entry": None, "fix": _CHECK_FIX[status]})
    return missing


# --- Public ------------------------------------------------------------------

def report_brief(folder, kind: str, *, results_root=None) -> dict:
    """The brief of the saved report in ``folder`` (``kind`` "report" or "partial")."""
    folder = Path(folder)
    try:
        report_text = strip_sources_appendix(_report_file(folder, kind).read_text(encoding="utf-8"))
    except OSError:
        report_text = ""
    manifest = _json(folder / "evidence.json")
    recorded = isinstance(manifest, dict)
    manifest = manifest if recorded else {}
    entries, texts = _entries(manifest, _store(folder, manifest, results_root))
    completion = _json(folder / "run_status.json")
    completion = completion if isinstance(completion, dict) else None
    decision = _decision(folder, report_text)
    check = _json(folder / "claim_check.json")
    return {
        "recorded": recorded,
        "header": _header(report_text, manifest.get("instrument") or {}),
        "run": _run(completion),
        "rating": _rating(decision, kind == "partial"),
        "summary": _summary(decision),
        "notProduced": list(NOT_PRODUCED),
        "statements": _statements(check if isinstance(check, dict) else None, recorded, entries, texts),
        "headlines": _headlines(entries, _json(folder / "1_analysts" / "sentiment_judgments.json")),
        "coverage": _coverage(entries),
        "missing": _missing(entries, completion),
        "entries": entries,
    }


def source_text(folder, digest: str, *, results_root) -> str | None:
    """A source text the report in ``folder`` lists, verified; None otherwise."""
    folder = Path(folder)
    manifest = _json(folder / "evidence.json")
    if not isinstance(manifest, dict):
        return None
    if digest not in {e.get("content_sha256") for e in manifest.get("entries") or () if isinstance(e, dict)}:
        return None
    store = _store(folder, manifest, results_root)
    return None if store is None else read_text(store, digest)
