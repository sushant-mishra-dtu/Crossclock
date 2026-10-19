"""Qualified pairs and the full failed/background spend ledger stay separate."""

import json

import pytest

from scripts.summarize_c01_baseline import digest, run_record, summarize


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _run(directory, pair, mode, status="completed", start=100, finish_reason=None, explicit=False):
    run_id = f"run-{mode}-{pair}"  # Old and new attempt run IDs intentionally collide.
    target = directory / run_id
    configuration = {"llm_provider": "nvidia", "max_tokens": 2048}
    sent = status != "not_started"
    spans = [{"span_id": "setup", "parent_id": None, "kind": "setup", "start_ms": start,
              "duration_ms": 1, "attributes": {}, "status": "ok"}] if sent else []
    if sent:
        spans += [
            {"span_id": "node", "parent_id": None, "kind": "graph_node", "name": "Bear Researcher",
             "duration_ms": 25, "start_ms": start + 1, "status": "ok", "attributes": {}},
            {"span_id": "invocation", "parent_id": "node", "kind": "llm_invocation", "status": "ok", "attributes": {}},
            {"span_id": "generation", "parent_id": "invocation", "kind": "llm", "status": "ok",
             "attributes": {"usage_status": "reported", "input_tokens": 100, "output_tokens": 10,
                            "cached_input_tokens": 30, **({"finish_reason": finish_reason} if finish_reason else {})}},
            {"span_id": "http", "parent_id": "generation", "kind": "transport_attempt", "status": "ok",
             "attributes": {"provider": "nvidia"}},
        ]
    if status == "failed":
        spans.append({"span_id": "denied", "parent_id": "generation", "kind": "transport_attempt",
                      "status": "not_sent", "attributes": {"provider": "nvidia"}})
    milestones = {"measurement_started": {"offset_ms": start}} if explicit and sent else {}
    if status == "completed":
        milestones.update(generated_report={"offset_ms": start + 10},
                          graph_returned={"offset_ms": start + 45}, persisted_report={"offset_ms": start + 50})
    _write(target / "trace.json", {"spans": spans, "milestones": milestones,
                                   "duration_ms": start + 55, "telemetry_gaps": []})
    sample = {"run_id": run_id, "mode": mode, "repetition": pair, "status": status,
              "wall_ms": start + 56, "configuration_sha256": digest(configuration),
              "workload_sha256": "same-workload", "corpus_sha256": "same-corpus"}
    if explicit:
        sample["end_to_end_ms"] = start + 60
    _write(target / "sample.json", sample)
    _write(target / "configuration.json", configuration)
    _write(target / "output.json", {"rating": "Hold", "reports": {"final_trade_decision": "**Claim Check**: 1 supported."}})
    return target / "sample.json"


def _session(directory, specifications, reservation, estimated):
    _write(directory / "registration.json", {"configuration": {"llm_provider": "nvidia", "max_tokens": 2048},
        "configuration_sha256": digest({"llm_provider": "nvidia", "max_tokens": 2048}),
        "source_revision": "revision", "source_tree_sha256": directory.name,
        "source_files_sha256": {"cli/stats_handler.py": directory.name},
        "registered_runs": len(specifications)})
    for pair, mode, status in specifications:
        _run(directory, pair, mode, status, finish_reason="length" if directory.name == "new" else None,
             explicit=directory.name == "new")
    sent = sum(status != "not_started" for _, _, status in specifications)
    _write(directory / "summary.json", {"nvidia_budget": {"http_attempts": sent},
        "jev_budget": {"http_attempts": 0, "reserved_worst_case_usd": reservation},
        "jev_reported_estimated_cost_usd": estimated})


