"""Filesystem helpers with path containment checks."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import ErrorCode, SwapDeskError


def safe_join(base: str | Path, *parts: str) -> Path:
    """Join untrusted parts and ensure the result remains below base."""
    root = Path(base).resolve()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SwapDeskError("路径越界", code=ErrorCode.PATH_ESCAPE,
                            details={"base": str(root), "target": str(target)}) from exc
    return target


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def reset_dir(path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def list_files(path: str | Path, suffixes: tuple[str, ...] = ()) -> list[Path]:
    root = Path(path)
    if not root.exists():
        return []
    files = [p for p in root.iterdir() if p.is_file()]
    if suffixes:
        files = [p for p in files if p.suffix.lower() in suffixes]
    return sorted(files, key=lambda p: p.name)
