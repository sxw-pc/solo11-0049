"""Per-consumer candidate tier selection (选档) for swap requests.

Each affected task/metric picks, for every retired source it references, the
highest-priority candidate tier whose fields — combined with the consumer's
other chosen candidates and its remaining sources — still cover the
consumer's own ``output_fields`` / ``required_fields``. When no tier
combination covers everything, the consumer degrades to the combination with
the fewest missing fields and the shortfall is recorded.

Shared entities (downstream tables, lineage edges) have no field
requirements of their own; they follow a deterministic global mapping: the
minimum tier chosen by any consumer of that source, defaulting to tier 1.
"""
from __future__ import annotations

from itertools import product
from typing import Any

from ..model.entities import Registry, SwapRequest


def usable_candidates(registry: Registry,
                      request: SwapRequest) -> dict[str, list[str]]:
    """Ordered, de-duplicated candidates resolving to another registered source."""
    usable: dict[str, list[str]] = {}
    for item in request.retire:
        resolved: list[str] = []
        for candidate in item.candidates:
            if candidate == item.source or candidate in resolved:
                continue
            if candidate not in registry.source_map:
                continue
            resolved.append(candidate)
        usable[item.source] = resolved
    return usable


def _other_fields(registry: Registry, refs: tuple[str, ...] | list[str],
                  retired: set[str]) -> set[str]:
    fields: set[str] = set()
    for ref in refs:
        if ref in retired:
            continue
        source = registry.source_map.get(ref)
        if source is not None:
            fields.update(source.fields)
    return fields


def _combinations(registry: Registry, usable: dict[str, list[str]],
                  sources: list[str], other_fields: set[str],
                  required_fields: tuple[str, ...] | list[str]
                  ) -> list[dict[str, Any]]:
    cand_lists = [usable[source] for source in sources]
    combos: list[dict[str, Any]] = []
    for picks in product(*(range(len(cands)) for cands in cand_lists)):
        tiers = tuple(pick + 1 for pick in picks)
        chosen = tuple(cand_lists[index][pick] for index, pick in enumerate(picks))
        available = set(other_fields)
        for candidate in chosen:
            available.update(registry.source_map[candidate].fields)
        missing = sorted(set(required_fields) - available)
        combos.append({"tiers": tiers, "chosen": chosen, "missing": missing})
    return combos


def select_consumer_tiers(registry: Registry, usable: dict[str, list[str]],
                          retired_refs: list[str], other_fields: set[str],
                          required_fields: tuple[str, ...] | list[str]
                          ) -> dict[str, Any] | None:
    """Pick the best tier combination for one consumer.

    Returns None when some retired source has no usable candidate; the
    conflict rules report that separately (C_REPLACEMENT_MISSING).
    """
    sources = sorted(retired_refs)
    if not sources or any(not usable.get(source) for source in sources):
        return None
    combos = _combinations(registry, usable, sources, other_fields,
                           required_fields)
    full = [combo for combo in combos if not combo["missing"]]
    if full:
        best = min(full, key=lambda combo: (sum(combo["tiers"]), combo["tiers"]))
        covered = True
    else:
        best = min(combos, key=lambda combo: (len(combo["missing"]),
                                              sum(combo["tiers"]),
                                              combo["tiers"]))
        covered = False
    primary = next(combo for combo in combos
                   if all(tier == 1 for tier in combo["tiers"]))
    max_tier = max(best["tiers"])
    if covered and max_tier == 1:
        reason = "首选档字段覆盖完整"
    elif covered:
        reason = (f"首选档缺字段 {', '.join(primary['missing'])}，"
                  f"退到第 {max_tier} 档后覆盖完整")
    else:
        reason = (f"所有候选档组合仍缺字段 {', '.join(best['missing'])}，"
                  f"退到第 {max_tier} 档（缺失最少组合）")
    choices = {
        source: {"candidate": best["chosen"][index], "tier": best["tiers"][index]}
        for index, source in enumerate(sources)
    }
    return {
        "choices": choices,
        "mapping": {source: choice["candidate"]
                    for source, choice in choices.items()},
        "covered": covered,
        "degraded": max_tier > 1,
        "missing_fields": best["missing"],
        "primary_missing": primary["missing"],
        "reason": reason,
    }


def _consumer_entry(kind: str, consumer_id: str, removed: list[str],
                    required_fields: tuple[str, ...],
                    selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "consumer_type": kind,
        "consumer_id": consumer_id,
        "required_fields": list(required_fields),
        "retired_refs": list(removed),
        "choices": selection["choices"],
        "mapping": selection["mapping"],
        "covered": selection["covered"],
        "degraded": selection["degraded"],
        "missing_fields": selection["missing_fields"],
        "primary_missing": selection["primary_missing"],
        "reason": selection["reason"],
    }


def select_tiers(registry: Registry, request: SwapRequest) -> dict[str, Any]:
    """Run tier selection for every affected task and metric.

    The result is deterministic: consumers are scanned in sorted order and
    combination search prefers lower tier sums, then lexicographic tiers.
    """
    usable = usable_candidates(registry, request)
    retired = set(request.retired_sources)
    consumers: list[dict[str, Any]] = []
    for task in sorted(registry.tasks, key=lambda item: item.id):
        removed = sorted(set(task.source_refs) & retired)
        if not removed:
            continue
        other = _other_fields(registry, task.source_refs, retired)
        selection = select_consumer_tiers(registry, usable, removed, other,
                                          task.output_fields)
        if selection is not None:
            consumers.append(_consumer_entry("task", task.id, removed,
                                             task.output_fields, selection))
    for metric in sorted(registry.metrics, key=lambda item: item.id):
        removed = sorted(set(metric.source_refs) & retired)
        if not removed:
            continue
        other = _other_fields(registry, metric.source_refs, retired)
        selection = select_consumer_tiers(registry, usable, removed, other,
                                          metric.required_fields)
        if selection is not None:
            consumers.append(_consumer_entry("metric", metric.id, removed,
                                             metric.required_fields, selection))
    global_choices: dict[str, dict[str, Any]] = {}
    for item in sorted(request.retire, key=lambda entry: entry.source):
        candidates = usable[item.source]
        if not candidates:
            continue
        chosen_tiers = [consumer["choices"][item.source]["tier"]
                        for consumer in consumers
                        if item.source in consumer["choices"]]
        if chosen_tiers:
            tier = min(chosen_tiers)
            basis = "消费方最小档位"
        else:
            tier = 1
            basis = "无消费方引用，取首选档"
        global_choices[item.source] = {
            "candidate": candidates[tier - 1], "tier": tier, "basis": basis,
        }
    return {
        "consumers": consumers,
        "global_choices": global_choices,
        "global_mapping": {source: choice["candidate"]
                           for source, choice in sorted(global_choices.items())},
    }
