"""
FG9.1 보안 테스트 공통 픽스처.

OWASP Top 10 (일반) + OWASP Top 10 for LLM Applications 검증에 사용.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 환경 설정 — DB 없이 동작
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret-owasp-phase9")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal-secret")
os.environ.setdefault("DEBUG", "true")


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def backend_root(project_root) -> Path:
    return project_root / "backend"


@pytest.fixture
def mock_db_conn(mocker):
    """DB 연결 mock — 보안 로직만 단위 테스트."""
    conn = mocker.MagicMock()
    conn.cursor.return_value.__enter__ = mocker.MagicMock(return_value=mocker.MagicMock())
    conn.cursor.return_value.__exit__ = mocker.MagicMock(return_value=False)
    return conn
