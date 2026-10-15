"""Repair recorded C01 invocation ancestry offline, retaining original bytes.

No providers, credentials, SDKs, or production instrumentation are imported.
Only a unique same-parent, same-model, containing invocation can be selected.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

TOLERANCE_MS = 0.01
LLM_KINDS = frozenset({"llm", "llm_generation"})
METHOD = "unique_same_parent_same_model_containing_interval_tolerance_0.01ms"
REASON = "Generation callback was recorded beside its public invocation, inflating logical call counts."


def _interval(span):
    values = (span.get("start_ms"), span.get("end_ms"))
    if all(isinstance(value, (int, float)) and not isinstance(value, bool)
           and math.isfinite(value) for value in values) and values[0] <= values[1]:
        return values
    return None


def _has_invocation_ancestor(span, spans, invocation_ids):
    parent = span.get("parent_id")
    seen = set()
    while parent in spans and parent not in seen:
        if parent in invocation_ids:
            return True
        seen.add(parent)
        parent = spans[parent].get("parent_id")
    return False


def derive_parent_corrections(snapshot):
    """Return a copy with provable corrections and explicit unresolved matches."""
    corrected = copy.deepcopy(snapshot)
    by_id = {span["span_id"]: span for span in corrected["spans"]}
    invocations = [span for span in corrected["spans"] if span["kind"] == "llm_invocation"]
    invocation_ids = {span["span_id"] for span in invocations}
    changes, ambiguous, unmatched = [], [], []
    for generation in corrected["spans"]:
        if generation["kind"] not in LLM_KINDS or _has_invocation_ancestor(generation, by_id, invocation_ids):
            continue
        model = generation.get("attributes", {}).get("model")
        interval = _interval(generation)
        candidates = []
        if isinstance(model, str) and model and interval is not None:
            for invocation in invocations:
                outer = _interval(invocation)
                if (outer is not None and invocation.get("parent_id") == generation.get("parent_id")
                        and invocation.get("attributes", {}).get("model") == model
                        and outer[0] <= interval[0] + TOLERANCE_MS
                        and interval[1] <= outer[1] + TOLERANCE_MS):
                    candidates.append(invocation)
        if len(candidates) == 1:
            invocation = candidates[0]
            changes.append({"span_id": generation["span_id"], "previous_parent_id": generation.get("parent_id"),
                            "parent_id": invocation["span_id"], "model": model})
            generation["parent_id"] = invocation["span_id"]
        elif len(candidates) > 1:
            ambiguous.append({"span_id": generation["span_id"],
                              "candidate_ids": [candidate["span_id"] for candidate in candidates]})
        else:
            unmatched.append(generation["span_id"])
    return corrected, {"correctedparents": changes, "count": len(changes),
                       "ambiguous": ambiguous, "unmatched": unmatched}


def trace_metrics(snapshot):
    """Pure equivalent of the production ancestry-aware trace metrics."""
    spans = snapshot["spans"]
    by_id = {span["span_id"]: span for span in spans}
    generations = [span for span in spans if span["kind"] in LLM_KINDS]
    invocations = [span for span in spans if span["kind"] == "llm_invocation"]
    invocation_ids = {span["span_id"] for span in invocations}
    return {
        "logical_llm_invocations": len(invocations) + sum(
            not _has_invocation_ancestor(span, by_id, invocation_ids) for span in generations),
        "llm_generation_attempts": len(generations),
        "observed_http_attempts": sum(span["kind"] == "transport_attempt" and span["status"] != "not_sent"
                                      for span in spans),
        "refused_http_sends": sum(span["kind"] == "transport_attempt" and span["status"] == "not_sent"
                                  for span in spans),
        "jev_logical_calls": sum(span["kind"] == "jev" for span in spans),
        "reported_input_tokens": sum(span.get("attributes", {}).get("input_tokens") or 0 for span in generations),
        "reported_generation_tokens": sum(span.get("attributes", {}).get("output_tokens") or 0 for span in generations),
        "reported_cached_input_tokens": sum(span.get("attributes", {}).get("cached_input_tokens") or 0
                                            for span in generations),
        "llm_usage_missing_calls": sum(span.get("attributes", {}).get("usage_status") != "reported"
                                       for span in generations),
    }


def _atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".c01-repair-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repair_trace(path, dry_run=False):
    path = Path(path)
    original_bytes = path.read_bytes()
    snapshot = json.loads(original_bytes)
    original_path = path.with_name(f"{path.stem}.original{path.suffix}")
    existing = snapshot.get("attribution_correction")
    if existing is not None:
        if not original_path.exists() or hashlib.sha256(original_path.read_bytes()).hexdigest() != existing["originalsha"]:
            raise ValueError(f"Original trace backup does not match correction provenance: {original_path}")
        return {"trace_path": str(path), "status": "already_corrected", **existing}
    if original_path.exists() and original_path.read_bytes() != original_bytes:
        raise ValueError(f"Refusing to replace a mismatched original trace backup: {original_path}")
    corrected, matches = derive_parent_corrections(snapshot)
    report = {"trace_path": str(path), "status": "dry_run" if dry_run else "corrected", **matches,
              "metrics_before": snapshot.get("metrics", trace_metrics(snapshot)),
              "metrics_after": trace_metrics(corrected)}
    if dry_run:
        return report
    metadata = {"reason": REASON, "originalsha": hashlib.sha256(original_bytes).hexdigest(),
                "correctedparents": matches["correctedparents"], "count": matches["count"], "method": METHOD}
    corrected["attribution_correction"] = metadata
    corrected["metrics"] = {**snapshot.get("metrics", {}), **report["metrics_after"]}
    if not original_path.exists():
        with original_path.open("xb") as handle:
            handle.write(original_bytes)
    # A running writer must not be replaced with a correction derived from an older file.
    if path.read_bytes() != original_bytes:
        raise ValueError(f"Trace changed during offline repair; retained original backup: {path}")
    _atomic_json(path, corrected)
    sample_path = path.parent / "sample.json"
    if path.name == "trace.json" and sample_path.exists():
        sample = json.loads(sample_path.read_bytes())
        sample["attribution_correction"] = {"count": matches["count"], "originalsha": metadata["originalsha"],
                                           "trace_path": path.name, "original_trace_path": original_path.name}
        _atomic_json(sample_path, sample)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+", help="Trace files or directories containing trace.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, help="Save the full offline derivation record")
    args = parser.parse_args(argv)
    traces = sorted({trace for path in args.paths
                     for trace in (path.rglob("trace.json") if path.is_dir() else [path])})
    reports = []
    for trace in traces:
        try:
            reports.append(repair_trace(trace, args.dry_run))
        except (OSError, ValueError, KeyError) as exc:
            reports.append({"trace_path": str(trace), "status": "refused", "reason": str(exc)})
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(args.report, {"method": METHOD, "offline": True, "traces": reports})
    compact = [{"trace_path": report["trace_path"], "status": report["status"],
                "corrected_count": report.get("count", 0),
                "ambiguous_count": len(report.get("ambiguous", [])),
                "unmatched_count": len(report.get("unmatched", [])),
                "logical_before": report.get("metrics_before", {}).get("logical_llm_invocations"),
                "logical_after": report.get("metrics_after", {}).get("logical_llm_invocations")}
               for report in reports]
    print(json.dumps({"method": METHOD, "offline": True, "traces": compact}, indent=2))
    return 1 if any(report["status"] == "refused" or report.get("ambiguous") or report.get("unmatched")
                    for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
