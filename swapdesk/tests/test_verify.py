from __future__ import annotations

from swapdesk.analysis import run_analysis
from swapdesk.cli import main
from swapdesk.config import work_paths
from swapdesk.execute.runner import execute_plan
from swapdesk.execute.verify import verify_run
from swapdesk.io.jsonout import read_json, write_json
from swapdesk.plan.planner import build_plan

STAMP = "2026-02-01T00:00:00Z"


def _run(registry, request, tmp_path):
    paths = work_paths(tmp_path / "work")
    analysis = run_analysis(registry, request)
    plan = build_plan(registry, request, analysis)
    execution = execute_plan(plan, registry.initial_state(), paths, STAMP)
    return paths, plan, execution


def test_verify_pass(registry, request_one, tmp_path) -> None:
    paths, plan, execution = _run(registry, request_one, tmp_path)
    result = verify_run(execution["run_id"], paths, registry, plan)
    assert result["verdict"] == "PASS"
    assert result["differences"] == []
    assert result["checks"]["expected_steps"] == 12
    assert result["checks"]["succeeded_steps"] == 12
    assert main(["verify", "--workdir", str(paths.root), "--run-id",
                 execution["run_id"], "--data-dir", str(tmp_path / "data")]) == 0


def test_verify_fail_and_exit_code(registry, request_one, tmp_path, capsys) -> None:
    paths, plan, execution = _run(registry, request_one, tmp_path)
    state_path = paths.runs / execution["run_id"] / "state.json"
    state = read_json(state_path)
    state["sources"]["src-alpha"]["status"] = "active"
    write_json(state_path, state)
    result = verify_run(execution["run_id"], paths, registry, plan)
    assert result["verdict"] == "FAIL"
    codes = {difference["code"] for difference in result["differences"]}
    assert "FINAL_HASH_MISMATCH" in codes
    for difference in result["differences"]:
        assert {"code", "message", "expected", "actual"} <= set(difference)
    code = main(["verify", "--workdir", str(paths.root), "--run-id",
                 execution["run_id"], "--data-dir", str(tmp_path / "data")])
    assert code == 4
    assert '"verdict": "FAIL"' in capsys.readouterr().out