def _attempts(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    cold, warm = "cold_fixture_cache", "warm_fixture_cache"
    _session(old, [(1, cold, "completed"), (1, warm, "completed"), (2, cold, "completed"),
                   (2, warm, "failed"), (3, cold, "not_started"), (3, warm, "not_started")], 0.1, 0.001)
    _session(new, [(pair, mode, "completed") for pair in (2, 3) for mode in (cold, warm)], 0.2, 0.002)
    return old, new


@pytest.mark.unit
def test_selected_three_pairs_keep_old_unmatched_failure_and_send_costs(tmp_path):
    old, new = _attempts(tmp_path)
    probe = tmp_path / "probe"
    _write(probe / "summary.json", {"http_attempts": 6, "reserved_worst_case_usd": 0.01,
        "reported_estimated_cost_usd": 0.0001, "samples": [{"status": "ok"}] * 6})
    result = summarize(old, new, probe, probe)
    assert result["baseline_status"] == "three_complete_comparable_pairs"
    assert len(result["selected_runs"]) == 6
    assert len(result["historical_excluded_runs"]) == 4
    extra, = [run for run in result["historical_excluded_runs"] if run["status"] == "completed"]
    assert extra["pair"] == 2 and extra["session"] == "old"
    assert all(group["n"] == 3 for group in result["groups"])
    assert result["groups"][0]["medians"]["research_setup_to_persist_ms"] == 50
    assert result["groups"][0]["counts"]["logical_llm_invocations"] == 3
    assert result["groups"][0]["counts"]["reported_length_finish_count"] == 2
    assert result["groups"][0]["counts"]["finish_reason_coverage"] == "partial"
    assert all(run["timing"]["evidence_available_ms"] is None for run in result["selected_runs"])
    assert all(run["claim_check_section"] == "present" for run in result["selected_runs"])
    totals = result["totals_including_excluded_and_components"]
    assert totals["nvidia_http_attempts"] == 14  # Eight graph sends + six probe sends.
    assert totals["jev_http_attempts"] == 6
    assert totals["graph_counts"]["refused_http_sends"] == 1
    assert totals["jev_reserved_worst_case_usd"] == 0.31
    assert totals["jev_reported_estimated_cost_usd"] == 0.0031
    assert totals["nvidia_cost_usd"] is None


@pytest.mark.unit
def test_measurement_timing_prefers_explicit_endpoint_and_retains_startup(tmp_path):
    path = _run(tmp_path, 2, "cold_fixture_cache", start=100000, explicit=True)
    record = run_record(path, tmp_path)
    assert record["timing"]["research_setup_to_persist_ms"] == 50
    assert record["timing"]["generated_report_ms"] == 10
    assert record["timing"]["pre_setup_ms"] == 100000
    assert record["timing"]["end_to_end_wall_ms"] == 100060
    assert record["timing"]["legacy_wall_ms"] == 100056


@pytest.mark.unit
def test_configuration_mismatch_cannot_be_silently_pooled_and_guard_counts_include_background(tmp_path):
    old, new = _attempts(tmp_path)
    config_path = next(new.glob("*/configuration.json"))
    _write(config_path, {"llm_provider": "nvidia", "max_tokens": 1024})
    summary_path = old / "summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["nvidia_budget"]["http_attempts"] += 2
    _write(summary_path, summary)
    result = summarize(old, new)
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert result["groups"] == []
    assert result["research_configuration"]["comparable"] is False
    assert result["all_attempts_ledger"][0]["send_count_reconciliation"]["nvidia_guard_minus_trace"] == 2
    assert result["totals_including_excluded_and_components"]["nvidia_http_attempts"] == 10
    assert result["totals_including_excluded_and_components"]["ledger_complete"] is False


@pytest.mark.unit
def test_complete_pairs_do_not_qualify_with_unreconciled_send_ledger(tmp_path):
    old, new = _attempts(tmp_path)
    path = old / "summary.json"
    summary = json.loads(path.read_bytes())
    summary["nvidia_budget"]["http_attempts"] += 1
    _write(path, summary)
    result = summarize(old, new)
    assert result["qualified_pair_count"] == 3
    assert result["research_configuration"]["comparable"] is True
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert "send_ledger_incomplete_or_not_reconciled" in result["qualification_gaps"]


@pytest.mark.unit
def test_missing_new_attempt_retains_only_old_complete_pair_and_never_claims_three(tmp_path):
    old, _ = _attempts(tmp_path)
    result = summarize(old, tmp_path / "not_registered")
    assert result["qualified_pair_count"] == 1
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert len(result["selected_runs"]) == 2
    totals = result["totals_including_excluded_and_components"]
    assert totals["known_jev_reservation_subtotal_usd"] == 0.1
    assert totals["jev_reserved_worst_case_usd"] is None
    assert totals["within_jev_cap"] is None
    assert totals["reservation_ledger_status"] == "partial_or_missing"
    with pytest.raises(ValueError, match="distinct"):
        summarize(old, old)


@pytest.mark.unit
def test_failed_additional_attempt_affects_only_history_and_shared_continuation_budget(tmp_path):
    old, new = _attempts(tmp_path)
    failed = tmp_path / "earlier-continuation"
    _session(failed, [(2, "cold_fixture_cache", "failed"), (2, "warm_fixture_cache", "not_started")], 0, 0)
    result = summarize(old, new, additional_sessions=[failed])
    assert result["baseline_status"] == "three_complete_comparable_pairs"
    assert len(result["selected_runs"]) == 6
    assert len(result["historical_excluded_runs"]) == 6
    assert not any(run["session"] == failed.name for run in result["selected_runs"])
    assert result["continuation_allowance"]["nvidia_http_attempts"] == 5  # Four retry plus one failed send.
    assert result["continuation_allowance"]["within_cap"] is True
    assert result["totals_including_excluded_and_components"]["nvidia_http_attempts"] == 9
    assert [ledger["directory"] for ledger in result["all_attempts_ledger"]] == [str(old), str(new), str(failed)]
    with pytest.raises(ValueError, match="double-count"):
        summarize(old, new, additional_sessions=[failed, failed])
    with pytest.raises(ValueError, match="double-count"):
        summarize(old, new, additional_sessions=[old])


@pytest.mark.unit
def test_continuation_cannot_qualify_above_shared_nvidia_allowance(tmp_path):
    old, new = _attempts(tmp_path)
    failed = tmp_path / "earlier-continuation"
    _session(failed, [(2, "cold_fixture_cache", "failed")], 0, 0)
    # Every send has a retained trace, so this fails the allowance independently
    # of ledger reconciliation or pair completeness.
    path = next(failed.glob("*/trace.json"))
    trace = json.loads(path.read_bytes())
    trace["spans"] += [{"span_id": f"background-{i}", "parent_id": "generation",
                        "kind": "transport_attempt", "status": "ok", "attributes": {"provider": "nvidia"}}
                       for i in range(192)]
    _write(path, trace)
    summary_path = failed / "summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["nvidia_budget"]["http_attempts"] = 193
    _write(summary_path, summary)
    result = summarize(old, new, additional_sessions=[failed])
    assert result["qualified_pair_count"] == 3
    assert result["totals_including_excluded_and_components"]["ledger_complete"] is True
    assert result["continuation_allowance"]["within_cap"] is False
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert "continuation_nvidia_send_cap_exceeded" in result["qualification_gaps"]


def _set_sample(directory, pair, mode, **fields):
    path = directory / f"run-{mode}-{pair}" / "sample.json"
    _write(path, {**json.loads(path.read_bytes()), **fields})


def _recovery_cohort(tmp_path, warm_seed="seed-2"):
    """Recovery pair 1 and cold 2 in one attempt; warm 2 (seeded) and pair 3 in the next."""
    cold, warm = "cold_fixture_cache", "warm_fixture_cache"
    old = tmp_path / "old"
    _session(old, [(1, cold, "completed"), (1, warm, "completed")], 0.1, 0.001)
    recovery, completion = tmp_path / "recovery", tmp_path / "completion"
    _session(recovery, [(1, cold, "completed"), (1, warm, "completed"), (2, cold, "completed"),
                        (2, warm, "failed")], 0.1, 0.001)
    _session(completion, [(2, warm, "completed"), (3, cold, "completed"), (3, warm, "completed")], 0.1, 0.001)
    _set_sample(recovery, 1, cold, fixture_cache_sha256="seed-1")
    _set_sample(recovery, 1, warm, input_fixture_cache_sha256="seed-1")
    _set_sample(recovery, 2, cold, fixture_cache_sha256="seed-2")
    _set_sample(recovery, 2, warm, input_fixture_cache_sha256="seed-2")
    if warm_seed is not None:
        _set_sample(completion, 2, warm, input_fixture_cache_sha256=warm_seed)
    _set_sample(completion, 3, cold, fixture_cache_sha256="seed-3")
    _set_sample(completion, 3, warm, input_fixture_cache_sha256="seed-3")
    selection = {(1, cold): recovery, (1, warm): recovery, (2, cold): recovery, (2, warm): completion,
                 (3, cold): completion, (3, warm): completion}
    return old, recovery, completion, selection


def _allowance(tmp_path, cap, sessions=("completion",)):
    path = tmp_path / "allowance.json"
    _write(path, {"name": "recovery_completion", "maximum_nvidia_http_attempts": cap, "sessions": list(sessions)})
    return path


@pytest.mark.unit
def test_seeded_warm_run_completes_a_pair_across_attempts_under_its_own_allowance(tmp_path):
    old, recovery, completion, selection = _recovery_cohort(tmp_path)
    result = summarize(old, recovery, additional_sessions=[completion], selection=selection,
                       allowances=[_allowance(tmp_path, 3)])
    assert result["baseline_status"] == "three_complete_comparable_pairs", result["qualification_gaps"]
    assert [pair["seed_link"] for pair in result["pairs"]] == ["verified"] * 3
    assert result["pairs"][1]["sessions"] == {"cold_fixture_cache": "recovery", "warm_fixture_cache": "completion"}
    assert not any(run["session"] == "old" for run in result["selected_runs"])
    failed, = [run for run in result["historical_excluded_runs"] if run["status"] == "failed"]
    assert failed["session"] == "recovery" and failed["pair"] == 2
    # The recovery attempt stays under the first continuation; the completion has its own cap.
    assert result["continuation_allowance"]["sessions"] == ["recovery"]
    assert result["continuation_allowance"]["nvidia_http_attempts"] == 4
    later, = result["later_nvidia_allowances"]
    assert later["sessions"] == ["completion"] and later["nvidia_http_attempts"] == 3 and later["within_cap"]
    assert result["totals_including_excluded_and_components"]["nvidia_http_attempts"] == 9


@pytest.mark.unit
def test_later_allowance_cap_is_enforced_separately(tmp_path):
    old, recovery, completion, selection = _recovery_cohort(tmp_path)
    result = summarize(old, recovery, additional_sessions=[completion], selection=selection,
                       allowances=[_allowance(tmp_path, 2)])
    assert result["qualified_pair_count"] == 3
    assert result["later_nvidia_allowances"][0]["within_cap"] is False
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert "continuation_nvidia_send_cap_exceeded" in result["qualification_gaps"]


@pytest.mark.unit
@pytest.mark.parametrize("warm_seed, gap", [("seed-other", "warm_seed_does_not_match_cold_cache"),
                                            (None, "cross_attempt_pair_seed_unverified")])
def test_cross_attempt_pair_needs_its_warm_seed_to_match_the_cold_cache(tmp_path, warm_seed, gap):
    old, recovery, completion, selection = _recovery_cohort(tmp_path, warm_seed)
    result = summarize(old, recovery, additional_sessions=[completion], selection=selection,
                       allowances=[_allowance(tmp_path, 3)])
    assert result["qualified_pair_count"] == 3
    assert result["baseline_status"] == "incomplete_or_not_comparable"
    assert gap in result["qualification_gaps"]


@pytest.mark.unit
def test_selection_and_allowances_must_name_supplied_sessions_once(tmp_path):
    old, recovery, completion, selection = _recovery_cohort(tmp_path)
    with pytest.raises(ValueError, match="supplied session"):
        summarize(old, recovery, selection=selection)
    with pytest.raises(ValueError, match="not supplied"):
        summarize(old, recovery, additional_sessions=[completion], selection=selection,
                  allowances=[_allowance(tmp_path, 3, ("completion", "elsewhere"))])
    with pytest.raises(ValueError, match="only one"):
        summarize(old, recovery, additional_sessions=[completion], selection=selection,
                  allowances=[_allowance(tmp_path, 3, ("completion", "completion"))])
