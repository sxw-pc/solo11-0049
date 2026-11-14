"""Human-readable Markdown report rendering."""
from __future__ import annotations

from typing import Any

from ..io.jsonout import atomic_write_text


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    return lines


def _path(path: list[str]) -> str:
    return " → ".join(path)


def _impact_lines(analysis: dict[str, Any]) -> list[str]:
    affected = analysis["impact"]["affected"]
    counts = analysis["impact"]["counts"]
    lines = ["## 影响面", "",
             f"待下线源：{', '.join(analysis['impact']['retired_sources'])}；"
             f"任务 {counts['tasks']}、下游 {counts['downstream']}、"
             f"口径 {counts['metrics']} 受影响。", ""]
    rows = [[item["id"], "task", item["impact"], item["depth"], _path(item["path"])]
            for item in affected["tasks"]]
    rows += [[item["id"], "downstream", item["impact"], item["depth"],
              _path(item["path"])] for item in affected["downstream"]]
    rows += [[item["id"], "metric", item["impact"], item["depth"],
              _path(item["path"])] for item in affected["metrics"]]
    lines += _table(["对象", "类型", "影响", "深度", "路径"], rows)
    warnings = analysis["impact"].get("warnings", [])
    if warnings:
        lines += ["", "### 扩散告警", ""]
        for warning in warnings:
            lines.append(f"- `{warning['rule_id']}`：{warning['message']}；"
                         f"环 `{_path(warning['evidence']['nodes'])}`")
    return lines


def _conflict_lines(analysis: dict[str, Any]) -> list[str]:
    lines = ["", "## 冲突与阻断", ""]
    conflicts = analysis["conflicts"]
    if not conflicts:
        lines.append("未发现冲突。")
        return lines
    rows = [[c["rule_id"], c["severity"], c["target"]["type"],
             c["target"]["id"], c["message"]] for c in conflicts]
    lines += _table(["规则", "分级", "对象类型", "对象", "说明"], rows)
    summary = analysis["summary"]
    lines += ["", f"可执行：`{str(summary['executable']).lower()}`；"
                  f"blocking {summary['counts']['blocking']}，"
                  f"warning {summary['counts']['warning']}，"
                  f"info {summary['counts']['info']}。"]
    return lines


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    lines = ["", "## 迁移计划", "",
             f"计划 `{plan['plan_id']}`，批次 {len(plan['batches'])}，"
             f"步骤 {len(plan['steps'])}，并发上限 {plan['max_parallel']}。", ""]
    rows = [[s["step_id"], s["batch"], s["sub_batch"], s["action"],
             f"{s['target_type']}:{s['target_id']}", len(s["depends_on"])]
            for s in plan["steps"]]
    lines += _table(["步骤", "批次", "子批", "动作", "目标", "前置数"], rows)
    return lines


def _execution_lines(execution: dict[str, Any]) -> list[str]:
    lines = ["", "## 执行摘要", "",
             f"运行 `{execution['run_id']}` 状态 `{execution['status']}`；"
             f"成功 {execution['succeeded_steps']}/{execution['total_steps']} 步。"]
    rows = [[s["step_id"], s["status"], s["attempts"], s["action"],
             f"{s['target_type']}:{s['target_id']}"] for s in execution["steps"]]
    lines += [""] + _table(["步骤", "状态", "尝试", "动作", "目标"], rows)
    return lines


def _verify_lines(verification: dict[str, Any]) -> list[str]:
    lines = ["", "## 复核", "", f"结论：**{verification['verdict']}**。"]
    differences = verification.get("differences", [])
    if differences:
        lines += [""]
        rows = [[d["code"], d.get("step_id", ""), d.get("field", ""),
                 d.get("expected", ""), d.get("actual", "")] for d in differences]
        lines += _table(["代码", "步骤", "字段", "期望", "实际"], rows)
    else:
        lines.append("计划声明、台账记录与最终状态逐项一致。")
    return lines


def render_markdown(analysis: dict[str, Any], plan: dict[str, Any] | None = None,
                    execution: dict[str, Any] | None = None,
                    verification: dict[str, Any] | None = None) -> str:
    request = analysis["request"]
    lines = ["# 数据源撤换与口径迁移报告", "",
             f"- 请求：`{request['request_id']}` {request['title']}",
             f"- 窗口：{request['window']['start']} 至 {request['window']['end']}",
             f"- 分析指纹：`{analysis['analysis_hash']}`"]
    if plan:
        lines.append(f"- 计划指纹：`{plan['plan_hash']}`")
    if execution:
        lines.append(f"- 最终状态指纹：`{execution['final_state_hash']}`")
    lines += [""] + _impact_lines(analysis) + _conflict_lines(analysis)
    if plan:
        lines += _plan_lines(plan)
    if execution:
        lines += _execution_lines(execution)
    if verification:
        lines += _verify_lines(verification)
    lines += ["", "---", "",
              "本报告由 swapdesk 在本地生成；全部登记数据为合成占位数据，"
              "不包含真实凭据或外部资源引用。", ""]
    return "\n".join(lines)


def write_markdown(path: str, **kwargs: Any) -> str:
    content = render_markdown(**kwargs)
    atomic_write_text(path, content)
    return path
