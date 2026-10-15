"""Offline correction must preserve paid observations and decline ambiguity."""

import copy
import hashlib
import json

import pytest

from scripts.repair_c01_trace_parents import derive_parent_corrections, repair_trace, trace_metrics


@pytest.mark.unit
def test_unique_match_ambiguity_and_exact_original_preservation(tmp_path):
    def span(identifier, kind, parent, start, end, model="model-a", status="ok"):
        return {"span_id": identifier, "parent_id": parent, "kind": kind,
                "start_ms": start, "end_ms": end, "duration_ms": end - start,
                "status": status, "attributes": {"model": model}}

    original = {"run_id": "paid-run", "status": "completed", "spans": [
        span("root", "run", None, 0, 100),
        span("node-one", "graph_node", "root", 0, 40),
        span("invocation-one", "llm_invocation", "node-one", 5, 30),
        span("generation-one", "llm", "node-one", 4.995, 30.005),
        span("node-two", "graph_node", "root", 40, 100),
        span("invocation-two", "llm_invocation", "node-two", 45, 90),
        span("invocation-three", "llm_invocation", "node-two", 45, 90),
        span("ambiguous-generation", "llm", "node-two", 50, 80),
        span("refused", "transport_attempt", "generation-one", 10, 10, status="not_sent"),
        span("http", "transport_attempt", "generation-one", 11, 20),
    ], "events": [{"name": "evidence", "offset_ms": 31}], "cost": {"amount": None}}
    original["spans"][3]["attributes"].update(input_tokens=50, output_tokens=10, usage_status="reported")
    unchanged = copy.deepcopy(original)
    corrected, report = derive_parent_corrections(original)
    assert original == unchanged
    assert report["count"] == 1
    assert report["ambiguous"] == [{"span_id": "ambiguous-generation",
                                     "candidate_ids": ["invocation-two", "invocation-three"]}]
    assert corrected["spans"][3]["parent_id"] == "invocation-one"
    assert corrected["spans"][7]["parent_id"] == "node-two"
    for before, after in zip(original["spans"], corrected["spans"], strict=True):
        assert {key: value for key, value in before.items() if key != "parent_id"} == {
            key: value for key, value in after.items() if key != "parent_id"}
    assert trace_metrics(corrected)["logical_llm_invocations"] == 4
    assert trace_metrics(corrected)["observed_http_attempts"] == 1
    assert trace_metrics(corrected)["refused_http_sends"] == 1

    path = tmp_path / "trace.json"
    raw = (" \n" + json.dumps(original, separators=(",", ":")) + "\n ").encode()
    path.write_bytes(raw)
    (tmp_path / "sample.json").write_text('{"wall_ms": 100, "status": "completed"}')
    repair_trace(path)
    assert (tmp_path / "trace.original.json").read_bytes() == raw
    saved = json.loads(path.read_bytes())
    assert saved["attribution_correction"]["originalsha"] == hashlib.sha256(raw).hexdigest()
    assert saved["events"] == original["events"]
    assert saved["cost"] == original["cost"]
    revised_bytes = path.read_bytes()
    assert repair_trace(path)["status"] == "already_corrected"
    assert path.read_bytes() == revised_bytes
    sample = json.loads((tmp_path / "sample.json").read_bytes())
    assert sample["wall_ms"] == 100 and sample["status"] == "completed"

    (tmp_path / "trace.original.json").write_bytes(b"mismatched backup")
    with pytest.raises(ValueError, match="provenance"):
        repair_trace(path)
    assert path.read_bytes() == revised_bytes
