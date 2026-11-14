"""Command line interface for swapdesk."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .config import DEFAULT_PORT, VERSION
from .errors import ErrorCode, SwapDeskError
from . import pipeline
from .report.server import serve


def _common(parser: argparse.ArgumentParser, *, data: bool = True) -> None:
    parser.add_argument("--workdir", default="var",
                        help="输出目录，默认 ./var")
    if data:
        parser.add_argument("--data-dir", default="data",
                            help="登记表目录，默认 ./data")
        parser.add_argument("--request", default="req-001.yaml",
                            help="撤换请求文件名或路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swapdesk",
        description="数据源撤换与口径迁移台账（离线、确定性）")
    parser.add_argument("--version", action="version", version=f"swapdesk {VERSION}")
    subs = parser.add_subparsers(dest="command", required=True)

    demo = subs.add_parser("demo", help="生成合成数据并跑通全流程")
    demo.add_argument("--workdir", default="var")
    demo.add_argument("--seed", type=int, default=11)

    analyze = subs.add_parser("analyze", help="加载登记表并输出影响面")
    _common(analyze)

    plan = subs.add_parser("plan", help="生成迁移计划；blocking 时拒绝")
    _common(plan)

    execute = subs.add_parser("execute", help="本地模拟执行计划")
    _common(execute)
    execute.add_argument("--plan", dest="plan_file", default=None,
                         help="可选计划 JSON；必须与登记表一致")
    execute.add_argument("--stop-after", type=int, default=None,
                         help="本次调用最多推进的步骤数")
    execute.add_argument("--resume", action="store_true",
                         help="从中断处按台账续跑")
    execute.add_argument("--timestamp", default=None,
                         help="固定执行时间戳（ISO-8601）")

    rollback = subs.add_parser("rollback", help="按相反顺序回滚已执行批次")
    rollback.add_argument("--workdir", default="var")
    rollback.add_argument("--run-id", required=True)
    rollback.add_argument("--data-dir", default="data")
    rollback.add_argument("--resume", action="store_true")
    rollback.add_argument("--timestamp", default=None)

    verify = subs.add_parser("verify", help="核验计划、台账与最终状态")
    verify.add_argument("--workdir", default="var")
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--data-dir", default="data")

    explain = subs.add_parser(
        "explain", help="仅从台账与产物复盘一次执行或回滚")
    explain.add_argument("--workdir", default="var")
    explain.add_argument("--run-id", required=True)
    explain.add_argument("--json-out", dest="json_out", default=None,
                         help="复盘 JSON 路径；默认 runs/<run-id>/explain.json")
    explain.add_argument("--markdown-out", dest="markdown_out", default=None,
                         help="复盘 Markdown 路径；默认 reports/explain-<run-id>.md")

    server = subs.add_parser("serve", help="启动 127.0.0.1 只读报告服务")
    server.add_argument("--workdir", default="var")
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def _print_error(exc: SwapDeskError) -> None:
    payload = exc.to_dict()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
          file=sys.stderr)


def _print_demo(result: dict[str, Any]) -> None:
    analysis = result["analysis"]
    execution = result["execution"]
    verification = result["verification"]
    plan = result["plan"]
    counts = analysis["impact"]["counts"]
    conflicts = analysis["summary"]["counts"]
    reports = result["reports"]
    print(f"[demo] workspace={result['paths'].root}")
    print(f"[request] {analysis['request']['request_id']} "
          f"{analysis['request']['title']}")
    print(f"[impact] tasks={counts['tasks']} downstream={counts['downstream']} "
          f"metrics={counts['metrics']}")
    print(f"[conflicts] blocking={conflicts['blocking']} "
          f"warning={conflicts['warning']} info={conflicts['info']} "
          f"executable={analysis['summary']['executable']}")
    print(f"[plan] plan_id={plan['plan_id']} steps={len(plan['steps'])} "
          f"batches={len(plan['batches'])} max_parallel={plan['max_parallel']}")
    print(f"[execute] run_id={execution['run_id']} status={execution['status']} "
          f"succeeded={execution['succeeded_steps']}/{execution['total_steps']}")
    print(f"[verify] {verification['verdict']}")
    print(f"[reports] markdown={reports['markdown']}")
    print(f"[reports] html={reports['html']}")


def _print_command(result: dict[str, Any], command: str) -> None:
    analysis = result["analysis"]
    counts = analysis["impact"]["counts"]
    conflicts = analysis["summary"]["counts"]
    print(f"[{command}] request={analysis['request']['request_id']} "
          f"tasks={counts['tasks']} downstream={counts['downstream']} "
          f"metrics={counts['metrics']}")
    print(f"[{command}] conflicts blocking={conflicts['blocking']} "
          f"warning={conflicts['warning']} info={conflicts['info']} "
          f"executable={analysis['summary']['executable']}")
    if "plan" in result:
        print(f"[{command}] plan_id={result['plan']['plan_id']} "
              f"steps={len(result['plan']['steps'])}")
    if "execution" in result:
        execution = result["execution"]
        print(f"[{command}] run_id={execution['run_id']} "
              f"status={execution['status']} "
              f"succeeded={execution['succeeded_steps']}/{execution['total_steps']}")
    if result.get("verification"):
        print(f"[{command}] verify={result['verification']['verdict']}")
    print(f"[{command}] analysis={result.get('analysis_path', '')}")


def _run(args: argparse.Namespace) -> int:
    if args.command == "demo":
        result = pipeline.demo_command(args.workdir, args.seed)
        _print_demo(result)
        return 0 if result["verification"]["verdict"] == "PASS" else 4
    if args.command == "analyze":
        result = pipeline.analyze_command(args.workdir, args.data_dir, args.request)
        _print_command(result, "analyze")
        return 0
    if args.command == "plan":
        result = pipeline.plan_command(args.workdir, args.data_dir, args.request)
        _print_command(result, "plan")
        return 0
    if args.command == "execute":
        result = pipeline.execute_command(
            args.workdir, args.data_dir, args.request,
            stop_after=args.stop_after, resume=args.resume,
            plan_file=args.plan_file, timestamp=args.timestamp)
        _print_command(result, "execute")
        if result["verification"] is not None:
            return 0 if result["verification"]["verdict"] == "PASS" else 4
        return 0
    if args.command == "rollback":
        result = pipeline.rollback_command(
            args.workdir, args.run_id, resume=args.resume,
            timestamp=args.timestamp, data_dir=args.data_dir)
        execution = result["execution"]
        verification = result["verification"]
        print(f"[rollback] run_id={execution['run_id']} "
              f"status={execution['status']} "
              f"restored={execution.get('restored', False)}")
        print(f"[rollback] verify={verification['verdict']}")
        return 0 if verification["verdict"] == "PASS" else 4
    if args.command == "verify":
        result = pipeline.verify_command(args.workdir, args.run_id, args.data_dir)
        verification = result["verification"]
        print(json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if verification["verdict"] == "PASS" else 4
    if args.command == "explain":
        result = pipeline.explain_command(
            args.workdir, args.run_id, json_out=args.json_out,
            markdown_out=args.markdown_out)
        explanation = result["explanation"]
        summary = explanation["summary"]
        print(f"[explain] run_id={explanation['run']['run_id']} "
              f"steps={summary['total_steps']} "
              f"empty_impact={summary['empty_impact_count']} "
              f"returned_to_initial={str(summary['returned_to_initial_hash']).lower()} "
              f"blocking_resolved={summary['blocking_conflict_resolved_count']} "
              f"evidence_ok={str(summary['evidence_ok']).lower()}")
        print(f"[explain] json={result['outputs']['json']}")
        print(f"[explain] markdown={result['outputs']['markdown']}")
        return 0
    if args.command == "serve":
        serve(args.workdir, args.port)
        return 0
    raise SwapDeskError("未知命令", code=ErrorCode.INTERNAL)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except SwapDeskError as exc:
        _print_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        if os.environ.get("SWAPDESK_DEBUG"):
            raise
        error = SwapDeskError(f"{type(exc).__name__}: {exc}",
                              code=ErrorCode.INTERNAL)
        _print_error(error)
        return 2
