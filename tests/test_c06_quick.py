"""Quick's output, process lifetime, persistence and decision boundaries."""
import json
import threading
import time
from pathlib import Path

import pytest

from tradingagents.quick import QuickResult, bounded_call, run_quick, validate_summary
from tradingagents.run_control import RunCancelled


def _blocked_writer(emit, path):
    emit({"ready": True})
    time.sleep(5)
    Path(path).write_text("late", encoding="utf-8")
    return "should not arrive"


def test_stop_terminates_actual_source_process_before_it_can_write(tmp_path):
    stop = threading.Event()
    ready = []

    def seen(event):
        ready.append(event)
        stop.set()

    with pytest.raises(RunCancelled):
        bounded_call(_blocked_writer, (str(tmp_path / "late"),), timeout=10,
                     cancel=stop, on_event=seen)
    assert ready == [{"ready": True}]
    assert not (tmp_path / "late").exists()


def test_timeout_terminates_a_process_not_just_its_future(tmp_path):
    with pytest.raises(TimeoutError):
        bounded_call(_blocked_writer, (str(tmp_path / "late"),), timeout=0.05,
                     cancel=threading.Event())
    assert not (tmp_path / "late").exists()


def _summary(text="Revenue was 100 crore.", ids=None, **extra):
    return {"observations": [{"text": text, "evidence_ids": ids or ["E1"]}],
            "gaps": [], "questions": [], **extra}


def test_checks_do_not_turn_number_presence_into_qualitative_support():
    checked = validate_summary(_summary(), {"E1": "Revenue was 100 crore."})
    assert checked["valid"]
    assert checked["qualitative_support"] == "not_checked"
    assert checked["observations"][0]["figures"][0]["found"]


@pytest.mark.parametrize("summary", [
    _summary(ids=["E999"]), _summary(rating="BUY"),
    _summary("BUY this stock and allocate 10% of your portfolio."),
    _summary("Price target: 120; stop loss 90."),
    {"observations": [], "gaps": [], "questions": []},
])
def test_invalid_or_trade_like_synthesis_is_withheld(summary):
    checked = validate_summary(summary, {"E1": "Revenue 100 crore. Price 120. 10%"})
    assert not checked["valid"]
    assert not checked["observations"]


def test_numeric_near_miss_is_flagged_without_claiming_support():
    checked = validate_summary(_summary("Revenue was 101 crore."), {"E1": "Revenue was 100 crore."})
    assert checked["valid"]
    assert checked["gaps"]
    assert not checked["observations"][0]["figures"][0]["found"]


def _fixture_phase(calls, summary=None, unavailable=False):
    def run(target, args, *, on_event=None, **kwargs):
        calls.append(target.__name__)
        if target.__name__ == "acquire":
            on_event({"type": "evidence", "tool": "local_facts", "text": "Revenue was 100 crore.",
                      "coverage": "unavailable" if unavailable else "available",
                      "arguments": {"ticker": "AAPL", "as_of": "2026-10-16"},
                      "acquisitions": [{"source": "local", "operation": "local_facts",
                                        "outcome": "ok", "fetched_at": "2026-10-16T10:00:00+00:00"}]})
            return None
        return {"summary": summary if summary is not None else _summary(), "usage": {}}
    return run


def test_quick_complete_persists_distinct_artifact_and_sources_without_rating(tmp_path):
    calls = []
    config = {"research_mode": "quick", "results_dir": str(tmp_path)}
    result = run_quick("AAPL", "2026-10-16", config=config, phase_runner=_fixture_phase(calls))
    assert isinstance(result, QuickResult)
    assert calls == ["acquire", "synthesize"]
    assert result.completion["state"] in ("complete", "complete_with_gaps")
    assert result.completion["decision_eligible"] is False
    assert result.completion["mode"] == "quick"
    assert result.path.name == "quick_brief.md"
    assert result.state["rating"] is None
    assert not list(tmp_path.rglob("complete_report*"))
    assert not list(tmp_path.rglob("full_states_log*"))
    assert not list(tmp_path.rglob("*memory*"))
    manifest = json.loads((result.path.parent / "evidence.json").read_text())
    assert manifest["entries"][0]["id"] == "E1"
    from tradingagents.brief import source_text
    assert source_text(result.path.parent, manifest["entries"][0]["content_sha256"], results_root=tmp_path)


def test_no_usable_evidence_never_sends_a_model_request(tmp_path):
    calls = []
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)},
                       phase_runner=_fixture_phase(calls, unavailable=True))
    assert calls == ["acquire"]
    assert result.completion["state"] == "none"
    assert result.state["quick_summary"] is None


def test_rejected_model_output_keeps_sources_and_saves_partial_without_raw_output(tmp_path):
    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)},
                       phase_runner=_fixture_phase([], summary=_summary(rating="BUY")))
    assert result.completion["state"] == "partial"
    assert result.state["quick_summary"] is None
    assert "BUY" not in result.path.read_text(encoding="utf-8")
    assert (result.path.parent / "evidence.json").exists()


def test_cancelled_run_keeps_acquired_facts_and_never_starts_synthesis(tmp_path):
    stop = threading.Event()
    original = _fixture_phase([])

    def cancelled(target, args, **kwargs):
        result = original(target, args, **kwargs)
        stop.set()
        return result

    result = run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path)},
                       phase_runner=cancelled, cancel=stop)
    assert result.completion["state"] == "partial"
    assert result.completion["stop_reason"] == "cancelled"
    assert result.path.exists()


def test_resume_request_is_rejected_before_creating_anything(tmp_path):
    with pytest.raises(ValueError, match="resume"):
        run_quick("AAPL", "2026-10-16", config={"results_dir": str(tmp_path / "absent")}, resume=True)
    assert not (tmp_path / "absent").exists()
