"""C01 validates tracing boundaries; fixture times are not live speed claims."""

import json
import socket

import pytest

from scripts import benchmark_c01 as benchmark
from tradingagents.telemetry import RunTrace, use_trace


@pytest.mark.unit
def test_fixture_is_synthetic_and_fingerprint_is_stable():
    fixture = benchmark.load_fixture()
    assert fixture["license"] == "CC0-1.0"
    assert "synthetic" in fixture["provenance"]
    assert benchmark.fingerprint({"b": 2, "a": 1}) == benchmark.fingerprint({"a": 1, "b": 2})
    assert fixture["configuration"]["jev_enabled"] is False


@pytest.mark.unit
def test_real_graph_cold_and_warm_keep_identical_semantic_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "9")
    fixture = benchmark.load_fixture()
    with benchmark.offline_network():
        cold, cache = benchmark.run_record("graph", "cold_fixture_cache", 1, fixture, tmp_path)
        warm, _ = benchmark.run_record("graph", "warm_fixture_cache", 1, fixture, tmp_path, cache)

    assert cold["status"] == warm["status"] == "completed"
    assert cold["output_sha256"] == warm["output_sha256"]
    assert cold["configuration_sha256"] == warm["configuration_sha256"]
    assert cold["metrics"]["fetches"] > 0
    assert warm["metrics"]["fetches"] == 0
    assert warm["metrics"]["cache_hits"] == warm["metrics"]["requests"]
    assert cold["metrics"]["scripted_model_calls"] == warm["metrics"]["scripted_model_calls"]
    output = json.loads((tmp_path / cold["output_path"]).read_text())
    assert output["rating"] == "Overweight"
    assert all(output["reports"].values())
    config = json.loads((tmp_path / cold["run_id"] / "configuration.json").read_text())
    assert config["max_debate_rounds"] == 1
    assert cold["first_useful_result_ms"] is None
    assert cold["available_report_ms"] is not None
    assert (tmp_path / cold["run_id"] / "reports").is_dir()


@pytest.mark.unit
def test_fixture_only_request_coalescing_preserves_all_outputs():
    fixture = benchmark.load_fixture()
    results = []
    for mode in ("uncoalesced", "coalesced"):
        trace = RunTrace()
        with use_trace(trace):
            output, metrics = benchmark.controlled_case("duplicate_requests", mode, fixture, trace)
        trace.finish("completed")
        results.append((output, metrics, trace.snapshot()))

    assert results[0][0] == results[1][0]
    assert results[0][1]["fetches"] == fixture["controls"]["duplicate_requests"]
    assert results[1][1]["fetches"] == 1
    assert results[1][1]["coalesced"] == fixture["controls"]["duplicate_requests"] - 1
    for _, _, snapshot in results:
        span_ids = {span["span_id"] for span in snapshot["spans"]}
        assert all(span["parent_id"] in span_ids for span in snapshot["spans"]
                   if span["parent_id"] is not None)
        assert all(span["end_ms"] is not None for span in snapshot["spans"])


@pytest.mark.unit
@pytest.mark.parametrize("case", ["slow_source", "rate_limit", "missing_data", "cancellation"])
def test_controlled_failures_are_attributed_and_raw_outputs_retained(tmp_path, case):
    fixture = benchmark.load_fixture()
    record, _ = benchmark.run_record(case, "controlled", 1, fixture, tmp_path)
    assert record["status"] == ("cancelled" if case == "cancellation" else "completed")
    trace = json.loads((tmp_path / record["trace_path"]).read_text())
    kinds = {span["kind"] for span in trace["spans"]}
    assert "source_fetch" in kinds
    if case == "slow_source":
        assert {"queue_wait", "model_inference"} <= kinds
    elif case == "rate_limit":
        assert "retry_backoff" in kinds
        assert record["metrics"]["rate_limits"] == record["metrics"]["retries"] == 2
        assert any(e["attributes"].get("http_status") == 429 for e in trace["events"])
    elif case == "missing_data":
        assert record["metrics"]["missing"] == 1
    else:
        assert "cancellation" in kinds
        assert "model_inference" not in kinds
        assert record["metrics"]["model_calls_after_cancel"] == 0
        assert record["metrics"]["fetches"] == 1
    assert (tmp_path / record["output_path"]).exists()
    assert "ui_render_unobserved" in trace["telemetry_gaps"]
    assert record["first_useful_result_ms"] is None


@pytest.mark.unit
def test_offline_guard_refuses_network():
    with benchmark.offline_network(), pytest.raises(OSError, match="C01 offline"):
        socket.getaddrinfo("example.com", 443)


@pytest.mark.unit
def test_small_sample_summary_has_explicit_tail_method():
    runs = [{"case": "graph", "mode": "cold", "wall_ms": duration,
             "status": "completed", "available_report_ms": duration / 2,
             "output_sha256": "matched"} for duration in (9, 1, 3)]
    group, = benchmark.summarize_runs(runs)
    assert group["n"] == 3
    assert group["median_wall_ms"] == 3
    assert group["p95_wall_ms"] == 9
    assert "small n" in group["p95_method"]
    assert group["first_useful_result_ms"] is None


@pytest.mark.unit
def test_phase_totals_keep_parallel_span_duration_separate_from_wall_time():
    durations = benchmark.phase_durations({"spans": [
        {"kind": "source_fetch", "duration_ms": 10},
        {"kind": "source_fetch", "duration_ms": 12},
        {"kind": "model_inference", "duration_ms": 4},
    ]})
    assert durations["source_fetch"]["inclusive_total_ms"] == 22
    assert durations["source_fetch"]["max_span_ms"] == 12
    assert durations["model_inference"]["count"] == 1


@pytest.mark.unit
def test_runner_never_overwrites_retained_evidence(tmp_path):
    fixture = benchmark.load_fixture()
    benchmark.run_record("missing_data", "controlled", 1, fixture, tmp_path)
    with pytest.raises(FileExistsError):
        benchmark.run_record("missing_data", "controlled", 1, fixture, tmp_path)
