from __future__ import annotations

from copy import deepcopy

from swapdesk.analysis.impact import analyze_impact
from swapdesk.fixtures.generator import fixture_documents, fixture_requests
from swapdesk.io.jsonout import canonical_json
from swapdesk.model.entities import Registry, SwapRequest
from swapdesk.model.schema import validate_registry_documents


def test_direct_indirect_chain_and_partial(registry, request_one) -> None:
    impact = analyze_impact(registry, request_one)
    tasks = {item["id"]: item for item in impact["affected"]["tasks"]}
    downstream = {item["id"]: item for item in impact["affected"]["downstream"]}
    metrics = {item["id"]: item for item in impact["affected"]["metrics"]}
    assert impact["counts"] == {"tasks": 3, "downstream": 6, "metrics": 3}
    assert tasks["task-orders-ingest"]["impact"] == "direct"
    assert tasks["task-orders-enrich"]["partial"] is True
    assert tasks["task-orders-enrich"]["remaining_sources"] == ["src-gamma"]
    assert downstream["dwd-orders"]["impact"] == "direct"
    assert downstream["ads-revenue"]["impact"] == "indirect"
    assert downstream["ads-revenue"]["path"] == [
        "src-alpha", "dwd-orders", "dws-order-daily", "ads-revenue",
    ]
    assert downstream["dwd-order-events"]["path"] == [
        "src-alpha", "dwd-orders", "dwd-order-events",
    ]
    assert metrics["metric-revenue"]["retired_refs"] == ["src-alpha"]
    assert metrics["metric-orders-count"]["impact"] == "indirect"


def test_lineage_cycle_is_reported_and_truncated() -> None:
    docs = deepcopy(fixture_documents())
    docs["lineage"].append({
        "id": "lin-cycle",
        "from_ref": "dwd-order-events",
        "to_ref": "dwd-orders",
        "fields": ["order_id"],
        "relation": "derived",
    })
    assert validate_registry_documents(docs) == []
    registry = Registry.from_documents(docs)
    req = SwapRequest.from_dict(fixture_requests()["req-001"])
    impact = analyze_impact(registry, req)
    warning = next(w for w in impact["warnings"]
                   if w["rule_id"] == "W_LINEAGE_CYCLE")
    assert warning["evidence"]["nodes"][0] == warning["evidence"]["nodes"][-1]
    assert warning["severity"] == "warning"
    assert len(impact["affected"]["tasks"]) == 3


def test_impact_is_deterministic(registry, request_one) -> None:
    first = analyze_impact(registry, request_one)
    second = analyze_impact(registry, request_one)
    assert canonical_json(first) == canonical_json(second)
    assert first["impact_hash"] == second["impact_hash"]
