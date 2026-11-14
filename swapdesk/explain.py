"""Reverse a completed run into a step-by-step, evidence-backed business account.

The explainer never re-runs analysis.  It reads only persisted material:

* the SQLite ledger snapshot (run row, step rows, artifact rows);
* ``runs/<run-id>/plan.json`` (or ``rollback_plan.json`` for rollback runs);
* ``runs/<run-id>/state.json`` and ``state_before.json``;
* per-step ``state_after`` artifacts referenced by the ledger.

For every step it turns the before/after state-hash movement into concrete
entity fields, and attributes downstream effects only to the endpoints that
actually changed.  In particular a lineage edge whose ``from_ref`` changed but
whose ``to_ref`` stayed put does *not* contribute the whole edge's downstream
subtree to that step's switched surface.
"""
from __future__ import annotations

import json
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .config import SCHEMA_VERSION, WorkPaths
from .execute.ledger import Ledger
from .execute.state import state_hash
from .io.jsonout import atomic_write_text, read_json, sha256_file, write_json

# Only these fields participate in the world-state hash.  Keeping the list
# explicit means the replay diff can never silently pick up presentation-only
# properties such as names or owners.
STATE_FIELDS: dict[str, tuple[str, ...]] = {
    "sources": ("status", "fields"),
    "tasks": ("status", "source_refs"),
    "downstream": ("status", "upstream_refs"),
    "lineage": ("from_ref", "to_ref"),
    "metrics": ("status", "source_refs"),
}
GROUP_ORDER = ("sources", "tasks", "downstream", "lineage", "metrics")
ENTITY_TYPE = {
    "sources": "source",
    "tasks": "task",
    "downstream": "downstream",
    "lineage": "lineage",
    "metrics": "metric",
}
REFERENCE_FIELDS = {
    ("tasks", "source_refs"),
    ("downstream", "upstream_refs"),
    ("metrics", "source_refs"),
}
LINEAGE_ENDPOINTS = ("from_ref", "to_ref")

_WHY_PLANNED = {
    "repoint_task": "先解除采集任务对拟退役源的直接引用，避免退役后任务仍指向已下线源。",
    "repoint_downstream": "在退役前把下游对象的直接上游依赖改指替代源，保持引用可解析。",
    "repoint_lineage": "保持血缘边端点与改指后的引用一致；这一步只处理端点，不改变未切换端点一侧的消费逻辑。",
    "repoint_metric": "把口径的直接源引用改指替代源，避免退役后口径引用悬空。",
    "review_metric": "该口径只通过血缘间接受影响，计划不修改其引用，仅要求人工复核定义。",
    "retire_source": "等所有引用步骤完成后才退役源，确保切换期间没有活跃消费方仍指向旧源。",
    "restore_source": "回滚时恢复数据源在迁移前的状态，使反向链路重新可用。",
    "restore_downstream": "回滚时恢复下游对象的迁移前状态。",
    "retire_downstream": "按计划将下游对象标记为退役。",
    "add_compat_field": "为替代源补充兼容字段，使改指后的下游字段需求仍可满足。",
}


def _entity_type(group: str) -> str:
    return ENTITY_TYPE[group]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _same(left: Any, right: Any) -> bool:
    return left == right


def _list_change(before: Any, after: Any) -> tuple[list[Any], list[Any]]:
    left = _as_list(before)
    right = _as_list(after)
    removed = [item for item in left if item not in right]
    added = [item for item in right if item not in left]
    return removed, added


