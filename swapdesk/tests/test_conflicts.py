from __future__ import annotations

from swapdesk.analysis import run_analysis
from swapdesk.analysis.conflicts import analyze_conflicts
from swapdesk.analysis.impact import analyze_impact


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
