"""Deterministic JSON serialization and content hashing helpers."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not allowed")
        if value == 0.0:
            return 0.0
        return float(format(value, ".12g"))
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def canonical_json(value: Any) -> str:
    """Compact canonical form used for hashes and byte-stable comparisons."""
    normalized = _normalize(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def pretty_json(value: Any) -> str:
    normalized = _normalize(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False) + "\n"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_bytes(content.encode("utf-8"))
    os.replace(temp, target)
    return target


def write_json(path: str | Path, value: Any) -> Path:
    return atomic_write_text(path, pretty_json(value))


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
