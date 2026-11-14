from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from swapdesk.errors import ErrorCode, SchemaError
from swapdesk.io.yamlload import load_registry
from swapdesk.pipeline import load_inputs


def _rewrite(data_dir: Path, filename: str, mutate) -> None:
    path = data_dir / filename
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                    encoding="utf-8", newline="\n")


def _find(exc: SchemaError, code: str):
    return next(issue for issue in exc.issues if issue.code == code)


def test_missing_field_has_file_record_and_field(data_dir: Path) -> None:
    _rewrite(data_dir, "sources.yaml", lambda docs: docs[0].pop("name"))
    with pytest.raises(SchemaError) as caught:
        load_registry(data_dir)
    issue = _find(caught.value, ErrorCode.SCHEMA_MISSING_FIELD)
    assert issue.file == "sources.yaml"
    assert issue.record_id == "src-alpha"
    assert issue.field == "name"


def test_type_error(data_dir: Path) -> None:
    def mutate(docs):
        docs[0]["source_refs"] = "src-alpha"
    _rewrite(data_dir, "tasks.yaml", mutate)
    with pytest.raises(SchemaError) as caught:
        load_registry(data_dir)
    issue = _find(caught.value, ErrorCode.SCHEMA_TYPE)
    assert issue.file == "tasks.yaml"
    assert issue.record_id == "task-orders-ingest"
    assert issue.field == "source_refs"


def test_unknown_reference(data_dir: Path) -> None:
    def mutate(docs):
        docs[0]["source_refs"] = ["src-does-not-exist"]
    _rewrite(data_dir, "tasks.yaml", mutate)
    with pytest.raises(SchemaError) as caught:
        load_registry(data_dir)
    issue = _find(caught.value, ErrorCode.REF_UNKNOWN_SOURCE)
    assert issue.record_id == "task-orders-ingest"
    assert issue.field == "source_refs"
    assert issue.details["ref"] == "src-does-not-exist"


def test_enum_out_of_range(data_dir: Path) -> None:
    def mutate(docs):
        docs[0]["kind"] = "ftp"
    _rewrite(data_dir, "sources.yaml", mutate)
    with pytest.raises(SchemaError) as caught:
        load_registry(data_dir)
    issue = _find(caught.value, ErrorCode.SCHEMA_ENUM)
    assert issue.file == "sources.yaml"
    assert issue.record_id == "src-alpha"
    assert issue.details["actual"] == "ftp"


def test_request_unknown_retire_source(data_dir: Path) -> None:
    source = data_dir / "swap_requests" / "req-001.yaml"
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc["retire"][0]["source"] = "src-unknown"
    target = data_dir / "swap_requests" / "bad-request.yaml"
    target.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                      encoding="utf-8", newline="\n")
    with pytest.raises(SchemaError) as caught:
        load_inputs(data_dir, target)
    issue = _find(caught.value, ErrorCode.REF_UNKNOWN_SOURCE)
    assert issue.file == "bad-request.yaml"
    assert issue.record_id == "req-001"
    assert issue.details["ref"] == "src-unknown"
