from __future__ import annotations

import json
from pathlib import Path

import pytest

from swapdesk.analysis import run_analysis
from swapdesk.config import work_paths
from swapdesk.errors import SwapDeskError
from swapdesk.execute.ledger import Ledger
from swapdesk.execute.runner import execute_plan
from swapdesk.explain import (diff_states, explain_run, ref_switches,
                              write_explanation)
from swapdesk.io.jsonout import read_json, sha256_file, write_json
from swapdesk.plan.planner import build_plan

STAMP = "2026-02-01T00:00:00Z"


def _prepare(registry, request, tmp_path):
    paths = work_paths(tmp_path / "work")
    analysis = run_analysis(registry, request)
    plan = build_plan(registry, request, analysis)
    execution = execute_plan(plan, registry.initial_state(), paths, STAMP)
    return paths, plan, execution


def _steps_by_id(explanation):
    return {step["step_id"]: step for step in explanation["steps"]}


def test_explain_happy_path_maps_hashes_to_entities(
        registry, request_one, tmp_path) -> None:
    paths, plan, execution = _prepare(registry, request_one, tmp_path)
    explanation = explain_run(execution["run_id"], paths)

    assert explanation["kind"] == "explain"
    assert explanation["run"]["run_id"] == execution["run_id"]
    assert explanation["run"]["status"] == "complete"
    assert explanation["plan"]["declared_steps"] == len(plan["steps"])
    assert explanation["summary"]["total_steps"] == len(plan["steps"])
    assert explanation["summary"]["evidence_ok"] is True
    assert explanation["summary"]["unverified_step_ids"] == []

    steps = _steps_by_id(explanation)
    task_step = steps["step-0002"]
    assert task_step["state_diff"]["available"] is True
    change = next(item for item in task_step["state_diff"]["changes"]
                  if item["entity_id"] == "task-orders-enrich")
    assert change["field"] == "source_refs"
    assert change["removed"] == ["src-alpha"]
    assert change["added"] == ["src-alpha-v2"]
    assert change["state_path"] == "tasks.task-orders-enrich.source_refs"
    assert task_step["ref_switches"] == [{
        "field": "task.task-orders-enrich.source_refs",
        "before": "src-alpha", "after": "src-alpha-v2"}]

    # The hash chain must be reproduced step by step.
    for previous, current in zip(explanation["steps"],
                                 explanation["steps"][1:]):
        assert previous["execution"]["after_hash"] == \
            current["execution"]["before_hash"]
    assert explanation["steps"][-1]["execution"]["after_hash"] == \
        explanation["run"]["final_hash"]

    # Every step artifact is locatable by relative path and sha256.
    ledger = Ledger(paths.ledger)
    artifacts = {(row["step_id"], row["kind"]): row
                 for row in ledger.artifacts_for_run(execution["run_id"])}
    for evidence in explanation["evidence_index"]:
        row = artifacts[("step-0001" if False else evidence["step_id"],
                         "state_after")]
        assert evidence["path"] == row["path"]
        assert evidence["sha256"] == row["sha256"]
        assert evidence["sha256_actual"] == row["sha256"]
        assert sha256_file(paths.root / evidence["path"]) == row["sha256"]
        assert evidence["ledger_artifact"]["path"] == row["path"]
        assert evidence["ok"] is True

    assert explanation["baseline"]["final_state_matches_ledger"] is True
    assert explanation["baseline"]["returned_to_initial_hash"] is False
    assert explanation["baseline"]["baseline_kind"] == "run_initial_hash"


def test_explain_empty_step_is_explicit(registry, request_one, tmp_path) -> None:
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    explanation = explain_run(execution["run_id"], paths)
    steps = _steps_by_id(explanation)

    review = steps["step-0009"]
    assert review["action"] == "review_metric"
    assert review["empty_impact"] is True
    assert review["impact_classification"] == "empty"
    assert review["execution"]["before_hash"] == \
        review["execution"]["after_hash"]
    assert review["state_diff"]["changes"] == []
    assert review["empty_impact_kind"] == "no_state_change"
    assert "空影响步骤" in review["narrative"]
    assert "step-0009" in explanation["summary"]["empty_impact_steps"]

    # A step that changes fields but no downstream/metric effective field set
    # is still reported as empty, with a distinct reason.
    lineage = steps["step-0005"]
    assert lineage["action"] == "repoint_lineage"
    assert lineage["state_diff"]["changes"]
    assert lineage["empty_impact"] is True
    assert lineage["empty_impact_kind"] == "no_downstream_effective_change"
    assert lineage["downstream_impact"]["effective"]["counts"]["total"] == 0

    non_empty = [step["step_id"] for step in explanation["steps"]
                 if not step["empty_impact"]]
    assert non_empty == ["step-0002", "step-0004"]


