"""Single-file HTML report with inline CSS and no external resources."""
from __future__ import annotations

import html
from typing import Any, Iterable

from ..io.jsonout import atomic_write_text

CSS = """
:root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1e242b; --muted:#5c6670;
  --card:#ffffff; --line:#d8dee4; --accent:#1f5f8b; --block:#a83a32;
  --warn:#946200; --info:#356680; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15191d; --fg:#e6e9ec; --muted:#a5afb8; --card:#1d2329;
    --line:#39434c; --accent:#8dc5e8; --block:#ff938a; --warn:#e5b75c;
    --info:#8fc5df; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; }
main { max-width:1100px; margin:0 auto; padding:32px 20px 56px; }
h1 { font-size:1.7rem; margin:0 0 4px; }
h2 { font-size:1.2rem; margin:2rem 0 .7rem; border-bottom:1px solid var(--line);
  padding-bottom:.35rem; }
h3 { font-size:1rem; margin:1rem 0 .4rem; }
p, li { color:var(--fg); }
code { background:rgba(127,127,127,.14); padding:.1em .35em; border-radius:4px;
  font-family:Consolas, monospace; font-size:.9em; }
.meta, .note { color:var(--muted); font-size:.9rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:12px; margin:18px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; }
.card .value { font-size:1.6rem; font-weight:600; color:var(--accent); }
.card .label { color:var(--muted); font-size:.85rem; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:8px; overflow:hidden;
  margin:8px 0 16px; font-size:.88rem; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { background:rgba(127,127,127,.09); font-weight:600; }
tr:last-child td { border-bottom:0; }
.sev-blocking { color:var(--block); font-weight:600; }
.sev-warning { color:var(--warn); font-weight:600; }
.sev-info { color:var(--info); }
.pass { color:#237a4b; font-weight:700; }
.fail { color:var(--block); font-weight:700; }
footer { margin-top:36px; padding-top:14px; border-top:1px solid var(--line);
  color:var(--muted); font-size:.8rem; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _path(path: Iterable[str]) -> str:
    return " → ".join(_e(item) for item in path)


def _table(headers: list[str], rows: list[list[Any]],
           classes: list[str] | None = None) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            cls = f' class="{classes[index]}"' if classes and classes[index] else ""
            cells.append(f"<td{cls}>{cell}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<table><thead><tr>{head}</tr></thead><tbody>"
            + "".join(body) + "</tbody></table>")


def _impact(analysis: dict[str, Any]) -> str:
    affected = analysis["impact"]["affected"]
    counts = analysis["impact"]["counts"]
    cards = "".join(
        f'<div class="card"><div class="value">{counts[key]}</div>'
        f'<div class="label">{label}</div></div>'
        for key, label in (("tasks", "受影响任务"), ("downstream", "受影响下游"),
                           ("metrics", "受影响口径"))
    )
    rows = [[_e(i["id"]), "task", _e(i["impact"]), _e(i["depth"]), _path(i["path"])]
            for i in affected["tasks"]]
    rows += [[_e(i["id"]), "downstream", _e(i["impact"]), _e(i["depth"]),
              _path(i["path"])] for i in affected["downstream"]]
    rows += [[_e(i["id"]), "metric", _e(i["impact"]), _e(i["depth"]),
              _path(i["path"])] for i in affected["metrics"]]
    parts = ["<h2>影响面</h2>", f'<div class="grid">{cards}</div>',
             _table(["对象", "类型", "影响", "深度", "路径"], rows)]
    warnings = analysis["impact"].get("warnings", [])
    if warnings:
        parts.append("<h3>扩散告警</h3><ul>")
        for warning in warnings:
            parts.append(f"<li><code>{_e(warning['rule_id'])}</code> "
                         f"{_e(warning['message'])}：{_path(warning['evidence']['nodes'])}</li>")
        parts.append("</ul>")
    return "".join(parts)


def _conflicts(analysis: dict[str, Any]) -> str:
    conflicts = analysis["conflicts"]
    if not conflicts:
        return "<h2>冲突与阻断</h2><p>未发现冲突。</p>"
    rows = [[f"<code>{_e(c['rule_id'])}</code>",
             f"<span class=\"sev-{_e(c['severity'])}\">{_e(c['severity'])}</span>",
             _e(c["target"]["type"]), _e(c["target"]["id"]),
             _e(c["message"])] for c in conflicts]
    summary = analysis["summary"]
    return ("<h2>冲突与阻断</h2>"
            + _table(["规则", "分级", "对象类型", "对象", "说明"], rows)
            + f'<p class="meta">可执行：{_e(summary["executable"])}；'
              f'blocking {summary["counts"]["blocking"]}，'
              f'warning {summary["counts"]["warning"]}，'
              f'info {summary["counts"]["info"]}。</p>')


def _plan(plan: dict[str, Any]) -> str:
    rows = [[_e(s["step_id"]), _e(s["batch"]), _e(s["sub_batch"]),
             _e(s["action"]), _e(f"{s['target_type']}:{s['target_id']}"),
             _e(len(s["depends_on"]))] for s in plan["steps"]]
    return ("<h2>迁移计划</h2>"
            f'<p class="meta">计划 <code>{_e(plan["plan_id"])}</code>，'
            f'批次 {len(plan["batches"])}，步骤 {len(plan["steps"])}，'
            f'并发上限 {_e(plan["max_parallel"])}。</p>'
            + _table(["步骤", "批次", "子批", "动作", "目标", "前置数"], rows))


def _execution(execution: dict[str, Any]) -> str:
    rows = [[_e(s["step_id"]), _e(s["status"]), _e(s["attempts"]),
             _e(s["action"]), _e(f"{s['target_type']}:{s['target_id']}")]
            for s in execution["steps"]]
    return ("<h2>执行摘要</h2>"
            f'<p>运行 <code>{_e(execution["run_id"])}</code>，状态 '
            f'<code>{_e(execution["status"])}</code>，成功 '
            f'{_e(execution["succeeded_steps"])}/{_e(execution["total_steps"])} 步。</p>'
            + _table(["步骤", "状态", "尝试", "动作", "目标"], rows))


def _verify(verification: dict[str, Any]) -> str:
    verdict = str(verification["verdict"])
    cls = "pass" if verdict == "PASS" else "fail"
    parts = ["<h2>复核</h2>", f'<p>结论：<span class="{cls}">{_e(verdict)}</span></p>']
    differences = verification.get("differences", [])
    if differences:
        rows = [[_e(d["code"]), _e(d.get("step_id", "")), _e(d.get("field", "")),
                 _e(d.get("expected", "")), _e(d.get("actual", ""))]
                for d in differences]
        parts.append(_table(["代码", "步骤", "字段", "期望", "实际"], rows))
    else:
        parts.append("<p>计划声明、台账记录与最终状态逐项一致。</p>")
    return "".join(parts)


def render_html(analysis: dict[str, Any], plan: dict[str, Any] | None = None,
                execution: dict[str, Any] | None = None,
                verification: dict[str, Any] | None = None) -> str:
    request = analysis["request"]
    meta = [f"请求 <code>{_e(request['request_id'])}</code> {_e(request['title'])}",
            f"窗口 {_e(request['window']['start'])} 至 {_e(request['window']['end'])}",
            f"分析指纹 <code>{_e(analysis['analysis_hash'])}</code>"]
    if plan:
        meta.append(f"计划指纹 <code>{_e(plan['plan_hash'])}</code>")
    if execution:
        meta.append(f"最终状态指纹 <code>{_e(execution['final_state_hash'])}</code>")
    body = ["<h1>数据源撤换与口径迁移报告</h1>",
            "".join(f'<div class="meta">{line}</div>' for line in meta),
            _impact(analysis), _conflicts(analysis)]
    if plan:
        body.append(_plan(plan))
    if execution:
        body.append(_execution(execution))
    if verification:
        body.append(_verify(verification))
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>swapdesk 撤换报告</title><style>" + CSS + "</style></head>"
        "<body><main>" + "".join(body)
        + "<footer>本报告由 swapdesk 在本地生成。全部登记数据为合成占位数据；"
          "页面不含外部资源引用。</footer></main></body></html>"
    )


def write_html(path: str, **kwargs: Any) -> str:
    atomic_write_text(path, render_html(**kwargs))
    return path