def diff_states(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Return concrete effective-field changes between two world states.

    Every top-level field of the world state participates in ``state_hash``,
    so non-entity metadata is diffed too; otherwise a hash movement could be
    reported as an empty-impact step even though the hash did change.
    """
    changes: list[dict[str, Any]] = []
    meta_keys = sorted((set(before) | set(after)) - set(GROUP_ORDER))
    for key in meta_keys:
        if _same(before.get(key), after.get(key)):
            continue
        changes.append({
            "entity_type": "state_meta",
            "entity_group": "meta",
            "entity_id": key,
            "field": key,
            "state_path": key,
            "before": deepcopy(before.get(key)),
            "after": deepcopy(after.get(key)),
            "change_kind": "meta_update",
            "removed": [],
            "added": [],
        })
    for group in GROUP_ORDER:
        left_group = before.get(group) or {}
        right_group = after.get(group) or {}
        entity_ids = sorted(set(left_group) | set(right_group))
        for entity_id in entity_ids:
            left_node = left_group.get(entity_id)
            right_node = right_group.get(entity_id)
            if left_node is None or right_node is None:
                changes.append({
                    "entity_type": _entity_type(group),
                    "entity_group": group,
                    "entity_id": entity_id,
                    "field": "*",
                    "state_path": f"{group}.{entity_id}",
                    "before": deepcopy(left_node),
                    "after": deepcopy(right_node),
                    "change_kind": ("entity_added" if left_node is None
                                    else "entity_removed"),
                    "removed": [],
                    "added": [],
                })
                continue
            fields = sorted(set(STATE_FIELDS[group])
                            | set(left_node) | set(right_node))
            for field in fields:
                left = left_node.get(field)
                right = right_node.get(field)
                if _same(left, right):
                    continue
                if isinstance(left, list) or isinstance(right, list):
                    removed, added = _list_change(left, right)
                    change_kind = "list_update"
                    if (group, field) in REFERENCE_FIELDS:
                        change_kind = "reference_repoint"
                else:
                    removed, added = [], []
                    change_kind = "scalar_update"
                changes.append({
                    "entity_type": _entity_type(group),
                    "entity_group": group,
                    "entity_id": entity_id,
                    "field": field,
                    "state_path": f"{group}.{entity_id}.{field}",
                    "before": deepcopy(left),
                    "after": deepcopy(right),
                    "change_kind": change_kind,
                    "removed": deepcopy(removed),
                    "added": deepcopy(added),
                })
    return changes


def _replacement_map(*sources: dict[str, Any]) -> dict[str, str]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        mapping = source.get("replacements")
        if isinstance(mapping, dict):
            return {str(key): str(value) for key, value in mapping.items()}
        request = source.get("request")
        retire = request.get("retire") if isinstance(request, dict) else None
        if isinstance(retire, list):
            derived = {}
            for item in retire:
                if not isinstance(item, dict):
                    continue
                old = item.get("source")
                new = item.get("replacement")
                if old and new:
                    derived[str(old)] = str(new)
            if derived:
                return derived
    return {}


def _pair_refs(removed: list[Any], added: list[Any],
               replacements: dict[str, str]) -> list[dict[str, Any]]:
    """Pair removed/added refs deterministically, preferring plan replacements."""
    remaining_added = [str(item) for item in added]
    pairs: list[dict[str, Any]] = []
    unmatched_removed: list[str] = []
    for item in removed:
        old = str(item)
        candidate = replacements.get(old)
        if candidate is not None and candidate in remaining_added:
            remaining_added.remove(candidate)
            pairs.append({"before": old, "after": candidate})
        else:
            unmatched_removed.append(old)
    matched = list(unmatched_removed)
    for old, new in zip(unmatched_removed, list(remaining_added)):
        matched.remove(old)
        remaining_added.remove(new)
        pairs.append({"before": old, "after": new})
    # Anything left only on one side is still a real change; keep it explicit.
    for old in matched:
        pairs.append({"before": old, "after": ""})
    for new in remaining_added:
        pairs.append({"before": "", "after": new})
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        key = (pair["before"], pair["after"])
        if key not in seen:
            seen.add(key)
            unique.append(pair)
    return unique


def ref_switches(changes: list[dict[str, Any]],
                 replacements: dict[str, str]) -> list[dict[str, Any]]:
    switches: list[dict[str, Any]] = []
    for change in changes:
        group = change["entity_group"]
        field = change["field"]
        if (group, field) in REFERENCE_FIELDS:
            for pair in _pair_refs(change["removed"], change["added"],
                                   replacements):
                switches.append({"field": f"{change['entity_type']}."
                                          f"{change['entity_id']}.{field}",
                                 "before": pair["before"],
                                 "after": pair["after"]})
        elif group == "lineage" and field in LINEAGE_ENDPOINTS:
            switches.append({"field": f"lineage.{change['entity_id']}.{field}",
                             "before": change["before"], "after": change["after"]})
    return switches


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if value is None:
        return "<missing>"
    if value == "":
        return '""'
    return str(value)


def _describe_change(change: dict[str, Any]) -> str:
    if change["change_kind"] == "entity_added":
        return (f"{change['entity_type']} {change['entity_id']} 在状态中被新增")
    if change["change_kind"] == "entity_removed":
        return (f"{change['entity_type']} {change['entity_id']} 从状态中被移除")
    if change["change_kind"] == "meta_update":
        return (f"状态元字段 {change['state_path']} 从 "
                f"{_format_value(change['before'])} 改为 "
                f"{_format_value(change['after'])}")
    label = {
        "source": "数据源",
        "task": "任务",
        "downstream": "下游对象",
        "lineage": "血缘边",
        "metric": "口径",
    }.get(change["entity_type"], change["entity_type"])
    return (f"{label} {change['entity_id']} 的 {change['field']} 从 "
            f"{_format_value(change['before'])} 改为 "
            f"{_format_value(change['after'])}")


def _graph(state: dict[str, Any],
           exclude_edge: str = "") -> dict[str, list[str]]:
    adjacency: dict[str, set[str]] = {}

    def add(left: str, right: str) -> None:
        adjacency.setdefault(str(left), set()).add(str(right))

    for task_id, node in sorted((state.get("tasks") or {}).items()):
        for ref in node.get("source_refs", []):
            add(ref, task_id)
    for table_id, node in sorted((state.get("downstream") or {}).items()):
        for ref in node.get("upstream_refs", []):
            add(ref, table_id)
    for edge_id, node in sorted((state.get("lineage") or {}).items()):
        if edge_id == exclude_edge:
            continue
        add(node.get("from_ref", ""), node.get("to_ref", ""))
    for metric_id, node in sorted((state.get("metrics") or {}).items()):
        for ref in node.get("source_refs", []):
            add(ref, metric_id)
    return {node: sorted(targets) for node, targets in sorted(adjacency.items())}


def _walk(graph: dict[str, list[str]], seed: str) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {seed: [seed]}
    queue: deque[str] = deque([seed])
    while queue:
        node = queue.popleft()
        for target in graph.get(node, []):
            if target not in paths:
                paths[target] = paths[node] + [target]
                queue.append(target)
    return paths


def _entity_kind(node_id: str, state: dict[str, Any]) -> str:
    for group in ("downstream", "metrics", "tasks", "lineage", "sources"):
        if node_id in (state.get(group) or {}):
            return ENTITY_TYPE[group]
    return "unknown"


def _effective_field_sets(state: dict[str, Any]
                          ) -> dict[tuple[str, str], dict[str, Any]]:
    """Effective state of downstream objects and metrics.

    Each entry captures the entity's own effective fields (``status`` and
    its reference list) plus the field availability derived from the sources
    reachable through structural references.  Lineage edges are provenance
    and deliberately excluded: switching only a ``from_ref`` therefore cannot
    change this set, which is exactly the boundary the review must respect.
    """
    sources = state.get("sources") or {}
    memo: dict[str, set[str]] = {}

    def fields_of(node_id: str, stack: set[str]) -> set[str]:
        if node_id in sources:
            node = sources[node_id]
            if node.get("status") == "retired":
                return set()
            return {str(field) for field in (node.get("fields") or [])}
        if node_id in memo:
            return set(memo[node_id])
        if node_id in stack:
            return set()
        refs: list[Any] | None = None
        status = None
        node = (state.get("tasks") or {}).get(node_id)
        if isinstance(node, dict):
            refs = node.get("source_refs")
            status = node.get("status")
        else:
            node = (state.get("downstream") or {}).get(node_id)
            if isinstance(node, dict):
                refs = node.get("upstream_refs")
                status = node.get("status")
            else:
                node = (state.get("metrics") or {}).get(node_id)
                if isinstance(node, dict):
                    refs = node.get("source_refs")
                    status = node.get("status")
        if refs is None:
            return set()
        if status == "retired":
            memo[node_id] = set()
            return set()
        result: set[str] = set()
        for ref in refs:
            result |= fields_of(str(ref), stack | {node_id})
        memo[node_id] = result
        return set(result)

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for group, ref_field in (("downstream", "upstream_refs"),
                             ("metrics", "source_refs")):
        for node_id, node in sorted((state.get(group) or {}).items()):
            result[(_entity_type(group), node_id)] = {
                "status": node.get("status"),
                "refs": sorted(str(ref) for ref in
                               (node.get(ref_field) or [])),
                "fields": sorted(fields_of(node_id, set())),
            }
    return result


def _field_set_diff(before: dict[str, Any],
                    after: dict[str, Any]) -> list[dict[str, Any]]:
    left = _effective_field_sets(before)
    right = _effective_field_sets(after)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        before_row = left.get(key) or {}
        after_row = right.get(key) or {}
        if (before_row.get("status"), before_row.get("fields")) == (
                after_row.get("status"), after_row.get("fields")):
            continue
        before_fields = list(before_row.get("fields", []))
        after_fields = list(after_row.get("fields", []))
        changes.append({
            "entity_type": key[0],
            "entity_id": key[1],
            "before": deepcopy(before_row),
            "after": deepcopy(after_row),
            "added_fields": [field for field in after_fields
                             if field not in before_fields],
            "removed_fields": [field for field in before_fields
                               if field not in after_fields],
        })
    return changes


def _impact_items(state: dict[str, Any], seed: str,
                  *, basis: str) -> list[dict[str, Any]]:
    paths = _walk(_graph(state), seed)
    items: list[dict[str, Any]] = []
    for node_id, path in paths.items():
        kind = _entity_kind(node_id, state)
        if kind not in ("downstream", "metric"):
            continue
        depth = len(path) - 1
        relation = "self" if depth == 0 else ("direct" if depth == 1 else "indirect")
        items.append({
            "entity_type": kind,
            "entity_id": node_id,
            "relation": relation,
            "depth": depth,
            "path": path,
            "basis": basis,
        })
    items.sort(key=lambda item: (item["depth"], item["entity_type"],
                                 item["entity_id"]))
    return items


def _lineage_scope(step: dict[str, Any], changes: list[dict[str, Any]],
                   before_state: dict[str, Any], after_state: dict[str, Any]
                   ) -> dict[str, Any] | None:
    if step["action"] != "repoint_lineage" and step["target_type"] != "lineage":
        return None
    edge_id = step["target_id"]
    switched = [change["field"] for change in changes
                if change["entity_group"] == "lineage"
                and change["entity_id"] == edge_id
                and change["field"] in LINEAGE_ENDPOINTS]
    switched = [field for field in LINEAGE_ENDPOINTS if field in switched]
    unchanged = [field for field in LINEAGE_ENDPOINTS if field not in switched]
    before_edge = (before_state.get("lineage") or {}).get(edge_id) or {}
    after_edge = (after_state.get("lineage") or {}).get(edge_id) or {}
    scope = {
        "edge_id": edge_id,
        "before": deepcopy(before_edge),
        "after": deepcopy(after_edge),
        "switched_endpoints": switched,
        "unchanged_endpoints": unchanged,
        "partial_endpoint_switch": len(switched) == 1 and len(unchanged) == 1,
        "unchanged_endpoint_downstream_not_counted": [],
    }
    # A from_ref-only switch keeps the same destination.  The edge's entire
    # downstream subtree therefore is *not* switched surface for this step.
    if switched == ["from_ref"]:
        to_ref = after_edge.get("to_ref") or before_edge.get("to_ref")
        if to_ref:
            descendants = _walk(_graph(after_state, exclude_edge=edge_id),
                                str(to_ref))
            excluded = []
            for node_id, path in descendants.items():
                kind = _entity_kind(node_id, after_state)
                if kind not in ("downstream", "metric"):
                    continue
                excluded.append({
                    "entity_type": kind,
                    "entity_id": node_id,
                    "path": path,
                    "reason": "to_ref 端点未切换，该对象不属于本次端点切换的影响面",
                })
            excluded.sort(key=lambda item: (item["entity_type"],
                                            item["entity_id"]))
            scope["unchanged_endpoint_downstream_not_counted"] = excluded
    return scope


def _downstream_impact(step: dict[str, Any], changes: list[dict[str, Any]],
                       after_state: dict[str, Any],
                       lineage_scope: dict[str, Any] | None,
                       field_set_changes: list[dict[str, Any]]
                       ) -> dict[str, Any]:
    action = step["action"]
    seeds: list[str] = []
    basis = "no_effective_change"
    if changes:
        if step["target_type"] == "lineage":
            scope = lineage_scope or {}
            if "to_ref" in scope.get("switched_endpoints", []):
                seed = (scope.get("after") or {}).get("to_ref")
                if seed:
                    seeds = [seed]
                    basis = "lineage_to_ref_switch"
            else:
                basis = "lineage_from_ref_switch_no_downstream_switch"
        else:
            seeds = [step["target_id"]]
            if any(change["change_kind"] == "reference_repoint"
                   for change in changes):
                basis = "reference_change"
            elif any(change["field"] == "status" for change in changes):
                basis = "status_change"
            else:
                basis = "effective_field_change"
    elif action in ("repoint_lineage",) and step.get("target_type") == "lineage":
        basis = "lineage_from_ref_switch_no_downstream_switch"
    elif not changes:
        basis = "no_effective_change"

    items: list[dict[str, Any]] = []
    for seed in seeds:
        items.extend(_impact_items(after_state, seed, basis=basis))
    # Deduplicate while keeping the shortest deterministic path.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (item["entity_type"], item["entity_id"])
        current = deduped.get(key)
        if current is None or item["depth"] < current["depth"]:
            deduped[key] = item
    ordered = sorted(deduped.values(),
                     key=lambda item: (item["depth"], item["entity_type"],
                                       item["entity_id"]))
    reachable_downstream = [item for item in ordered
                            if item["entity_type"] == "downstream"]
    reachable_metrics = [item for item in ordered
                         if item["entity_type"] == "metric"]
    changed_keys = {(item["entity_type"], item["entity_id"])
                    for item in field_set_changes}
    effective_downstream = [item for item in reachable_downstream
                            if ("downstream", item["entity_id"]) in changed_keys]
    effective_metrics = [item for item in reachable_metrics
                         if ("metric", item["entity_id"]) in changed_keys]
    # A changed entity can be outside the reachable set (e.g. the step's own
    # downstream target).  Keep it visible instead of silently dropping it.
    reachable_keys = {(item["entity_type"], item["entity_id"])
                      for item in reachable_downstream + reachable_metrics}
    for change in field_set_changes:
        key = (change["entity_type"], change["entity_id"])
        if key in reachable_keys:
            continue
        item = {
            "entity_type": change["entity_type"],
            "entity_id": change["entity_id"],
            "relation": "self",
            "depth": 0,
            "path": [step["target_id"]],
            "basis": basis,
        }
        if change["entity_type"] == "downstream":
            effective_downstream.append(item)
        else:
            effective_metrics.append(item)
    effective_downstream.sort(key=lambda item: (item["depth"],
                                                item["entity_id"]))
    effective_metrics.sort(key=lambda item: (item["depth"],
                                             item["entity_id"]))
    return {
        "basis": basis,
        "reachable": {
            "downstream": reachable_downstream,
            "metrics": reachable_metrics,
            "counts": {
                "downstream": len(reachable_downstream),
                "metrics": len(reachable_metrics),
                "total": len(reachable_downstream) + len(reachable_metrics),
            },
        },
        "effective": {
            "downstream": effective_downstream,
            "metrics": effective_metrics,
            "field_set_changes": field_set_changes,
            "counts": {
                "downstream": len(effective_downstream),
                "metrics": len(effective_metrics),
                "total": len(effective_downstream) + len(effective_metrics),
            },
        },
        "not_counted": (
            (lineage_scope or {}).get(
                "unchanged_endpoint_downstream_not_counted", [])
        ),
    }


def _narrative(step: dict[str, Any], changes: list[dict[str, Any]],
               switches: list[dict[str, Any]],
               impact: dict[str, Any],
               lineage_scope: dict[str, Any] | None,
               before_hash: str, after_hash: str,
               classification: str, empty_kind: str = "") -> str:
    if classification == "empty":
        if empty_kind == "no_state_change":
            return (
                f"该步骤在计划中声明为「{step.get('description', step['action'])}」，"
                f"但执行前后状态哈希均为 {before_hash or '<unknown>'}，"
                "没有改变任何实体有效字段；因此标注为空影响步骤，而不是把计划动作"
                "当作实际影响。")
        return (
            f"该步骤在计划中声明为「{step.get('description', step['action'])}」，"
            f"状态哈希由 {before_hash or '<unknown>'} 变为 "
            f"{after_hash or '<unknown>'}，但没有任何下游对象或口径的有效字段集"
            "发生变化；因此标注为空影响步骤。")
    if classification == "unknown":
        return (
            f"该步骤在计划中声明为「{step.get('description', step['action'])}」，"
            "但台账或产物缺少可对比的前后状态，无法把哈希差异落回具体实体字段；"
            "复盘不对其影响下结论。")
    parts = [_describe_change(change) + "。" for change in changes]
    if switches:
        text = "；".join(
            f"{item['field']}：{item['before'] or '<新增>'} → "
            f"{item['after'] or '<移除>'}" for item in switches)
        parts.append(f"引用切换：{text}。")
    if lineage_scope and lineage_scope.get("partial_endpoint_switch"):
        switched = ", ".join(lineage_scope["switched_endpoints"])
        unchanged = ", ".join(lineage_scope["unchanged_endpoints"])
        parts.append(
            f"血缘边 {lineage_scope['edge_id']} 只切换了 {switched} 端点，"
            f"{unchanged} 端点保持不变；{unchanged} 一侧的下游对象不计入"
            "本次切换影响面。")
    reach = impact["reachable"]["counts"]
    if reach["total"]:
        parts.append(
            f"按改指后的依赖图，该步骤可达 {reach['downstream']} 个下游对象、"
            f"{reach['metrics']} 个口径。")
    else:
        parts.append("按改指后的依赖图，该步骤没有可达的下游对象或口径。")
    effective = impact["effective"]["counts"]
    if effective["total"]:
        parts.append(
            f"其中 {effective['downstream']} 个下游对象、{effective['metrics']} "
            "个口径的有效字段集发生了实际变化。")
    else:
        parts.append("没有任何下游对象或口径的有效字段集发生变化。")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Conflicting / resolvability evidence
# ---------------------------------------------------------------------------

def _analysis_for_run(paths: WorkPaths, run: dict[str, Any]
                      ) -> tuple[dict[str, Any], str, list[str]]:
    """Load persisted analysis evidence, following a rollback's parent run."""
    warnings: list[str] = []
    placeholders: list[str] = []
    candidates: list[tuple[Path, str]] = []
    run_dir = paths.runs / run["run_id"]
    candidates.append((run_dir / "analysis.json", f"runs/{run['run_id']}/analysis.json"))
    parent = run.get("parent_run_id") or ""
    if parent:
        candidates.append((paths.runs / parent / "analysis.json",
                           f"runs/{parent}/analysis.json"))
    for path, label in candidates:
        if not path.exists():
            continue
        try:
            body = read_json(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"无法读取分析证据 {label}: {exc}")
            continue
        if isinstance(body, dict) and isinstance(body.get("conflicts"), list):
            return body, label, warnings
        if isinstance(body, dict) and body.get("kind") == "rollback":
            placeholders.append(label)
    if placeholders:
        warnings.append(
            "分析证据中只有回滚占位记录，不含冲突清单："
            + ", ".join(placeholders))
    return {}, "", warnings


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _strings(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _analysis_replacements(analysis: dict[str, Any]) -> dict[str, str]:
    request = analysis.get("request")
    if not isinstance(request, dict):
        return {}
    derived: dict[str, str] = {}
    for item in request.get("retire", []) or []:
        if not isinstance(item, dict):
            continue
        old = item.get("source")
        new = item.get("replacement")
        if old and new:
            derived[str(old)] = str(new)
    return derived


def _source_fields(state: dict[str, Any], refs: Iterable[str]) -> set[str]:
    available: set[str] = set()
    sources = state.get("sources") or {}
    for ref in refs:
        node = sources.get(ref)
        if isinstance(node, dict) and node.get("status") != "retired":
            available.update(node.get("fields", []))
    return available


def _state_conditions(state: dict[str, Any],
                      analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Structural, state-only conditions that make a state non-executable.

    These are recomputed per step from the two persisted states, so a step can
    be shown to move a condition from blocking to executable (or introduce a
    new one) without trusting the original conflict list.  Requirement
    metadata (metric required fields) is read from the persisted analysis
    when present; otherwise the condition is simply not evaluated.
    """
    known: set[str] = set()
    for group in ("sources", "tasks", "downstream", "metrics"):
        known |= set(state.get(group) or {})
    retired = {sid for sid, node in (state.get("sources") or {}).items()
               if isinstance(node, dict) and node.get("status") == "retired"}
    conditions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(rule_id: str, target_type: str, target_id: str,
            field: str, basis: str) -> None:
        key = (rule_id, target_type, target_id, basis)
        if key in seen:
            return
        seen.add(key)
        conditions.append({
            "source": "state_condition",
            "rule_id": rule_id,
            "severity": "blocking",
            "target": {"type": target_type, "id": target_id},
            "field": field,
            "message": basis,
            "basis": basis,
        })

    ref_groups = (("tasks", "source_refs"), ("downstream", "upstream_refs"),
                  ("metrics", "source_refs"))
    for group, field in ref_groups:
        for node_id, node in sorted((state.get(group) or {}).items()):
            if not isinstance(node, dict):
                continue
            for ref in node.get(field) or []:
                ref = str(ref)
                if ref not in known:
                    add("C_DANGLING_REFERENCE", _entity_type(group), node_id,
                        field, f"{group}.{node_id}.{field} 引用未知实体 {ref}")
                if ref in retired:
                    add("C_RETIRED_SOURCE_REFERENCED", _entity_type(group),
                        node_id, field,
                        f"{group}.{node_id}.{field} 仍引用已退役源 {ref}")
    for node_id, node in sorted((state.get("lineage") or {}).items()):
        if not isinstance(node, dict):
            continue
        for field in LINEAGE_ENDPOINTS:
            ref = str(node.get(field, ""))
            if ref and ref not in known:
                add("C_DANGLING_REFERENCE", "lineage", node_id, field,
                    f"lineage.{node_id}.{field} 引用未知实体 {ref}")
            if ref in retired:
                add("C_RETIRED_SOURCE_REFERENCED", "lineage", node_id, field,
                    f"lineage.{node_id}.{field} 仍引用已退役源 {ref}")
    for node_id, node in sorted((state.get("tasks") or {}).items()):
        if not isinstance(node, dict):
            continue
        if node.get("status") == "active" and not node.get("source_refs"):
            add("C_EMPTY_TASK_SOURCES", "task", node_id, "source_refs",
                f"任务 {node_id} 处于 active 但没有任何源引用")

    impact = (analysis.get("impact") or {}) if analysis else {}
    affected_metrics = (impact.get("affected") or {}).get("metrics") or []
    for info in affected_metrics:
        if not isinstance(info, dict) or not info.get("required"):
            continue
        node = (state.get("metrics") or {}).get(info.get("id"))
        if not isinstance(node, dict):
            continue
        required = set(info.get("required_fields") or [])
        available = _source_fields(state, node.get("source_refs") or [])
        missing = sorted(required - available)
        if missing:
            add("C_METRIC_BROKEN", "metric", str(info["id"]), "source_refs",
                f"必需口径 {info['id']} 缺少字段 {', '.join(missing)}")
    return conditions


def _conflict_status(conflict: dict[str, Any],
                     state: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the concrete condition behind a conflict from persisted state.

    ``C_FIELD_MISSING`` / ``C_METRIC_BROKEN`` are recalculated against the
    target's *current* effective source set.  Comparing the status in the
    pre-step and post-step states gives a genuine before/after test instead of
    restating the original severity.  Rules that depend only on request
    metadata return ``unknown`` rather than a guess.
    """
    rule = conflict.get("rule_id", "")
    target = conflict.get("target") or {}
    target_type = target.get("type", "")
    target_id = target.get("id", "")
    evidence = conflict.get("evidence") or {}
    if rule in ("C_FIELD_MISSING", "C_METRIC_BROKEN"):
        group = "tasks" if target_type == "task" else "metrics"
        node = (state.get(group) or {}).get(target_id)
        if node is None:
            return {"status": "unresolved",
                    "basis": f"状态中不存在 {target_type} {target_id}"}
        missing = list(evidence.get("missing_fields") or [])
        if not missing:
            return {"status": "unknown",
                    "basis": "冲突未记录缺失字段，无法从状态重算"}
        available = _source_fields(state, node.get("source_refs", []))
        still_missing = sorted(field for field in missing
                               if field not in available)
        if still_missing:
            return {"status": "unresolved",
                    "basis": "当前有效源字段仍缺少 "
                             + ", ".join(still_missing)}
        return {"status": "resolved",
                "basis": "当前有效源字段并集已覆盖原缺失字段 "
                         + ", ".join(missing)}
    if rule == "C_REPLACEMENT_MISSING":
        replacement = evidence.get("replacement")
        if not replacement:
            return {"status": "unknown",
                    "basis": "冲突未记录替代源，无法从状态重算"}
        if replacement in (state.get("sources") or {}):
            return {"status": "resolved",
                    "basis": f"替代源 {replacement} 已登记"}
        return {"status": "unresolved",
                "basis": f"替代源 {replacement} 仍未登记"}
    if rule in ("C_SOURCE_ALREADY_RETIRED", "C_KIND_CHANGED",
                "C_WINDOW_OVERLAP", "C_WINDOW_ZERO", "C_METRIC_REVIEW",
                "C_LINEAGE_REVIEW", "C_PARTIAL_REPOINT"):
        return {"status": "unknown",
                "basis": "该冲突不由单个引用步骤或落盘状态直接消解"}
    return {"status": "unknown", "basis": "该规则无法仅从落盘状态判定"}


def _condition_key(condition: dict[str, Any]) -> tuple[str, str, str]:
    target = condition.get("target") or {}
    return (condition.get("rule_id", ""), target.get("type", ""),
            target.get("id", ""))


def _state_condition_transitions(before_state: dict[str, Any],
                                 after_state: dict[str, Any],
                                 analysis: dict[str, Any]
                                 ) -> list[dict[str, Any]]:
    """Compare blocking state conditions across one step.

    A condition present before but absent after is a genuine
    ``blocking_to_executable`` transition; the reverse is a regression.
    Conditions described only by request metadata are not included.
    """
    before_rows = {_condition_key(item): item for item in
                   _state_conditions(before_state, analysis)}
    after_rows = {_condition_key(item): item for item in
                  _state_conditions(after_state, analysis)}
    transitions: list[dict[str, Any]] = []
    for key in sorted(set(before_rows) | set(after_rows)):
        if key in before_rows and key in after_rows:
            continue
        if key in before_rows:
            row = before_rows[key]
            transitions.append({
                "source": "state_condition",
                "rule_id": key[0],
                "severity": "blocking",
                "target": row["target"],
                "field": row["field"],
                "transition": "blocking_to_executable",
                "from": {"blocking": True, "basis": row["basis"]},
                "to": {"blocking": False, "basis": "条件在步骤后消失"},
                "basis": row["basis"],
            })
        else:
            row = after_rows[key]
            transitions.append({
                "source": "state_condition",
                "rule_id": key[0],
                "severity": "blocking",
                "target": row["target"],
                "field": row["field"],
                "transition": "executable_to_blocking",
                "from": {"blocking": False, "basis": "条件在步骤前不存在"},
                "to": {"blocking": True, "basis": row["basis"]},
                "basis": row["basis"],
            })
    return transitions


def _conflict_matches(conflict: dict[str, Any], step: dict[str, Any],
                      changed_ids: set[str]) -> bool:
    target = conflict.get("target") or {}
    if target.get("id") in (step["target_id"], *changed_ids):
        return True
    evidence_strings = set(_strings(conflict.get("evidence") or {}))
    return bool(evidence_strings & (changed_ids | {step["target_id"]}))


def _conflict_transitions(conflict_rows: list[dict[str, Any]],
                          step: dict[str, Any], changes: list[dict[str, Any]],
                          before_state: dict[str, Any],
                          after_state: dict[str, Any]
                          ) -> list[dict[str, Any]]:
    changed_ids = {change["entity_id"] for change in changes}
    changed_ids |= {str(item) for change in changes
                    for item in (*change.get("removed", []),
                                 *change.get("added", []))}
    result: list[dict[str, Any]] = []
    for conflict in conflict_rows:
        if not _conflict_matches(conflict, step, changed_ids):
            continue
        before = _conflict_status(conflict, before_state)
        after = _conflict_status(conflict, after_state)
        severity = conflict.get("severity", "")
        was_blocking = severity == "blocking" and before["status"] == "unresolved"
        now_resolved = after["status"] == "resolved"
        transition = "unchanged"
        if was_blocking and now_resolved:
            transition = "blocking_to_executable"
        elif was_blocking:
            transition = ("blocking_still_present"
                          if after["status"] == "unresolved"
                          else "blocking_unknown")
        elif severity != "blocking" and now_resolved:
            transition = "cleared"
        basis = before["basis"] if before["basis"] == after["basis"] else (
            before["basis"] + "；" + after["basis"])
        result.append({
            "rule_id": conflict.get("rule_id", ""),
            "severity": severity,
            "target": conflict.get("target", {}),
            "message": conflict.get("message", ""),
            "from": {"status": before["status"],
                     "blocking": was_blocking,
                     "executable": not was_blocking},
            "to": {"status": after["status"],
                   "blocking": bool(was_blocking
                                    and after["status"] != "resolved"),
                   "executable": (not was_blocking) or now_resolved,
                   "resolved": now_resolved},
            "transition": transition,
            "basis": basis,
        })
    result.sort(key=lambda item: (item["rule_id"],
                                  item["target"].get("type", ""),
                                  item["target"].get("id", "")))
    return result


def _related_conflicts(conflict_rows: list[dict[str, Any]],
                       step: dict[str, Any],
                       changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changed_ids = {change["entity_id"] for change in changes}
    related = []
    for conflict in conflict_rows:
        if _conflict_matches(conflict, step, changed_ids):
            related.append({
                "rule_id": conflict.get("rule_id", ""),
                "severity": conflict.get("severity", ""),
                "target": conflict.get("target", {}),
                "message": conflict.get("message", ""),
            })
    return related


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------

def _artifact_index(artifacts: list[dict[str, Any]]
                    ) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.get("kind") == "state_after":
            index[artifact["step_id"]] = artifact
    for artifact in artifacts:
        index.setdefault(artifact["step_id"], artifact)
    return index


def _verify_artifact(paths: WorkPaths, artifact: dict[str, Any] | None,
                     step_row: dict[str, Any]) -> tuple[dict[str, Any] | None,
                                                        dict[str, Any]]:
    evidence: dict[str, Any] = {
        "run_id": step_row.get("run_id", ""),
        "step_id": step_row["step_id"],
        "ordinal": int(step_row.get("ordinal", 0)),
        "kind": "state_after" if artifact else "",
        "path": "",
        "sha256": "",
        "sha256_actual": "",
        "exists": False,
        "sha256_match": False,
        "state_hash_match": False,
        "artifact_after_hash_match": False,
        "ledger_artifact_path_match": False,
        "ok": False,
        "body": None,
    }
    if artifact is None:
        evidence["error"] = "台账缺少该步骤的 state_after 产物记录"
        return None, evidence
    path = paths.root / artifact["path"]
    evidence.update({
        "kind": artifact.get("kind", ""),
        "path": artifact.get("path", ""),
        "sha256": artifact.get("sha256", ""),
        "ledger_artifact": {
            "table": "artifacts",
            "run_id": artifact.get("run_id", ""),
            "step_id": artifact.get("step_id", ""),
            "kind": artifact.get("kind", ""),
            "path": artifact.get("path", ""),
            "sha256": artifact.get("sha256", ""),
        },
        "ledger_artifact_path_match": (
            artifact.get("path", "") == step_row.get("artifact_path", "")),
    })
    if not path.exists():
        evidence["error"] = "产物文件不存在"
        return None, evidence
    evidence["exists"] = True
    actual = sha256_file(path)
    evidence["sha256_actual"] = actual
    evidence["sha256_match"] = actual == artifact.get("sha256", "")
    try:
        body = read_json(path)
    except (OSError, ValueError) as exc:
        evidence["error"] = f"产物无法解析: {exc}"
        return None, evidence
    evidence["body"] = body
    after_state = body.get("state_after")
    if isinstance(after_state, dict):
        evidence["state_hash_match"] = (
            state_hash(after_state) == step_row.get("after_hash", ""))
    evidence["artifact_after_hash_match"] = (
        body.get("after_hash", "") == step_row.get("after_hash", ""))
    evidence["ok"] = bool(evidence["sha256_match"]
                          and evidence["state_hash_match"]
                          and evidence["artifact_after_hash_match"]
                          and evidence["ledger_artifact_path_match"])
    return body, evidence


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def _load_plan(paths: WorkPaths, run: dict[str, Any]
               ) -> tuple[dict[str, Any], str, list[str]]:
    warnings: list[str] = []
    run_dir = paths.runs / run["run_id"]
    candidates: list[Path] = []
    if run.get("kind") == "rollback":
        candidates.append(run_dir / "rollback_plan.json")
    candidates.append(run_dir / "plan.json")
    candidates.append(run_dir / "rollback_plan.json")
    for path in candidates:
        if not path.exists():
            continue
        try:
            body = read_json(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"无法读取计划产物 {path}: {exc}")
            continue
        if isinstance(body, dict) and isinstance(body.get("steps"), list):
            return body, str(path.relative_to(paths.root).as_posix()), warnings
    warnings.append(f"运行 {run['run_id']} 缺少可读的计划产物")
    return {}, "", warnings


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        body = read_json(path)
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _hash_chain_warnings(steps: list[dict[str, Any]],
                         run: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not steps:
        return warnings
    first = steps[0]
    if first.get("before_hash") and first["before_hash"] != run["initial_hash"]:
        warnings.append(
            f"首步 before_hash 与运行 initial_hash 不一致："
            f"{first['before_hash']} != {run['initial_hash']}")
    for previous, current in zip(steps, steps[1:]):
        if previous.get("after_hash") != current.get("before_hash"):
            warnings.append(
                f"步骤哈希链断裂：{previous['step_id']}.after_hash != "
                f"{current['step_id']}.before_hash")
    if steps[-1].get("after_hash") != run["final_hash"]:
        warnings.append(
            f"末步 after_hash 与运行 final_hash 不一致："
            f"{steps[-1].get('after_hash')} != {run['final_hash']}")
    return warnings


def _final_baseline(run: dict[str, Any], plan: dict[str, Any],
                    final_state: dict[str, Any] | None,
                    initial_state: dict[str, Any] | None) -> dict[str, Any]:
    initial_hash = run.get("initial_hash", "")
    final_hash = run.get("final_hash", "")
    expected_source_initial = ""
    if run.get("kind") == "rollback":
        expected_source_initial = str(plan.get("expected_initial_hash", "") or "")
    actual_final_state_hash = state_hash(final_state) if final_state is not None else ""
    actual_initial_state_hash = (state_hash(initial_state)
                                 if initial_state is not None else "")
    if run.get("kind") == "rollback" and expected_source_initial:
        returned = bool(final_hash) and final_hash == expected_source_initial
        baseline_kind = "source_initial_hash"
        baseline_hash = expected_source_initial
    else:
        returned = bool(final_hash) and final_hash == initial_hash
        baseline_kind = "run_initial_hash"
        baseline_hash = initial_hash
    return {
        "baseline_kind": baseline_kind,
        "baseline_hash": baseline_hash,
        "run_initial_hash": initial_hash,
        "run_final_hash": final_hash,
        "final_state_hash": actual_final_state_hash,
        "initial_state_hash": actual_initial_state_hash,
        "returned_to_initial_hash": returned,
        "final_equals_run_initial": bool(final_hash)
        and final_hash == initial_hash,
        "final_state_matches_ledger": bool(final_hash)
        and actual_final_state_hash == final_hash,
        "initial_state_matches_ledger": bool(initial_hash)
        and actual_initial_state_hash == initial_hash,
        "source_initial_hash": expected_source_initial,
        "final_equals_source_initial": bool(expected_source_initial)
        and final_hash == expected_source_initial,
    }


def _overall_narrative(run: dict[str, Any], plan: dict[str, Any],
                        steps: list[dict[str, Any]], baseline: dict[str, Any],
                        empty_steps: list[str],
                        resolved: list[dict[str, Any]],
                        replacements: dict[str, str],
                        pending_steps: list[str]) -> str:
    replacement_text = ""
    if replacements:
        replacement_text = "，替换关系为 " + "；".join(
            f"{key}→{value}" for key, value in sorted(replacements.items()))
    kind_text = "回滚" if run.get("kind") == "rollback" else "迁移执行"
    parent_text = ""
    if run.get("kind") == "rollback" and run.get("parent_run_id"):
        parent_text = f"（回滚自运行 {run['parent_run_id']}）"
    parts = [
        f"运行 {run['run_id']} 是一次{kind_text}{parent_text}，对应请求 "
        f"{run.get('request_id', '')}、计划 {run.get('plan_id', '')}"
        f"{replacement_text}。",
        f"台账记录了 {len(steps)} 个步骤，其中 "
        f"{sum(1 for step in steps if step.get('status') == 'succeeded')} 步成功。",
    ]
    if pending_steps:
        parts.append(
            f"运行状态为 {run.get('status', '')}，仍有 {len(pending_steps)} 个"
            "计划步骤没有出现在台账；以下结论只覆盖已落盘步骤。")
    if baseline["baseline_kind"] == "source_initial_hash":
        if baseline["returned_to_initial_hash"]:
            parts.append("回滚结束后状态哈希回到原执行前基线，"
                         f"即 {baseline['baseline_hash']}。")
        else:
            parts.append("回滚结束后状态哈希未回到原执行前基线"
                         f"（期望 {baseline['baseline_hash']}，实际 "
                         f"{baseline['run_final_hash']}）。")
    else:
        if baseline["returned_to_initial_hash"]:
            parts.append("执行结束后状态哈希回到开始前基线。")
        else:
            parts.append("执行结束后状态已离开开始前基线，"
                         f"由 {baseline['run_initial_hash']} 变为 "
                         f"{baseline['run_final_hash']}。")
    if empty_steps:
        parts.append("空影响步骤：" + "、".join(empty_steps) + "。")
    if resolved:
        parts.append("从 blocking 走向可执行的冲突："
                     + "、".join(item["rule_id"] + "@"
                                 + item["target"].get("id", "")
                                 for item in resolved) + "。")
    return "".join(parts)


def explain_run(run_id: str, paths: WorkPaths) -> dict[str, Any]:
    """Build the structured explanation object for one ledger run."""
    ledger = Ledger(paths.ledger)
    snapshot = ledger.snapshot(run_id)
    run = snapshot["run"]
    step_rows = snapshot["steps"]
    artifacts = snapshot["artifacts"]
    run_dir = paths.runs / run_id

    plan, plan_path, warnings = _load_plan(paths, run)
    plan_steps = {step.get("step_id"): step for step in plan.get("steps", [])
                  if isinstance(step, dict)}
    analysis, analysis_path, analysis_warnings = _analysis_for_run(paths, run)
    warnings.extend(analysis_warnings)
    conflict_rows = [item for item in analysis.get("conflicts", [])
                     if isinstance(item, dict)] if analysis else []
    replacements = _replacement_map(plan, analysis)
    if not replacements:
        replacements = _analysis_replacements(analysis)
    artifact_index = _artifact_index(artifacts)

    initial_state = _load_state(run_dir / "state_before.json")
    if initial_state is None:
        warnings.append("缺少 state_before.json，首步无法进行字段级前后对比")
    final_state = _load_state(run_dir / "state.json")
    if final_state is None:
        warnings.append("缺少 state.json，无法核对最终状态文件哈希")

    before_state = deepcopy(initial_state) if initial_state is not None else None
    evidence_items: list[dict[str, Any]] = []
    step_outputs: list[dict[str, Any]] = []
    resolved_transitions: list[dict[str, Any]] = []
    empty_steps: list[str] = []
    empty_step_kinds: dict[str, str] = {}

    for step_row in step_rows:
        step_id = step_row["step_id"]
        plan_step = plan_steps.get(step_id, {})
        step = {
            "step_id": step_id,
            "ordinal": int(step_row.get("ordinal", 0)),
            "batch": int(step_row.get("batch", 0)),
            "action": step_row.get("action", plan_step.get("action", "")),
            "target_type": step_row.get("target_type",
                                        plan_step.get("target_type", "")),
            "target_id": step_row.get("target_id",
                                      plan_step.get("target_id", "")),
            "status": step_row.get("status", ""),
            "attempts": int(step_row.get("attempts", 0)),
            "description": plan_step.get("description", ""),
            "depends_on": list(plan_step.get("depends_on", [])),
            "declared_payload": deepcopy(plan_step.get("payload", {})),
            "declared_before_after": {
                "before_hash": step_row.get("before_hash", ""),
                "after_hash": step_row.get("after_hash", ""),
            },
        }
        artifact = artifact_index.get(step_id)
        body, evidence = _verify_artifact(paths, artifact, step_row)
        evidence["ledger_before_hash"] = step_row.get("before_hash", "")
        evidence["ledger_after_hash"] = step_row.get("after_hash", "")
        evidence["artifact_before_hash"] = (
            body.get("before_hash", "") if isinstance(body, dict) else "")
        evidence["artifact_after_hash"] = (
            body.get("after_hash", "") if isinstance(body, dict) else "")
        evidence["ok"] = bool(
            evidence["ok"]
            and evidence["artifact_before_hash"] == step_row.get("before_hash", ""))
        evidence_items.append({
            **{key: value for key, value in evidence.items() if key != "body"},
            "ok_step_status": step_row.get("status", ""),
        })
        if step_row.get("status") == "succeeded" and not evidence["ok"]:
            warnings.append(
                f"{step_id} 的产物证据未通过完整性校验："
                f"{evidence.get('error', '哈希或路径不一致')}")

        after_state: dict[str, Any] | None = None
        if isinstance(body, dict) and isinstance(body.get("state_after"), dict):
            after_state = body["state_after"]
        diff_available = before_state is not None and after_state is not None
        changes = diff_states(before_state or {}, after_state or {}) \
            if diff_available else []
        if not diff_available:
            warnings.append(
                f"{step_id} 缺少前后状态，无法把哈希差异落回实体字段")

        lineage_scope = _lineage_scope(step, changes, before_state or {},
                                       after_state or {})
        switches = ref_switches(changes, replacements)
        field_set_changes = (_field_set_diff(before_state or {},
                                             after_state or {})
                             if diff_available else [])
        impact = _downstream_impact(step, changes, after_state or {},
                                    lineage_scope, field_set_changes)
        impact["effective_field_set_changed"] = bool(field_set_changes)
        empty_impact = diff_available and not field_set_changes
        if not diff_available:
            classification = "unknown"
        elif empty_impact:
            classification = "empty"
        else:
            classification = "effective"
        if empty_impact:
            empty_kind = ("no_state_change" if not changes
                          else "no_downstream_effective_change")
            empty_steps.append(step_id)
            empty_step_kinds[step_id] = empty_kind
        recorded_transitions = _conflict_transitions(
            conflict_rows, step, changes, before_state or {}, after_state or {})
        blocking_transitions = (_state_condition_transitions(
            before_state or {}, after_state or {}, analysis)
            if diff_available else [])
        for item in blocking_transitions:
            item["source"] = "state_condition"
        for item in recorded_transitions:
            item.setdefault("source", "recorded_conflict")
        transitions = recorded_transitions + blocking_transitions
        for item in blocking_transitions:
            if item["transition"] != "blocking_to_executable":
                continue
            resolved_transitions.append({
                "step_id": step_id,
                "ordinal": step["ordinal"],
                "rule_id": item["rule_id"],
                "target": item["target"],
                "field": item.get("field", ""),
                "basis": item["basis"],
                "source": "state_condition",
            })
        for item in recorded_transitions:
            if not (item["from"].get("blocking")
                    and item["to"].get("resolved")):
                continue
            resolved_transitions.append({
                "step_id": step_id,
                "ordinal": step["ordinal"],
                "rule_id": item["rule_id"],
                "target": item["target"],
                "field": "",
                "basis": item["basis"],
                "source": "recorded_conflict",
            })

        before_hash = step_row.get("before_hash", "")
        after_hash = step_row.get("after_hash", "")
        step_record = {
            **step,
            "planned": {
                "description": step["description"],
                "why_planned": _WHY_PLANNED.get(
                    step["action"], "按计划声明的目标执行该步骤。"),
                "payload": step["declared_payload"],
                "depends_on": step["depends_on"],
                "related_conflicts": _related_conflicts(
                    conflict_rows, step, changes),
            },
            "execution": {
                **step["declared_before_after"],
                "hash_changed": before_hash != after_hash,
                "artifact": {key: value for key, value in evidence.items()
                             if key != "body"},
            },
            "state_diff": {
                "available": diff_available,
                "changes": changes,
                "changed_entities": _changed_entities(changes),
                "change_count": len(changes),
            },
            "declaration_check": _declaration_check(step, changes),
            "ref_switches": switches,
            "lineage_scope": lineage_scope,
            "downstream_impact": impact,
            "conflict_transitions": transitions,
            "empty_impact": empty_impact,
            "empty_impact_kind": empty_step_kinds.get(step_id, ""),
            "impact_classification": classification,
            "downstream_effective_field_set_changed": bool(field_set_changes),
            "empty_impact_reason": {
                "no_state_change": (
                    "计划声明了该步骤，但执行前后状态哈希与实体有效字段均未变化"),
                "no_downstream_effective_change": (
                    "计划声明了该步骤，但执行后没有任何下游对象或口径的有效"
                    "字段集发生变化"),
            }.get(empty_step_kinds.get(step_id, ""), ""),
            "narrative": _narrative(step, changes, switches, impact,
                                    lineage_scope, before_hash, after_hash,
                                    classification,
                                    empty_step_kinds.get(step_id, "")),
        }
        step_outputs.append(step_record)
        if after_state is not None:
            before_state = deepcopy(after_state)

    baseline = _final_baseline(run, plan, final_state, initial_state)
    warnings.extend(_hash_chain_warnings(step_rows, run))

    executed_ids = [row["step_id"] for row in step_rows]
    plan_step_ids = [step.get("step_id") for step in plan.get("steps", [])
                     if isinstance(step, dict)]
    pending_steps = [sid for sid in plan_step_ids if sid not in set(executed_ids)]
    unexpected_steps = [sid for sid in executed_ids
                        if sid not in set(plan_step_ids)]
    if plan_step_ids and pending_steps:
        warnings.append(
            "计划中以下步骤尚未出现在台账：" + ", ".join(pending_steps))
    if unexpected_steps:
        warnings.append(
            "台账中存在计划外步骤：" + ", ".join(unexpected_steps))

    final_transitions = []
    for conflict in conflict_rows:
        if conflict.get("severity") != "blocking":
            continue
        status = _conflict_status(conflict, final_state or {})
        final_transitions.append({
            "rule_id": conflict.get("rule_id", ""),
            "target": conflict.get("target", {}),
            "message": conflict.get("message", ""),
            "status": status["status"],
            "basis": status["basis"],
            "resolved": status["status"] == "resolved",
        })
    final_transitions.sort(key=lambda item: (item["rule_id"],
                                             item["target"].get("id", "")))

    summary = {
        "narrative": _overall_narrative(run, plan, step_outputs, baseline,
                                         empty_steps, resolved_transitions,
                                         replacements, pending_steps),
        "total_steps": len(step_outputs),
        "succeeded_steps": sum(1 for step in step_outputs
                               if step["status"] == "succeeded"),
        "failed_steps": sum(1 for step in step_outputs
                            if step["status"] == "failed"),
        "empty_impact_steps": empty_steps,
        "empty_impact_count": len(empty_steps),
        "empty_impact_kinds": dict(empty_step_kinds),
        "changed_entity_count": len({
            (change["entity_group"], change["entity_id"])
            for step in step_outputs for change in step["state_diff"]["changes"]
        }),
        "returned_to_initial_hash": baseline["returned_to_initial_hash"],
        "blocking_conflicts_resolved": resolved_transitions,
        "blocking_conflict_resolved_count": len(resolved_transitions),
        "evidence_ok": all(item["ok"] for item in evidence_items
                           if item["ok_step_status"] == "succeeded"),
    }
    summary["unverified_step_ids"] = [
        item["step_id"] for item in evidence_items
        if item["ok_step_status"] == "succeeded" and not item["ok"]]

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "explain",
        "run": {
            "run_id": run["run_id"],
            "kind": run["kind"],
            "status": run["status"],
            "request_id": run["request_id"],
            "plan_id": run["plan_id"],
            "plan_hash": run["plan_hash"],
            "parent_run_id": run.get("parent_run_id", ""),
            "started_at": run["started_at"],
            "updated_at": run["updated_at"],
            "initial_hash": run["initial_hash"],
            "final_hash": run["final_hash"],
        },
        "plan": {
            "path": plan_path,
            "plan_id": plan.get("plan_id", run["plan_id"]),
            "plan_hash": plan.get("plan_hash", run["plan_hash"]),
            "source_plan_id": plan.get("source_plan_id", ""),
            "request_id": plan.get("request_id", run["request_id"]),
            "retired_sources": list(plan.get("retired_sources", [])),
            "replacements": replacements,
            "declared_steps": len(plan.get("steps", [])),
            "executed_steps": len(step_outputs),
            "pending_steps": pending_steps,
            "unexpected_steps": unexpected_steps,
            "batches": len(plan.get("batches", [])),
            "expected_initial_hash": (
                plan.get("expected_initial_hash", "")
                if run.get("kind") == "rollback" else ""),
        },
        "baseline": baseline,
        "summary": summary,
        "conflict_context": {
            "analysis_path": analysis_path,
            "analysis_available": bool(analysis),
            "counts": (analysis.get("summary", {}).get("counts", {})
                       if analysis else {}),
            "conflicts": conflict_rows,
            "transitions": resolved_transitions,
            "final_blocking": final_transitions,
        },
        "steps": step_outputs,
        "evidence_index": evidence_items,
        "warnings": sorted(set(warnings)),
    }
    return result


def _declaration_check(step: dict[str, Any], changes: list[dict[str, Any]]
                       ) -> dict[str, Any]:
    """Check the plan's declared before/after against observed state changes."""
    payload = step.get("declared_payload") or {}
    action = step["action"]
    target_id = step["target_id"]
    result: dict[str, Any] = {
        "available": False,
        "matches": None,
        "declared": {},
        "observed": {},
        "note": "",
    }
    if action in ("repoint_task", "repoint_downstream", "repoint_metric"):
        field = {"repoint_task": "source_refs",
                 "repoint_downstream": "upstream_refs",
                 "repoint_metric": "source_refs"}[action]
        observed = next((change for change in changes
                         if change["entity_id"] == target_id
                         and change["field"] == field), None)
        result["available"] = "before" in payload and "after" in payload
        if not result["available"]:
            return result
        declared_before = list(payload.get("before") or [])
        declared_after = list(payload.get("after") or [])
        result["declared"] = {"before": declared_before,
                              "after": declared_after}
        observed_before = list(observed["before"]) if observed else None
        observed_after = list(observed["after"]) if observed else None
        result["observed"] = {"before": observed_before,
                              "after": observed_after}
        result["matches"] = (observed is not None
                             and declared_before == observed_before
                             and declared_after == observed_after)
        if not result["matches"]:
            result["note"] = "计划声明的引用与产物中观测到的差异不一致"
        return result
    if action == "repoint_lineage":
        observed = {change["field"]: change for change in changes
                    if change["entity_id"] == target_id}
        result["available"] = "before" in payload and "after" in payload
        if not result["available"]:
            return result
        declared_before = dict(payload.get("before") or {})
        declared_after = dict(payload.get("after") or {})
        result["declared"] = {"before": declared_before,
                              "after": declared_after}
        observed_before = {field: observed[field]["before"]
                           for field in LINEAGE_ENDPOINTS if field in observed}
        observed_after = {field: observed[field]["after"]
                          for field in LINEAGE_ENDPOINTS if field in observed}
        result["observed"] = {"before": observed_before,
                              "after": observed_after}
        result["matches"] = bool(observed) and all(
            declared_before.get(field) == observed_before.get(field)
            and declared_after.get(field) == observed_after.get(field)
            for field in observed)
        if not result["matches"]:
            result["note"] = "计划声明的血缘端点与产物中观测到的差异不一致"
        return result
    if action in ("retire_source", "restore_source", "retire_downstream",
                  "restore_downstream"):
        observed = next((change for change in changes
                         if change["entity_id"] == target_id
                         and change["field"] == "status"), None)
        expected_after = payload.get("status")
        if action == "retire_source" and expected_after is None:
            expected_after = "retired"
        result["available"] = expected_after is not None
        result["declared"] = {"status": expected_after}
        result["observed"] = (
            {"before": observed["before"], "after": observed["after"]}
            if observed else {"before": None, "after": None})
        result["matches"] = (observed is not None
                             and observed["after"] == expected_after)
        if not result["matches"]:
            result["note"] = "计划声明的状态变更与产物中观测到的差异不一致"
        return result
    result["note"] = "该动作没有可直接比较的前后声明"
    return result


def _changed_entities(changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    entities: list[dict[str, str]] = []
    for change in changes:
        key = (change["entity_type"], change["entity_id"])
        if key in seen:
            continue
        seen.add(key)
        entities.append({"entity_type": key[0], "entity_id": key[1]})
    return entities


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return lines


def render_explanation_markdown(explanation: dict[str, Any]) -> str:
    run = explanation["run"]
    summary = explanation["summary"]
    baseline = explanation["baseline"]
    lines = [
        f"# 运行复盘：{run['run_id']}",
        "",
        f"- 类型：`{run['kind']}`；状态：`{run['status']}`",
        f"- 请求：`{run['request_id']}`；计划：`{run['plan_id']}`",
        f"- 初始哈希：`{run['initial_hash']}`",
        f"- 最终哈希：`{run['final_hash']}`",
        f"- 回到基线：`{str(summary['returned_to_initial_hash']).lower()}`"
        f"（{baseline['baseline_kind']}）",
        f"- 空影响步骤：{summary['empty_impact_count']}",
        "",
        "## 总览",
        "",
        summary["narrative"],
        "",
        "## 最终状态核对",
        "",
    ]
    baseline_rows = [
        ["运行初始哈希", baseline["run_initial_hash"]],
        ["运行最终哈希", baseline["run_final_hash"]],
        ["state.json 实际哈希", baseline["final_state_hash"]],
        ["最终状态与台账一致", str(baseline["final_state_matches_ledger"]).lower()],
        ["最终回到基线", str(baseline["returned_to_initial_hash"]).lower()],
    ]
    if baseline["baseline_kind"] == "source_initial_hash":
        baseline_rows.append(["原执行前基线", baseline["source_initial_hash"]])
    lines += _md_table(["项目", "值"], baseline_rows)

    transitions = explanation["conflict_context"]["transitions"]
    lines += ["", "## 冲突状态迁移", ""]
    if transitions:
        rows = [[item["rule_id"], item["target"].get("id", ""),
                 item["basis"], item["step_id"]]
                for item in transitions]
        lines += _md_table(["规则", "对象", "依据", "消解步骤"], rows)
    else:
        lines.append("本次运行没有记录到从 blocking 走向可执行的冲突迁移。")

    lines += ["", "## 逐步复盘", ""]
    lines += _md_table(
        ["步骤", "动作", "对象", "状态", "空影响", "有效字段集变化"],
        [[step["step_id"], step["action"],
          f"{step['target_type']}:{step['target_id']}", step["status"],
          str(step["empty_impact"]).lower(),
          str(step["downstream_effective_field_set_changed"]).lower()]
         for step in explanation["steps"]])
    lines += [""]
    for step in explanation["steps"]:
        target = f"{step['target_type']}:{step['target_id']}"
        lines += [f"### {step['step_id']} · {step['action']} · `{target}`", ""]
        lines += [f"- 计划说明：{step['planned']['description'] or '（计划产物未记录）'}",
                  f"- 计划动因：{step['planned']['why_planned']}",
                  f"- 台账状态：`{step['status']}`；尝试 {step['attempts']} 次；"
                  f"批次 {step['batch']}",
                  f"- 前后哈希：`{step['execution']['before_hash']}` → "
                  f"`{step['execution']['after_hash']}`",
                  f"- 旁白：{step['narrative']}"]
        if step["empty_impact"]:
            lines += ["", f"> **空影响步骤**：{step['empty_impact_reason']}。", ""]
        scope = step.get("lineage_scope")
        if scope:
            lines += ["", "#### 血缘端点范围", ""]
            lines += [f"- 切换端点：{', '.join(scope['switched_endpoints']) or '无'}",
                      f"- 未切换端点：{', '.join(scope['unchanged_endpoints']) or '无'}",
                      f"- 局部端点切换："
                      f"`{str(scope['partial_endpoint_switch']).lower()}`"]
            excluded = scope["unchanged_endpoint_downstream_not_counted"]
            if excluded:
                lines += ["", "未计入本次切换影响的对象（未切换端点一侧）：", ""]
                lines += [f"- `{item['entity_type']}:{item['entity_id']}` "
                          f"路径 {' → '.join(item['path'])}"
                          for item in excluded]
            lines += [""]
        lines += ["", "#### 状态字段差异", ""]
        changes = step["state_diff"]["changes"]
        if changes:
            rows = [[change["entity_type"], change["entity_id"],
                     change["field"], _format_value(change["before"]),
                     _format_value(change["after"])] for change in changes]
            lines += _md_table(["实体类型", "实体", "字段", "之前", "之后"], rows)
        else:
            lines.append("无实体有效字段变化。")
        check = step["declaration_check"]
        if check["available"]:
            declared = json.dumps(check["declared"], ensure_ascii=False,
                                  sort_keys=True)
            observed = json.dumps(check["observed"], ensure_ascii=False,
                                  sort_keys=True)
            lines += ["", "#### 计划声明核对", "",
                      f"- 计划声明：`{declared}`",
                      f"- 产物观测：`{observed}`",
                      f"- 一致：`{str(bool(check['matches'])).lower()}`"]
            if check["note"]:
                lines.append(f"- 说明：{check['note']}")
        if step["ref_switches"]:
            lines += ["", "#### 引用切换", ""]
            lines += _md_table(
                ["字段", "之前", "之后"],
                [[item["field"], item["before"] or "—", item["after"] or "—"]
                 for item in step["ref_switches"]])
        impact = step["downstream_impact"]
        lines += ["", "#### 下游影响", ""]
        lines.append(
            f"可达影响面（依赖图层面）：下游 {impact['reachable']['counts']['downstream']} "
            f"个、口径 {impact['reachable']['counts']['metrics']} 个；"
            f"有效字段集实际变化：下游 {impact['effective']['counts']['downstream']} "
            f"个、口径 {impact['effective']['counts']['metrics']} 个。")
        rows = [[item["entity_type"], item["entity_id"], item["relation"],
                 item["depth"], " → ".join(item["path"]), item["basis"]]
                for item in (impact["reachable"]["downstream"]
                             + impact["reachable"]["metrics"])]
        if rows:
            lines += [""] + _md_table(
                ["类型", "对象", "关系", "深度", "路径", "依据"], rows)
        else:
            lines.append("")
            lines.append("没有可达的下游对象或口径。")
        effective = (impact["effective"]["downstream"]
                     + impact["effective"]["metrics"])
        if effective:
            lines += ["", "有效字段集发生变化的对象：", ""]
            rows = [[item["entity_type"], item["entity_id"],
                     item["relation"], " → ".join(item["path"])]
                    for item in effective]
            lines += _md_table(["类型", "对象", "关系", "路径"], rows)
            for change in impact["effective"]["field_set_changes"]:
                added = ", ".join(change["added_fields"]) or "—"
                removed = ", ".join(change["removed_fields"]) or "—"
                lines.append(
                    f"- `{change['entity_type']}:{change['entity_id']}` "
                    f"新增字段 {added}；移除字段 {removed}")
        if impact["not_counted"]:
            lines += ["", "保留端点一侧的下游对象已显式排除，"
                          "未计入可达影响面，见上文血缘端点范围。"]
        if step["conflict_transitions"]:
            lines += ["", "#### 冲突判定", ""]
            rows = [[item["rule_id"], item.get("source", ""),
                     item["severity"], item["transition"], item["basis"]]
                    for item in step["conflict_transitions"]]
            lines += _md_table(["规则", "证据来源", "原分级", "迁移", "依据"], rows)
        artifact = step["execution"]["artifact"]
        lines += ["", "#### 证据", "",
                  f"- 产物路径：`{artifact['path'] or '—'}`",
                  f"- 台账 SHA-256：`{artifact['sha256'] or '—'}`",
                  f"- 实际 SHA-256：`{artifact['sha256_actual'] or '—'}`",
                  f"- 证据校验：`{str(artifact['ok']).lower()}`", ""]

    lines += ["## 证据索引", ""]
    rows = [[item["step_id"], item["path"], item["sha256"],
             str(item["ok"]).lower()] for item in explanation["evidence_index"]]
    lines += _md_table(["步骤", "产物相对路径", "SHA-256", "校验"], rows)
    warnings = explanation["warnings"]
    if warnings:
        lines += ["", "## 警告", ""]
        lines += [f"- {warning}" for warning in warnings]
    lines += ["", "---", "",
              "本复盘只读取落盘台账与产物，未重新运行影响分析。", ""]
    return "\n".join(lines)


def write_explanation(explanation: dict[str, Any], paths: WorkPaths,
                      *, json_path: str | Path | None = None,
                      markdown_path: str | Path | None = None
                      ) -> dict[str, str]:
    run_id = explanation["run"]["run_id"]
    json_target = (Path(json_path) if json_path is not None else
                   paths.runs / run_id / "explain.json")
    md_target = (Path(markdown_path) if markdown_path is not None else
                 paths.reports / f"explain-{run_id}.md")
    write_json(json_target, explanation)
    md_target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(md_target, render_explanation_markdown(explanation))
    return {"json": str(json_target), "markdown": str(md_target)}
