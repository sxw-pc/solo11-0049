from __future__ import annotations

from swapdesk.analysis import run_analysis
from swapdesk.analysis.classify import attribute_field_gaps, gap_severity
from swapdesk.analysis.conflicts import analyze_conflicts
from swapdesk.analysis.impact import analyze_impact
from swapdesk.model.entities import LineageEdge, Source


def _by_rule(conflicts, rule_id):
    return [c for c in conflicts if c["rule_id"] == rule_id]


def test_window_overlap_is_warning(registry, request_one) -> None:
    impact = analyze_impact(registry, request_one)
    result = analyze_conflicts(registry, request_one, impact)
    overlaps = _by_rule(result["conflicts"], "C_WINDOW_OVERLAP")
    assert len(overlaps) == 1
    assert overlaps[0]["severity"] == "warning"
    assert overlaps[0]["evidence"]["other_window_id"] == "chg-114"
    assert result["summary"]["executable"] is True


def test_missing_replacement_and_fields_are_blocking(registry, request_two) -> None:
    analysis = run_analysis(registry, request_two)
    conflicts = analysis["conflicts"]
    missing = _by_rule(conflicts, "C_REPLACEMENT_MISSING")
    fields = _by_rule(conflicts, "C_FIELD_MISSING")
    metrics = _by_rule(conflicts, "C_METRIC_BROKEN")
    assert missing and missing[0]["severity"] == "blocking"
    assert missing[0]["target"]["id"] == "src-alpha"
    assert fields and fields[0]["severity"] == "blocking"
    assert "event_type" in fields[0]["evidence"]["missing_fields"]
    assert metrics and metrics[0]["severity"] == "blocking"
    assert analysis["summary"]["executable"] is False
    assert analysis["summary"]["blocking_rule_ids"] == [
        "C_FIELD_MISSING", "C_METRIC_BROKEN", "C_REPLACEMENT_MISSING",
    ]


