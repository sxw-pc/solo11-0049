"""Schema validation for the five registries and the swap request."""
from __future__ import annotations

from typing import Any

from ..config import (DOWNSTREAM_KINDS, DOWNSTREAM_STATUSES, REGISTRY_FILES,
                      SOURCE_KINDS, SOURCE_STATUSES, TASK_STATUSES,
                      normalize_timestamp)
from ..errors import ErrorCode, Issue

class _Validator:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, code: str, message: str, file: str = "", record_id: str = "",
            field: str = "", **details: Any) -> None:
        self.issues.append(Issue(code, message, file, record_id, field, dict(details)))

    def required(self, row: dict[str, Any], names: tuple[str, ...], file: str,
                 rid: str) -> bool:
        ok = True
        for name in names:
            if name not in row or row[name] is None:
                self.add(ErrorCode.SCHEMA_MISSING_FIELD, f"缺少必填字段 {name}",
                         file=file, record_id=rid, field=name)
                ok = False
        return ok

    def string(self, row: dict[str, Any], name: str, file: str, rid: str,
               *, allow_empty: bool = False) -> None:
        if name not in row or row[name] is None:
            return
        value = row[name]
        if not isinstance(value, str):
            self.add(ErrorCode.SCHEMA_TYPE, f"字段 {name} 必须是字符串",
                     file=file, record_id=rid, field=name,
                     expected="string", actual=type(value).__name__)
        elif not allow_empty and not value.strip():
            self.add(ErrorCode.SCHEMA_EMPTY, f"字段 {name} 不能为空",
                     file=file, record_id=rid, field=name)

    def string_list(self, row: dict[str, Any], name: str, file: str, rid: str,
                    *, nonempty: bool = False) -> None:
        if name not in row or row[name] is None:
            return
        value = row[name]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.add(ErrorCode.SCHEMA_TYPE, f"字段 {name} 必须是字符串列表",
                     file=file, record_id=rid, field=name,
                     expected="list[string]", actual=type(value).__name__)
            return
        if nonempty and not value:
            self.add(ErrorCode.SCHEMA_EMPTY, f"字段 {name} 不能为空列表",
                     file=file, record_id=rid, field=name)
        if len(set(value)) != len(value):
            self.add(ErrorCode.SCHEMA_TYPE, f"字段 {name} 不允许重复值",
                     file=file, record_id=rid, field=name)

    def enum(self, row: dict[str, Any], name: str, allowed: tuple[str, ...],
             file: str, rid: str) -> None:
        if name not in row or row[name] is None:
            return
        value = row[name]
        if value not in allowed:
            self.add(ErrorCode.SCHEMA_ENUM, f"字段 {name} 的枚举值不合法",
                     file=file, record_id=rid, field=name,
                     allowed=list(allowed), actual=value)


def _rows(doc: Any, key: str, validator: _Validator) -> list[dict[str, Any]]:
    filename = REGISTRY_FILES[key]
    if not isinstance(doc, list):
        validator.add(ErrorCode.SCHEMA_ROOT, f"{filename} 根节点必须是列表",
                      file=filename, field="$")
        return []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(doc):
        if not isinstance(item, dict):
            validator.add(ErrorCode.SCHEMA_ROOT, f"第 {index} 条记录必须是映射",
                          file=filename, record_id=f"#{index}", field="$")
        else:
            rows.append(item)
    return rows


def _unique_ids(rows: list[dict[str, Any]], file: str,
                validator: _Validator) -> set[str]:
    seen: set[str] = set()
    for row in rows:
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        if rid in seen:
            validator.add(ErrorCode.SCHEMA_DUPLICATE_ID, f"ID {rid} 重复",
                          file=file, record_id=rid, field="id")
        seen.add(rid)
    return seen

def _check_sources(rows: list[dict[str, Any]], v: _Validator) -> None:
    file = REGISTRY_FILES["sources"]
    for row in rows:
        rid = str(row.get("id", ""))
        v.required(row, ("id", "name", "kind", "status", "fields"), file, rid)
        for name in ("id", "name", "kind", "status"):
            v.string(row, name, file, rid)
        v.enum(row, "kind", SOURCE_KINDS, file, rid)
        v.enum(row, "status", SOURCE_STATUSES, file, rid)
        v.string_list(row, "fields", file, rid, nonempty=True)
        v.string(row, "owner", file, rid, allow_empty=True)
        if "endpoint_host" in row and not isinstance(row["endpoint_host"], str):
            v.add(ErrorCode.SCHEMA_TYPE, "endpoint_host 必须是字符串",
                  file=file, record_id=rid, field="endpoint_host")
        if "endpoint_port" in row and (not isinstance(row["endpoint_port"], int)
                                       or isinstance(row["endpoint_port"], bool)):
            v.add(ErrorCode.SCHEMA_TYPE, "endpoint_port 必须是整数",
                  file=file, record_id=rid, field="endpoint_port")

