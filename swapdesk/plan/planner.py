"""Migration plan construction: stable steps, batches and assertions."""
from __future__ import annotations

from typing import Any

from ..analysis.selection import select_tiers
from ..config import SCHEMA_VERSION
from ..errors import PlanBlockedError
from ..io.jsonout import sha256_json
from ..model.entities import Registry, SwapRequest

RANK = {"repoint_task": 1, "repoint_lineage": 2, "repoint_downstream": 2,
        "repoint_metric": 3, "review_metric": 3, "retire_source": 4}


def _replace_refs(refs: list[str], mapping: dict[str, str]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        new_ref = mapping.get(ref, ref)
        if new_ref not in result:
            result.append(new_ref)
    return result


def _repoint_payload(before: list[str], after: list[str]) -> dict[str, Any]:
    return {"before": list(before), "after": list(after)}


def _step(action: str, target_type: str, target_id: str, payload: dict[str, Any],
          description: str,
          postconditions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action": action, "target_type": target_type, "target_id": target_id,
        "payload": payload, "description": description,
        "depends_on": [], "expected_postconditions": postconditions,
        "rank": RANK[action],
    }


def _consumer_selections(selections: dict[str, Any] | None,
                         kind: str) -> dict[str, Any]:
    if not selections:
        return {}
    return {entry["consumer_id"]: entry
            for entry in selections["consumers"]
            if entry["consumer_type"] == kind}


def _global_reason(choices: dict[str, Any]) -> str:
    parts = [f"{source}→{choice['candidate']}"
             f"（第{choice['tier']}档，{choice['basis']}）"
             for source, choice in sorted(choices.items())]
    return "共享实体按全局候选档映射改指：" + "；".join(parts)


def _global_selection(selections: dict[str, Any],
                      removed: list[str]) -> dict[str, Any]:
    choices = {source: selections["global_choices"][source]
               for source in removed
               if source in selections["global_choices"]}
    return {"choices": choices, "reason": _global_reason(choices)}


def _task_steps(registry: Registry, analysis: dict[str, Any],
                retired: set[str], mapping: dict[str, str],
                selections: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = []
    selected = _consumer_selections(selections, "task")
    for info in analysis["impact"]["affected"]["tasks"]:
        task = registry.task_map[info["id"]]
        removed = sorted(set(task.source_refs) & retired)
        if not removed:
            continue
        selection = selected.get(task.id)
        task_mapping = selection["mapping"] if selection is not None else mapping
        after = _replace_refs(list(task.source_refs), task_mapping)
        payload = _repoint_payload(list(task.source_refs), after)
        if selection is not None:
            payload["selection"] = {"choices": selection["choices"],
                                    "reason": selection["reason"]}
        conditions = [{"path": f"tasks.{task.id}.source_refs", "op": "equals",
                       "value": after}]
        conditions.extend({"path": f"tasks.{task.id}.source_refs",
                           "op": "not_contains", "value": ref} for ref in removed)
        steps.append(_step(
            "repoint_task", "task", task.id,
            payload,
            f"将任务 {task.id} 的源引用从 {', '.join(removed)} 改指替代源",
            conditions,
        ))
    return steps


def _downstream_steps(registry: Registry, analysis: dict[str, Any],
                      retired: set[str], mapping: dict[str, str],
                      selections: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = []
    for info in analysis["impact"]["affected"]["downstream"]:
        table = registry.downstream_map[info["id"]]
        removed = sorted(set(table.upstream_refs) & retired)
        if not removed:
            continue
        after = _replace_refs(list(table.upstream_refs), mapping)
        payload = _repoint_payload(list(table.upstream_refs), after)
        if selections is not None:
            payload["selection"] = _global_selection(selections, removed)
        conditions = [{"path": f"downstream.{table.id}.upstream_refs",
                       "op": "equals", "value": after}]
        steps.append(_step(
            "repoint_downstream", "downstream", table.id,
            payload,
            f"将下游对象 {table.id} 的直接源依赖改指替代源",
            conditions,
        ))
    return steps


def _lineage_endpoint(ref: str, other_ref: str,
                      retired: set[str], mapping: dict[str, str],
                      consumer_choices: dict[str, dict[str, Any]],
                      selections: dict[str, Any] | None
                      ) -> tuple[str, dict[str, Any] | None]:
    """Resolve one lineage endpoint; consumer-aware when possible."""
    if ref not in retired:
        return ref, None
    choice = consumer_choices.get((other_ref, ref))
    if choice is not None:
        return choice["candidate"], {
            "candidate": choice["candidate"], "tier": choice["tier"],
            "basis": f"跟随消费方 {other_ref} 选档",
        }
    candidate = mapping.get(ref, ref)
    if selections is not None and ref in selections["global_choices"]:
        global_choice = selections["global_choices"][ref]
        return candidate, {
            "candidate": candidate, "tier": global_choice["tier"],
            "basis": f"全局候选档映射（{global_choice['basis']}）",
        }
    return candidate, None


def _lineage_steps(registry: Registry, retired: set[str], mapping: dict[str, str],
                   selections: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = []
    consumer_choices: dict[tuple[str, str], dict[str, Any]] = {}
    if selections is not None:
        for entry in selections["consumers"]:
            for source, choice in entry["choices"].items():
                consumer_choices[(entry["consumer_id"], source)] = choice
    for edge in sorted(registry.lineage, key=lambda item: item.id):
        if edge.from_ref not in retired and edge.to_ref not in retired:
            continue
        new_from, from_choice = _lineage_endpoint(
            edge.from_ref, edge.to_ref, retired, mapping,
            consumer_choices, selections)
        new_to, to_choice = _lineage_endpoint(
            edge.to_ref, edge.from_ref, retired, mapping,
            consumer_choices, selections)
        after = {"from_ref": new_from, "to_ref": new_to}
        payload: dict[str, Any] = {
            "before": {"from_ref": edge.from_ref, "to_ref": edge.to_ref},
            "after": after,
        }
        if selections is not None:
            endpoint_choices = {}
            reason_parts = []
            for ref, choice in ((edge.from_ref, from_choice),
                                (edge.to_ref, to_choice)):
                if choice is None:
                    continue
                endpoint_choices[ref] = {"candidate": choice["candidate"],
                                         "tier": choice["tier"]}
                reason_parts.append(
                    f"端点 {ref}→{choice['candidate']}"
                    f"（第{choice['tier']}档，{choice['basis']}）")
            payload["selection"] = {"choices": endpoint_choices,
                                    "reason": "；".join(reason_parts)}
        conditions = [
            {"path": f"lineage.{edge.id}.from_ref", "op": "equals", "value": new_from},
            {"path": f"lineage.{edge.id}.to_ref", "op": "equals", "value": new_to},
        ]
        steps.append(_step(
            "repoint_lineage", "lineage", edge.id,
            payload,
            f"迁移血缘边 {edge.id} 的撤换端点",
            conditions,
        ))
    return steps


def _metric_steps(registry: Registry, analysis: dict[str, Any],
                  retired: set[str], mapping: dict[str, str],
                  selections: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = []
    selected = _consumer_selections(selections, "metric")
    for info in analysis["impact"]["affected"]["metrics"]:
        metric = registry.metric_map[info["id"]]
        removed = sorted(set(metric.source_refs) & retired)
        if removed:
            selection = selected.get(metric.id)
            metric_mapping = (selection["mapping"]
                              if selection is not None else mapping)
            after = _replace_refs(list(metric.source_refs), metric_mapping)
            payload = _repoint_payload(list(metric.source_refs), after)
            if selection is not None:
                payload["selection"] = {"choices": selection["choices"],
                                        "reason": selection["reason"]}
            conditions = [{"path": f"metrics.{metric.id}.source_refs",
                           "op": "equals", "value": after}]
            steps.append(_step(
                "repoint_metric", "metric", metric.id,
                payload,
                f"将口径 {metric.id} 的源引用改指替代源",
                conditions,
            ))
        else:
            conditions = [{"path": f"metrics.{metric.id}.source_refs",
                           "op": "exists"}]
            steps.append(_step(
                "review_metric", "metric", metric.id, {"metric": metric.id},
                f"复核仅经血缘受影响的口径 {metric.id}",
                conditions,
            ))
    return steps


def _retire_steps(registry: Registry, request: SwapRequest) -> list[dict[str, Any]]:
    steps = []
    for item in sorted(request.retire, key=lambda r: r.source):
        source = registry.source_map[item.source]
        steps.append(_step(
            "retire_source", "source", item.source,
            {"source": item.source, "previous_status": source.status,
             "status": "retired"},
            f"将数据源 {item.source} 标记为 retired",
            [{"path": f"sources.{item.source}.status", "op": "equals",
              "value": "retired"}],
        ))
    return steps


def _assign_dependencies(steps: list[dict[str, Any]]) -> None:
    by_rank: dict[int, list[str]] = {}
    for step in steps:
        by_rank.setdefault(step["rank"], []).append(step["step_id"])
    for step in steps:
        step["depends_on"] = [dep for rank in sorted(by_rank)
                              if rank < step["rank"] for dep in by_rank[rank]]


def build_plan(registry: Registry, request: SwapRequest,
               analysis: dict[str, Any]) -> dict[str, Any]:
    conflicts = analysis["conflicts"]
    if not analysis["summary"]["executable"]:
        raise PlanBlockedError(conflicts)
    selections = analysis.get("selections")
    if selections is None and request.uses_candidates:
        selections = select_tiers(registry, request)
    if selections is not None:
        retired = set(request.retired_sources)
        shared_mapping = dict(selections["global_mapping"])
    else:
        retired = set(request.replacement_map)
        shared_mapping = request.replacement_map
    steps = (_task_steps(registry, analysis, retired, shared_mapping, selections)
             + _downstream_steps(registry, analysis, retired, shared_mapping,
                                 selections)
             + _lineage_steps(registry, retired, shared_mapping, selections)
             + _metric_steps(registry, analysis, retired, shared_mapping,
                             selections)
             + _retire_steps(registry, request))
    steps.sort(key=lambda s: (s["rank"], s["target_type"], s["target_id"],
                              s["action"]))
    for index, step in enumerate(steps, start=1):
        step["step_id"] = f"step-{index:04d}"
    _assign_dependencies(steps)
    batches: list[dict[str, Any]] = []
    for batch_no, rank in enumerate(sorted({s["rank"] for s in steps}), start=1):
        rank_steps = [s for s in steps if s["rank"] == rank]
        sub_batches: list[list[str]] = []
        for start in range(0, len(rank_steps), request.max_parallel):
            chunk = rank_steps[start:start + request.max_parallel]
            sub_batch = len(sub_batches) + 1
            for step in chunk:
                step["batch"] = batch_no
                step["sub_batch"] = sub_batch
            sub_batches.append([s["step_id"] for s in chunk])
        batches.append({
            "batch": batch_no, "rank": rank,
            "sub_batches": sub_batches,
            "step_ids": [sid for group in sub_batches for sid in group],
        })
    body = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request.request_id,
        "analysis_hash": analysis["analysis_hash"],
        "status": "ready",
        "max_parallel": request.max_parallel,
        "retired_sources": list(request.retired_sources),
        "replacements": dict(sorted(shared_mapping.items())),
        "batches": batches,
        "steps": steps,
        "action_counts": {action: sum(1 for s in steps if s["action"] == action)
                          for action in sorted({s["action"] for s in steps})},
    }
    if selections is not None:
        body["selections"] = selections
    plan_hash = sha256_json(body)
    body["plan_hash"] = plan_hash
    body["plan_id"] = f"plan-{plan_hash[:16]}"
    return body


def validate_plan(plan: dict[str, Any]) -> None:
    from ..errors import ErrorCode, SwapDeskError
    required = ("plan_id", "plan_hash", "steps", "batches", "max_parallel")
    for key in required:
        if key not in plan:
            raise SwapDeskError(f"计划缺少字段 {key}", code=ErrorCode.PLAN_INVALID,
                                field=key)
    seen = set()
    all_ids = {step.get("step_id") for step in plan["steps"]}
    for step in plan["steps"]:
        sid = step.get("step_id")
        if not sid or sid in seen:
            raise SwapDeskError("计划 step_id 缺失或重复",
                                code=ErrorCode.PLAN_INVALID, field="step_id")
        seen.add(sid)
        for dep in step.get("depends_on", []):
            if dep not in all_ids:
                raise SwapDeskError("计划依赖不存在", code=ErrorCode.PLAN_INVALID,
                                    field="depends_on", details={"step_id": sid})
