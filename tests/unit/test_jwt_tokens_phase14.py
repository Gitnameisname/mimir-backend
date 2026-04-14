"""
Phase 14 JWT 토큰 단위 테스트.

테스트 대상:
  - tokens.py: AT 발급/검증 (jti, type claim), RT 생성/검증
"""

import hashlib
import time

import pytest


class TestAccessToken:
    """Access Token 발급 및 검증 테스트."""

    def test_create_includes_jti_and_type(self):
        """AT에 jti, type=access claim이 포함된다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        token = create_access_token("user-123", "VIEWER")
        payload = decode_access_token(token)

        assert payload is not None
        assert payload["sub"] == "user-123"
        assert payload["role"] == "VIEWER"
        assert payload["type"] == "access"
        assert "jti" in payload
        assert len(payload["jti"]) > 0

    def test_different_jti_each_time(self):
        """매번 다른 jti가 생성된다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        t1 = create_access_token("user-1", "VIEWER")
        t2 = create_access_token("user-1", "VIEWER")
        p1 = decode_access_token(t1)
        p2 = decode_access_token(t2)

        assert p1["jti"] != p2["jti"]

    def test_decode_rejects_refresh_type(self):
        """type=refresh인 토큰은 decode_access_token에서 거부된다."""
        import jwt as pyjwt
        from app.config import settings

        # type=refresh 토큰을 수동으로 생성
        payload = {
            "sub": "user-1",
            "role": "VIEWER",
            "type": "refresh",
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")

        from app.api.auth.tokens import decode_access_token
        assert decode_access_token(token) is None

    def test_decode_accepts_no_type_for_backward_compat(self):
        """type claim이 없는 기존 토큰도 허용 (하위 호환)."""
        import jwt as pyjwt
        from app.config import settings

        payload = {
            "sub": "user-1",
            "role": "VIEWER",
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")

        from app.api.auth.tokens import decode_access_token
        result = decode_access_token(token)
        assert result is not None
        assert result["sub"] == "user-1"


class TestRefreshToken:
    """Refresh Token 생성 및 검증 테스트."""

    def test_create_returns_raw_and_hash(self):
        """create_refresh_token()이 (raw, hash) 튜플을 반환한다."""
        from app.api.auth.tokens import create_refresh_token

        raw, token_hash = create_refresh_token()
        assert len(raw) > 50  # URL-safe 64바이트
        assert len(token_hash) == 64  # SHA-256 hex digest

    def test_hash_matches_raw(self):
        """SHA-256 해시가 raw 토큰과 일치한다."""
        from app.api.auth.tokens import create_refresh_token, verify_refresh_token_hash

        raw, token_hash = create_refresh_token()
        assert verify_refresh_token_hash(raw, token_hash) is True

    def test_hash_mismatch(self):
        """다른 토큰의 해시는 불일치한다."""
        from app.api.auth.tokens import create_refresh_token, verify_refresh_token_hash

        raw1, hash1 = create_refresh_token()
        raw2, hash2 = create_refresh_token()
        assert verify_refresh_token_hash(raw1, hash2) is False

    def test_different_tokens_each_time(self):
        """매번 다른 RT가 생성된다."""
        from app.api.auth.tokens import create_refresh_token

        r1, h1 = create_refresh_token()
        r2, h2 = create_refresh_token()
        assert r1 != r2
        assert h1 != h2

    def test_manual_hash_verification(self):
        """수동으로 SHA-256 해시를 계산하여 일치 확인."""
        from app.api.auth.tokens import create_refresh_token

        raw, token_hash = create_refresh_token()
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert token_hash == expected


class TestFamilyId:
    """Family ID 생성 테스트."""

    def test_generate_family_id_is_uuid(self):
        """generate_family_id()가 UUID 형식 문자열을 반환한다."""
        from app.api.auth.tokens import generate_family_id
        from uuid import UUID

        fid = generate_family_id()
        UUID(fid)  # 유효한 UUID가 아니면 ValueError 발생

    def test_different_family_ids(self):
        """매번 다른 family ID가 생성된다."""
        from app.api.auth.tokens import generate_family_id

        f1 = generate_family_id()
        f2 = generate_family_id()
        assert f1 != f2
