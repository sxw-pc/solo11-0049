"""Compatibility, completeness and scheduling conflict detection."""
from __future__ import annotations

from typing import Any

from ..model.entities import Registry, SwapRequest
from .classify import (attribute_field_gaps, gap_severity, make_conflict,
                       sort_conflicts, summarize)


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end


def _replacement_checks(registry: Registry, request: SwapRequest) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for item in sorted(request.retire, key=lambda r: r.source):
        source = registry.source_map.get(item.source)
        if source is None:
            conflicts.append(make_conflict(
                "C_SOURCE_UNKNOWN", "blocking", "source", item.source,
                "撤换请求引用了未登记的数据源",
                {"source": item.source},
            ))
            continue
        if not item.replacement:
            conflicts.append(make_conflict(
                "C_REPLACEMENT_MISSING", "blocking", "source", item.source,
                "待下线源没有指定替代源", {"source": item.source},
            ))
            continue
        replacement = registry.source_map.get(item.replacement)
        if replacement is None:
            conflicts.append(make_conflict(
                "C_REPLACEMENT_MISSING", "blocking", "source", item.source,
                "替代源未登记", {"source": item.source,
                                 "replacement": item.replacement},
            ))
            continue
        if replacement.id == source.id:
            conflicts.append(make_conflict(
                "C_REPLACEMENT_SELF", "blocking", "source", item.source,
                "替代源不能是待下线源自身",
                {"source": item.source, "replacement": item.replacement},
            ))
        if source.status == "retired":
            conflicts.append(make_conflict(
                "C_SOURCE_ALREADY_RETIRED", "warning", "source", source.id,
                "待下线源已经是 retired 状态", {"status": source.status},
            ))
        if source.kind != replacement.kind:
            conflicts.append(make_conflict(
                "C_KIND_CHANGED", "warning", "source", source.id,
                "替代源类型与原源不同，需要额外验证",
                {"from_kind": source.kind, "to_kind": replacement.kind},
            ))
    return conflicts


def _task_checks(registry: Registry, request: SwapRequest,
                 impact: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    mapping = request.replacement_map
    for task_info in impact["affected"]["tasks"]:
        task = registry.task_map[task_info["id"]]
        removed = task_info["retired_refs"]
        if not removed:
            conflicts.append(make_conflict(
                "C_LINEAGE_REVIEW", "info", "task", task.id,
                "任务仅通过血缘间接受影响，请人工确认字段语义",
                {"path": task_info["path"]},
            ))
            continue
        if task_info["partial"]:
            conflicts.append(make_conflict(
                "C_PARTIAL_REPOINT", "info", "task", task.id,
                "多源任务仅撤换部分源，保留其余源",
                {"remaining_sources": task_info["remaining_sources"]},
            ))
        replacements = {}
        for source_id in removed:
            replacement = registry.source_map.get(mapping.get(source_id, ""))
            if replacement is not None:
                replacements[source_id] = replacement
        if len(replacements) != len(removed):
            continue
        gaps = attribute_field_gaps(
            task.id, task.output_fields, task.source_refs, removed,
            replacements, registry.source_map, registry.lineage)
        if not gaps["field_evidence"]:
            continue
        if gaps["has_source_gap"]:
            message = "替代后的有效源集合缺少任务必需字段"
        else:
            message = "任务字段在替代后的血缘边上缺少登记证据"
        conflicts.append(make_conflict(
            "C_FIELD_MISSING", gap_severity(gaps["has_source_gap"]),
            "task", task.id, message,
            {"sources": sorted(replacements),
             "replacement": sorted(r.id for r in replacements.values()),
             "effective_sources": gaps["effective_sources"],
             "missing_fields": gaps["missing_fields"],
             "edge_missing_fields": gaps["edge_missing_fields"],
             "field_evidence": gaps["field_evidence"]},
        ))
    return conflicts


def _metric_checks(registry: Registry, request: SwapRequest,
                   impact: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    mapping = request.replacement_map
    for metric_info in impact["affected"]["metrics"]:
        metric = registry.metric_map[metric_info["id"]]
        removed = metric_info["retired_refs"]
        if not removed:
            if metric.required:
                conflicts.append(make_conflict(
                    "C_METRIC_REVIEW", "warning", "metric", metric.id,
                    "必需口径通过血缘间接受影响，需要复核定义",
                    {"path": metric_info["path"]},
                ))
            continue
        replacements = {}
        for source_id in removed:
            replacement = registry.source_map.get(mapping.get(source_id, ""))
            if replacement is not None:
                replacements[source_id] = replacement
        if len(replacements) != len(removed):
            continue
        gaps = attribute_field_gaps(
            metric.id, metric.required_fields, metric.source_refs, removed,
            replacements, registry.source_map, registry.lineage)
        if not gaps["field_evidence"]:
            continue
        evidence = {"sources": sorted(replacements),
                    "replacement": sorted(r.id for r in replacements.values()),
                    "effective_sources": gaps["effective_sources"],
                    "missing_fields": gaps["missing_fields"],
                    "edge_missing_fields": gaps["edge_missing_fields"],
                    "field_evidence": gaps["field_evidence"]}
        if metric.required:
            if gaps["has_source_gap"]:
                message = "必需口径无法在替代源上成立"
            else:
                message = "必需口径字段在替代后的血缘边上缺少登记证据"
            conflicts.append(make_conflict(
                "C_METRIC_BROKEN", gap_severity(gaps["has_source_gap"]),
                "metric", metric.id, message, evidence,
            ))
        else:
            if gaps["has_source_gap"]:
                message = "非必需口径在替代源上缺少字段"
            else:
                message = "非必需口径字段在替代后的血缘边上缺少登记证据"
            conflicts.append(make_conflict(
                "C_FIELD_MISSING", "warning", "metric", metric.id,
                message, evidence,
            ))
    return conflicts


def _window_checks(request: SwapRequest) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for other in sorted(request.other_windows, key=lambda w: w.id):
        if _overlap(request.window.start, request.window.end,
                    other.start, other.end):
            conflicts.append(make_conflict(
                "C_WINDOW_OVERLAP", "warning", "request", request.request_id,
                "撤换窗口与其他变更窗口重叠",
                {"window": request.window.to_dict(),
                 "other_window": other.to_dict(),
                 "other_window_id": other.id},
            ))
    if request.window.start == request.window.end:
        conflicts.append(make_conflict(
            "C_WINDOW_ZERO", "info", "request", request.request_id,
            "撤换窗口只有一个时间点，执行缓冲为零",
            {"window": request.window.to_dict()},
        ))
    return conflicts


def analyze_conflicts(registry: Registry, request: SwapRequest,
                      impact: dict[str, Any]) -> dict[str, Any]:
    conflicts = (_replacement_checks(registry, request)
                 + _task_checks(registry, request, impact)
                 + _metric_checks(registry, request, impact)
                 + _window_checks(request))
    conflicts = sort_conflicts(conflicts)
    result = {
        "request_id": request.request_id,
        "conflicts": conflicts,
        "summary": summarize(conflicts),
    }
    return result
