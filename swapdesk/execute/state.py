"""Mock world state, action application and consistency assertions."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import ErrorCode, SwapDeskError
from ..io.jsonout import sha256_json


def state_hash(state: dict[str, Any]) -> str:
    return sha256_json(state)


def clone_state(state: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(state)


def get_path(state: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def check_conditions(state: dict[str, Any],
                     conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    differences = []
    for condition in conditions:
        path = condition["path"]
        op = condition["op"]
        expected = condition.get("value")
        exists, actual = get_path(state, path)
        ok = True
        if op == "exists":
            ok = exists
        elif not exists:
            ok = False
        elif op == "equals":
            ok = actual == expected
        elif op == "not_contains":
            ok = isinstance(actual, list) and expected not in actual
        elif op == "contains":
            ok = isinstance(actual, list) and expected in actual
        elif op == "empty":
            ok = actual in ([], {}, "")
        else:
            raise SwapDeskError(f"未知断言操作 {op}",
                                code=ErrorCode.PLAN_INVALID, field="expected_postconditions")
        if not ok:
            differences.append({"path": path, "op": op, "expected": expected,
                                "actual": actual if exists else "<missing>",
                                "exists": exists})
    return differences


def _expect(actual: Any, expected: Any, step: dict[str, Any]) -> None:
    if actual != expected:
        raise SwapDeskError(
            "执行前状态与步骤声明不一致",
            code=ErrorCode.STATE_MISMATCH,
            record_id=step["step_id"], field="payload.before",
            details={"expected": expected, "actual": actual},
        )


def _repoint_list(state: dict[str, Any], group: str, field_name: str,
                  step: dict[str, Any]) -> None:
    node = state[group][step["target_id"]]
    before = step["payload"]["before"]
    after = step["payload"]["after"]
    actual = node[field_name]
    if actual == before:
        node[field_name] = list(after)
        return
    details: dict[str, Any] = {
        "target_type": step["target_type"], "target_id": step["target_id"],
        "expected_before": before, "actual": actual, "planned_after": after,
    }
    selection = step["payload"].get("selection")
    if selection:
        details["selection"] = selection
    if actual == after:
        raise SwapDeskError(
            "目标已被重指到本次计划的候选源，疑似前序批次已为其他消费方完成，"
            "拒绝静默重复",
            code=ErrorCode.STATE_MISMATCH, record_id=step["step_id"],
            field="payload.before", details=details)
    raise SwapDeskError(
        "执行前状态与步骤声明不一致",
        code=ErrorCode.STATE_MISMATCH, record_id=step["step_id"],
        field="payload.before", details=details)


def apply_step(state: dict[str, Any], step: dict[str, Any]) -> None:
    """Mutate state in place; callers pass a clone and commit only on success."""
    action = step["action"]
    target_id = step["target_id"]
    payload = step.get("payload", {})
    if action == "repoint_task":
        _repoint_list(state, "tasks", "source_refs", step)
    elif action == "repoint_downstream":
        _repoint_list(state, "downstream", "upstream_refs", step)
    elif action == "repoint_metric":
        _repoint_list(state, "metrics", "source_refs", step)
    elif action == "repoint_lineage":
        node = state["lineage"][target_id]
        prior = {"from_ref": node["from_ref"], "to_ref": node["to_ref"]}
        _expect(prior, payload["before"], step)
        node["from_ref"] = payload["after"]["from_ref"]
        node["to_ref"] = payload["after"]["to_ref"]
    elif action == "retire_source":
        previous = state["sources"][target_id]["status"]
        _expect(previous, payload.get("previous_status", previous), step)
        state["sources"][target_id]["status"] = payload.get("status", "retired")
    elif action == "restore_source":
        _expect(state["sources"][target_id]["status"], "retired", step)
        state["sources"][target_id]["status"] = payload.get("status", "active")
    elif action == "retire_downstream":
        state["downstream"][target_id]["status"] = "retired"
    elif action == "restore_downstream":
        state["downstream"][target_id]["status"] = payload.get("status", "active")
    elif action == "add_compat_field":
        fields = state["sources"][target_id]["fields"]
        for field in payload.get("fields", []):
            if field not in fields:
                fields.append(field)
    elif action == "review_metric":
        if target_id not in state["metrics"]:
            raise SwapDeskError("口径不存在", code=ErrorCode.STATE_MISMATCH,
                                record_id=target_id)
    else:
        raise SwapDeskError(f"未知动作 {action}", code=ErrorCode.PLAN_INVALID,
                            record_id=step.get("step_id"), field="action")


def references(state: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for task_id, node in state["tasks"].items():
        for ref in node["source_refs"]:
            refs.append({"owner_type": "task", "owner_id": task_id,
                         "field": "source_refs", "ref": ref})
    for table_id, node in state["downstream"].items():
        for ref in node["upstream_refs"]:
            refs.append({"owner_type": "downstream", "owner_id": table_id,
                         "field": "upstream_refs", "ref": ref})
    for metric_id, node in state["metrics"].items():
        for ref in node["source_refs"]:
            refs.append({"owner_type": "metric", "owner_id": metric_id,
                         "field": "source_refs", "ref": ref})
    for edge_id, node in state["lineage"].items():
        for field in ("from_ref", "to_ref"):
            refs.append({"owner_type": "lineage", "owner_id": edge_id,
                         "field": field, "ref": node[field]})
    return refs


def consistency_issues(state: dict[str, Any]) -> list[dict[str, Any]]:
    known = (set(state["sources"]) | set(state["tasks"])
             | set(state["downstream"]) | set(state["metrics"]))
    retired = {sid for sid, node in state["sources"].items()
               if node.get("status") == "retired"}
    issues = []
    for ref in references(state):
        if ref["ref"] not in known:
            issues.append({"kind": "dangling_reference", **ref})
        if ref["ref"] in retired:
            issues.append({"kind": "retired_source_referenced", **ref})
    for task_id, node in state["tasks"].items():
        if node.get("status") == "active" and not node.get("source_refs"):
            issues.append({"kind": "empty_task_sources", "owner_type": "task",
                           "owner_id": task_id, "field": "source_refs"})
    return issues
