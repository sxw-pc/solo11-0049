"""Build idempotent, reversible rollback scripts from an executed plan."""
from __future__ import annotations

from typing import Any

from ..config import SCHEMA_VERSION
from ..io.jsonout import sha256_json

_REVERSIBLE = {"repoint_task", "repoint_downstream", "repoint_metric",
               "repoint_lineage", "retire_source"}


def _reverse_step(plan_step: dict[str, Any], original: dict[str, Any],
                  index: int) -> dict[str, Any] | None:
    action = original["action"]
    if action not in _REVERSIBLE:
        return None
    payload = plan_step.get("payload", {})
    if action == "retire_source":
        previous = payload.get("previous_status", "active")
        reverse_action = "restore_source"
        reverse_payload = {"source": original["target_id"], "status": previous}
        conditions = [{"path": f"sources.{original['target_id']}.status",
                       "op": "equals", "value": previous}]
        description = f"恢复数据源 {original['target_id']} 的状态为 {previous}"
    elif action == "repoint_lineage":
        reverse_action = action
        reverse_payload = {"before": payload.get("after", {}),
                           "after": payload.get("before", {})}
        after = reverse_payload["after"]
        conditions = [
            {"path": f"lineage.{original['target_id']}.from_ref",
             "op": "equals", "value": after.get("from_ref")},
            {"path": f"lineage.{original['target_id']}.to_ref",
             "op": "equals", "value": after.get("to_ref")},
        ]
        description = f"回退血缘边 {original['target_id']}"
    else:
        reverse_action = action
        reverse_payload = {"before": payload.get("after", []),
                           "after": payload.get("before", [])}
        path = {
            "repoint_task": f"tasks.{original['target_id']}.source_refs",
            "repoint_downstream": (
                f"downstream.{original['target_id']}.upstream_refs"),
            "repoint_metric": f"metrics.{original['target_id']}.source_refs",
        }[action]
        conditions = [{"path": path, "op": "equals",
                       "value": reverse_payload["after"]}]
        description = f"回退 {original['target_id']} 的引用到执行前状态"
    return {
        "step_id": f"rb-step-{index:04d}",
        "reverse_of_step_id": original["step_id"],
        "action": reverse_action,
        "target_type": original["target_type"],
        "target_id": original["target_id"],
        "payload": reverse_payload,
        "description": description,
        "expected_postconditions": conditions,
        "depends_on": [],
        "batch": index,
        "sub_batch": 1,
    }


def build_rollback_script(plan: dict[str, Any],
                          executed_steps: list[dict[str, Any]],
                          initial_state_hash: str) -> dict[str, Any]:
    plan_steps = {step["step_id"]: step for step in plan.get("steps", [])}
    successes = [s for s in executed_steps if s.get("status") == "succeeded"]
    successes.sort(key=lambda s: int(s.get("ordinal", 0)), reverse=True)
    steps: list[dict[str, Any]] = []
    for original in successes:
        plan_step = plan_steps.get(original["step_id"])
        if plan_step is None:
            continue
        reverse = _reverse_step(plan_step, original, len(steps) + 1)
        if reverse is None:
            continue
        if steps:
            reverse["depends_on"] = [steps[-1]["step_id"]]
        steps.append(reverse)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "rollback",
        "source_run_id": plan.get("_run_id", ""),
        "source_plan_id": plan.get("plan_id", ""),
        "source_plan_hash": plan.get("plan_hash", ""),
        "expected_initial_hash": initial_state_hash,
        "steps": steps,
    }
    body["rollback_hash"] = sha256_json(body)
    body["rollback_id"] = f"rollback-{body['rollback_hash'][:16]}"
    return body
