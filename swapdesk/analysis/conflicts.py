"""Compatibility, completeness and scheduling conflict detection."""
from __future__ import annotations

from typing import Any

from ..model.entities import Registry, SwapRequest
from .classify import make_conflict, sort_conflicts, summarize
from .selection import select_tiers


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end


def _replacement_checks(registry: Registry, request: SwapRequest) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    detailed = request.uses_candidates
    for item in sorted(request.retire, key=lambda r: r.source):
        source = registry.source_map.get(item.source)
        if source is None:
            conflicts.append(make_conflict(
                "C_SOURCE_UNKNOWN", "blocking", "source", item.source,
                "撤换请求引用了未登记的数据源",
                {"source": item.source},
            ))
            continue
        candidates = list(item.candidates)
        if not candidates:
            conflicts.append(make_conflict(
                "C_REPLACEMENT_MISSING", "blocking", "source", item.source,
                "待下线源没有指定替代源", {"source": item.source},
            ))
            continue
        registered: list[tuple[int, Any]] = []
        for tier, candidate_id in enumerate(candidates, start=1):
            replacement = registry.source_map.get(candidate_id)
            if replacement is None:
                evidence: dict[str, Any] = {"source": item.source,
                                            "replacement": candidate_id}
                if detailed:
                    evidence["tier"] = tier
                conflicts.append(make_conflict(
                    "C_REPLACEMENT_MISSING", "blocking", "source", item.source,
                    "替代源未登记", evidence,
                ))
                continue
            registered.append((tier, replacement))
            if replacement.id == source.id:
                evidence = {"source": item.source, "replacement": candidate_id}
                if detailed:
                    evidence["tier"] = tier
                conflicts.append(make_conflict(
                    "C_REPLACEMENT_SELF", "blocking", "source", item.source,
                    "替代源不能是待下线源自身", evidence,
                ))
        if registered and source.status == "retired":
            conflicts.append(make_conflict(
                "C_SOURCE_ALREADY_RETIRED", "warning", "source", source.id,
                "待下线源已经是 retired 状态", {"status": source.status},
            ))
        for tier, replacement in registered:
            if source.kind != replacement.kind:
                evidence = {"from_kind": source.kind, "to_kind": replacement.kind}
                if detailed:
                    evidence["candidate"] = replacement.id
                    evidence["tier"] = tier
                conflicts.append(make_conflict(
                    "C_KIND_CHANGED", "warning", "source", source.id,
                    "替代源类型与原源不同，需要额外验证", evidence,
                ))
    return conflicts


def _selection_index(selections: dict[str, Any], kind: str) -> dict[str, Any]:
    return {entry["consumer_id"]: entry
            for entry in selections["consumers"]
            if entry["consumer_type"] == kind}


def _coverage_evidence(removed: list[str], selection: dict[str, Any],
                       detailed: bool, *, include_replacement: bool) -> dict[str, Any]:
    if detailed:
        return {"sources": list(removed), "choices": selection["choices"],
                "missing_fields": selection["missing_fields"],
                "reason": selection["reason"]}
    evidence: dict[str, Any] = {"sources": sorted(selection["mapping"]),
                                "missing_fields": selection["missing_fields"]}
    if include_replacement:
        evidence["replacement"] = sorted(selection["mapping"].values())
    return evidence


def _task_checks(registry: Registry, request: SwapRequest,
                 impact: dict[str, Any],
                 selections: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    detailed = request.uses_candidates
    selected = _selection_index(selections, "task")
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
        selection = selected.get(task.id)
        if selection is None:
            continue
        if selection["covered"]:
            continue
        conflicts.append(make_conflict(
            "C_FIELD_MISSING", "blocking", "task", task.id,
            "替代后的有效源集合缺少任务必需字段",
            _coverage_evidence(removed, selection, detailed,
                               include_replacement=True),
        ))
    return conflicts


def _metric_checks(registry: Registry, request: SwapRequest,
                   impact: dict[str, Any],
                   selections: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    detailed = request.uses_candidates
    selected = _selection_index(selections, "metric")
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
        selection = selected.get(metric.id)
        if selection is None:
            continue
        missing = selection["missing_fields"]
        if not missing:
            continue
        evidence = _coverage_evidence(removed, selection, detailed,
                                      include_replacement=False)
        if metric.required:
            conflicts.append(make_conflict(
                "C_METRIC_BROKEN", "blocking", "metric", metric.id,
                "必需口径无法在替代源上成立", evidence,
            ))
        else:
            conflicts.append(make_conflict(
                "C_FIELD_MISSING", "warning", "metric", metric.id,
                "非必需口径在替代源上缺少字段", evidence,
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
                      impact: dict[str, Any],
                      selections: dict[str, Any] | None = None) -> dict[str, Any]:
    if selections is None:
        selections = select_tiers(registry, request)
    conflicts = (_replacement_checks(registry, request)
                 + _task_checks(registry, request, impact, selections)
                 + _metric_checks(registry, request, impact, selections)
                 + _window_checks(request))
    conflicts = sort_conflicts(conflicts)
    result = {
        "request_id": request.request_id,
        "conflicts": conflicts,
        "summary": summarize(conflicts),
    }
    return result
