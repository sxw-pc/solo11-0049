"""Deterministic synthetic registries and swap requests.

Every host is the loopback placeholder 127.0.0.1. No name, key, request or
address in this file refers to a real system.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config import REGISTRY_FILES
from ..model.schema import validate_registry_documents, validate_request_document

HOST = "127.0.0.1"
FIXED_TIME = "2026-01-10T09:00:00Z"


def fixture_documents(seed: int = 11) -> dict[str, list[dict[str, Any]]]:
    int(seed)  # accepted for API stability; synthetic catalogue is fixed
    sources = [
        {"id": "src-alpha", "name": "上游源-A", "kind": "database",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 15432,
         "owner": "team-ingest",
         "description": "合成订单源，仅用于离线演示",
         "fields": ["order_id", "customer_id", "amount", "currency", "created_at"]},
        {"id": "src-alpha-v2", "name": "上游源-A-替代", "kind": "database",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 15433,
         "owner": "team-ingest", "description": "合成替代数据库",
         "fields": ["order_id", "customer_id", "amount", "currency",
                    "created_at", "load_ts"]},
        {"id": "src-beta", "name": "上游源-B", "kind": "api",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 18080,
         "owner": "team-events", "description": "合成事件接口",
         "fields": ["event_id", "user_id", "event_type", "occurred_at"]},
        {"id": "src-beta-v2", "name": "上游源-B-替代", "kind": "api",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 18081,
         "owner": "team-events", "description": "合成替代事件接口",
         "fields": ["event_id", "user_id", "event_type", "occurred_at"]},
        {"id": "src-beta-lite", "name": "上游源-B-精简", "kind": "api",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 18082,
         "owner": "team-events", "description": "缺少 event_type 的合成接口",
         "fields": ["event_id", "user_id"]},
        {"id": "src-gamma", "name": "上游源-C", "kind": "stream",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 19090,
         "owner": "team-iot", "description": "合成设备流",
         "fields": ["device_id", "reading", "unit"]},
        {"id": "src-delta", "name": "上游源-D", "kind": "file",
         "status": "active", "endpoint_host": HOST, "endpoint_port": 0,
         "owner": "team-ops", "description": "合成工单文件源",
         "fields": ["ticket_id", "status", "updated_at"]},
    ]
    tasks = [
        {"id": "task-orders-ingest", "name": "订单采集",
         "source_refs": ["src-alpha"],
         "output_fields": ["order_id", "amount", "created_at"],
         "schedule": "every 10m", "status": "active", "owner": "team-ingest"},
        {"id": "task-orders-enrich", "name": "订单设备补全",
         "source_refs": ["src-alpha", "src-gamma"],
         "output_fields": ["order_id", "device_id", "amount"],
         "schedule": "every 15m", "status": "active", "owner": "team-ingest"},
        {"id": "task-events-ingest", "name": "事件采集",
         "source_refs": ["src-beta"],
         "output_fields": ["event_id", "event_type", "occurred_at"],
         "schedule": "every 5m", "status": "active", "owner": "team-events"},
        {"id": "task-device-metrics", "name": "设备读数采集",
         "source_refs": ["src-gamma"],
         "output_fields": ["device_id", "reading", "unit"],
         "schedule": "every 1m", "status": "active", "owner": "team-iot"},
        {"id": "task-ticket-sync", "name": "工单同步",
         "source_refs": ["src-delta"],
         "output_fields": ["ticket_id", "status", "updated_at"],
         "schedule": "every 30m", "status": "active", "owner": "team-ops"},
    ]
    downstream = [
        {"id": "dwd-orders", "name": "订单明细", "kind": "table",
         "upstream_refs": ["task-orders-ingest", "task-orders-enrich"],
         "status": "active", "owner": "team-warehouse"},
        {"id": "raw-order-extract", "name": "订单原始抽取", "kind": "view",
         "upstream_refs": ["src-alpha"], "status": "active",
         "owner": "team-warehouse"},
        {"id": "dwd-order-events", "name": "订单事件宽表", "kind": "table",
         "upstream_refs": ["dwd-orders", "task-events-ingest"],
         "status": "active", "owner": "team-warehouse"},
        {"id": "dws-order-daily", "name": "订单日汇总", "kind": "model",
         "upstream_refs": ["dwd-orders"], "status": "active",
         "owner": "team-analytics"},
        {"id": "ads-revenue", "name": "收入看板", "kind": "report",
         "upstream_refs": ["dws-order-daily"], "status": "active",
         "owner": "team-analytics"},
        {"id": "ads-event-funnel", "name": "事件漏斗", "kind": "report",
         "upstream_refs": ["dwd-order-events"], "status": "active",
         "owner": "team-analytics"},
        {"id": "dwd-devices", "name": "设备明细", "kind": "table",
         "upstream_refs": ["task-device-metrics"], "status": "active",
         "owner": "team-warehouse"},
        {"id": "ops-tickets", "name": "运维工单视图", "kind": "view",
         "upstream_refs": ["task-ticket-sync"], "status": "active",
         "owner": "team-ops"},
    ]
    lineage = [
        {"id": "lin-001", "from_ref": "src-alpha", "to_ref": "task-orders-ingest",
         "fields": ["order_id", "amount"], "relation": "derived"},
        {"id": "lin-002", "from_ref": "task-orders-ingest", "to_ref": "dwd-orders",
         "fields": ["order_id"], "relation": "derived"},
        {"id": "lin-003", "from_ref": "dwd-orders", "to_ref": "dws-order-daily",
         "fields": ["order_id"], "relation": "derived"},
        {"id": "lin-004", "from_ref": "dws-order-daily", "to_ref": "ads-revenue",
         "fields": ["amount"], "relation": "derived"},
        {"id": "lin-005", "from_ref": "src-beta", "to_ref": "task-events-ingest",
         "fields": ["event_id"], "relation": "derived"},
        {"id": "lin-006", "from_ref": "task-events-ingest", "to_ref": "dwd-order-events",
         "fields": ["event_id"], "relation": "derived"},
        {"id": "lin-007", "from_ref": "src-alpha", "to_ref": "dwd-orders",
         "fields": ["order_id"], "relation": "derived"},
        {"id": "lin-008", "from_ref": "src-gamma", "to_ref": "task-device-metrics",
         "fields": ["device_id"], "relation": "derived"},
        {"id": "lin-009", "from_ref": "task-orders-enrich", "to_ref": "dwd-orders",
         "fields": ["order_id"], "relation": "derived"},
        {"id": "lin-010", "from_ref": "dwd-order-events", "to_ref": "ads-event-funnel",
         "fields": ["event_id"], "relation": "derived"},
    ]
    metrics = [
        {"id": "metric-revenue", "name": "订单收入",
         "source_refs": ["src-alpha", "dwd-orders"],
         "required_fields": ["order_id", "amount"], "required": True,
         "owner": "team-analytics", "definition": "按订单汇总的收入口径"},
        {"id": "metric-orders-count", "name": "订单计数",
         "source_refs": ["dwd-orders"], "required_fields": ["order_id"],
         "required": False, "owner": "team-analytics",
         "definition": "订单明细行数"},
        {"id": "metric-event-funnel", "name": "事件转化漏斗",
         "source_refs": ["src-beta", "dwd-order-events"],
         "required_fields": ["event_id", "event_type"], "required": True,
         "owner": "team-events", "definition": "按事件类型统计的漏斗口径"},
        {"id": "metric-device-readings", "name": "设备读数均值",
         "source_refs": ["src-gamma"], "required_fields": ["device_id", "reading"],
         "required": True, "owner": "team-iot", "definition": "设备读数聚合口径"},
        {"id": "metric-ticket-backlog", "name": "工单积压",
         "source_refs": ["src-delta"], "required_fields": ["ticket_id", "status"],
         "required": True, "owner": "team-ops", "definition": "未关闭工单计数"},
    ]
    return {"sources": sources, "tasks": tasks, "downstream": downstream,
            "lineage": lineage, "metrics": metrics}


def fixture_requests() -> dict[str, dict[str, Any]]:
    return {
        "req-001": {
            "request_id": "req-001",
            "title": "订单与事件源替换（可执行样例）",
            "created_at": FIXED_TIME,
            "owner": "data-platform",
            "retire": [
                {"source": "src-alpha", "replacement": "src-alpha-v2"},
                {"source": "src-beta", "replacement": "src-beta-v2"},
            ],
            "window": {"start": "2026-02-01T00:00:00Z",
                       "end": "2026-02-05T23:59:59Z"},
            "other_windows": [
                {"id": "chg-114", "start": "2026-02-03T00:00:00Z",
                 "end": "2026-02-04T00:00:00Z"},
            ],
            "max_parallel": 2,
            "notes": "全部为合成数据；先改引用，再退役源。",
        },
        "req-002": {
            "request_id": "req-002",
            "title": "字段不完整与替代源缺失（阻断样例）",
            "created_at": "2026-01-11T10:30:00Z",
            "owner": "data-platform",
            "retire": [
                {"source": "src-alpha", "replacement": "src-omega-absent"},
                {"source": "src-beta", "replacement": "src-beta-lite"},
            ],
            "window": {"start": "2026-02-10T00:00:00Z",
                       "end": "2026-02-12T00:00:00Z"},
            "other_windows": [],
            "max_parallel": 1,
            "notes": "用于演示 blocking 与证据引用，不允许生成执行计划。",
        },
        "req-003": {
            "request_id": "req-003",
            "title": "候选档撤换（首选精简、保底全量）",
            "created_at": "2026-01-12T08:00:00Z",
            "owner": "data-platform",
            "retire": [
                {"source": "src-alpha", "candidates": ["src-alpha-v2"]},
                {"source": "src-beta",
                 "candidates": ["src-beta-lite", "src-beta-v2"]},
            ],
            "window": {"start": "2026-02-15T00:00:00Z",
                       "end": "2026-02-20T23:59:59Z"},
            "other_windows": [],
            "max_parallel": 2,
            "notes": "首选精简源；字段覆盖不足的消费方自动退到保底档。",
        },
    }


def _dump(value: Any) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def write_fixtures(base: str | Path, seed: int = 11) -> list[Path]:
    root = Path(base)
    root.mkdir(parents=True, exist_ok=True)
    (root / "swap_requests").mkdir(parents=True, exist_ok=True)
    docs = fixture_documents(seed)
    issues = validate_registry_documents(docs)
    if issues:
        raise ValueError(f"generated fixture is invalid: {issues[0]}")
    paths: list[Path] = []
    for key, filename in REGISTRY_FILES.items():
        path = root / filename
        path.write_text(_dump(docs[key]), encoding="utf-8", newline="\n")
        paths.append(path)
    for request_id, request in fixture_requests().items():
        issues = validate_request_document(request)
        if issues:
            raise ValueError(f"generated request {request_id} is invalid: {issues[0]}")
        path = root / "swap_requests" / f"{request_id}.yaml"
        path.write_text(_dump(request), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def write_docs() -> dict[str, Path]:
    """Write the canonical repository fixture files next to this package."""
    repo = Path(__file__).resolve().parents[2]
    written = write_fixtures(repo / "data")
    return {path.name: path for path in written}
