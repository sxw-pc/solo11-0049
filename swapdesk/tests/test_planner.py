from __future__ import annotations

import pytest

from swapdesk.analysis import run_analysis
from swapdesk.errors import PlanBlockedError
from swapdesk.io.jsonout import pretty_json
from swapdesk.plan.planner import build_plan


def _plan(registry, request):
    analysis = run_analysis(registry, request)
    return analysis, build_plan(registry, request, analysis)


def test_topological_batches_and_parallel_limit(registry, request_one) -> None:
    analysis, plan = _plan(registry, request_one)
    assert len(plan["steps"]) == 12
    assert len(plan["batches"]) == 4
    position = {step["step_id"]: index for index, step in
                enumerate(plan["steps"])}
    for batch in plan["batches"]:
        for group in batch["sub_batches"]:
            assert 1 <= len(group) <= request_one.max_parallel
        assert [sid for group in batch["sub_batches"] for sid in group] \
            == batch["step_ids"]
    for step in plan["steps"]:
        for dep in step["depends_on"]:
            assert position[dep] < position[step["step_id"]]
    actions = {step["action"] for step in plan["steps"]}
    assert {"repoint_task", "repoint_downstream", "repoint_metric",
            "repoint_lineage", "retire_source"} <= actions


def test_step_ids_and_plan_bytes_are_stable(registry, request_one) -> None:
    first_analysis, first = _plan(registry, request_one)
    second_analysis, second = _plan(registry, request_one)
    assert [s["step_id"] for s in first["steps"]] == [
        "step-0001", "step-0002", "step-0003", "step-0004", "step-0005",
        "step-0006", "step-0007", "step-0008", "step-0009", "step-0010",
        "step-0011", "step-0012",
    ]
    assert pretty_json(first) == pretty_json(second)
    assert first["plan_hash"] == second["plan_hash"]
    assert first_analysis["analysis_hash"] == second_analysis["analysis_hash"]


def test_blocking_request_cannot_generate_plan(registry, request_two) -> None:
    analysis = run_analysis(registry, request_two)
    with pytest.raises(PlanBlockedError) as caught:
        build_plan(registry, request_two, analysis)
    assert caught.value.exit_code == 3
    assert "C_REPLACEMENT_MISSING" in caught.value.details["rule_ids"]
    assert caught.value.details["blocking"]
