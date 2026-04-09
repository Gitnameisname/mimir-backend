"""
pytest 공통 픽스처.

범위:
  - TestClient (FastAPI 앱 전체)
  - 인증 헤더 픽스처 (역할별)
  - DB mock 픽스처 (단위 테스트용)
  - 실제 DB 연결 픽스처 (통합 테스트용, INTEGRATION_TEST=1 환경 변수 필요)
"""
from __future__ import annotations

import os
from typing import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# 테스트 환경 강제 설정 (DB 연결 없이 앱 임포트 허용)
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-for-testing-only")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal-secret")
os.environ.setdefault("DEBUG", "true")


@pytest.fixture(scope="session")
def app():
    """FastAPI 앱 인스턴스 (세션 전체 공유)."""
    from app.main import app as _app
    return _app


@pytest.fixture(scope="session")
def client(app) -> Generator[TestClient, None, None]:
    """TestClient 픽스처 — DB 연결 없이 동작."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --------------------------------------------------------------------------- #
# 인증 헤더 픽스처 (debug=True 개발 헤더 방식)
# --------------------------------------------------------------------------- #

@pytest.fixture
def auth_viewer():
    return {"X-Actor-Id": "test-viewer-001", "X-Actor-Role": "VIEWER"}


@pytest.fixture
def auth_author():
    return {"X-Actor-Id": "test-author-001", "X-Actor-Role": "AUTHOR"}


@pytest.fixture
def auth_admin():
    return {"X-Actor-Id": "test-admin-001", "X-Actor-Role": "SUPER_ADMIN"}


@pytest.fixture
def auth_approver():
    return {"X-Actor-Id": "test-approver-001", "X-Actor-Role": "APPROVER"}


# --------------------------------------------------------------------------- #
# DB mock 픽스처 (단위 테스트 — 실제 DB 연결 없음)
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_db():
    """DB 연결 mock. 단위 테스트에서 사용."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# --------------------------------------------------------------------------- #
# 통합 테스트용 실제 DB 연결 (INTEGRATION_TEST=1 필요)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def integration_db():
    """실제 DB 연결 픽스처 — INTEGRATION_TEST=1 환경 변수 필요."""
    if not os.environ.get("INTEGRATION_TEST"):
        pytest.skip("INTEGRATION_TEST=1 환경 변수가 없어 통합 테스트를 건너뜁니다.")
    from app.db.connection import get_db
    with get_db() as conn:
        yield conn
