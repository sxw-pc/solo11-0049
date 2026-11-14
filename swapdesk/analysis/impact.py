"""Deterministic impact propagation through the registered dependency graph."""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

from ..io.jsonout import canonical_json, sha256_json
from ..model.entities import Registry, SwapRequest


def build_graph(registry: Registry) -> dict[str, list[str]]:
    """Build source -> task -> downstream -> metric edges plus lineage edges."""
    nodes = sorted(registry.entity_ids())
    graph: dict[str, set[str]] = {node: set() for node in nodes}
    for task in registry.tasks:
        for ref in task.source_refs:
            if ref in graph:
                graph[ref].add(task.id)
    for table in registry.downstream:
        for ref in table.upstream_refs:
            if ref in graph:
                graph[ref].add(table.id)
    for edge in registry.lineage:
        if edge.from_ref in graph and edge.to_ref in graph:
            graph[edge.from_ref].add(edge.to_ref)
    for metric in registry.metrics:
        for ref in metric.source_refs:
            if ref in graph:
                graph[ref].add(metric.id)
    return {node: sorted(targets) for node, targets in sorted(graph.items())}


def _canonical_cycle(nodes: list[str]) -> tuple[str, ...]:
    if not nodes:
        return ()
    pivot = min(range(len(nodes)), key=lambda i: nodes[i])
    return tuple(nodes[pivot:] + nodes[:pivot])


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find directed cycles with a deterministic, depth-first walk."""
    color: dict[str, int] = {}
    stack: list[str] = []
    found: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            state = color.get(nxt, 0)
            if state == 1:
                start = stack.index(nxt)
                cycle = _canonical_cycle(stack[start:])
                found.add(cycle)
            elif state == 0:
                visit(nxt)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            visit(node)
    cycles = [list(cycle) + [cycle[0]] for cycle in sorted(found)]
    return cycles


def _shortest_paths(registry: Registry, graph: dict[str, list[str]],
                    starts: list[str]) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    queue: deque[str] = deque()
    for start in sorted(starts):
        paths[start] = [start]
        queue.append(start)
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, []):
            if nxt not in paths:
                paths[nxt] = paths[node] + [nxt]
                queue.append(nxt)
    return paths


def _warnings(cycles: list[list[str]]) -> list[dict[str, Any]]:
    warnings = []
    for cycle in cycles:
        warnings.append({
            "rule_id": "W_LINEAGE_CYCLE",
            "severity": "warning",
            "message": "检测到依赖环，扩散在该环处按已访问集合截断",
            "evidence": {"nodes": cycle, "node_count": len(cycle) - 1},
        })
    return warnings


def analyze_impact(registry: Registry, request: SwapRequest) -> dict[str, Any]:
    graph = build_graph(registry)
    cycles = find_cycles(graph)
    starts = sorted(request.retired_sources)
    paths = _shortest_paths(registry, graph, starts)
    retired = set(starts)

    tasks: list[dict[str, Any]] = []
    for task in sorted(registry.tasks, key=lambda item: item.id):
        if task.id not in paths or paths[task.id][0] in retired and len(paths[task.id]) == 1:
            continue
        path = paths[task.id]
        removed = sorted(set(task.source_refs) & retired)
        remaining = [ref for ref in task.source_refs if ref not in retired]
        if not removed and len(path) < 2:
            continue
        tasks.append({
            "id": task.id, "name": task.name,
            "impact": "direct" if len(path) == 2 else "indirect",
            "depth": len(path) - 1, "path": path,
            "retired_refs": removed, "remaining_sources": remaining,
            "partial": bool(removed and remaining),
            "replacement_refs": sorted({request.replacement_map[ref]
                                        for ref in removed
                                        if request.replacement_map.get(ref)}),
        })

    downstream: list[dict[str, Any]] = []
    for table in sorted(registry.downstream, key=lambda item: item.id):
        if table.id not in paths:
            continue
        path = paths[table.id]
        downstream.append({
            "id": table.id, "name": table.name, "kind": table.kind,
            "impact": "direct" if len(path) == 2 else "indirect",
            "depth": len(path) - 1, "path": path,
            "retired_refs": sorted(set(table.upstream_refs) & retired),
            "upstream_refs": list(table.upstream_refs),
        })

    metrics: list[dict[str, Any]] = []
    for metric in sorted(registry.metrics, key=lambda item: item.id):
        if metric.id not in paths:
            continue
        path = paths[metric.id]
        metrics.append({
            "id": metric.id, "name": metric.name,
            "impact": "direct" if len(path) == 2 else "indirect",
            "depth": len(path) - 1, "path": path,
            "required": metric.required,
            "required_fields": list(metric.required_fields),
            "retired_refs": sorted(set(metric.source_refs) & retired),
            "source_refs": list(metric.source_refs),
        })

    grouping = {
        "tasks": tasks,
        "downstream": downstream,
        "metrics": metrics,
    }
    counts = {key: len(value) for key, value in grouping.items()}
    impact = {
        "request_id": request.request_id,
        "retired_sources": starts,
        "affected": grouping,
        "counts": counts,
        "warnings": _warnings(cycles),
        "cycles": cycles,
        "graph": {
            "node_count": len(graph),
            "edge_count": sum(len(targets) for targets in graph.values()),
        },
    }
    impact["impact_hash"] = sha256_json(impact)
    return impact


def analysis_fingerprint(impact: dict[str, Any],
                         conflicts: list[dict[str, Any]]) -> str:
    body = {"impact": impact, "conflicts": conflicts}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
