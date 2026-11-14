"""Deterministic local execution of migration and rollback plans."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import WorkPaths
from ..errors import ErrorCode, SwapDeskError
from ..io.jsonout import sha256_file, write_json
from .ledger import Ledger
from .state import apply_step, check_conditions, state_hash


def run_id_for_plan(plan: dict[str, Any]) -> str:
    return f"run-{plan['plan_hash'][:16]}"


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _resolve(path_text: str, paths: WorkPaths) -> Path:
    return paths.root / path_text


def _summary(run: dict[str, Any], steps: list[dict[str, Any]],
             paths: WorkPaths, expected_hash: str = "",
             final_hash: str = "", planned_total: int | None = None
             ) -> dict[str, Any]:
    succeeded = [s for s in steps if s["status"] == "succeeded"]
    total = planned_total if planned_total is not None else len(steps)
    summary = {
        "run_id": run["run_id"],
        "kind": run["kind"],
        "request_id": run["request_id"],
        "plan_id": run["plan_id"],
        "plan_hash": run["plan_hash"],
        "status": run["status"],
        "initial_state_hash": run["initial_hash"],
        "final_state_hash": run["final_hash"],
        "total_steps": total,
        "succeeded_steps": len(succeeded),
        "pending_steps": max(0, total - len(succeeded)),
        "state_path": _relative(paths.runs / run["run_id"] / "state.json", paths.root),
        "ledger_path": _relative(paths.ledger, paths.root),
        "steps": steps,
    }
    if expected_hash:
        summary["expected_restored_hash"] = expected_hash
        summary["restored"] = bool(final_hash) and final_hash == expected_hash
        if not summary["restored"]:
            summary["restore_reason"] = (
                "回滚执行后状态哈希与执行前不一致，可能存在计划外状态变更"
                if run["status"] == "complete"
                else "回滚尚未完成"
            )
    return summary


def _execute(run_id: str, kind: str, request_id: str, plan_id: str,
             plan_hash: str, steps: list[dict[str, Any]],
             initial_state: dict[str, Any], paths: WorkPaths, timestamp: str,
             *, stop_after: int | None = None, resume: bool = False,
             parent_run_id: str = "", expected_final_hash: str = "",
             script: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = paths.ensure()
    ledger = Ledger(paths.ledger)
    run_dir = paths.runs / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    initial_hash = state_hash(initial_state)
    existing = ledger.get_run(run_id)
    created = ledger.start_run(
        run_id=run_id, kind=kind, request_id=request_id, plan_id=plan_id,
        plan_hash=plan_hash, initial_hash=initial_hash, timestamp=timestamp,
        parent_run_id=parent_run_id,
    )
    if existing and existing["status"] == "complete":
        ledger_steps = ledger.steps_for_run(run_id)
        return _summary(ledger.get_run(run_id) or existing, ledger_steps, paths,
                        expected_final_hash,
                        (ledger.get_run(run_id) or existing)["final_hash"],
                        planned_total=len(steps))
    if existing and not resume:
        raise SwapDeskError(
            "运行未完成，续跑需要显式 --resume",
            code=ErrorCode.RUN_INCOMPLETE, record_id=run_id,
            details={"status": existing["status"]},
        )
    if created:
        write_json(run_dir / "state.json", initial_state)
        write_json(run_dir / "state_before.json", initial_state)
    else:
        ledger.update_run(run_id, timestamp=timestamp, status="running")
    if kind == "rollback" and script is not None:
        write_json(run_dir / "rollback_plan.json", script)
    state_path = run_dir / "state.json"
    if not state_path.exists():
        write_json(state_path, initial_state)
    from ..io.jsonout import read_json
    state = read_json(state_path) if existing else deepcopy(initial_state)
    executed_now = 0
    stopped = False
    for ordinal, step in enumerate(steps, start=1):
        prior = ledger.get_step(run_id, step["step_id"])
        if prior and prior["status"] == "succeeded":
            continue
        if stop_after is not None and executed_now >= stop_after:
            stopped = True
            break
        for dep in step.get("depends_on", []):
            dep_row = ledger.get_step(run_id, dep)
            if not dep_row or dep_row["status"] != "succeeded":
                raise SwapDeskError("前置步骤尚未成功", code=ErrorCode.STEP_FAILED,
                                    record_id=step["step_id"],
                                    details={"dependency": dep})
        before_hash = state_hash(state)
        ledger.begin_step(run_id, step, ordinal, timestamp)
        candidate = deepcopy(state)
        try:
            apply_step(candidate, step)
            differences = check_conditions(candidate, step.get("expected_postconditions", []))
            if differences:
                raise SwapDeskError("步骤后置断言失败", code=ErrorCode.STEP_FAILED,
                                    record_id=step["step_id"],
                                    details={"differences": differences})
            after_hash = state_hash(candidate)
            artifact = {
                "run_id": run_id, "step_id": step["step_id"],
                "action": step["action"],
                "target": {"type": step["target_type"], "id": step["target_id"]},
                "before_hash": before_hash, "after_hash": after_hash,
                "payload": step.get("payload", {}),
                "state_after": candidate,
            }
            artifact_path = run_dir / "steps" / f"{step['step_id']}.json"
            write_json(artifact_path, artifact)
            rel_path = _relative(artifact_path, paths.root)
            ledger.finish_step(run_id, step["step_id"], before_hash=before_hash,
                               after_hash=after_hash, artifact_path=rel_path,
                               timestamp=timestamp)
            ledger.record_artifact(run_id, step["step_id"], "state_after",
                                   rel_path, sha256_file(artifact_path), timestamp)
            state = candidate
            write_json(state_path, state)
            executed_now += 1
        except SwapDeskError as exc:
            ledger.fail_step(run_id, step["step_id"], error_code=exc.code,
                             error_message=exc.message, before_hash=before_hash,
                             timestamp=timestamp)
            ledger.update_run(run_id, timestamp=timestamp, status="failed",
                              final_hash=state_hash(state))
            raise
    run = ledger.get_run(run_id)
    if run is None:
        raise SwapDeskError("台账中不存在该运行", code=ErrorCode.RUN_NOT_FOUND,
                            record_id=run_id)
    if stopped:
        ledger.update_run(run_id, timestamp=timestamp, status="stopped",
                          final_hash=state_hash(state))
    else:
        ledger.update_run(run_id, timestamp=timestamp, status="complete",
                          final_hash=state_hash(state))
    final_run = ledger.get_run(run_id) or run
    ledger_steps = ledger.steps_for_run(run_id)
    summary = _summary(final_run, ledger_steps, paths, expected_final_hash,
                       final_run["final_hash"], planned_total=len(steps))
    summary["executed_now"] = executed_now
    write_json(run_dir / "execution.json", summary)
    return summary


def execute_plan(plan: dict[str, Any], initial_state: dict[str, Any],
                 paths: WorkPaths, timestamp: str, *, stop_after: int | None = None,
                 resume: bool = False) -> dict[str, Any]:
    run_dir = paths.runs / run_id_for_plan(plan)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "plan.json", plan)
    return _execute(
        run_id_for_plan(plan), "execute", plan["request_id"], plan["plan_id"],
        plan["plan_hash"], plan["steps"], initial_state, paths, timestamp,
        stop_after=stop_after, resume=resume,
    )


def execute_rollback(script: dict[str, Any], plan: dict[str, Any],
                     current_state: dict[str, Any], paths: WorkPaths,
                     timestamp: str, *, resume: bool = False) -> dict[str, Any]:
    source_run_id = script["source_run_id"]
    run_id = f"rb-{source_run_id}"
    plan_copy = dict(plan)
    plan_copy["_run_id"] = source_run_id
    if not script.get("steps"):
        from ..plan.rollback import build_rollback_script
        script = build_rollback_script(plan_copy, [], script["expected_initial_hash"])
    return _execute(
        run_id, "rollback", plan["request_id"], script["rollback_id"],
        script["rollback_hash"], script["steps"], current_state, paths, timestamp,
        resume=resume, parent_run_id=source_run_id,
        expected_final_hash=script["expected_initial_hash"], script=script,
    )
