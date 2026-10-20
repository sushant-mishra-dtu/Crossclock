"""C05: the web API serves a saved report's brief, its run status and its source texts (docs/adr/0003).

Reports are written to the server's results folder by the same writers a run
uses; the server runs on loopback, with no provider or vendor called.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from tests.test_webui_server import (
    base,  # noqa: F401 — the server fixture, used through `web`
    call,
)
from tradingagents.dataflows import provenance
from tradingagents.reporting import write_partial_report, write_report_tree

TRADE_DATE = "2026-01-09"
TEXT = "Revenue growth: 22.4%"
COMPLETION = {"state": "complete_with_gaps", "stop_reason": None, "stages": {},
              "checks": {"claim_check": "unavailable"}, "gaps": ["claim_check"]}


def _entry(sha):
    return {
        "analyst": "Fundamentals Analyst", "seq": 0, "tool": "get_fundamentals",
        "arguments": {"ticker": "NVDA", "curr_date": TRADE_DATE}, "content_sha256": sha,
        "chars": len(TEXT), "stored": True, "coverage": "available",
        "acquisitions": [{"source": "yfinance", "operation": "get_fundamentals", "outcome": "ok",
                          "fetched_at": "2026-10-19T04:00:00+00:00", "window_start": None,
                          "window_end": None, "as_of": TRADE_DATE}],
    }


def _state(sha, **extra):
    return {"company_of_interest": "NVDA", "trade_date": TRADE_DATE, "fundamentals_report": "Revenue grew.",
            "final_trade_decision": "**Rating**: Buy\n\n**Executive Summary**: Accumulate.",
            "evidence": {"Fundamentals Analyst#000": _entry(sha)},
            "claim_check": {"status": "unavailable", "claims": []}, **extra}


@pytest.fixture
def web(base):  # noqa: F811 — the imported fixture, by name
    """The running server's URL."""
    return base


@pytest.fixture
def saved(tmp_path):
    """A complete report (with gaps) and a partial one, in the server's results folder."""
    root = tmp_path / "results"
    store = root / "evidence"
    sha = provenance.store_text(TEXT, store)
    write_report_tree(_state(sha), "NVDA", root / "reports" / "NVDA_20260109_120000",
                      completion=COMPLETION, evidence_store=store)
    write_partial_report(_state(sha), "NVDA", root / "reports" / "NVDA_20260109_130000_partial",
                         {"state": "partial", "stop_reason": "cancelled", "stages": {}, "checks": {}, "gaps": []},
                         evidence_store=store)
    return {"root": root, "store": store, "sha": sha}


def _ids(base_url):
    status, reports = call(base_url, "/api/reports")
    assert status == 200
    return {r["kind"]: r["id"] for r in reports}


@pytest.mark.unit
def test_the_brief_endpoint_serves_a_saved_reports_brief(web, saved):
    status, b = call(web, "/api/report/brief?id=" + quote(_ids(web)["report"]))

    assert status == 200
    assert b["recorded"] is True and [e["id"] for e in b["entries"]] == ["E1"]
    assert b["entries"][0]["textAvailable"] is True
    assert b["run"]["state"] == "complete_with_gaps"
    assert b["rating"]["final"] == "Buy" and b["summary"]["executiveSummary"] == "Accumulate."
    assert b["statements"]["status"] == "unavailable"
    assert any("TYPESAFE_API_KEY" in (m["fix"] or "") for m in b["missing"])


@pytest.mark.unit
def test_a_partial_reports_brief_has_no_rating(web, saved):
    status, b = call(web, "/api/report/brief?id=" + quote(_ids(web)["partial"]))

    assert status == 200
    assert b["rating"]["final"] is None and b["rating"]["partial"] is True
    assert b["run"]["state"] == "partial" and b["run"]["summary"] == "Stopped by the user."


@pytest.mark.unit
def test_a_saved_report_shows_its_run_status(web, saved):
    status, detail = call(web, "/api/report?id=" + quote(_ids(web)["report"]))

    assert status == 200
    assert detail["hasBrief"] is True
    assert detail["completion"]["state"] == "complete_with_gaps"
    assert detail["completion"]["gaps"] == [
        "Claim check not available (Jev is not set up); the decision's claims are unchecked"]


@pytest.mark.unit
def test_the_source_endpoint_serves_only_texts_the_report_lists_and_that_still_match(web, saved):
    report = quote(_ids(web)["report"])

    status, body = call(web, f"/api/report/source?id={report}&sha={saved['sha']}")
    assert status == 200 and body == {"sha": saved["sha"], "text": TEXT}

    for sha in ("f" * 64, "../../etc/passwd", ""):
        status, body = call(web, f"/api/report/source?id={report}&sha={quote(sha)}")
        assert status == 404 and "not available" in body["error"]
    assert call(web, f"/api/report/source?id=nope&sha={saved['sha']}")[0] == 404

    (saved["store"] / f"{saved['sha']}.txt").write_text("Revenue growth: 99%", encoding="utf-8")
    assert call(web, f"/api/report/source?id={report}&sha={saved['sha']}")[0] == 404


@pytest.mark.unit
def test_a_state_log_has_no_brief(web, saved):
    logs = saved["root"] / "NVDA" / "TradingAgentsStrategy_logs"
    logs.mkdir(parents=True)
    (logs / f"full_states_log_{TRADE_DATE}.json").write_text(
        json.dumps({"company_of_interest": "NVDA", "trade_date": TRADE_DATE}), encoding="utf-8")

    state_id = _ids(web)["state"]
    status, detail = call(web, "/api/report?id=" + quote(state_id))
    assert status == 200 and detail["hasBrief"] is False and detail["completion"] is None
    status, body = call(web, "/api/report/brief?id=" + quote(state_id))
    assert status == 404 and "no brief" in body["error"]
