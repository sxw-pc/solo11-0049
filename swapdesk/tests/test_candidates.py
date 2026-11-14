"""候选档（多替代源按优先级选档）的回归、选档、降档与状态冲突测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from swapdesk.analysis import run_analysis
from swapdesk.config import work_paths
from swapdesk.errors import ErrorCode, PlanBlockedError, SwapDeskError
from swapdesk.execute.ledger import Ledger
from swapdesk.execute.runner import execute_plan
from swapdesk.execute.verify import verify_run
from swapdesk.io.jsonout import read_json, write_json
from swapdesk.io.yamlload import load_registry, load_request
from swapdesk.model.entities import Registry, SwapRequest
from swapdesk.model.schema import validate_request_document
from swapdesk.plan.planner import build_plan

STAMP = "2026-02-01T00:00:00Z"

# req-001（旧单字符串格式）在改动前的基线计划 ID，用于回归锁定。
LEGACY_REQ001_PLAN_ID = "plan-28c2a4069cbb43a1"


def _tier_docs() -> dict:
    """Mini registry: one retired source with a lean 首选 and a full 保底."""
    return {
        "sources": [
            {"id": "src-old", "name": "旧源", "kind": "database",
             "status": "active", "fields": ["f1", "f2", "f3"]},
            {"id": "src-new", "name": "新源(首选)", "kind": "database",
             "status": "active", "fields": ["f1", "f2"]},
            {"id": "src-legacy", "name": "遗留源(保底)", "kind": "database",
             "status": "active", "fields": ["f1", "f2", "f3"]},
            {"id": "src-t1", "name": "极简源", "kind": "database",
             "status": "active", "fields": ["f1"]},
            {"id": "src-other", "name": "保留源", "kind": "api",
             "status": "active", "fields": ["g1"]},
        ],
        "tasks": [
            {"id": "task-full", "name": "全字段任务",
             "source_refs": ["src-old"], "output_fields": ["f1", "f2", "f3"],
             "schedule": "", "status": "active"},
            {"id": "task-lite", "name": "精简任务",
             "source_refs": ["src-old"], "output_fields": ["f1", "f2"],
             "schedule": "", "status": "active"},
            {"id": "task-mixed", "name": "多源任务",
             "source_refs": ["src-old", "src-other"],
             "output_fields": ["f1", "g1"], "schedule": "", "status": "active"},
        ],
        "downstream": [
            {"id": "dwn-shared", "name": "共享下游", "kind": "view",
             "upstream_refs": ["src-old"], "status": "active"},
            {"id": "dwn-indirect", "name": "间接下游", "kind": "table",
             "upstream_refs": ["task-lite"], "status": "active"},
        ],
        "lineage": [
            {"id": "lin-full", "from_ref": "src-old", "to_ref": "task-full"},
            {"id": "lin-lite", "from_ref": "src-old", "to_ref": "task-lite"},
            {"id": "lin-shared", "from_ref": "src-old", "to_ref": "dwn-shared"},
        ],
        "metrics": [
            {"id": "metric-full", "name": "全字段口径",
             "source_refs": ["src-old"], "required_fields": ["f1", "f2", "f3"],
             "required": True},
            {"id": "metric-lite", "name": "精简口径",
             "source_refs": ["src-old"], "required_fields": ["f2"],
             "required": False},
        ],
    }


def _request_doc(retire: list[dict], **overrides) -> dict:
    doc = {
        "request_id": "req-tier",
        "title": "候选档测试请求",
        "created_at": "2026-01-10T09:00:00Z",
        "retire": retire,
        "window": {"start": "2026-02-01T00:00:00Z",
                   "end": "2026-02-05T00:00:00Z"},
        "other_windows": [],
        "max_parallel": 2,
    }
    doc.update(overrides)
    return doc


def _tier_registry() -> Registry:
    return Registry.from_documents(_tier_docs())


def _tier_request(candidates=("src-new", "src-legacy")) -> SwapRequest:
    return SwapRequest.from_dict(_request_doc(
        [{"source": "src-old", "candidates": list(candidates)}]))


def _plan(registry, request):
    analysis = run_analysis(registry, request)
    return analysis, build_plan(registry, request, analysis)


def _steps_by_target(plan, action):
    return {step["target_id"]: step for step in plan["steps"]
            if step["action"] == action}


# ---------------------------------------------------------------------------
# 1. 单源单档回归：旧格式输出必须与改动前一致
# ---------------------------------------------------------------------------

def test_legacy_single_replacement_output_unchanged(registry, request_one) -> None:
    analysis = run_analysis(registry, request_one)
    plan = build_plan(registry, request_one, analysis)
    assert plan["plan_id"] == LEGACY_REQ001_PLAN_ID
    assert "selections" not in analysis
    assert "selections" not in plan
    assert all("selection" not in step["payload"] for step in plan["steps"])
    assert analysis["request"]["retire"] == [
        {"source": "src-alpha", "replacement": "src-alpha-v2"},
        {"source": "src-beta", "replacement": "src-beta-v2"},
    ]
    assert plan["replacements"] == {"src-alpha": "src-alpha-v2",
                                    "src-beta": "src-beta-v2"}


def test_single_candidate_list_is_tier_one() -> None:
    registry = _tier_registry()
    request = _tier_request(candidates=("src-legacy",))
    analysis, plan = _plan(registry, request)
    assert analysis["summary"]["executable"] is True
    selections = analysis["selections"]
    for consumer in selections["consumers"]:
        assert consumer["choices"]["src-old"]["tier"] == 1
        assert consumer["choices"]["src-old"]["candidate"] == "src-legacy"
    task_steps = _steps_by_target(plan, "repoint_task")
    assert task_steps["task-lite"]["payload"]["after"] == ["src-legacy"]
    assert task_steps["task-lite"]["payload"]["selection"]["choices"] == {
        "src-old": {"candidate": "src-legacy", "tier": 1},
    }


# ---------------------------------------------------------------------------
# 2. 多消费方选档不同 + 共享实体稳定可重放
# ---------------------------------------------------------------------------

def test_consumers_pick_different_tiers() -> None:
    registry = _tier_registry()
    request = _tier_request()
    analysis, plan = _plan(registry, request)
    selections = {entry["consumer_id"]: entry
                  for entry in analysis["selections"]["consumers"]}
    # 同一待下线源，不同消费方按各自字段需求选到不同档位
    assert selections["task-lite"]["choices"]["src-old"] == {
        "candidate": "src-new", "tier": 1}
    assert selections["task-full"]["choices"]["src-old"] == {
        "candidate": "src-legacy", "tier": 2}
    assert selections["task-full"]["degraded"] is True
    assert selections["task-full"]["primary_missing"] == ["f3"]
    assert "退到第 2 档" in selections["task-full"]["reason"]
    assert selections["metric-full"]["choices"]["src-old"]["tier"] == 2
    assert selections["metric-lite"]["choices"]["src-old"]["tier"] == 1
    # 多源消费方：保留源字段与候选档字段合并判定
    assert selections["task-mixed"]["choices"]["src-old"]["tier"] == 1

    task_steps = _steps_by_target(plan, "repoint_task")
    assert task_steps["task-full"]["payload"]["after"] == ["src-legacy"]
    assert task_steps["task-full"]["payload"]["selection"]["reason"] == \
        selections["task-full"]["reason"]
    assert task_steps["task-lite"]["payload"]["after"] == ["src-new"]
    metric_steps = _steps_by_target(plan, "repoint_metric")
    assert metric_steps["metric-full"]["payload"]["after"] == ["src-legacy"]
    assert metric_steps["metric-lite"]["payload"]["after"] == ["src-new"]

    # 共享下游/无消费方血缘走全局映射（消费方最小档位 = 第 1 档）
    downstream = _steps_by_target(plan, "repoint_downstream")
    assert downstream["dwn-shared"]["payload"]["after"] == ["src-new"]
    assert downstream["dwn-shared"]["payload"]["selection"]["choices"][
        "src-old"]["tier"] == 1
    lineage = _steps_by_target(plan, "repoint_lineage")
    assert lineage["lin-lite"]["payload"]["after"]["from_ref"] == "src-new"
    assert lineage["lin-full"]["payload"]["after"]["from_ref"] == "src-legacy"
    assert lineage["lin-shared"]["payload"]["after"]["from_ref"] == "src-new"

    # 每个目标只出现一次重指步骤：某消费方降档不会让别人重复重指
    repoint_targets = [step["target_id"] for step in plan["steps"]
                       if step["action"].startswith("repoint")]
    assert len(repoint_targets) == len(set(repoint_targets))


def test_shared_candidate_plan_is_replayable(tmp_path: Path) -> None:
    registry = _tier_registry()
    request = _tier_request()
    paths = work_paths(tmp_path / "work")
    analysis, plan = _plan(registry, request)
    # 批次与依赖确定性：重复生成逐字节一致
    _, plan_again = _plan(registry, request)
    assert plan["plan_hash"] == plan_again["plan_hash"]
    position = {step["step_id"]: index
                for index, step in enumerate(plan["steps"])}
    for step in plan["steps"]:
        for dep in step["depends_on"]:
            assert position[dep] < position[step["step_id"]]
    # 中断续跑 + 完整重放，断言不错乱
    stopped = execute_plan(plan, registry.initial_state(), paths, STAMP,
                           stop_after=2)
    assert stopped["status"] == "stopped"
    resumed = execute_plan(plan, registry.initial_state(), paths, STAMP,
                           resume=True)
    assert resumed["status"] == "complete"
    assert resumed["succeeded_steps"] == resumed["total_steps"]
    replayed = execute_plan(plan, registry.initial_state(), paths, STAMP)
    assert replayed["status"] == "complete"
    assert verify_run(replayed["run_id"], paths, registry,
                      plan)["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# 3. 降档回退链
# ---------------------------------------------------------------------------

def test_tier_chain_degrades_until_covered() -> None:
    registry = _tier_registry()
    request = _tier_request(candidates=("src-t1", "src-new", "src-legacy"))
    analysis, plan = _plan(registry, request)
    selections = {entry["consumer_id"]: entry
                  for entry in analysis["selections"]["consumers"]}
    full = selections["task-full"]
    # 第 1 档缺 f2/f3，第 2 档缺 f3，第 3 档才覆盖
    assert full["choices"]["src-old"] == {"candidate": "src-legacy", "tier": 3}
    assert full["primary_missing"] == ["f2", "f3"]
    assert "退到第 3 档" in full["reason"]
    assert selections["task-lite"]["choices"]["src-old"]["tier"] == 2
    step = _steps_by_target(plan, "repoint_task")["task-full"]
    assert step["payload"]["selection"]["choices"]["src-old"]["tier"] == 3


def test_multi_retired_sources_need_combined_coverage() -> None:
    docs = _tier_docs()
    docs["sources"] += [
        {"id": "src-b-old", "name": "旧源B", "kind": "api", "status": "active",
         "fields": ["fb1", "fb2"]},
        {"id": "src-b1", "name": "B首选", "kind": "api", "status": "active",
         "fields": ["fb1"]},
        {"id": "src-b2", "name": "B保底", "kind": "api", "status": "active",
         "fields": ["fb1", "fb2"]},
    ]
    docs["tasks"] = [
        {"id": "task-ab-full", "name": "双源全字段",
         "source_refs": ["src-old", "src-b-old"],
         "output_fields": ["f1", "f2", "f3", "fb1", "fb2"],
         "schedule": "", "status": "active"},
        {"id": "task-ab-lite", "name": "双源精简",
         "source_refs": ["src-old", "src-b-old"],
         "output_fields": ["f1", "fb1"], "schedule": "", "status": "active"},
    ]
    docs["downstream"] = []
    docs["lineage"] = []
    docs["metrics"] = []
    registry = Registry.from_documents(docs)
    request = SwapRequest.from_dict(_request_doc([
        {"source": "src-old", "candidates": ["src-new", "src-legacy"]},
        {"source": "src-b-old", "candidates": ["src-b1", "src-b2"]},
    ]))
    analysis = run_analysis(registry, request)
    selections = {entry["consumer_id"]: entry
                  for entry in analysis["selections"]["consumers"]}
    # 两个待下线源各自选档后合并判定：全字段任务双降，精简任务双首选
    assert selections["task-ab-full"]["choices"] == {
        "src-old": {"candidate": "src-legacy", "tier": 2},
        "src-b-old": {"candidate": "src-b2", "tier": 2},
    }
    assert selections["task-ab-lite"]["choices"] == {
        "src-old": {"candidate": "src-new", "tier": 1},
        "src-b-old": {"candidate": "src-b1", "tier": 1},
    }


def test_exhausted_tiers_record_shortfall_and_block() -> None:
    registry = _tier_registry()
    request = _tier_request(candidates=("src-new",))
    analysis = run_analysis(registry, request)
    conflicts = analysis["conflicts"]
    field_missing = [c for c in conflicts
                     if c["rule_id"] == "C_FIELD_MISSING"
                     and c["severity"] == "blocking"]
    metric_broken = [c for c in conflicts if c["rule_id"] == "C_METRIC_BROKEN"]
    # 缺什么、退到哪档都写进证据
    task_conflict = next(c for c in field_missing
                         if c["target"]["id"] == "task-full")
    assert task_conflict["evidence"]["missing_fields"] == ["f3"]
    assert task_conflict["evidence"]["choices"] == {
        "src-old": {"candidate": "src-new", "tier": 1}}
    assert "仍缺字段 f3" in task_conflict["evidence"]["reason"]
    assert any(c["target"]["id"] == "metric-full" for c in metric_broken)
    assert analysis["summary"]["executable"] is False
    with pytest.raises(PlanBlockedError):
        build_plan(registry, request, analysis)


# ---------------------------------------------------------------------------
# 4. 状态冲突：续跑/重放识别差异并明确报错
# ---------------------------------------------------------------------------

def _failed_resume(registry, request, plan, paths, mutate) -> SwapDeskError:
    stopped = execute_plan(plan, registry.initial_state(), paths, STAMP,
                           stop_after=3)
    assert stopped["status"] == "stopped"
    state_path = paths.runs / stopped["run_id"] / "state.json"
    state = read_json(state_path)
    mutate(state)
    write_json(state_path, state)
    with pytest.raises(SwapDeskError) as caught:
        execute_plan(plan, registry.initial_state(), paths, STAMP, resume=True)
    return caught.value


def test_resume_rejects_silently_repeated_repoint(tmp_path: Path) -> None:
    """首选档缺失被降到 backup 的消费方，其目标状态若已在前序批次为另一
    消费方重指过，续跑必须明确报错而不是静默重复。"""
    registry = _tier_registry()
    request = _tier_request()
    _, plan = _plan(registry, request)
    paths = work_paths(tmp_path / "work")
    metric_step = _steps_by_target(plan, "repoint_metric")["metric-full"]
    assert metric_step["payload"]["after"] == ["src-legacy"]  # backup 档

    def mutate(state):
        # 模拟 backup 已在前序批次为另一消费方完成重指后的状态
        state["metrics"]["metric-full"]["source_refs"] = ["src-legacy"]

    error = _failed_resume(registry, request, plan, paths, mutate)
    assert error.code == ErrorCode.STATE_MISMATCH
    assert "拒绝静默重复" in error.message
    assert error.details["target_id"] == "metric-full"
    assert error.details["expected_before"] == ["src-old"]
    assert error.details["actual"] == ["src-legacy"]
    assert error.details["selection"]["choices"]["src-old"] == {
        "candidate": "src-legacy", "tier": 2}
    # 没有静默重复：台账中该步骤为失败，运行状态为 failed
    run_id = f"run-{plan['plan_hash'][:16]}"
    ledger = Ledger(paths.ledger)
    row = ledger.get_step(run_id, metric_step["step_id"])
    assert row["status"] == "failed"
    assert ledger.get_run(run_id)["status"] == "failed"
    # 重放依旧确定性报错，不会悄悄跳过
    with pytest.raises(SwapDeskError) as caught:
        execute_plan(plan, registry.initial_state(), paths, STAMP, resume=True)
    assert caught.value.code == ErrorCode.STATE_MISMATCH


def test_resume_rejects_unexpected_state(tmp_path: Path) -> None:
    registry = _tier_registry()
    request = _tier_request()
    _, plan = _plan(registry, request)
    paths = work_paths(tmp_path / "work")

    def mutate(state):
        # dwn-shared 的步骤在 rank 2，stop_after=3 时还未执行
        state["downstream"]["dwn-shared"]["upstream_refs"] = ["src-t1"]

    error = _failed_resume(registry, request, plan, paths, mutate)
    assert error.code == ErrorCode.STATE_MISMATCH
    assert "执行前状态与步骤声明不一致" in error.message
    assert error.details["target_id"] == "dwn-shared"
    assert error.details["expected_before"] == ["src-old"]
    assert error.details["actual"] == ["src-t1"]


# ---------------------------------------------------------------------------
# 5. 请求格式校验与候选登记
# ---------------------------------------------------------------------------

def test_request_schema_accepts_both_forms() -> None:
    legacy = _request_doc([{"source": "src-old", "replacement": "src-new"}])
    assert validate_request_document(legacy) == []
    tiered = _request_doc([{"source": "src-old",
                            "candidates": ["src-new", "src-legacy"]}])
    assert validate_request_document(tiered) == []


@pytest.mark.parametrize("retire, code", [
    ([{"source": "s", "replacement": "r", "candidates": ["r"]}],
     ErrorCode.REQUEST_INVALID),
    ([{"source": "s", "candidates": []}], ErrorCode.SCHEMA_EMPTY),
    ([{"source": "s", "candidates": ["a", "a"]}], ErrorCode.SCHEMA_TYPE),
    ([{"source": "s", "candidates": [1]}], ErrorCode.SCHEMA_TYPE),
    ([{"source": "s", "candidates": ["a", ""]}], ErrorCode.SCHEMA_EMPTY),
    ([{"source": "s"}], ErrorCode.SCHEMA_MISSING_FIELD),
    ([{"source": "s", "replacement": 1}], ErrorCode.SCHEMA_TYPE),
])
def test_request_schema_rejects_bad_candidate_forms(retire, code) -> None:
    issues = validate_request_document(_request_doc(retire))
    assert code in {issue.code for issue in issues}


def test_unregistered_candidate_is_blocking() -> None:
    registry = _tier_registry()
    request = _tier_request(candidates=("src-new", "src-ghost"))
    analysis = run_analysis(registry, request)
    missing = [c for c in analysis["conflicts"]
               if c["rule_id"] == "C_REPLACEMENT_MISSING"]
    assert len(missing) == 1
    assert missing[0]["severity"] == "blocking"
    assert missing[0]["evidence"] == {"source": "src-old",
                                      "replacement": "src-ghost", "tier": 2}
    assert analysis["summary"]["executable"] is False


# ---------------------------------------------------------------------------
# 6. 仓库自带 req-003 样例：首选精简、保底全量，端到端通过
# ---------------------------------------------------------------------------

def test_req003_fixture_end_to_end(data_dir: Path, tmp_path: Path) -> None:
    request = load_request(data_dir / "swap_requests" / "req-003.yaml")
    registry = load_registry(data_dir)
    analysis = run_analysis(registry, request)
    assert analysis["summary"]["executable"] is True
    selections = {entry["consumer_id"]: entry
                  for entry in analysis["selections"]["consumers"]}
    # 事件类消费方首选 src-beta-lite 缺 event_type，降到保底 src-beta-v2
    assert selections["task-events-ingest"]["choices"]["src-beta"] == {
        "candidate": "src-beta-v2", "tier": 2}
    assert selections["metric-event-funnel"]["choices"]["src-beta"] == {
        "candidate": "src-beta-v2", "tier": 2}
    assert selections["task-orders-ingest"]["choices"]["src-alpha"] == {
        "candidate": "src-alpha-v2", "tier": 1}
    plan = build_plan(registry, request, analysis)
    repoint_steps = [step for step in plan["steps"]
                     if step["action"].startswith("repoint")]
    assert repoint_steps
    assert all("selection" in step["payload"] for step in repoint_steps)
    paths = work_paths(tmp_path / "work")
    execution = execute_plan(plan, registry.initial_state(), paths, STAMP)
    assert execution["status"] == "complete"
    assert verify_run(execution["run_id"], paths, registry,
                      plan)["verdict"] == "PASS"
