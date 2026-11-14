from __future__ import annotations

from swapdesk.analysis import run_analysis
from swapdesk.config import work_paths
from swapdesk.execute.ledger import Ledger
from swapdesk.execute.runner import execute_plan
from swapdesk.plan.planner import build_plan

STAMP = "2026-02-01T00:00:00Z"


def _prepare(registry, request, tmp_path):
    paths = work_paths(tmp_path / "work")
    analysis = run_analysis(registry, request)
    plan = build_plan(registry, request, analysis)
    return paths, plan


def test_stop_resume_and_idempotent_ledger(registry, request_one, tmp_path) -> None:
    paths, plan = _prepare(registry, request_one, tmp_path)
    stopped = execute_plan(plan, registry.initial_state(), paths, STAMP,
                           stop_after=3)
    assert stopped["status"] == "stopped"
    assert stopped["succeeded_steps"] == 3
    assert stopped["total_steps"] == 12
    assert stopped["pending_steps"] == 9
    ledger = Ledger(paths.ledger)
    rows = ledger.steps_for_run(stopped["run_id"])
    assert len(rows) == 3
    assert all(row["attempts"] == 1 for row in rows)

    resumed = execute_plan(plan, registry.initial_state(), paths, STAMP,
                           resume=True)
    assert resumed["status"] == "complete"
    assert resumed["succeeded_steps"] == 12
    rows = ledger.steps_for_run(stopped["run_id"])
    assert len(rows) == 12
    assert len({row["step_id"] for row in rows}) == 12
    assert all(row["attempts"] == 1 for row in rows)
    before_attempts = [row["attempts"] for row in rows]

    replayed = execute_plan(plan, registry.initial_state(), paths, STAMP)
    assert replayed["status"] == "complete"
    assert replayed["succeeded_steps"] == 12
    rows_after = ledger.steps_for_run(stopped["run_id"])
    assert [row["attempts"] for row in rows_after] == before_attempts
    assert len(rows_after) == 12


def test_hash_chain_and_artifacts(registry, request_one, tmp_path) -> None:
    paths, plan = _prepare(registry, request_one, tmp_path)
    result = execute_plan(plan, registry.initial_state(), paths, STAMP)
    ledger = Ledger(paths.ledger)
    run = ledger.get_run(result["run_id"])
    rows = ledger.steps_for_run(result["run_id"])
    assert rows[0]["before_hash"] == run["initial_hash"]
    for previous, current in zip(rows, rows[1:]):
        assert current["before_hash"] == previous["after_hash"]
    assert rows[-1]["after_hash"] == run["final_hash"]
    artifact_rows = ledger.artifacts_for_run(result["run_id"])
    assert len(artifact_rows) == len(rows)
    for row in rows:
        assert (paths.root / row["artifact_path"]).exists()
    snapshot = ledger.snapshot(result["run_id"])
    assert set(snapshot) == {"run", "steps", "artifacts"}
    assert run["final_hash"] == result["final_state_hash"]
