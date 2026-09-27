"""Pytest fixtures: isolated temp-file SQLite app per test session."""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.TemporaryDirectory()
_DB_PATH = os.path.join(_TMP.name, "test.db")
os.environ["WAKE_DB_PATH"] = _DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    database.init_db(_DB_PATH)
    with TestClient(app) as c:
        yield c
    _TMP.cleanup()
