"""YAML loading entry points for registries and swap requests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config import REGISTRY_FILES
from ..errors import ErrorCode, Issue, SchemaError, SwapDeskError
from ..model.entities import Registry, SwapRequest
from ..model.schema import validate_registry_documents, validate_request_document


def _read_yaml(path: Path, display: str) -> Any:
    if not path.exists():
        raise SwapDeskError("文件不存在", code=ErrorCode.FILE_MISSING,
                            file=display, details={"path": str(path)})
    try:
        text = path.read_text(encoding="utf-8")
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SwapDeskError("YAML 解析失败", code=ErrorCode.YAML_PARSE,
                            file=display, details={"error": str(exc)}) from exc
    except OSError as exc:
        raise SwapDeskError("文件读取失败", code=ErrorCode.FILE_MISSING,
                            file=display, details={"error": str(exc)}) from exc


def load_registry(data_dir: str | Path) -> Registry:
    root = Path(data_dir)
    docs: dict[str, Any] = {}
    for key, filename in REGISTRY_FILES.items():
        docs[key] = _read_yaml(root / filename, filename)
    issues = validate_registry_documents(docs)
    if issues:
        raise SchemaError(issues)
    return Registry.from_documents(docs)


def load_request(path: str | Path) -> SwapRequest:
    target = Path(path)
    doc = _read_yaml(target, target.name)
    issues = validate_request_document(doc)
    if issues:
        raise SchemaError(issues)
    return SwapRequest.from_dict(doc)


def find_request(data_dir: str | Path, request: str | Path | None) -> Path:
    if request is None:
        return Path(data_dir) / "swap_requests" / "req-001.yaml"
    candidate = Path(request)
    if candidate.exists():
        return candidate
    nested = Path(data_dir) / "swap_requests" / candidate.name
    if nested.exists():
        return nested
    return candidate


def issue(code: str, message: str, *, file: str = "", record_id: str = "",
          field: str = "", **details: Any) -> Issue:
    return Issue(code, message, file=file, record_id=record_id,
                 field=field, details=dict(details))