def test_explain_partial_lineage_switch_does_not_inflate_surface(
        registry, request_one, tmp_path) -> None:
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    explanation = explain_run(execution["run_id"], paths)
    steps = _steps_by_id(explanation)

    for step_id in ("step-0005", "step-0006", "step-0007"):
        step = steps[step_id]
        scope = step["lineage_scope"]
        assert scope["partial_endpoint_switch"] is True
        assert scope["switched_endpoints"] == ["from_ref"]
        assert scope["unchanged_endpoints"] == ["to_ref"]
        # Only the switched endpoint is a ref switch.
        assert step["ref_switches"] == [{
            "field": f"lineage.{step['target_id']}.from_ref",
            "before": scope["before"]["from_ref"],
            "after": scope["after"]["from_ref"],
        }]
        # The to_ref side's downstream subtree is explicitly excluded rather
        # than counted as switched surface.
        excluded = {item["entity_id"] for item in
                    scope["unchanged_endpoint_downstream_not_counted"]}
        assert excluded
        assert "dwd-orders" in excluded or "dwd-order-events" in excluded
        reachable = {item["entity_id"] for item in
                     step["downstream_impact"]["reachable"]["downstream"]
                     + step["downstream_impact"]["reachable"]["metrics"]}
        assert not (reachable & excluded)
        assert step["downstream_impact"]["reachable"]["counts"]["total"] == 0
        assert step["downstream_impact"]["effective"]["counts"]["total"] == 0

    # A to_ref switch would legitimately contribute the new destination.
    edge_step = {"step_id": "x", "action": "repoint_lineage",
                 "target_type": "lineage", "target_id": "lin-001"}
    from swapdesk.explain import _lineage_scope
    before = {"sources": {}, "tasks": {}, "downstream": {}, "metrics": {},
              "lineage": {"lin-001": {"from_ref": "a", "to_ref": "b"}}}
    after = {"sources": {}, "tasks": {}, "downstream": {}, "metrics": {},
             "lineage": {"lin-001": {"from_ref": "a", "to_ref": "c"}}}
    scope = _lineage_scope(
        edge_step,
        [{"entity_group": "lineage", "entity_id": "lin-001",
          "field": "to_ref", "before": "b", "after": "c"}],
        before, after)
    assert scope["switched_endpoints"] == ["to_ref"]
    assert scope["unchanged_endpoints"] == ["from_ref"]


def test_explain_writes_json_and_markdown(registry, request_one, tmp_path) -> None:
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    explanation = explain_run(execution["run_id"], paths)
    outputs = write_explanation(explanation, paths)

    json_path = Path(outputs["json"])
    markdown_path = Path(outputs["markdown"])
    assert json_path == paths.runs / execution["run_id"] / "explain.json"
    assert markdown_path == (paths.reports /
                             f"explain-{execution['run_id']}.md")
    body = read_json(json_path)
    assert body["kind"] == "explain"
    assert body["run"]["run_id"] == execution["run_id"]
    text = markdown_path.read_text(encoding="utf-8")
    assert "# 运行复盘" in text
    assert "## 逐步复盘" in text
    assert "#### 状态字段差异" in text
    assert "#### 证据" in text
    assert "空影响步骤" in text
    # Deterministic rendering.
    from swapdesk.explain import render_explanation_markdown
    assert (render_explanation_markdown(explanation)
            == render_explanation_markdown(read_json(json_path)))


def test_explain_cli_and_missing_run(registry, request_one, tmp_path,
                                     capsys) -> None:
    from swapdesk.cli import main
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    code = main(["explain", "--workdir", str(paths.root),
                 "--run-id", execution["run_id"]])
    output = capsys.readouterr().out
    assert code == 0
    assert "[explain] run_id=" in output
    assert "evidence_ok=true" in output
    assert (paths.runs / execution["run_id"] / "explain.json").exists()

    code = main(["explain", "--workdir", str(paths.root),
                 "--run-id", "run-absent"])
    assert code == 2
    assert "E_RUN_NOT_FOUND" in capsys.readouterr().err


def test_explain_rollback_returns_to_initial_hash(registry, request_one,
                                                  tmp_path) -> None:
    from swapdesk.pipeline import rollback_command
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    result = rollback_command(paths.root, execution["run_id"],
                              data_dir=tmp_path / "work" / "fixtures")
    rb_run = result["execution"]["run_id"]
    explanation = explain_run(rb_run, paths)
    assert explanation["run"]["kind"] == "rollback"
    assert explanation["plan"]["expected_initial_hash"] == \
        explanation["baseline"]["baseline_hash"]
    assert explanation["summary"]["returned_to_initial_hash"] is True
    assert explanation["baseline"]["returned_to_initial_hash"] is True
    assert "回到原执行前基线" in explanation["summary"]["narrative"]


