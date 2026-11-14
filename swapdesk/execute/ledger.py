"""SQLite execution ledger with content hashes and artifacts."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, SwapDeskError

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_id TEXT NOT NULL,
  plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL, parent_run_id TEXT DEFAULT '',
  status TEXT NOT NULL, initial_hash TEXT NOT NULL, final_hash TEXT DEFAULT '',
  started_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  batch INTEGER NOT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL,
  target_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
  before_hash TEXT DEFAULT '', after_hash TEXT DEFAULT '',
  artifact_path TEXT DEFAULT '', error_code TEXT DEFAULT '',
  error_message TEXT DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  run_id TEXT NOT NULL, step_id TEXT NOT NULL, kind TEXT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, step_id, kind)
);
"""


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?",
                               (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at, run_id").fetchall()
        return [dict(row) for row in rows]

    def start_run(self, *, run_id: str, kind: str, request_id: str,
                  plan_id: str, plan_hash: str, initial_hash: str,
                  timestamp: str, parent_run_id: str = "") -> bool:
        existing = self.get_run(run_id)
        if existing:
            if existing["plan_hash"] != plan_hash or existing["kind"] != kind:
                raise SwapDeskError(
                    "同一 run_id 的计划指纹不一致",
                    code=ErrorCode.RUN_CONFLICT, record_id=run_id,
                    details={"expected": existing["plan_hash"],
                             "actual": plan_hash},
                )
            return False
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, kind, request_id, plan_id, plan_hash, "
                "parent_run_id, status, initial_hash, final_hash, started_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, '', ?, ?)",
                (run_id, kind, request_id, plan_id, plan_hash, parent_run_id,
                 initial_hash, timestamp, timestamp),
            )
        return True

    def update_run(self, run_id: str, *, timestamp: str, status: str | None = None,
                   final_hash: str | None = None) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise SwapDeskError("台账中不存在该运行",
                                code=ErrorCode.RUN_NOT_FOUND, record_id=run_id)
        values = {
            "status": status if status is not None else run["status"],
            "final_hash": final_hash if final_hash is not None else run["final_hash"],
            "updated_at": timestamp,
        }
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, final_hash = ?, updated_at = ? "
                "WHERE run_id = ?",
                (values["status"], values["final_hash"], values["updated_at"], run_id),
            )

    def get_step(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
        return dict(row) if row else None

    def begin_step(self, run_id: str, step: dict[str, Any], ordinal: int,
                   timestamp: str) -> None:
        existing = self.get_step(run_id, step["step_id"])
        attempts = 1 if existing is None else int(existing["attempts"]) + 1
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO steps (run_id, step_id, ordinal, batch, action, "
                "target_type, target_id, status, attempts, before_hash, after_hash, "
                "artifact_path, error_code, error_message, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, '', '', '', '', '', ?) "
                "ON CONFLICT(run_id, step_id) DO UPDATE SET status = 'running', "
                "attempts = ?, before_hash = '', after_hash = '', artifact_path = '', "
                "error_code = '', error_message = '', updated_at = ?",
                (run_id, step["step_id"], ordinal, step["batch"], step["action"],
                 step["target_type"], step["target_id"], attempts, timestamp,
                 attempts, timestamp),
            )

    def finish_step(self, run_id: str, step_id: str, *, before_hash: str,
                    after_hash: str, artifact_path: str, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE steps SET status = 'succeeded', before_hash = ?, "
                "after_hash = ?, artifact_path = ?, error_code = '', "
                "error_message = '', updated_at = ? WHERE run_id = ? AND step_id = ?",
                (before_hash, after_hash, artifact_path, timestamp, run_id, step_id),
            )

    def fail_step(self, run_id: str, step_id: str, *, error_code: str,
                  error_message: str, before_hash: str, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE steps SET status = 'failed', before_hash = ?, "
                "error_code = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND step_id = ?",
                (before_hash, error_code, error_message, timestamp, run_id, step_id),
            )

    def record_artifact(self, run_id: str, step_id: str, kind: str, path: str,
                        sha256: str, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts (run_id, step_id, kind, path, "
                "sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, step_id, kind, path, sha256, timestamp),
            )

    def steps_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY ordinal, step_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY step_id, kind",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise SwapDeskError("台账中不存在该运行",
                                code=ErrorCode.RUN_NOT_FOUND, record_id=run_id)
        return {
            "run": run,
            "steps": self.steps_for_run(run_id),
            "artifacts": self.artifacts_for_run(run_id),
        }