def _check_tasks(rows: list[dict[str, Any]], sources: set[str],
                 v: _Validator) -> None:
    file = REGISTRY_FILES["tasks"]
    for row in rows:
        rid = str(row.get("id", ""))
        v.required(row, ("id", "name", "source_refs", "output_fields", "status"),
                   file, rid)
        for name in ("id", "name", "status"):
            v.string(row, name, file, rid)
        v.enum(row, "status", TASK_STATUSES, file, rid)
        v.string_list(row, "source_refs", file, rid, nonempty=True)
        v.string_list(row, "output_fields", file, rid, nonempty=True)
        v.string(row, "schedule", file, rid, allow_empty=True)
        for ref in row.get("source_refs", []) if isinstance(row.get("source_refs"), list) else []:
            if ref not in sources:
                v.add(ErrorCode.REF_UNKNOWN_SOURCE, f"任务引用未知数据源 {ref}",
                      file=file, record_id=rid, field="source_refs", ref=ref)

def _check_downstream(rows: list[dict[str, Any]], sources: set[str],
                      tasks: set[str], v: _Validator) -> None:
    file = REGISTRY_FILES["downstream"]
    known = sources | tasks | {str(r.get("id")) for r in rows}
    for row in rows:
        rid = str(row.get("id", ""))
        v.required(row, ("id", "name", "kind", "upstream_refs", "status"),
                   file, rid)
        for name in ("id", "name", "status"):
            v.string(row, name, file, rid)
        v.enum(row, "kind", DOWNSTREAM_KINDS, file, rid)
        v.enum(row, "status", DOWNSTREAM_STATUSES, file, rid)
        v.string_list(row, "upstream_refs", file, rid, nonempty=True)
        refs = row.get("upstream_refs")
        for ref in refs if isinstance(refs, list) else []:
            if ref not in known:
                v.add(ErrorCode.REF_UNKNOWN_ENTITY, f"下游表引用未知实体 {ref}",
                      file=file, record_id=rid, field="upstream_refs", ref=ref)

def _check_lineage(rows: list[dict[str, Any]], entities: set[str],
                   v: _Validator) -> None:
    file = REGISTRY_FILES["lineage"]
    for row in rows:
        rid = str(row.get("id", ""))
        v.required(row, ("id", "from_ref", "to_ref"), file, rid)
        for name in ("id", "from_ref", "to_ref"):
            v.string(row, name, file, rid)
        v.string_list(row, "fields", file, rid)
        for name in ("from_ref", "to_ref"):
            ref = row.get(name)
            if isinstance(ref, str) and ref and ref not in entities:
                v.add(ErrorCode.REF_UNKNOWN_ENTITY, f"血缘引用未知实体 {ref}",
                      file=file, record_id=rid, field=name, ref=ref)
        if row.get("from_ref") == row.get("to_ref") and row.get("from_ref"):
            v.add(ErrorCode.REQUEST_INVALID, "血缘边不能自环",
                  file=file, record_id=rid, field="to_ref")


def _check_metrics(rows: list[dict[str, Any]], entities: set[str],
                   v: _Validator) -> None:
    file = REGISTRY_FILES["metrics"]
    for row in rows:
        rid = str(row.get("id", ""))
        v.required(row, ("id", "name", "source_refs", "required_fields", "required"),
                   file, rid)
        for name in ("id", "name"):
            v.string(row, name, file, rid)
        v.string_list(row, "source_refs", file, rid, nonempty=True)
        v.string_list(row, "required_fields", file, rid)
        if "required" in row and not isinstance(row["required"], bool):
            v.add(ErrorCode.SCHEMA_TYPE, "required 必须是布尔值",
                  file=file, record_id=rid, field="required")
        refs = row.get("source_refs")
        for ref in refs if isinstance(refs, list) else []:
            if ref not in entities:
                v.add(ErrorCode.REF_UNKNOWN_ENTITY, f"口径引用未知实体 {ref}",
                      file=file, record_id=rid, field="source_refs", ref=ref)


def validate_registry_documents(docs: dict[str, Any]) -> list[Issue]:
    v = _Validator()
    loaded: dict[str, list[dict[str, Any]]] = {}
    for key in REGISTRY_FILES:
        loaded[key] = _rows(docs.get(key), key, v)
    ids = {key: _unique_ids(rows, REGISTRY_FILES[key], v)
           for key, rows in loaded.items()}
    _check_sources(loaded["sources"], v)
    _check_tasks(loaded["tasks"], ids["sources"], v)
    _check_downstream(loaded["downstream"], ids["sources"], ids["tasks"], v)
    entities = set().union(*ids.values())
    _check_lineage(loaded["lineage"], entities, v)
    _check_metrics(loaded["metrics"], entities, v)
    return v.issues


