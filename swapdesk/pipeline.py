"""Command orchestration shared by the CLI and tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .analysis import run_analysis
from .config import (DEMO_TIMESTAMP, WorkPaths, demo_workdir, resolve_timestamp,
                     work_paths)
from .errors import ErrorCode, Issue, SchemaError, SwapDeskError
from .execute.ledger import Ledger
from .execute.runner import execute_plan, execute_rollback
from .execute.verify import verify_run
from .explain import explain_run, write_explanation
from .fixtures.generator import write_fixtures
from .io.jsonout import read_json, write_json
from .io.yamlload import find_request, load_registry, load_request
from .model.entities import Registry
from .plan.planner import build_plan, validate_plan
from .plan.rollback import build_rollback_script
from .report.html import write_html
from .report.markdown import write_markdown


def load_inputs(data_dir: str | Path, request_file: str | Path | None
                ) -> tuple[Registry, Any, dict[str, Any], Path]:
    registry = load_registry(data_dir)
    request_path = find_request(data_dir, request_file)
    request = load_request(request_path)
    unknown = sorted(set(request.retired_sources) - set(registry.source_map))
    if unknown:
        raise SchemaError([Issue(
            ErrorCode.REF_UNKNOWN_SOURCE,
            f"撤换请求引用了未登记数据源 {unknown[0]}",
            file=request_path.name, record_id=request.request_id,
            field="retire", details={"ref": unknown[0]},
        )])
    analysis = run_analysis(registry, request)
    return registry, request, analysis, request_path


def _write_analysis(paths: WorkPaths, analysis: dict[str, Any]) -> Path:
    path = paths.analysis / f"{analysis['request']['request_id']}.json"
    return write_json(path, analysis)


def _write_plan_artifacts(paths: WorkPaths, plan: dict[str, Any],
                          registry: Registry) -> Path:
    path = paths.plans / f"{plan['plan_id']}.json"
    write_json(path, plan)
    write_json(paths.plans / f"{plan['plan_id']}.initial.json",
               registry.initial_state())
    return path


def write_reports(paths: WorkPaths, name: str, analysis: dict[str, Any],
                  plan: dict[str, Any] | None = None,
                  execution: dict[str, Any] | None = None,
                  verification: dict[str, Any] | None = None) -> dict[str, str]:
    md_path = paths.reports / f"{name}.md"
    html_path = paths.reports / f"{name}.html"
    kwargs = {"analysis": analysis, "plan": plan, "execution": execution,
              "verification": verification}
    write_markdown(str(md_path), **kwargs)
    write_html(str(html_path), **kwargs)
    return {"markdown": str(md_path), "html": str(html_path)}


def analyze_command(workdir: str | Path | None, data_dir: str | Path,
                    request_file: str | Path | None) -> dict[str, Any]:
    paths = work_paths(workdir)
    _, request, analysis, request_path = load_inputs(data_dir, request_file)
    analysis_path = _write_analysis(paths, analysis)
    name = f"analysis-{request.request_id}"
    reports = write_reports(paths, name, analysis)
    return {"request": request, "analysis": analysis, "paths": paths,
            "analysis_path": str(analysis_path), "request_path": str(request_path),
            "reports": reports}


def plan_command(workdir: str | Path | None, data_dir: str | Path,
                 request_file: str | Path | None) -> dict[str, Any]:
    paths = work_paths(workdir)
    registry, request, analysis, request_path = load_inputs(data_dir, request_file)
    plan = build_plan(registry, request, analysis)
    analysis_path = _write_analysis(paths, analysis)
    plan_path = _write_plan_artifacts(paths, plan, registry)
    reports = write_reports(paths, plan["plan_id"], analysis, plan=plan)
    return {"request": request, "analysis": analysis, "plan": plan,
            "paths": paths, "analysis_path": str(analysis_path),
            "request_path": str(request_path), "plan_path": str(plan_path),
            "reports": reports}


def execute_command(workdir: str | Path | None, data_dir: str | Path,
                    request_file: str | Path | None, *,
                    stop_after: int | None = None, resume: bool = False,
                    plan_file: str | Path | None = None,
                    timestamp: str | None = None) -> dict[str, Any]:
    if stop_after is not None and stop_after < 0:
        raise SwapDeskError("--stop-after 不能为负数",
                            code=ErrorCode.PLAN_INVALID, field="stop_after")
    paths = work_paths(workdir)
    registry, request, analysis, request_path = load_inputs(data_dir, request_file)
    plan = build_plan(registry, request, analysis)
    if plan_file is not None:
        supplied = read_json(plan_file)
        validate_plan(supplied)
        if supplied.get("plan_hash") != plan["plan_hash"]:
            raise SwapDeskError("提供的计划与当前登记表不匹配",
                                code=ErrorCode.PLAN_INVALID,
                                field="plan_hash",
                                details={"expected": plan["plan_hash"],
                                         "actual": supplied.get("plan_hash")})
    analysis_path = _write_analysis(paths, analysis)
    _write_plan_artifacts(paths, plan, registry)
    stamp = resolve_timestamp(timestamp)
    execution = execute_plan(plan, registry.initial_state(), paths, stamp,
                             stop_after=stop_after, resume=resume)
    verification = None
    if execution["status"] == "complete":
        verification = verify_run(execution["run_id"], paths, registry, plan)
    run_dir = paths.runs / execution["run_id"]
    write_json(run_dir / "analysis.json", analysis)
    reports = write_reports(paths, execution["run_id"], analysis, plan,
                            execution, verification)
    return {"request": request, "analysis": analysis, "plan": plan,
            "execution": execution, "verification": verification,
            "paths": paths, "request_path": str(request_path),
            "analysis_path": str(analysis_path), "reports": reports}


def rollback_command(workdir: str | Path | None, run_id: str, *,
                     resume: bool = False, timestamp: str | None = None,
                     data_dir: str | Path | None = None) -> dict[str, Any]:
    paths = work_paths(workdir)
    ledger = Ledger(paths.ledger)
    run = ledger.get_run(run_id)
    if run is None:
        raise SwapDeskError("没有可回滚的运行", code=ErrorCode.ROLLBACK_UNAVAILABLE,
                            record_id=run_id)
    if run["kind"] != "execute":
        raise SwapDeskError("只能回滚 execute 类型的运行",
                            code=ErrorCode.ROLLBACK_UNAVAILABLE,
                            record_id=run_id)
    run_dir = paths.runs / run_id
    plan_path = run_dir / "plan.json"
    if not plan_path.exists():
        raise SwapDeskError("运行缺少计划产物，无法回滚",
                            code=ErrorCode.ROLLBACK_UNAVAILABLE,
                            record_id=run_id)
    plan = read_json(plan_path)
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise SwapDeskError("运行缺少状态产物，无法回滚",
                            code=ErrorCode.ROLLBACK_UNAVAILABLE,
                            record_id=run_id)
    current_state = read_json(state_path)
    plan_for_script = dict(plan)
    plan_for_script["_run_id"] = run_id
    script = build_rollback_script(plan_for_script,
                                   ledger.steps_for_run(run_id), run["initial_hash"])
    write_json(paths.plans / f"{script['rollback_id']}.json", script)
    stamp = resolve_timestamp(timestamp)
    execution = execute_rollback(script, plan_for_script, current_state, paths,
                                 stamp, resume=resume)
    registry = None
    if data_dir is not None and Path(data_dir).exists():
        try:
            registry = load_registry(data_dir)
        except SwapDeskError:
            registry = None
    verification = verify_run(execution["run_id"], paths, registry)
    write_json(paths.runs / execution["run_id"] / "analysis.json",
               {"run_id": execution["run_id"], "source_run_id": run_id,
                "kind": "rollback"})
    return {"rollback_script": script, "execution": execution,
            "verification": verification, "paths": paths, "run": run}


def verify_command(workdir: str | Path | None, run_id: str,
                   data_dir: str | Path | None = None) -> dict[str, Any]:
    paths = work_paths(workdir)
    registry = None
    if data_dir is not None and Path(data_dir).exists():
        try:
            registry = load_registry(data_dir)
        except SwapDeskError:
            registry = None
    return {"verification": verify_run(run_id, paths, registry),
            "paths": paths}


def explain_command(workdir: str | Path | None, run_id: str, *,
                    json_out: str | Path | None = None,
                    markdown_out: str | Path | None = None
                    ) -> dict[str, Any]:
    """Explain a completed run from persisted ledger + artifacts only."""
    paths = work_paths(workdir)
    explanation = explain_run(run_id, paths)
    outputs = write_explanation(explanation, paths, json_path=json_out,
                                markdown_path=markdown_out)
    return {"explanation": explanation, "paths": paths, "outputs": outputs}


def demo_command(workdir: str | Path | None = None, seed: int = 11
                 ) -> dict[str, Any]:
    root = demo_workdir(workdir)
    paths = work_paths(root)
    write_fixtures(paths.fixtures, seed)
    registry, request, analysis, request_path = load_inputs(
        paths.fixtures, "req-001.yaml")
    plan = build_plan(registry, request, analysis)
    analysis_path = _write_analysis(paths, analysis)
    _write_plan_artifacts(paths, plan, registry)
    execution = execute_plan(plan, registry.initial_state(), paths,
                             DEMO_TIMESTAMP, resume=True)
    verification = verify_run(execution["run_id"], paths, registry, plan)
    run_dir = paths.runs / execution["run_id"]
    write_json(run_dir / "analysis.json", analysis)
    write_json(run_dir / "demo_summary.json", {
        "request_id": request.request_id,
        "impact_counts": analysis["impact"]["counts"],
        "conflict_counts": analysis["summary"]["counts"],
        "step_count": len(plan["steps"]),
        "batch_count": len(plan["batches"]),
        "verdict": verification["verdict"],
    })
    reports = write_reports(paths, execution["run_id"], analysis, plan,
                            execution, verification)
    return {"request": request, "analysis": analysis, "plan": plan,
            "execution": execution, "verification": verification,
            "paths": paths, "request_path": str(request_path),
            "analysis_path": str(analysis_path), "reports": reports}