def test_state_condition_transitions_blocking_to_executable() -> None:
    """State-level blocking conditions are recomputed from both states."""
    from swapdesk.explain import _state_condition_transitions
    before = {
        "sources": {"a": {"status": "retired", "fields": ["x"]}},
        "tasks": {"t": {"status": "active", "source_refs": ["a"]}},
        "downstream": {}, "lineage": {}, "metrics": {},
    }
    after = {
        "sources": {"a": {"status": "retired", "fields": ["x"]},
                    "b": {"status": "active", "fields": ["x"]}},
        "tasks": {"t": {"status": "active", "source_refs": ["b"]}},
        "downstream": {}, "lineage": {}, "metrics": {},
    }
    transitions = _state_condition_transitions(before, after, {})
    retired_ref = [item for item in transitions
                   if item["rule_id"] == "C_RETIRED_SOURCE_REFERENCED"]
    assert len(retired_ref) == 1
    item = retired_ref[0]
    assert item["target"] == {"type": "task", "id": "t"}
    assert item["transition"] == "blocking_to_executable"
    assert item["from"]["blocking"] is True
    assert item["to"]["blocking"] is False

    # The reverse direction is a regression, not a resolution.
    backwards = _state_condition_transitions(after, before, {})
    regression = [item for item in backwards
                  if item["rule_id"] == "C_RETIRED_SOURCE_REFERENCED"]
    assert regression[0]["transition"] == "executable_to_blocking"


def test_explain_reports_recorded_blocking_conflict_resolution(
        registry, request_one, tmp_path) -> None:
    """A recorded blocking conflict is re-evaluated against both states."""
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    run_dir = paths.runs / execution["run_id"]
    analysis = run_analysis(registry, request_one)
    analysis["conflicts"].append({
        "rule_id": "C_FIELD_MISSING",
        "severity": "blocking",
        "target": {"type": "task", "id": "task-orders-enrich"},
        "message": "替代后的有效源集合缺少任务必需字段",
        "evidence": {"missing_fields": ["load_ts"]},
    })
    analysis["summary"]["counts"]["blocking"] = 1
    write_json(run_dir / "analysis.json", analysis)

    explanation = explain_run(execution["run_id"], paths)
    steps = _steps_by_id(explanation)
    step = steps["step-0002"]
    transition = next(item for item in step["conflict_transitions"]
                      if item["rule_id"] == "C_FIELD_MISSING")
    assert transition["source"] == "recorded_conflict"
    assert transition["from"]["blocking"] is True
    assert transition["to"]["resolved"] is True
    assert transition["transition"] == "blocking_to_executable"
    resolved = [item for item in explanation["summary"]["blocking_conflicts_resolved"]
                if item["rule_id"] == "C_FIELD_MISSING"]
    assert resolved and resolved[0]["source"] == "recorded_conflict"


def test_explain_detects_tampered_artifact(registry, request_one,
                                          tmp_path) -> None:
    paths, _, execution = _prepare(registry, request_one, tmp_path)
    ledger = Ledger(paths.ledger)
    artifact = next(row for row in ledger.artifacts_for_run(execution["run_id"])
                    if row["step_id"] == "step-0001")
    artifact_path = paths.root / artifact["path"]
    body = read_json(artifact_path)
    body["state_after"]["tasks"]["task-events-ingest"]["source_refs"] = \
        ["src-beta"]
    write_json(artifact_path, body)

    explanation = explain_run(execution["run_id"], paths)
    evidence = next(item for item in explanation["evidence_index"]
                    if item["step_id"] == "step-0001")
    assert evidence["sha256_match"] is False
    assert evidence["ok"] is False
    assert explanation["summary"]["evidence_ok"] is False
    assert "step-0001" in explanation["summary"]["unverified_step_ids"]
    assert any("step-0001" in warning for warning in explanation["warnings"])


def test_explain_rejects_unknown_run(tmp_path) -> None:
    paths = work_paths(tmp_path / "empty")
    with pytest.raises(SwapDeskError) as info:
        explain_run("run-missing", paths)
    assert info.value.code == "E_RUN_NOT_FOUND"


def test_diff_states_reports_all_hash_participating_fields() -> None:
    before = {
        "schema_version": 1,
        "sources": {"s1": {"status": "active", "fields": ["a"]}},
        "tasks": {"t1": {"status": "active", "source_refs": ["s1"]}},
        "downstream": {}, "lineage": {}, "metrics": {},
    }
    after = {
        "schema_version": 1,
        "sources": {"s1": {"status": "retired", "fields": ["a", "b"]}},
        "tasks": {"t1": {"status": "active", "source_refs": ["s2"]}},
        "downstream": {}, "lineage": {}, "metrics": {},
    }
    changes = diff_states(before, after)
    paths = {(item["entity_type"], item["entity_id"], item["field"])
             for item in changes}
    assert ("source", "s1", "status") in paths
    assert ("source", "s1", "fields") in paths
    assert ("task", "t1", "source_refs") in paths
    switches = ref_switches(changes, {"s1": "s2"})
    assert switches == [{"field": "task.t1.source_refs",
                         "before": "s1", "after": "s2"}]