def test_partial_repoint_is_info(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    partial = _by_rule(analysis["conflicts"], "C_PARTIAL_REPOINT")
    assert len(partial) == 1
    assert partial[0]["severity"] == "info"
    assert partial[0]["target"]["id"] == "task-orders-enrich"


def _field(evidence, name):
    for item in evidence["field_evidence"]:
        if item["field"] == name:
            return item
    raise AssertionError(f"no field evidence for {name}")


def test_source_level_gap_stays_blocking(registry, request_two) -> None:
    analysis = run_analysis(registry, request_two)
    fields = [c for c in _by_rule(analysis["conflicts"], "C_FIELD_MISSING")
              if c["target"]["id"] == "task-events-ingest"]
    assert len(fields) == 1
    conflict = fields[0]
    assert conflict["severity"] == "blocking"
    evidence = conflict["evidence"]
    assert evidence["missing_fields"] == ["event_type", "occurred_at"]
    assert evidence["edge_missing_fields"] == []
    assert evidence["effective_sources"] == ["src-beta-lite"]
    event_type = _field(evidence, "event_type")
    assert event_type["gap"] == "source"
    assert event_type["retired_providers"] == ["src-beta"]
    assert event_type["replacement_registered"] == {"src-beta": False}
    assert event_type["covered_by"] == []
    assert event_type["coverage"] == "none"
    assert event_type["lineage_edges"] == []
    event_id_fields = [item for item in evidence["field_evidence"]
                       if item["field"] == "event_id"]
    assert event_id_fields == []


def test_edge_only_gap_downgrades_to_warning(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    fields = [c for c in _by_rule(analysis["conflicts"], "C_FIELD_MISSING")
              if c["target"]["id"] == "task-orders-ingest"]
    assert len(fields) == 1
    conflict = fields[0]
    assert conflict["severity"] == "warning"
    evidence = conflict["evidence"]
    assert evidence["missing_fields"] == []
    assert evidence["edge_missing_fields"] == ["created_at"]
    created_at = _field(evidence, "created_at")
    assert created_at["gap"] == "edge"
    assert created_at["retired_providers"] == ["src-alpha"]
    assert created_at["replacement_registered"] == {"src-alpha": True}
    assert created_at["covered_by"] == ["src-alpha-v2"]
    assert created_at["coverage"] == "sole"
    assert created_at["lineage_edges"] == []
    assert analysis["summary"]["executable"] is True


def test_kept_source_backfills_multi_source_task(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    fields = [c for c in _by_rule(analysis["conflicts"], "C_FIELD_MISSING")
              if c["target"]["id"] == "task-orders-enrich"]
    assert len(fields) == 1
    conflict = fields[0]
    assert conflict["severity"] == "warning"
    evidence = conflict["evidence"]
    assert evidence["missing_fields"] == []
    assert evidence["edge_missing_fields"] == ["amount", "device_id", "order_id"]
    assert evidence["effective_sources"] == ["src-alpha-v2", "src-gamma"]
    device_id = _field(evidence, "device_id")
    assert device_id["gap"] == "edge"
    assert device_id["retired_providers"] == []
    assert device_id["replacement_registered"] == {}
    assert device_id["covered_by"] == ["src-gamma"]
    assert device_id["coverage"] == "sole"
    order_id = _field(evidence, "order_id")
    assert order_id["covered_by"] == ["src-alpha-v2"]
    assert order_id["retired_providers"] == ["src-alpha"]


def test_mixed_gaps_keep_strongest_severity(registry, request_two) -> None:
    analysis = run_analysis(registry, request_two)
    metrics = _by_rule(analysis["conflicts"], "C_METRIC_BROKEN")
    assert len(metrics) == 1
    conflict = metrics[0]
    assert conflict["target"]["id"] == "metric-event-funnel"
    assert conflict["severity"] == "blocking"
    evidence = conflict["evidence"]
    assert evidence["missing_fields"] == ["event_type"]
    assert evidence["edge_missing_fields"] == ["event_id"]
    gaps = {item["field"]: item["gap"] for item in evidence["field_evidence"]}
    assert gaps == {"event_type": "source", "event_id": "edge"}
    event_id = _field(evidence, "event_id")
    assert event_id["covered_by"] == ["src-beta-lite"]
    assert event_id["coverage"] == "sole"
    assert event_id["replacement_registered"] == {"src-beta": True}


def test_required_metric_edge_only_is_warning(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    metrics = _by_rule(analysis["conflicts"], "C_METRIC_BROKEN")
    assert [m["target"]["id"] for m in metrics] == [
        "metric-event-funnel", "metric-revenue"]
    for conflict in metrics:
        assert conflict["severity"] == "warning"
        assert conflict["evidence"]["missing_fields"] == []
        assert conflict["evidence"]["edge_missing_fields"]
    assert analysis["summary"]["executable"] is True


def test_attribute_field_gaps_shared_coverage() -> None:
    sources = {
        "src-old": Source(id="src-old", name="旧", kind="api", status="active",
                          fields=("a", "b")),
        "src-new": Source(id="src-new", name="新", kind="api", status="active",
                          fields=("a", "b")),
        "src-kept": Source(id="src-kept", name="留", kind="api",
                           status="active", fields=("a", "b")),
    }
    lineage = [
        LineageEdge(id="lin-x", from_ref="src-kept", to_ref="task-t",
                    fields=("a",)),
    ]
    gaps = attribute_field_gaps(
        "task-t", ["a", "b"], ["src-old", "src-kept"], ["src-old"],
        {"src-old": sources["src-new"]}, sources, lineage)
    assert gaps["missing_fields"] == []
    assert gaps["edge_missing_fields"] == ["b"]
    shared = _field(gaps, "b")
    assert shared["coverage"] == "shared"
    assert shared["covered_by"] == ["src-kept", "src-new"]
    assert shared["retired_providers"] == ["src-old"]
    assert shared["replacement_registered"] == {"src-old": True}
    assert gaps["effective_sources"] == ["src-kept", "src-new"]


def test_gap_severity_rules() -> None:
    assert gap_severity(True) == "blocking"
    assert gap_severity(False) == "warning"
    assert gap_severity(True, blocking_capable=False) == "warning"


def test_analysis_hash_reproducible_with_field_evidence(
        registry, request_one, request_two) -> None:
    for request in (request_one, request_two):
        first = run_analysis(registry, request)
        second = run_analysis(registry, request)
        assert first["analysis_hash"] == second["analysis_hash"]
        assert first["result_hash"] == second["result_hash"]
