"""Impact and conflict analysis."""
from __future__ import annotations

from typing import Any

from ..config import SCHEMA_VERSION
from ..io.jsonout import sha256_json
from ..model.entities import Registry, SwapRequest
from .conflicts import analyze_conflicts
from .impact import analyze_impact, analysis_fingerprint
from .selection import select_tiers


def run_analysis(registry: Registry, request: SwapRequest) -> dict[str, Any]:
    impact = analyze_impact(registry, request)
    selections = select_tiers(registry, request)
    conflict_result = analyze_conflicts(registry, request, impact, selections)
    fingerprint = analysis_fingerprint(impact, conflict_result["conflicts"])
    summary = dict(conflict_result["summary"])
    summary["impacted"] = impact["counts"]
    summary["analysis_warnings"] = len(impact["warnings"])
    result = {
        "schema_version": SCHEMA_VERSION,
        "request": request.to_dict(),
        "impact": impact,
        "conflicts": conflict_result["conflicts"],
        "summary": summary,
        "analysis_hash": fingerprint,
    }
    if request.uses_candidates:
        result["selections"] = selections
    result["result_hash"] = sha256_json(result)
    return result
