"""Typed registration entities and the swap request."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    kind: str
    status: str
    fields: tuple[str, ...]
    owner: str = ""
    description: str = ""
    endpoint_host: str = "127.0.0.1"
    endpoint_port: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        return cls(
            id=str(data["id"]), name=str(data["name"]), kind=str(data["kind"]),
            status=str(data["status"]), fields=_tuple(data.get("fields")),
            owner=str(data.get("owner", "")),
            description=str(data.get("description", "")),
            endpoint_host=str(data.get("endpoint_host", "127.0.0.1")),
            endpoint_port=int(data.get("endpoint_port", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "status": self.status, "fields": list(self.fields),
                "owner": self.owner, "description": self.description,
                "endpoint_host": self.endpoint_host,
                "endpoint_port": self.endpoint_port}


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    source_refs: tuple[str, ...]
    output_fields: tuple[str, ...]
    schedule: str
    status: str
    owner: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]), name=str(data["name"]),
            source_refs=_tuple(data.get("source_refs")),
            output_fields=_tuple(data.get("output_fields")),
            schedule=str(data.get("schedule", "")),
            status=str(data["status"]), owner=str(data.get("owner", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "source_refs": list(self.source_refs),
                "output_fields": list(self.output_fields),
                "schedule": self.schedule, "status": self.status,
                "owner": self.owner}


@dataclass(frozen=True)
class DownstreamTable:
    id: str
    name: str
    kind: str
    upstream_refs: tuple[str, ...]
    status: str
    owner: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownstreamTable":
        return cls(
            id=str(data["id"]), name=str(data["name"]),
            kind=str(data["kind"]),
            upstream_refs=_tuple(data.get("upstream_refs")),
            status=str(data["status"]), owner=str(data.get("owner", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "upstream_refs": list(self.upstream_refs),
                "status": self.status, "owner": self.owner}


@dataclass(frozen=True)
class LineageEdge:
    id: str
    from_ref: str
    to_ref: str
    fields: tuple[str, ...] = ()
    relation: str = "derived"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LineageEdge":
        return cls(
            id=str(data["id"]), from_ref=str(data["from_ref"]),
            to_ref=str(data["to_ref"]), fields=_tuple(data.get("fields")),
            relation=str(data.get("relation", "derived")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "from_ref": self.from_ref, "to_ref": self.to_ref,
                "fields": list(self.fields), "relation": self.relation}


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    source_refs: tuple[str, ...]
    required_fields: tuple[str, ...]
    required: bool
    owner: str = ""
    definition: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Metric":
        return cls(
            id=str(data["id"]), name=str(data["name"]),
            source_refs=_tuple(data.get("source_refs")),
            required_fields=_tuple(data.get("required_fields")),
            required=bool(data.get("required", False)),
            owner=str(data.get("owner", "")),
            definition=str(data.get("definition", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "source_refs": list(self.source_refs),
                "required_fields": list(self.required_fields),
                "required": self.required, "owner": self.owner,
                "definition": self.definition}


@dataclass(frozen=True)
class Retirement:
    source: str
    replacement: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Retirement":
        return cls(source=str(data["source"]),
                   replacement=str(data.get("replacement", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "replacement": self.replacement}


@dataclass(frozen=True)
class Window:
    start: str
    end: str
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], window_id: str = "") -> "Window":
        return cls(start=str(data["start"]), end=str(data["end"]),
                   id=str(data.get("id", window_id)))

    def to_dict(self) -> dict[str, Any]:
        data = {"start": self.start, "end": self.end}
        if self.id:
            data["id"] = self.id
        return data


@dataclass(frozen=True)
class SwapRequest:
    request_id: str
    title: str
    created_at: str
    retire: tuple[Retirement, ...]
    window: Window
    other_windows: tuple[Window, ...] = ()
    max_parallel: int = 2
    owner: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SwapRequest":
        retire = tuple(Retirement.from_dict(item) for item in data.get("retire", []))
        other = tuple(Window.from_dict(item, str(item.get("id", "")))
                      for item in data.get("other_windows", []))
        return cls(
            request_id=str(data["request_id"]), title=str(data["title"]),
            created_at=str(data["created_at"]), retire=retire,
            window=Window.from_dict(data["window"]),
            other_windows=other,
            max_parallel=int(data.get("max_parallel", 2)),
            owner=str(data.get("owner", "")), notes=str(data.get("notes", "")),
        )

    @property
    def retired_sources(self) -> tuple[str, ...]:
        return tuple(item.source for item in self.retire)

    @property
    def replacement_map(self) -> dict[str, str]:
        return {item.source: item.replacement for item in self.retire}

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "title": self.title,
                "created_at": self.created_at,
                "retire": [item.to_dict() for item in self.retire],
                "window": self.window.to_dict(),
                "other_windows": [item.to_dict() for item in self.other_windows],
                "max_parallel": self.max_parallel, "owner": self.owner,
                "notes": self.notes}

@dataclass
class Registry:
    sources: tuple[Source, ...]
    tasks: tuple[Task, ...]
    downstream: tuple[DownstreamTable, ...]
    lineage: tuple[LineageEdge, ...]
    metrics: tuple[Metric, ...]
    source_map: dict[str, Source] = field(init=False, repr=False)
    task_map: dict[str, Task] = field(init=False, repr=False)
    downstream_map: dict[str, DownstreamTable] = field(init=False, repr=False)
    metric_map: dict[str, Metric] = field(init=False, repr=False)
    lineage_map: dict[str, LineageEdge] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.source_map = {item.id: item for item in self.sources}
        self.task_map = {item.id: item for item in self.tasks}
        self.downstream_map = {item.id: item for item in self.downstream}
        self.metric_map = {item.id: item for item in self.metrics}
        self.lineage_map = {item.id: item for item in self.lineage}

    @classmethod
    def from_documents(cls, docs: dict[str, Any]) -> "Registry":
        return cls(
            sources=tuple(Source.from_dict(item) for item in docs["sources"]),
            tasks=tuple(Task.from_dict(item) for item in docs["tasks"]),
            downstream=tuple(DownstreamTable.from_dict(item)
                             for item in docs["downstream"]),
            lineage=tuple(LineageEdge.from_dict(item) for item in docs["lineage"]),
            metrics=tuple(Metric.from_dict(item) for item in docs["metrics"]),
        )

    def entity_ids(self) -> set[str]:
        return (set(self.source_map) | set(self.task_map)
                | set(self.downstream_map) | set(self.metric_map))

    def node_type(self, node_id: str) -> str:
        if node_id in self.source_map:
            return "source"
        if node_id in self.task_map:
            return "task"
        if node_id in self.downstream_map:
            return "downstream"
        if node_id in self.metric_map:
            return "metric"
        return "unknown"

    def all_entities(self) -> Iterable[Any]:
        yield from self.sources
        yield from self.tasks
        yield from self.downstream
        yield from self.metrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [item.to_dict() for item in self.sources],
            "tasks": [item.to_dict() for item in self.tasks],
            "downstream": [item.to_dict() for item in self.downstream],
            "lineage": [item.to_dict() for item in self.lineage],
            "metrics": [item.to_dict() for item in self.metrics],
        }

    def initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "sources": {item.id: {"status": item.status,
                                  "fields": list(item.fields)}
                        for item in self.sources},
            "tasks": {item.id: {"status": item.status,
                                "source_refs": list(item.source_refs)}
                      for item in self.tasks},
            "downstream": {item.id: {"status": item.status,
                                     "upstream_refs": list(item.upstream_refs)}
                           for item in self.downstream},
            "lineage": {item.id: {"from_ref": item.from_ref,
                                  "to_ref": item.to_ref}
                        for item in self.lineage},
            "metrics": {item.id: {"status": "active",
                                  "source_refs": list(item.source_refs)}
                        for item in self.metrics},
        }
