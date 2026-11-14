"""Conflict severity helpers shared by analysis, planning and reports."""
from __future__ import annotations

from typing import Any

from ..config import SEVERITIES, SEVERITY_RANK


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
