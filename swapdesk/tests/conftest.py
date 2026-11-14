from __future__ import annotations

from pathlib import Path

import pytest

from swapdesk.fixtures.generator import write_fixtures
from swapdesk.io.yamlload import load_registry, load_request


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    write_fixtures(root)
    return root


@pytest.fixture
def registry(data_dir: Path):
    return load_registry(data_dir)


@pytest.fixture
def request_one(data_dir: Path):
    return load_request(data_dir / "swap_requests" / "req-001.yaml")


@pytest.fixture
def request_two(data_dir: Path):
    return load_request(data_dir / "swap_requests" / "req-002.yaml")
