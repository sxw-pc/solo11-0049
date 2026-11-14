from __future__ import annotations

import re
from copy import deepcopy

from swapdesk.analysis import run_analysis
from swapdesk.config import work_paths
from swapdesk.execute.runner import execute_plan
from swapdesk.execute.verify import verify_run
from swapdesk.io.jsonout import pretty_json
from swapdesk.plan.planner import build_plan
from swapdesk.report.html import render_html
from swapdesk.report.markdown import render_markdown

STAMP = "2026-02-01T00:00:00Z"


def _materials(registry, request, tmp_path):
    analysis = run_analysis(registry, request)
    plan = build_plan(registry, request, analysis)
    paths = work_paths(tmp_path / "work")
    execution = execute_plan(plan, registry.initial_state(), paths, STAMP)
    verification = verify_run(execution["run_id"], paths, registry, plan)
    return analysis, plan, execution, verification


def test_markdown_sections_and_stability(registry, request_one, tmp_path) -> None:
    analysis, plan, execution, verification = _materials(
        registry, request_one, tmp_path)
    first = render_markdown(analysis, plan, execution, verification)
    second = render_markdown(analysis, plan, execution, verification)
    assert first == second
    for heading in ("# 数据源撤换与口径迁移报告", "## 影响面", "## 冲突与阻断",
                    "## 迁移计划", "## 执行摘要", "## 复核"):
        assert heading in first
    assert "PASS" in first
    assert "合成占位数据" in first


def test_html_is_self_contained(registry, request_one, tmp_path) -> None:
    analysis, plan, execution, verification = _materials(
        registry, request_one, tmp_path)
    page = render_html(analysis, plan, execution, verification)
    assert page.startswith("<!doctype html>")
    assert "<style>" in page
    assert "rel=\"stylesheet\"" not in page
    assert "<script" not in page
    assert re.search(r"https?://", page) is None
    assert "//cdn" not in page
    assert render_html(analysis, plan, execution, verification) == page


def test_json_key_order_is_stable(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    plan = build_plan(registry, request_one, analysis)
    clone = deepcopy(plan)
    assert pretty_json(plan) == pretty_json(clone)
    lines = pretty_json(plan).splitlines()
    assert lines[0] == "{"
    assert lines[1].strip().startswith('"action_counts"')
