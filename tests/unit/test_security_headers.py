"""
보안 헤더 미들웨어 단위 테스트 (Phase 13-1).

검증 항목:
  - X-Content-Type-Options: nosniff
  - X-Frame-Options: DENY
  - X-XSS-Protection: 0
  - Content-Security-Policy
  - Referrer-Policy
  - Permissions-Policy
  - Cache-Control: no-store
  - Server 헤더 제거
"""
import pytest


pytestmark = pytest.mark.unit


def test_security_headers_present(client):
    """보안 헤더가 모든 응답에 포함된다."""
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200

    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("x-xss-protection") == "0"
    assert "Content-Security-Policy" in response.headers or "content-security-policy" in response.headers
    assert response.headers.get("referrer-policy") == "no-referrer"


def test_server_header_removed(client):
    """Server 헤더가 응답에 없어야 한다."""
    response = client.get("/api/v1/system/health")
    assert "server" not in response.headers


def test_cache_control_no_store(client):
    """API 응답은 캐시되지 않아야 한다."""
    response = client.get("/api/v1/system/health")
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control


def test_permissions_policy_present(client):
    """Permissions-Policy 헤더가 존재한다."""
    response = client.get("/api/v1/system/health")
    assert response.headers.get("permissions-policy") is not None
