"""Compare declared plans with the ledger, step artifacts and final state."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import WorkPaths
from ..errors import ErrorCode, SwapDeskError
from ..io.jsonout import read_json, sha256_file, sha256_json
from ..model.entities import Registry
from .ledger import Ledger
from .state import check_conditions, consistency_issues, state_hash


def _diff(code: str, message: str, *, step_id: str = "",
          field: str = "", expected: Any = None, actual: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if step_id:
        item["step_id"] = step_id
    if field:
        item["field"] = field
    item["expected"] = expected
    item["actual"] = actual
    return item


def _artifact_for(ledger: Ledger, run_id: str, step_id: str) -> dict[str, Any] | None:
    for artifact in ledger.artifacts_for_run(run_id):
        if artifact["step_id"] == step_id and artifact["kind"] == "state_after":
            return artifact
    return None


def _check_steps(paths: WorkPaths, ledger: Ledger, run: dict[str, Any],
                 expected_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    actual_steps = ledger.steps_for_run(run["run_id"])
    actual_by_id = {step["step_id"]: step for step in actual_steps}
    expected_ids = {step["step_id"] for step in expected_steps}
    for step in expected_steps:
        sid = step["step_id"]
        actual = actual_by_id.get(sid)
        if actual is None:
            differences.append(_diff("STEP_MISSING", "计划步骤未出现在台账",
                                     step_id=sid, expected="succeeded",
                                     actual="missing"))
            continue
        if actual["status"] != "succeeded":
            differences.append(_diff("STEP_NOT_SUCCEEDED", "步骤未成功",
                                     step_id=sid, expected="succeeded",
                                     actual=actual["status"]))
        if actual["action"] != step["action"]:
            differences.append(_diff("STEP_ACTION_MISMATCH", "动作与计划不一致",
                                     step_id=sid, expected=step["action"],
                                     actual=actual["action"]))
        if actual["target_id"] != step["target_id"]:
            differences.append(_diff("STEP_TARGET_MISMATCH", "目标与计划不一致",
                                     step_id=sid, expected=step["target_id"],
                                     actual=actual["target_id"]))
        artifact = _artifact_for(ledger, run["run_id"], sid)
        if artifact is None:
            differences.append(_diff("ARTIFACT_MISSING", "步骤产物缺失",
                                     step_id=sid, expected="state_after",
                                     actual="missing"))
            continue
        artifact_path = paths.root / artifact["path"]
        if not artifact_path.exists():
            differences.append(_diff("ARTIFACT_FILE_MISSING", "步骤产物文件不存在",
                                     step_id=sid, expected=artifact["path"],
                                     actual="missing"))
            continue
        digest = sha256_file(artifact_path)
        if digest != artifact["sha256"]:
            differences.append(_diff("ARTIFACT_HASH_MISMATCH", "步骤产物哈希不一致",
                                     step_id=sid, expected=artifact["sha256"],
                                     actual=digest))
            continue
        artifact_body = read_json(artifact_path)
        after_state = artifact_body.get("state_after", {})
        after_digest = state_hash(after_state)
        if after_digest != actual["after_hash"]:
            differences.append(_diff(
                "STEP_AFTER_HASH_MISMATCH", "台账后置状态哈希与产物不一致",
                step_id=sid, expected=actual["after_hash"], actual=after_digest))
        if artifact_body.get("after_hash") != actual["after_hash"]:
            differences.append(_diff(
                "ARTIFACT_AFTER_HASH_MISMATCH", "产物记录的后置哈希不一致",
                step_id=sid, expected=actual["after_hash"],
                actual=artifact_body.get("after_hash")))
        conditions = step.get("expected_postconditions", [])
        condition_diffs = check_conditions(after_state, conditions)
        for item in condition_diffs:
            differences.append(_diff(
                "POSTCONDITION_FAILED", "步骤后置断言未满足", step_id=sid,
                field=item["path"], expected=item["expected"],
                actual=item["actual"]))
    for actual in actual_steps:
        if actual["step_id"] not in expected_ids:
            differences.append(_diff("UNEXPECTED_STEP", "台账中存在计划外步骤",
                                     step_id=actual["step_id"],
                                     expected="absent", actual=actual["action"]))
    return differences, {
        "expected_steps": len(expected_steps),
        "ledger_steps": len(actual_steps),
        "succeeded_steps": sum(1 for s in actual_steps if s["status"] == "succeeded"),
    }


def _metric_breaks(registry: Registry | None, state: dict[str, Any]) -> list[dict[str, Any]]:
    if registry is None:
        return []
    issues = []
    for metric in registry.metrics:
        if not metric.required:
            continue
        node = state.get("metrics", {}).get(metric.id)
        if node is None:
            continue
        direct_sources = [ref for ref in node["source_refs"]
                          if ref in state["sources"]]
        if not direct_sources:
            continue
        available = set()
        for source_id in direct_sources:
            available.update(state["sources"][source_id].get("fields", []))
        missing = sorted(set(metric.required_fields) - available)
        if missing:
            issues.append({"kind": "required_metric_broken",
                           "owner_type": "metric", "owner_id": metric.id,
                           "missing_fields": missing})
    return issues


def _final_checks(paths: WorkPaths, registry: Registry | None,
                  run: dict[str, Any], final_hash: str) -> tuple[list[dict[str, Any]],
                                                                  dict[str, Any]]:
    differences = []
    state_path = paths.runs / run["run_id"] / "state.json"
    checks: dict[str, Any] = {"state_path": str(state_path)}
    if not state_path.exists():
        differences.append(_diff("STATE_FILE_MISSING", "最终状态文件不存在",
                                 expected="state.json", actual="missing"))
        return differences, checks
    state = read_json(state_path)
    actual_digest = state_hash(state)
    checks["state_hash"] = actual_digest
    if actual_digest != final_hash:
        differences.append(_diff("FINAL_HASH_MISMATCH", "最终状态哈希不一致",
                                 expected=final_hash, actual=actual_digest))
    for issue in consistency_issues(state):
        differences.append(_diff("STATE_INCONSISTENT", "最终状态一致性检查失败",
                                 field=issue.get("field", ""),
                                 expected="known, active reference",
                                 actual=issue))
    for issue in _metric_breaks(registry, state):
        differences.append(_diff("METRIC_BROKEN", "必需口径字段断裂",
                                 expected="required fields available",
                                 actual=issue))
    checks["source_statuses"] = {sid: node["status"]
                                 for sid, node in sorted(state["sources"].items())}
    return differences, checks


def verify_run(run_id: str, paths: WorkPaths,
               registry: Registry | None = None,
               plan: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger = Ledger(paths.ledger)
    run = ledger.get_run(run_id)
    if run is None:
        raise SwapDeskError("台账中不存在该运行", code=ErrorCode.RUN_NOT_FOUND,
                            record_id=run_id)
    run_dir = paths.runs / run_id
    if run["kind"] == "rollback":
        plan_path = run_dir / "rollback_plan.json"
        if plan is not None:
            expected_steps = plan.get("steps", [])
        elif plan_path.exists():
            expected_steps = read_json(plan_path).get("steps", [])
        else:
            expected_steps = []
        expected_final = ""
        if plan_path.exists():
            expected_final = read_json(plan_path).get("expected_initial_hash", "")
    else:
        if plan is None:
            plan_path = run_dir / "plan.json"
            plan = read_json(plan_path) if plan_path.exists() else {"steps": []}
        expected_steps = plan.get("steps", [])
        expected_final = run["final_hash"]
    differences, step_checks = _check_steps(paths, ledger, run, expected_steps)
    final_diffs, final_checks = _final_checks(paths, registry, run, run["final_hash"])
    differences.extend(final_diffs)
    state_path = paths.runs / run_id / "state.json"
    if run["kind"] == "execute" and plan is not None and state_path.exists():
        state = read_json(state_path)
        for source_id in sorted(plan.get("retired_sources", [])):
            status = state.get("sources", {}).get(source_id, {}).get("status")
            if status != "retired":
                differences.append(_diff(
                    "SOURCE_NOT_RETIRED", "计划要求退役的源状态不正确",
                    field=f"sources.{source_id}.status", expected="retired",
                    actual=status))
    if run["status"] != "complete":
        differences.append(_diff("RUN_NOT_COMPLETE", "运行未完成",
                                 expected="complete", actual=run["status"]))
    if run["kind"] == "rollback":
        restored = bool(expected_final) and run["final_hash"] == expected_final
        final_checks["restored"] = restored
        final_checks["expected_initial_hash"] = expected_final
        if not restored:
            differences.append(_diff(
                "ROLLBACK_NOT_RESTORED", "回滚未回到执行前状态",
                expected=expected_final, actual=run["final_hash"]))
    if plan is not None and run["kind"] == "execute":
        if run["plan_hash"] != plan.get("plan_hash"):
            differences.append(_diff("PLAN_HASH_MISMATCH", "计划哈希与台账不一致",
                                     expected=run["plan_hash"],
                                     actual=plan.get("plan_hash")))
        if run["plan_id"] != plan.get("plan_id"):
            differences.append(_diff("PLAN_ID_MISMATCH", "计划 ID 与台账不一致",
                                     expected=run["plan_id"],
                                     actual=plan.get("plan_id")))
    differences.sort(key=lambda d: (d["code"], d.get("step_id", ""), d.get("field", "")))
    verdict = "PASS" if not differences else "FAIL"
    return {
        "verdict": verdict,
        "run_id": run_id,
        "kind": run["kind"],
        "plan_id": run["plan_id"],
        "differences": differences,
        "checks": {**step_checks, **final_checks,
                   "ledger_snapshot_hash": sha256_json(ledger.snapshot(run_id))},
    }
