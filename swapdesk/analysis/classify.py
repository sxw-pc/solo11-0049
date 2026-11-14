"""Conflict severity helpers shared by analysis, planning and reports."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from ..config import SEVERITIES, SEVERITY_RANK

if TYPE_CHECKING:
    from ..model.entities import LineageEdge, Source


def make_conflict(rule_id: str, severity: str, target_type: str, target_id: str,
                  message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity}")
    return {
        "rule_id": rule_id,
        "severity": severity,
        "target": {"type": target_type, "id": target_id},
        "message": message,
        "evidence": evidence or {},
    }


def sort_conflicts(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        conflicts,
        key=lambda c: (
            SEVERITY_RANK.get(c["severity"], 9),
            c["rule_id"],
            c["target"]["type"],
            c["target"]["id"],
            c["message"],
        ),
    )


def summarize(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {name: 0 for name in SEVERITIES}
    for conflict in conflicts:
        counts[conflict["severity"]] = counts.get(conflict["severity"], 0) + 1
    blocking = counts.get("blocking", 0)
    executable = blocking == 0
    return {
        "counts": counts,
        "blocking_rule_ids": sorted({c["rule_id"] for c in conflicts
                                     if c["severity"] == "blocking"}),
        "executable": executable,
        "halt_reason": "" if executable else "blocking conflicts present",
    }


def attribute_field_gaps(target_id: str,
                         required_fields: Iterable[str],
                         source_refs: Iterable[str],
                         retired_refs: Iterable[str],
                         replacements: dict[str, "Source"],
                         source_map: dict[str, "Source"],
                         lineage: Iterable["LineageEdge"]) -> dict[str, Any]:
    """Attribute required-field gaps to the target's retired/kept source set.

    Availability is computed per field as the intersection of the target's
    effective sources (retired refs swapped for their replacements, kept refs
    retained) with the sources registering that field — never as a union over
    every registered source. For each gap the evidence records which retired
    sources provided the field, whether their replacements register it, and
    which relevant lineage edges carry the field in their ``fields`` list.

    A field is a ``source`` gap when no effective source registers it, and an
    ``edge`` gap when it is registered but no relevant lineage edge carries
    it. All returned lists are sorted so analysis hashes stay reproducible.
    """
    retired = sorted(set(retired_refs))
    effective: dict[str, Source] = {}
    for ref in source_refs:
        source = replacements.get(ref) or source_map.get(ref)
        if source is not None:
            effective[source.id] = source
    relevant_from = set(source_refs) | {src.id for src in replacements.values()}
    edges = [edge for edge in lineage
             if edge.to_ref == target_id and edge.from_ref in relevant_from]
    evidence: list[dict[str, Any]] = []
    for name in sorted(set(required_fields)):
        providers = sorted(sid for sid, source in effective.items()
                           if name in source.fields)
        retired_providers = [rid for rid in retired
                             if source_map.get(rid) is not None
                             and name in source_map[rid].fields]
        on_edges = sorted(edge.id for edge in edges if name in edge.fields)
        if not providers:
            gap = "source"
        elif not on_edges:
            gap = "edge"
        else:
            continue
        if not providers:
            coverage = "none"
        elif len(providers) == 1:
            coverage = "sole"
        else:
            coverage = "shared"
        evidence.append({
            "field": name,
            "gap": gap,
            "retired_providers": retired_providers,
            "replacement_registered": {
                rid: rid in replacements and name in replacements[rid].fields
                for rid in retired_providers
            },
            "covered_by": providers,
            "coverage": coverage,
            "lineage_edges": on_edges,
        })
    missing = [item["field"] for item in evidence if item["gap"] == "source"]
    edge_missing = [item["field"] for item in evidence if item["gap"] == "edge"]
    return {
        "effective_sources": sorted(effective),
        "missing_fields": missing,
        "edge_missing_fields": edge_missing,
        "field_evidence": evidence,
        "has_source_gap": bool(missing),
        "has_edge_gap": bool(edge_missing),
    }


def gap_severity(has_source_gap: bool, *, blocking_capable: bool = True) -> str:
    """Grade a field-gap conflict by its strongest evidence.

    Source-level gaps stay blocking (for blocking-capable targets); edge-only
    evidence downgrades to warning. Mixed evidence keeps the source-level
    grade because severity follows the strongest evidence.
    """
    if has_source_gap and blocking_capable:
        return "blocking"
    return "warning"
