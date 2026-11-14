"""Configuration, paths and small shared constants for swapdesk."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "swapdesk"
VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = "data"
DEFAULT_WORKDIR = "var"
DEMO_TIMESTAMP = "2026-01-15T00:00:00Z"
DEFAULT_REQUEST = "req-001.yaml"

SOURCE_KINDS = ("database", "api", "file", "stream")
SOURCE_STATUSES = ("active", "retiring", "retired")
TASK_STATUSES = ("active", "paused", "retired")
DOWNSTREAM_KINDS = ("table", "view", "model", "report")
DOWNSTREAM_STATUSES = ("active", "retired")
ENTITY_TYPES = ("source", "task", "downstream", "metric")

SEVERITIES = ("blocking", "warning", "info")
SEVERITY_RANK = {"blocking": 0, "warning": 1, "info": 2}

REGISTRY_FILES = {
    "sources": "sources.yaml",
    "tasks": "tasks.yaml",
    "downstream": "downstream.yaml",
    "lineage": "lineage.yaml",
    "metrics": "metrics.yaml",
}

REPORT_SUFFIXES = (".html", ".md")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_timestamp(value: str | None = None) -> str:
    """Return an explicit timestamp, allowing deterministic test/demo runs."""
    if value:
        return normalize_timestamp(value)
    env_value = os.environ.get("SWAPDESK_TIMESTAMP")
    if env_value:
        return normalize_timestamp(env_value)
    return utc_now()


def normalize_timestamp(value: str) -> str:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WorkPaths:
    root: Path
    fixtures: Path
    runs: Path
    reports: Path
    plans: Path
    analysis: Path
    state: Path
    ledger: Path

    def ensure(self) -> "WorkPaths":
        for path in (self.root, self.fixtures, self.runs, self.reports,
                     self.plans, self.analysis, self.state):
            path.mkdir(parents=True, exist_ok=True)
        return self


def resolve_workdir(value: str | Path | None = None) -> Path:
    raw = Path(value) if value is not None else Path(DEFAULT_WORKDIR)
    return raw.expanduser().resolve()


def work_paths(value: str | Path | None = None) -> WorkPaths:
    root = resolve_workdir(value)
    return WorkPaths(
        root=root,
        fixtures=root / "fixtures",
        runs=root / "runs",
        reports=root / "reports",
        plans=root / "plans",
        analysis=root / "analysis",
        state=root / "state",
        ledger=root / "ledger.sqlite3",
    ).ensure()


def demo_workdir(value: str | Path | None = None) -> Path:
    """Demo writes directly under the selected workspace, defaulting to ./var."""
    return resolve_workdir(value)
