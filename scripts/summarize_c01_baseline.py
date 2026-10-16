"""Read retained C01 results and summarize the explicitly selected three pairs.

This helper imports no provider SDK and never changes evidence files. Its JSON
is a derived result: old unmatched/failed work remains in the attempt ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODES = ("cold_fixture_cache", "warm_fixture_cache")
CONTINUATION_NVIDIA_CAP = 192
PATH_KEYS = {"results_dir", "data_cache_dir", "memory_log_path", "india_db_path", "screener_db_path"}


def read_json(path):
    return json.loads(Path(path).read_bytes())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def research_config(config):
    normalized = dict(config)
    for key in PATH_KEYS & normalized.keys():
        normalized[key] = f"<run_directory>/{key}"
    return normalized


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _offset(trace, name):
    return _number(trace.get("milestones", {}).get(name, {}).get("offset_ms"))


def _delta(end, start):
    return round(end - start, 3) if end is not None and start is not None and end >= start else None


def _sum_decimal(values):
    return float(sum((Decimal(str(value)) for value in values if value is not None), Decimal("0")))


def trace_counts(trace):
    spans = trace.get("spans", [])
    by_id = {span["span_id"]: span for span in spans}
    invocations = {span["span_id"] for span in spans if span["kind"] == "llm_invocation"}
    generations = [span for span in spans if span["kind"] == "llm"]

    def inside(span):
        parent, seen = span.get("parent_id"), set()
        while parent in by_id and parent not in seen:
            if parent in invocations:
                return True
            seen.add(parent)
            parent = by_id[parent].get("parent_id")
        return False

    http = Counter()
    refused = 0
    for span in spans:
        if span["kind"] != "transport_attempt":
            continue
        if span.get("status") == "not_sent":
            refused += 1
        else:
            http[span.get("attributes", {}).get("provider") or "unknown"] += 1
    usage = {"reported_input_tokens": 0, "reported_output_tokens": 0,
             "reported_cached_input_tokens": 0, "usage_missing_calls": 0,
             "cached_usage_missing_calls": 0, "finish_reason_missing_calls": 0}
    finish = Counter()
    for span in generations:
        attributes = span.get("attributes", {})
        for key, target in (("input_tokens", "reported_input_tokens"),
                            ("output_tokens", "reported_output_tokens"),
                            ("cached_input_tokens", "reported_cached_input_tokens")):
            value = _number(attributes.get(key))
            if value is not None:
                usage[target] += value
        usage["usage_missing_calls"] += attributes.get("usage_status") != "reported"
        usage["cached_usage_missing_calls"] += _number(attributes.get("cached_input_tokens")) is None
        reason = attributes.get("finish_reason")
        if isinstance(reason, str) and reason:
            finish[reason] += 1
        else:
            usage["finish_reason_missing_calls"] += 1
    return {"logical_llm_invocations": len(invocations) + sum(not inside(span) for span in generations),
            "llm_generation_attempts": len(generations), "observed_http_attempts": sum(http.values()),
            "http_by_provider": dict(http), "refused_http_sends": refused,
            "jev_logical_calls": sum(span["kind"] == "jev" for span in spans),
            "usage": usage, "finish_reasons": dict(finish)}


def combined_counts(runs):
    scalars, usage, providers, finish = Counter(), Counter(), Counter(), Counter()
    for run in runs:
        counts = run["counts"]
        for name in ("logical_llm_invocations", "llm_generation_attempts", "observed_http_attempts",
                     "refused_http_sends", "jev_logical_calls"):
            scalars[name] += counts[name]
        usage.update(counts["usage"])
        providers.update(counts["http_by_provider"])
        finish.update(counts["finish_reasons"])
    return {**dict(scalars), "http_by_provider": dict(providers), "usage": dict(usage),
            "finish_reasons": dict(finish), "reported_length_finish_count": finish["length"],
            "finish_reason_coverage": "partial" if usage["finish_reason_missing_calls"] else "reported"}


def run_record(sample_path, session):
    sample_path = Path(sample_path)
    sample, trace = read_json(sample_path), read_json(sample_path.with_name("trace.json"))
    setup = [span["start_ms"] for span in trace["spans"]
             if span["kind"] == "setup" and _number(span.get("start_ms")) is not None]
    explicit_start = _offset(trace, "measurement_started")
    start = explicit_start if explicit_start is not None else min(setup, default=None)
    persisted = _offset(trace, "persisted_report")
    nodes = defaultdict(float)
    for span in trace["spans"]:
        if span["kind"] == "graph_node" and _number(span.get("duration_ms")) is not None:
            nodes[span["name"]] += span["duration_ms"]
    config_path = sample_path.with_name("configuration.json")
    config = research_config(read_json(config_path)) if config_path.exists() else None
    config_hash = digest(config) if config is not None else None
    declared_hash = sample.get("configuration_sha256")
    counts = trace_counts(trace)
    output_path = sample_path.with_name("output.json")
    output = read_json(output_path) if output_path.exists() else {}
    final_report = output.get("reports", {}).get("final_trade_decision")
    gaps = set(trace.get("telemetry_gaps", []))
    if counts["usage"]["finish_reason_missing_calls"]:
        gaps.add("finish_reason_unobserved_for_some_generations")
    gaps.update({"browser_first_useful_result_unobserved", "quick_brief_path_absent",
                 "full_original_source_validation_unobserved"})
    return {"session": Path(session).name, "attempt_directory": str(Path(session).resolve()),
            "run_id": sample["run_id"], "pair": sample["repetition"],
            "mode": sample["mode"], "status": sample["status"],
            "rating": output.get("rating"),
            "claim_check_section": ("present" if "**Claim Check**:" in final_report else "absent")
            if isinstance(final_report, str) else "unknown",
            "claim_check_scope": "Analyst-report comparison; original-source/full-material validation remains unobserved.",
            "timing": {"end_to_end_wall_ms": sample.get("end_to_end_ms", sample.get("wall_ms")),
                       "legacy_wall_ms": sample.get("wall_ms"),
                       "end_to_end_basis": "measure_run_entry_to_trace_finish" if "end_to_end_ms" in sample
                       else "legacy_per_run_wall_clock; earlier registration/config work is outside this clock",
                       "trace_duration_ms": trace.get("duration_ms"),
                       "pre_setup_ms": min(setup, default=None),
                       "measurement_start_offset_ms": start,
                       "measurement_start_basis": "explicit_milestone" if explicit_start is not None else "first_setup_span",
                       "research_setup_to_persist_ms": _delta(persisted, start),
                       "generated_report_ms": _delta(_offset(trace, "generated_report"), start),
                       "evidence_available_ms": _delta(_offset(trace, "evidence_available"), start),
                       "graph_returned_ms": _delta(_offset(trace, "graph_returned"), start),
                       "persisted_report_offset_ms": persisted, "first_useful_result_ms": None},
            "configuration_sha256": config_hash, "declared_configuration_sha256": declared_hash,
            "configuration_hash_verified": config_hash is not None and config_hash == declared_hash,
            "workload_sha256": sample.get("workload_sha256"), "corpus_sha256": sample.get("corpus_sha256"),
            "fixture_cache_sha256": sample.get("fixture_cache_sha256"),
            "input_fixture_cache_sha256": sample.get("input_fixture_cache_sha256"),
            "input_fixture_cache_source": sample.get("input_fixture_cache_source"),
            "source_metrics": sample.get("source_metrics", {}), "counts": counts,
            "top_level_node_ms": dict(nodes), "telemetry_gaps": sorted(gaps),
            "attribution_correction": trace.get("attribution_correction", {}).get("count", 0),
            "sample_path": str(sample_path), "trace_path": str(sample_path.with_name("trace.json")),
            "trace_sha256": file_sha(sample_path.with_name("trace.json"))}


def load_session(directory):
    directory = Path(directory)
    registration_path = directory / "registration.json"
    if not registration_path.exists():
        return {"directory": str(directory), "status": "not_registered", "runs": [], "registration": {}, "summary": {}}
    registration = read_json(registration_path)
    summary_path = directory / "summary.json"
    summary = read_json(summary_path) if summary_path.exists() else {}
    runs = [run_record(path, directory) for path in sorted(directory.glob("*/sample.json"))]
    return {"directory": str(directory), "attempt_directory": str(directory.resolve()),
            "status": "finished" if summary else "partial",
            "runs": runs, "registration": registration, "summary": summary,
            "manifest": {"registration_path": str(registration_path), "registration_sha256": file_sha(registration_path),
                         **{key: registration.get(key) for key in ("source_revision", "source_dirty", "source_tree_sha256",
                                                                  "dependency_lock_sha256", "configuration_sha256",
                                                                  "workload_sha256", "corpus_sha256", "hardware")},
                         "startup_path": str(directory / "startup.json") if (directory / "startup.json").exists() else None,
                         "startup_trace_path": str(directory / "startup_trace.json")
                         if (directory / "startup_trace.json").exists() else None,
                         "session_startup": registration.get("session_startup"),
                         "timing_protocol_sha256": registration.get("timing_protocol_sha256")}}


def _ledger(session):
    summary, runs = session["summary"], session["runs"]
    traced = Counter()
    for run in runs:
        traced.update(run["counts"]["http_by_provider"])
    nvidia = summary.get("nvidia_budget", {}).get("http_attempts")
    jev = summary.get("jev_budget", {}).get("http_attempts")
    return {"directory": session["directory"], "status": session["status"],
            "registered_runs": session["registration"].get("registered_runs"),
            "observed_run_statuses": dict(Counter(run["status"] for run in runs)),
            "nvidia_http_attempts": nvidia if nvidia is not None else traced["nvidia"],
            "jev_http_attempts": jev if jev is not None else traced["typesafe"],
            "count_basis": "completed_session_send_guards" if nvidia is not None and jev is not None else "partial_traces",
            "trace_http_by_provider": dict(traced),
            "send_count_reconciliation": {"nvidia_guard_minus_trace": nvidia - traced["nvidia"] if nvidia is not None else None,
                                          "jev_guard_minus_trace": jev - traced["typesafe"] if jev is not None else None},
            "refused_http_sends": sum(run["counts"]["refused_http_sends"] for run in runs),
            "jev_reserved_worst_case_usd": summary.get("jev_budget", {}).get("reserved_worst_case_usd"),
            "jev_reported_estimated_cost_usd": summary.get("jev_reported_estimated_cost_usd"),
            "nvidia_cost_usd": None}


def _group(runs, mode):
    selected = [run for run in runs if run["mode"] == mode]
    nodes = defaultdict(list)
    for run in selected:
        for name, duration in run["top_level_node_ms"].items():
            nodes[name].append(duration)
    ranking = sorted(({"node": name, "median_inclusive_ms": round(statistics.median(values), 3), "n": len(values)}
                      for name, values in nodes.items()), key=lambda row: row["median_inclusive_ms"], reverse=True)
    medians = {}
    for key in ("research_setup_to_persist_ms", "end_to_end_wall_ms", "pre_setup_ms", "generated_report_ms"):
        values = [run["timing"][key] for run in selected if run["timing"][key] is not None]
        medians[key] = round(statistics.median(values), 3) if values else None
    return {"mode": mode, "n": len(selected), "medians": medians,
            "top_three_latency_contributors": ranking[:3],
            "counts": combined_counts(selected),
            "tail": "unqualified_small_n", "first_useful_result_ms": None}


def seed_link(cold, warm):
    """Whether the warm run's recorded input cache is the cold run's saved cache."""
    expected, used = cold.get("fixture_cache_sha256"), warm.get("input_fixture_cache_sha256")
    if expected is None or used is None:
        return "unrecorded"
    return "verified" if expected == used else "mismatch"


def load_allowance(path):
    """A registered NVIDIA send allowance: a cap shared by the named attempt directories."""
    path = Path(path)
    allowance = read_json(path)
    cap = allowance.get("maximum_nvidia_http_attempts")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0 or not allowance.get("sessions"):
        raise ValueError(f"Allowance {path.name} needs a positive maximum_nvidia_http_attempts and its sessions")
    return {"name": allowance.get("name", path.stem), "maximum_nvidia_http_attempts": cap,
            "sessions": [path.parent / name for name in allowance["sessions"]],
            "registration_path": str(path), "registration_sha256": file_sha(path)}


def summarize(old_directory, new_directory, jev_probe_directory=None, nvidia_probe_directory=None,
              additional_sessions=None, selection=None, allowances=None):
    """Select three pairs and account for every supplied attempt.

    By default pair 1 comes from the old attempt and pairs 2 and 3 from the new
    one. ``selection`` maps (pair, mode) to an attempt directory instead, so a
    warm run can finish a pair whose cold half is in an earlier attempt.
    """
    directories = [old_directory, new_directory, *(additional_sessions or [])]
    resolved = [Path(directory).resolve() for directory in directories]
    if len(set(resolved)) != len(resolved):
        raise ValueError("All attempt directories must be distinct; duplicates would double-count sends")
    if selection is None:
        selection = {(pair, mode): directory for pair, directory in
                     ((1, old_directory), (2, new_directory), (3, new_directory)) for mode in MODES}
    for (pair, mode), directory in selection.items():
        if pair not in (1, 2, 3) or mode not in MODES:
            raise ValueError(f"Unknown selection ({pair}, {mode}); pairs are 1-3 and modes {', '.join(MODES)}")
        if Path(directory).resolve() not in resolved:
            raise ValueError("Every selected directory must also be a supplied session, so its sends are counted")
    sessions = [load_session(directory) for directory in directories]
    all_runs = [run for session in sessions for run in session["runs"]]
    lookup = {(run["attempt_directory"], run["pair"], run["mode"]): run for run in all_runs}
    selected, pairs, selected_keys = [], [], set()
    seed_mismatch = cross_unverified = False
    for pair in (1, 2, 3):
        sources = {mode: selection.get((pair, mode)) for mode in MODES}
        members = [lookup.get((str(Path(sources[mode]).resolve()), pair, mode)) if sources[mode] is not None
                   else None for mode in MODES]
        complete = all(member is not None and member["status"] == "completed" for member in members)
        link = seed_link(*members) if complete else None
        if complete and link == "mismatch":
            seed_mismatch = True
        # Across attempts, only the recorded cache hashes show the warm run used this cold cache.
        if complete and members[0]["attempt_directory"] != members[1]["attempt_directory"] and link != "verified":
            cross_unverified = True
        pairs.append({"pair": pair, "session": Path(sources[MODES[0]]).name if sources[MODES[0]] else None,
                      "sessions": {mode: Path(source).name if source else None for mode, source in sources.items()},
                      "status": "complete" if complete else "incomplete", "seed_link": link,
                      "runs": [member["run_id"] if member else None for member in members]})
        if complete:
            selected.extend(members)
            selected_keys.update((member["attempt_directory"], member["run_id"]) for member in members)
    configs = {run["configuration_sha256"] for run in selected}
    workloads = {run["workload_sha256"] for run in selected}
    corpora = {run["corpus_sha256"] for run in selected}
    comparable = (len(configs) == len(workloads) == len(corpora) == 1 and None not in configs | workloads | corpora
                  and all(run["configuration_hash_verified"] for run in selected))
    normalized_timing = all(run["timing"]["research_setup_to_persist_ms"] is not None for run in selected)
    qualified = len(selected) == 6 and comparable and normalized_timing
    excluded = []
    for run in all_runs:
        if (run["attempt_directory"], run["run_id"]) not in selected_keys:
            excluded.append({**run, "exclusion_reason": "historical_unmatched_completed_sample" if run["status"] == "completed"
                             else "failed_or_unstarted_attempt"})
    differences = {}
    old_config = research_config(sessions[0]["registration"].get("configuration", {}))
    new_config = research_config(sessions[1]["registration"].get("configuration", {}))
    if old_config and new_config:
        differences = {key: {"old": old_config.get(key), "new": new_config.get(key)}
                       for key in sorted(old_config.keys() | new_config.keys()) if old_config.get(key) != new_config.get(key)}
    old_hashes = sessions[0]["registration"].get("source_files_sha256", {})
    new_hashes = sessions[1]["registration"].get("source_files_sha256", {})
    changed_sources = {key: {"old": old_hashes.get(key), "new": new_hashes.get(key)}
                       for key in sorted(old_hashes.keys() | new_hashes.keys())
                       if old_hashes.get(key) != new_hashes.get(key)} if old_hashes and new_hashes else {}
    ledgers = [_ledger(session) for session in sessions]
    probes = {}
    for provider, directory in (("typesafe", jev_probe_directory), ("nvidia", nvidia_probe_directory)):
        if directory is None:
            continue
        path = Path(directory) / "summary.json"
        probe = read_json(path)
        probes[provider] = {"summary_path": str(path), "summary_sha256": file_sha(path),
                            "http_attempts": probe["http_attempts"],
                            "jev_reserved_worst_case_usd": probe.get("reserved_worst_case_usd"),
                            "jev_reported_estimated_cost_usd": probe.get("reported_estimated_cost_usd"),
                            "sample_statuses": dict(Counter(sample["status"] for sample in probe["samples"]))}
    nvidia_total = sum(ledger["nvidia_http_attempts"] for ledger in ledgers) + probes.get("nvidia", {}).get("http_attempts", 0)
    jev_total = sum(ledger["jev_http_attempts"] for ledger in ledgers) + probes.get("typesafe", {}).get("http_attempts", 0)
    reservation = _sum_decimal([ledger["jev_reserved_worst_case_usd"] for ledger in ledgers]
                               + [probes.get("typesafe", {}).get("jev_reserved_worst_case_usd")])
    reported_cost = _sum_decimal([ledger["jev_reported_estimated_cost_usd"] for ledger in ledgers]
                                 + [probes.get("typesafe", {}).get("jev_reported_estimated_cost_usd")])
    reservation_complete = (all(ledger["status"] == "finished" and ledger["jev_reserved_worst_case_usd"] is not None
                                for ledger in ledgers)
                            and (jev_probe_directory is None or probes.get("typesafe", {}).get("jev_reserved_worst_case_usd") is not None))
    ledger_complete = (all(session["status"] == "finished" for session in sessions)
                       and all(value == 0 for ledger in ledgers for value in ledger["send_count_reconciliation"].values()))
    explicit = [load_allowance(path) for path in (allowances or [])]
    covered = {}
    for allowance in explicit:
        for directory in allowance["sessions"]:
            key = directory.resolve()
            if key not in resolved:
                raise ValueError(f"Allowance {allowance['name']} names {directory.name}, which was not supplied")
            if key in covered:
                raise ValueError("A session may belong to only one NVIDIA allowance; it would be counted twice")
            covered[key] = allowance["name"]
    ledger_by_key = dict(zip(resolved, ledgers, strict=True))

    def account(keys, cap):
        attempts = sum(ledger_by_key[key]["nvidia_http_attempts"] for key in keys)
        return {"nvidia_http_attempts": attempts, "maximum_nvidia_http_attempts": cap,
                "within_cap": attempts <= cap if ledger_complete else None, "sessions": [key.name for key in keys]}

    # Sessions after the original attempt that no later allowance names share
    # the first 192-send continuation allowance.
    continuation = {**account([key for key in resolved[1:] if key not in covered], CONTINUATION_NVIDIA_CAP),
                    "basis": "All supplied graph sessions except the original old attempt and sessions under a later "
                             "registered allowance; includes failed continuation and retry sends."}
    later = [{"name": allowance["name"], **account([key.resolve() for key in allowance["sessions"]],
                                                   allowance["maximum_nvidia_http_attempts"]),
              "registration_path": allowance["registration_path"],
              "registration_sha256": allowance["registration_sha256"]} for allowance in explicit]
    allowances_within = [entry["within_cap"] for entry in (continuation, *later)]
    gaps = []
    if len(selected) != 6:
        gaps.append("three_complete_pairs_not_achieved")
    if not comparable:
        gaps.append("research_configuration_or_corpus_not_verified_comparable")
    if not normalized_timing:
        gaps.append("research_timing_endpoint_missing")
    if seed_mismatch:
        gaps.append("warm_seed_does_not_match_cold_cache")
    if cross_unverified:
        gaps.append("cross_attempt_pair_seed_unverified")
    if not ledger_complete:
        gaps.append("send_ledger_incomplete_or_not_reconciled")
    if False in allowances_within:
        gaps.append("continuation_nvidia_send_cap_exceeded")
    if not reservation_complete:
        gaps.append("reservation_ledger_incomplete")
    elif reservation > 1.0:
        gaps.append("jev_authorized_cap_exceeded")
    qualified = (qualified and ledger_complete and reservation_complete and reservation <= 1.0
                 and all(within is True for within in allowances_within)
                 and not seed_mismatch and not cross_unverified)
    return {"schema_version": 2, "benchmark_id": "C01", "measurement_kind": "live_graph_on_synthetic_sources",
            "baseline_status": "three_complete_comparable_pairs" if qualified else "incomplete_or_not_comparable",
            "qualification_gaps": gaps,
            "qualified_pair_count": sum(pair["status"] == "complete" for pair in pairs), "registered_pair_count": 3,
            "pairs": pairs, "selected_runs": selected, "historical_excluded_runs": excluded,
            "research_configuration": {"comparable": comparable, "configuration_hashes": sorted(configs, key=str),
                                       "workload_hashes": sorted(workloads, key=str), "corpus_hashes": sorted(corpora, key=str),
                                       "registration_differences": differences},
            "timing_semantics": "Research completion is first setup/explicit measurement-start to persisted_report; full wall and pre-setup are retained. Startup is not silently discarded.",
            "node_duration_semantics": "Inclusive top-level node waits; parallel/nested durations overlap and are not summed as wall time.",
            "cache_semantics": "Cold/warm controls only synthetic source-response cache. NVIDIA prompt cache is uncontrolled; cached input is a reported subset and is not added again.",
            "groups": [_group(selected, mode) for mode in MODES] if comparable else [],
            "coverage": {"first_useful_result_ms": None, "browser_render": "unobserved", "quick_brief": "path_absent",
                         "original_source_full_material_validation": "unobserved",
                         "quality_or_speedup_claim": "not_qualified", "p95": "not_qualified_at_n3"},
            "manifests": [session.get("manifest") for session in sessions], "changed_source_hashes": changed_sources,
            "all_attempts_ledger": ledgers, "component_probes": probes,
            "continuation_allowance": continuation, "later_nvidia_allowances": later,
            "totals_including_excluded_and_components": {"nvidia_http_attempts": nvidia_total, "jev_http_attempts": jev_total,
                "ledger_complete": ledger_complete,
                "graph_counts": combined_counts(all_runs),
                "jev_reported_estimated_cost_usd": reported_cost,
                "jev_reserved_worst_case_usd": reservation if reservation_complete else None,
                "known_jev_reservation_subtotal_usd": reservation,
                "reservation_ledger_status": "complete" if reservation_complete else "partial_or_missing",
                "jev_authorized_cap_usd": 1.0, "within_jev_cap": reservation <= 1.0 if reservation_complete else None,
                "nvidia_cost_usd": None, "total_dollar_cost_usd": None,
                "cost_status": "Jev reported usage estimate only; NVIDIA dollars unknown. Failed and background attempts included via send guards."}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / "results" / "benchmarks"
    parser.add_argument("--old", type=Path, default=base / "c01-live-graph-20261014")
    parser.add_argument("--new", type=Path, default=base / "c01-live-graph-20261015-retry")
    parser.add_argument("--additional-session", type=Path, action="append", default=[],
                        help="Retain another graph attempt in spend/excluded history; selected only through --select")
    parser.add_argument("--select", nargs=3, action="append", metavar=("PAIR", "MODE", "DIRECTORY"),
                        help="Take this pair's cold_fixture_cache or warm_fixture_cache run from DIRECTORY; "
                             "replaces the default old/new selection")
    parser.add_argument("--allowance", type=Path, action="append", default=[],
                        help="Registered NVIDIA allowance JSON naming its sessions; others share the first continuation")
    parser.add_argument("--jev-probe", type=Path, default=base / "c01-jev-20261014")
    parser.add_argument("--nvidia-probe", type=Path, default=base / "c01-nvidia-20261014")
    args = parser.parse_args(argv)
    selection = None
    if args.select:
        selection = {}
        for pair, mode, directory in args.select:
            if not pair.isdigit() or mode not in MODES or (int(pair), mode) in selection:
                parser.error(f"--select needs a pair 1-3, a mode ({', '.join(MODES)}) and one directory per pair/mode")
            selection[(int(pair), mode)] = Path(directory)
    print(json.dumps(summarize(args.old, args.new, args.jev_probe, args.nvidia_probe,
                               args.additional_session, selection, args.allowance), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