def validate_request_document(doc: Any) -> list[Issue]:
    v = _Validator()
    file = "swap_request"
    if not isinstance(doc, dict):
        v.add(ErrorCode.SCHEMA_ROOT, "撤换请求根节点必须是映射", file=file, field="$")
        return v.issues
    rid = str(doc.get("request_id", ""))
    v.required(doc, ("request_id", "title", "created_at", "retire", "window"),
               file, rid)
    for name in ("request_id", "title", "created_at"):
        v.string(doc, name, file, rid)
    for name in ("created_at",):
        if isinstance(doc.get(name), str):
            try:
                normalize_timestamp(doc[name])
            except (ValueError, TypeError):
                v.add(ErrorCode.REQUEST_INVALID, "created_at 必须是 ISO-8601 时间",
                      file=file, record_id=rid, field=name)
    retire = doc.get("retire")
    if not isinstance(retire, list) or not retire:
        v.add(ErrorCode.SCHEMA_EMPTY, "retire 必须是非空列表",
              file=file, record_id=rid, field="retire")
    else:
        seen: set[str] = set()
        for index, item in enumerate(retire):
            if not isinstance(item, dict):
                v.add(ErrorCode.SCHEMA_TYPE, "retire 条目必须是映射",
                      file=file, record_id=rid, field=f"retire[{index}]")
                continue
            source = item.get("source")
            replacement = item.get("replacement")
            candidates = item.get("candidates")
            if not isinstance(source, str) or not source:
                v.add(ErrorCode.SCHEMA_MISSING_FIELD, "retire 条目缺少 source",
                      file=file, record_id=rid, field=f"retire[{index}].source")
            elif source in seen:
                v.add(ErrorCode.SCHEMA_DUPLICATE_ID, f"待下线源 {source} 重复",
                      file=file, record_id=rid, field="retire")
            else:
                seen.add(source)
            if replacement is not None and not isinstance(replacement, str):
                v.add(ErrorCode.SCHEMA_TYPE, "replacement 必须是字符串",
                      file=file, record_id=rid,
                      field=f"retire[{index}].replacement")
            if candidates is not None:
                field_name = f"retire[{index}].candidates"
                if (not isinstance(candidates, list)
                        or not all(isinstance(c, str) for c in candidates)):
                    v.add(ErrorCode.SCHEMA_TYPE, "candidates 必须是字符串列表",
                          file=file, record_id=rid, field=field_name)
                elif not candidates:
                    v.add(ErrorCode.SCHEMA_EMPTY, "candidates 不能为空列表",
                          file=file, record_id=rid, field=field_name)
                elif len(set(candidates)) != len(candidates):
                    v.add(ErrorCode.SCHEMA_TYPE, "candidates 不允许重复值",
                          file=file, record_id=rid, field=field_name)
                elif any(not c.strip() for c in candidates):
                    v.add(ErrorCode.SCHEMA_EMPTY, "candidates 不允许空字符串",
                          file=file, record_id=rid, field=field_name)
            if replacement is not None and candidates is not None:
                v.add(ErrorCode.REQUEST_INVALID,
                      "replacement 与 candidates 只能二选一",
                      file=file, record_id=rid, field=f"retire[{index}]")
            if replacement is None and candidates is None:
                v.add(ErrorCode.SCHEMA_MISSING_FIELD,
                      "retire 条目缺少 replacement 或 candidates",
                      file=file, record_id=rid,
                      field=f"retire[{index}].replacement")
    for field_name in ("window",):
        window = doc.get(field_name)
        if not isinstance(window, dict):
            v.add(ErrorCode.SCHEMA_TYPE, f"{field_name} 必须是映射",
                  file=file, record_id=rid, field=field_name)
        else:
            v.required(window, ("start", "end"), file, rid)
            _check_window(v, window, file, rid, field_name)
    other = doc.get("other_windows", [])
    if not isinstance(other, list):
        v.add(ErrorCode.SCHEMA_TYPE, "other_windows 必须是列表",
              file=file, record_id=rid, field="other_windows")
    else:
        for index, window in enumerate(other):
            if not isinstance(window, dict):
                v.add(ErrorCode.SCHEMA_TYPE, "窗口条目必须是映射",
                      file=file, record_id=rid, field=f"other_windows[{index}]")
                continue
            v.required(window, ("id", "start", "end"), file, rid)
            _check_window(v, window, file, rid, f"other_windows[{index}]")
    max_parallel = doc.get("max_parallel", 2)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool):
        v.add(ErrorCode.SCHEMA_TYPE, "max_parallel 必须是整数",
              file=file, record_id=rid, field="max_parallel")
    elif not 1 <= max_parallel <= 32:
        v.add(ErrorCode.SCHEMA_ENUM, "max_parallel 必须在 1..32 之间",
              file=file, record_id=rid, field="max_parallel")
    return v.issues


def _check_window(v: _Validator, window: dict[str, Any], file: str, rid: str,
                  field: str) -> None:
    for name in ("start", "end"):
        value = window.get(name)
        if not isinstance(value, str):
            v.add(ErrorCode.SCHEMA_TYPE, f"窗口 {name} 必须是字符串",
                  file=file, record_id=rid, field=f"{field}.{name}")
            return
        try:
            window[name] = normalize_timestamp(value)
        except (ValueError, TypeError):
            v.add(ErrorCode.REQUEST_INVALID, f"窗口 {name} 不是合法时间",
                  file=file, record_id=rid, field=f"{field}.{name}")
            return
    if window["start"] > window["end"]:
        v.add(ErrorCode.REQUEST_INVALID, "窗口 start 不能晚于 end",
              file=file, record_id=rid, field=field)
