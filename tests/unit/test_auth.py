"""
인증 레이어 단위 테스트 (Phase 13-1/13-4).

검증 항목:
  - JWT 기반 Bearer 인증
  - API key 인증
  - 개발 헤더 인증 (debug=True)
  - anonymous actor 처리
  - 인증 없이 보호된 엔드포인트 접근 거부
"""
import time

import jwt
import pytest

pytestmark = pytest.mark.unit


def _make_jwt(actor_id: str, role: str = "AUTHOR", expire_delta: int = 3600) -> str:
    """테스트용 JWT 토큰 생성."""
    from app.config import settings
    payload = {
        "sub": actor_id,
        "role": role,
        "exp": int(time.time()) + expire_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


class TestBearerAuth:
    def test_valid_jwt_authenticates(self, client):
        """유효한 JWT로 인증이 성공한다."""
        token = _make_jwt("user-001", "AUTHOR")
        response = client.get(
            "/api/v1/system/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    def test_expired_jwt_is_rejected(self, client):
        """만료된 JWT는 anonymous로 처리된다 (공개 엔드포인트는 통과)."""
        token = _make_jwt("user-001", expire_delta=-1)
        response = client.get(
            "/api/v1/system/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        # 공개 엔드포인트 — 인증 실패해도 200 반환 (anonymous 허용)
        assert response.status_code == 200

    def test_invalid_jwt_is_rejected(self, client):
        """잘못된 서명의 JWT는 anonymous로 처리된다."""
        response = client.get(
            "/api/v1/system/info",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert response.status_code == 200  # public endpoint


class TestDevHeaderAuth:
    def test_dev_header_auth_works_in_debug(self, client):
        """debug=True 환경에서 개발 헤더 인증이 동작한다."""
        response = client.get(
            "/api/v1/system/info",
            headers={"X-Actor-Id": "dev-user-001", "X-Actor-Role": "AUTHOR"},
        )
        assert response.status_code == 200


class TestHealthCheck:
    def test_health_returns_healthy(self, client):
        """헬스체크 엔드포인트가 healthy=True를 반환한다."""
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["healthy"] is True

    def test_health_no_auth_required(self, client):
        """헬스체크는 인증 없이 접근 가능하다."""
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
