"""C05: the brief a saved report opens with, and what its source drawer shows (docs/adr/0003).

The brief is assembled in code from what the run saved: the report header,
``run_status.json``, ``evidence.json``, ``claim_check.json`` and the sentiment
judgments. No model is called. Scripted models, no network.
"""

from __future__ import annotations

import json

import pytest

from tests.test_graph_end_to_end import (
    TRADE_DATE,
    ScriptedModel,
    _graph,
    offline,  # noqa: F401 — the fixture, used by name
)
from tradingagents import brief
from tradingagents.completion import run_completion
from tradingagents.dataflows import provenance, router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.feed import Feed, FeedItem
from tradingagents.reporting import write_partial_report, write_report_tree

ANALYSTS = ("market", "social", "news", "fundamentals")

FUNDAMENTALS = "\n".join([
    "Fundamentals for NVDA",
    "Revenue growth: 22.4%",
    "Revenue: $30.77 billion",
    "Operating margin: 71.2%",
])


def _fundamentals(ticker, curr_date=None):
    return FUNDAMENTALS


def _vendors(monkeypatch, **methods):
    for method, func in methods.items():
        for vendor in router.VENDOR_METHODS[method]:
            monkeypatch.setitem(router.VENDOR_METHODS[method], vendor, func)


def _saved(tmp_path, monkeypatch, *, structured=True, claim_check=None, partial=None, drop=()):
    """Run the graph offline and save its report; return (folder, results root)."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(structured=structured))
    state, _ = graph.propagate("NVDA", TRADE_DATE)
    for key in drop:
        state.pop(key, None)
    if claim_check is not None:
        state["claim_check"] = claim_check
    root = tmp_path / "results"
    if partial is not None:
        folder = root / "reports" / "NVDA_20260109_120000_partial"
        write_partial_report(state, "NVDA", folder, partial)
    else:
        folder = root / "reports" / "NVDA_20260109_120000"
        write_report_tree(state, "NVDA", folder, completion=run_completion(state, ANALYSTS))
    return folder, root


def _brief(folder, root, kind="report"):
    return brief.report_brief(folder, kind, results_root=root)


# --- Header, run state, rating, summary --------------------------------------

@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_the_brief_leads_with_the_run_state_rating_and_summary(tmp_path, monkeypatch):
    folder, root = _saved(tmp_path, monkeypatch)

    b = _brief(folder, root)

    assert b["recorded"] is True
    assert b["header"]["ticker"] == "NVDA" and b["header"]["analysisDate"] == TRADE_DATE
    assert b["header"]["company"] == "NVIDIA"
    # Without a TypeSafe key the claim check cannot run: complete, with that gap named.
    assert b["run"]["state"] == "complete_with_gaps"
    assert any("Claim check" in gap for gap in b["run"]["gaps"])
    assert b["rating"] == {"final": "Overweight", "portfolioManager": "Overweight", "partial": False}
    assert b["summary"]["executiveSummary"] == "s"
    assert b["summary"]["priceTarget"] is None and b["summary"]["timeHorizon"] is None
    assert b["summary"]["currency"] is None
    assert b["notProduced"] == ["business overview", "key risks", "next review trigger"]


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_a_partial_report_has_no_rating_and_says_why_it_stopped(tmp_path, monkeypatch):
    completion = {"state": "partial", "stop_reason": "cancelled", "stages": {"Trader": "missing"},
                  "missing": ["Trader"], "checks": {}, "gaps": []}
    folder, root = _saved(tmp_path, monkeypatch, partial=completion)

    b = _brief(folder, root, kind="partial")

    assert b["rating"] == {"final": None, "portfolioManager": None, "partial": True}
    assert b["run"]["state"] == "partial"
    assert "stopped" in b["run"]["summary"].lower()


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_a_report_saved_before_c05_says_its_sources_were_not_recorded(tmp_path, monkeypatch):
    folder, root = _saved(tmp_path, monkeypatch, drop=("evidence", "claim_check"))
    assert not (folder / "evidence.json").exists()

    b = _brief(folder, root)

    assert b["recorded"] is False
    assert b["entries"] == [] and b["coverage"] == [] and b["headlines"]["items"] == []
    assert b["statements"]["status"] == "not_recorded"
    assert b["rating"]["final"] == "Overweight"      # what the report itself holds still shows


# --- Statements: claim-check verdicts, sources behind the section, figure trace ---

CHECK = {
    "status": "ran", "candidates": 4, "review": False, "reasons": [],
    "claims": [
        {"claim": "Revenue grew 22.4% to $30.77 billion.", "verdict": "supported",
         "section": {"report": "fundamentals", "heading": "Revenue", "part": 1,
                     "label": 'fundamentals report, "Revenue"', "text": "Revenue grew 22.4%."},
         "figures_unstated": []},
        {"claim": "Operating margin was 71.3%.", "verdict": "unverified",
         "section": {"report": "fundamentals", "heading": "", "part": 2,
                     "label": "fundamentals report, part 2", "text": "Margins."},
         "figures_unstated": ["71.3%"]},
        {"claim": "Gross margin is 99.9%.", "verdict": "unsupported", "section": None,
         "figures_unstated": ["99.9%"]},
        {"claim": "We recommend accumulating.", "verdict": "not_checkable", "section": None,
         "figures_unstated": []},
        {"claim": "The stock fell after the call.", "verdict": "contradicted",
         "section": {"report": "market", "heading": "Price", "part": 1,
                     "label": 'market report, "Price"', "text": "The stock rose."},
         "figures_unstated": []},
    ],
}


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_statements_show_their_verdict_the_sources_behind_their_section_and_a_figure_trace(
        tmp_path, monkeypatch):
    _vendors(monkeypatch, get_fundamentals=_fundamentals)
    folder, root = _saved(tmp_path, monkeypatch, claim_check=CHECK)

    b = _brief(folder, root)

    claims = b["statements"]["claims"]
    # Contradicted first, then not found, unverified and supported; unchecked last.
    assert [c["verdict"] for c in claims] == [
        "contradicted", "unsupported", "unverified", "supported", "not_checkable"]
    entries = {e["id"]: e for e in b["entries"]}
    revenue = claims[3]
    assert {entries[i]["analyst"] for i in revenue["sources"]} == {"Fundamentals Analyst"}
    fundamentals = next(e["id"] for e in b["entries"] if e["tool"] == "get_fundamentals")
    assert revenue["figures"] == [
        {"figure": "22.4%", "found": [{"id": fundamentals, "line": "Revenue growth: 22.4%"}]},
        {"figure": "$30.77 billion", "found": [{"id": fundamentals, "line": "Revenue: $30.77 billion"}]},
    ]
    # A near miss at the claim's precision is not a match, and a figure in no source says so.
    assert claims[2]["figures"] == [{"figure": "71.3%", "found": []}]
    assert claims[1]["figures"] == [{"figure": "99.9%", "found": []}]
    assert claims[1]["sources"] == []               # no section: no analyst behind it
    assert {entries[i]["analyst"] for i in claims[0]["sources"]} == {"Market Analyst"}
    assert claims[4]["figures"] == [] and claims[4]["sources"] == []
    assert b["statements"]["status"] == "ran"


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_an_unchecked_decision_lists_no_statement_as_supported(tmp_path, monkeypatch):
    folder, root = _saved(tmp_path, monkeypatch)    # no Jev: the check could not run

    b = _brief(folder, root)

    assert b["statements"]["status"] == "unavailable" and b["statements"]["claims"] == []
    assert any(m["what"].startswith("Claim check") and "TYPESAFE_API_KEY" in m["fix"] for m in b["missing"])


# --- Headlines, coverage and missing inputs ----------------------------------

ITEMS = (
    FeedItem("news", "a", title="Older", author="Wire", url="https://example.com/older",
             published_at="2026-01-05T09:00:00+00:00"),
    FeedItem("news", "b", title="Undated", author="Blog", url="https://example.com/undated"),
    FeedItem("news", "c", title="Newest", author="Desk", url="https://example.com/newest",
             published_at="2026-01-08T14:30:00+00:00"),
)


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_headlines_are_newest_first_with_unknown_times_last(tmp_path, monkeypatch):
    _vendors(monkeypatch, get_news_feed=lambda *a, **k: Feed("Three headlines.", items=ITEMS))
    folder, root = _saved(tmp_path, monkeypatch)

    headlines = _brief(folder, root)["headlines"]

    assert headlines["orderedBy"] == "time"
    assert [h["title"] for h in headlines["items"]] == ["Newest", "Older", "Undated"]
    assert headlines["items"][2]["publishedAt"] is None
    assert headlines["items"][0]["url"] == "https://example.com/newest"
    assert headlines["items"][0]["entry"].startswith("E")


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_headlines_follow_jevs_material_event_judgment_when_it_ran(tmp_path, monkeypatch):
    _vendors(monkeypatch, get_news_feed=lambda *a, **k: Feed("Three headlines.", items=ITEMS))
    folder, root = _saved(tmp_path, monkeypatch)
    judged = {"items": [{"source": "news", "title": "Older", "material": 0.91},
                        {"source": "news", "title": "Newest", "material": 0.12}]}
    (folder / "1_analysts" / "sentiment_judgments.json").write_text(json.dumps(judged), encoding="utf-8")

    headlines = _brief(folder, root)["headlines"]

    assert headlines["orderedBy"] == "material"
    assert [(h["title"], h["material"]) for h in headlines["items"]] == [
        ("Older", 0.91), ("Newest", 0.12), ("Undated", None)]


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_coverage_rows_and_missing_inputs_name_each_gap(tmp_path, monkeypatch):
    def down(*args, **kwargs):
        raise VendorUnavailableError("throttled")

    _vendors(monkeypatch, get_fundamentals=down)
    folder, root = _saved(tmp_path, monkeypatch)

    b = _brief(folder, root)

    assert [row["analyst"] for row in b["coverage"]] == [
        "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"]
    fundamentals = next(row for row in b["coverage"] if row["analyst"] == "Fundamentals Analyst")
    gap = next(e for e in fundamentals["entries"] if e["tool"] == "get_fundamentals")
    assert gap["coverage"] == "unavailable"
    missing = next(m for m in b["missing"] if m.get("entry") == gap["id"])
    assert "get_fundamentals" in missing["what"] and missing["fix"]


# --- The drawer --------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_drawer_entries_name_who_else_read_them_and_serve_only_verified_listed_texts(tmp_path, monkeypatch):
    folder, root = _saved(tmp_path, monkeypatch)
    b = _brief(folder, root)
    news = [e for e in b["entries"] if e["tool"] == "get_news"]

    assert news[0]["sharedWith"] == [news[1]["id"]] and news[1]["sharedWith"] == [news[0]["id"]]
    assert all(e["textAvailable"] for e in b["entries"])
    sha = news[0]["content_sha256"]
    assert brief.source_text(folder, sha, results_root=root) == "get_news_feed data"
    assert brief.source_text(folder, "f" * 64, results_root=root) is None        # not in this report
    other = provenance.store_text("Another report's source.", root / "evidence")
    assert brief.source_text(folder, other, results_root=root) is None          # stored, but not listed here
    assert brief.source_text(folder, "../../x", results_root=root) is None
    (root / "evidence" / f"{sha}.txt").write_text("changed", encoding="utf-8")
    assert brief.source_text(folder, sha, results_root=root) is None            # no longer its hash


@pytest.mark.unit
@pytest.mark.usefixtures("offline")
def test_texts_are_never_read_from_outside_the_results_folder(tmp_path, monkeypatch):
    folder, root = _saved(tmp_path, monkeypatch)
    manifest = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    sha = manifest["entries"][0]["content_sha256"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / f"{sha}.txt").write_bytes((root / "evidence" / f"{sha}.txt").read_bytes())
    manifest["store"] = str(outside)
    (folder / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert brief.source_text(folder, sha, results_root=root) is None
    assert not any(e["textAvailable"] for e in _brief(folder, root)["entries"])
