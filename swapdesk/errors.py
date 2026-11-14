"""Stable error codes and exceptions used by the command line and tests."""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


class ErrorCode:
    FILE_MISSING = "E_FILE_MISSING"
    YAML_PARSE = "E_YAML_PARSE"
    SCHEMA_ROOT = "E_SCHEMA_ROOT"
    SCHEMA_MISSING_FIELD = "E_SCHEMA_MISSING_FIELD"
    SCHEMA_TYPE = "E_SCHEMA_TYPE"
    SCHEMA_ENUM = "E_SCHEMA_ENUM"
    SCHEMA_DUPLICATE_ID = "E_SCHEMA_DUPLICATE_ID"
    SCHEMA_EMPTY = "E_SCHEMA_EMPTY"
    REF_UNKNOWN_SOURCE = "E_REF_UNKNOWN_SOURCE"
    REF_UNKNOWN_TASK = "E_REF_UNKNOWN_TASK"
    REF_UNKNOWN_DOWNSTREAM = "E_REF_UNKNOWN_DOWNSTREAM"
    REF_UNKNOWN_ENTITY = "E_REF_UNKNOWN_ENTITY"
    REQUEST_INVALID = "E_REQUEST_INVALID"
    PLAN_BLOCKED = "E_PLAN_BLOCKED"
    PLAN_INVALID = "E_PLAN_INVALID"
    RUN_NOT_FOUND = "E_RUN_NOT_FOUND"
    RUN_CONFLICT = "E_RUN_CONFLICT"
    RUN_INCOMPLETE = "E_RUN_INCOMPLETE"
    STATE_MISMATCH = "E_STATE_MISMATCH"
    STEP_FAILED = "E_STEP_FAILED"
    ROLLBACK_UNAVAILABLE = "E_ROLLBACK_UNAVAILABLE"
    VERIFY_FAILED = "E_VERIFY_FAILED"
    PATH_ESCAPE = "E_PATH_ESCAPE"
    HTTP_METHOD = "E_HTTP_METHOD"
    HTTP_NOT_FOUND = "E_HTTP_NOT_FOUND"
    INTERNAL = "E_INTERNAL"


ERROR_DESCRIPTIONS = {
    ErrorCode.FILE_MISSING: "登记表文件不存在",
    ErrorCode.YAML_PARSE: "YAML 无法解析",
    ErrorCode.SCHEMA_ROOT: "登记表根节点类型不正确",
    ErrorCode.SCHEMA_MISSING_FIELD: "记录缺少必填字段",
    ErrorCode.SCHEMA_TYPE: "字段类型不正确",
    ErrorCode.SCHEMA_ENUM: "枚举值越界",
    ErrorCode.SCHEMA_DUPLICATE_ID: "ID 重复",
    ErrorCode.SCHEMA_EMPTY: "必填集合为空",
    ErrorCode.REF_UNKNOWN_SOURCE: "引用了未知数据源",
    ErrorCode.REF_UNKNOWN_TASK: "引用了未知采集任务",
    ErrorCode.REF_UNKNOWN_DOWNSTREAM: "引用了未知下游表",
    ErrorCode.REF_UNKNOWN_ENTITY: "引用了未知实体",
    ErrorCode.REQUEST_INVALID: "撤换请求不合法",
    ErrorCode.PLAN_BLOCKED: "存在 blocking 冲突，计划不可执行",
    ErrorCode.PLAN_INVALID: "计划结构不合法",
    ErrorCode.RUN_NOT_FOUND: "台账中不存在该运行",
    ErrorCode.RUN_CONFLICT: "运行与计划不匹配",
    ErrorCode.RUN_INCOMPLETE: "运行未完成，需要使用 --resume",
    ErrorCode.STATE_MISMATCH: "执行前状态与计划不一致",
    ErrorCode.STEP_FAILED: "步骤执行失败",
    ErrorCode.ROLLBACK_UNAVAILABLE: "没有可回滚的执行记录",
    ErrorCode.VERIFY_FAILED: "计划与实际不一致",
    ErrorCode.PATH_ESCAPE: "路径越界",
    ErrorCode.HTTP_METHOD: "只读服务仅允许 GET",
    ErrorCode.HTTP_NOT_FOUND: "请求的资源不存在",
    ErrorCode.INTERNAL: "内部错误",
}


@dataclass
class Issue:
    code: str
    message: str
    file: str = ""
    record_id: str = ""
    field: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.file:
            data["file"] = self.file
        if self.record_id:
            data["record_id"] = self.record_id
        if self.field:
            data["field"] = self.field
        if self.details:
            data["details"] = self.details
        return data

    def __str__(self) -> str:
        loc = self.file or "<input>"
        if self.record_id:
            loc += f"[{self.record_id}]"
        if self.field:
            loc += f".{self.field}"
        return f"{self.code} {loc}: {self.message}"


class SwapDeskError(Exception):
    code = ErrorCode.INTERNAL
    exit_code = 2

    def __init__(self, message: str, *, code: str | None = None,
                 file: str = "", record_id: str = "", field: str = "",
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.file = file
        self.record_id = record_id
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        issue = Issue(self.code, self.message, self.file, self.record_id,
                      self.field, self.details)
        payload: dict[str, Any] = {"ok": False, "error": issue.to_dict()}
        if "issues" in self.details:
            payload["issues"] = self.details["issues"]
        return payload


class SchemaError(SwapDeskError):
    code = ErrorCode.SCHEMA_ROOT

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        first = issues[0] if issues else Issue(self.code, "schema validation failed")
        super().__init__(
            f"{len(issues)} 个登记表校验问题；首个问题：{first}",
            code=first.code, file=first.file, record_id=first.record_id,
            field=first.field,
            details={"issues": [issue.to_dict() for issue in issues]},
        )

    def all_codes(self) -> set[str]:
        return {issue.code for issue in self.issues}


class PlanBlockedError(SwapDeskError):
    code = ErrorCode.PLAN_BLOCKED
    exit_code = 3

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        blocking = [c for c in conflicts if c.get("severity") == "blocking"]
        rules = sorted({str(c.get("rule_id")) for c in blocking})
        super().__init__(
            "存在 blocking 冲突，计划被拒绝：" + ", ".join(rules),
            code=ErrorCode.PLAN_BLOCKED,
            details={"blocking": blocking, "rule_ids": rules},
        )
        self.conflicts = blocking


class VerifyFailed(SwapDeskError):
    code = ErrorCode.VERIFY_FAILED
    exit_code = 4

    def __init__(self, differences: list[dict[str, Any]]) -> None:
        super().__init__(
            f"复核失败，共 {len(differences)} 项差异",
            code=ErrorCode.VERIFY_FAILED,
            details={"differences": differences},
        )
        self.differences = differences
