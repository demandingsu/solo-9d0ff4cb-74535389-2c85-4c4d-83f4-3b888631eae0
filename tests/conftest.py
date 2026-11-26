import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "correction_test.db")


@pytest.fixture
def client(db_path: str):
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c
