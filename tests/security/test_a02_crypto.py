"""
A02 Cryptographic Failures 검증 테스트.

검증 항목:
  - bcrypt 패스워드 해싱
  - JWT HS256 서명 및 검증
  - 변조된 토큰 거부
  - 환경 변수 기반 시크릿 로드
  - HTTPS enforcement 설정
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret-a02-owasp")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A02-001~004: 패스워드 해싱 (bcrypt)
# ---------------------------------------------------------------------------

class TestA02PasswordHashing:
    """bcrypt 패스워드 해싱 검증."""

    def test_hash_password_uses_bcrypt(self):
        """A02-001: hash_password()가 bcrypt 형식($2b$)을 반환한다."""
        from app.api.auth.password import hash_password

        hashed = hash_password("TestPassword123!")
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$"), (
            f"bcrypt 형식 아님: {hashed[:10]}"
        )

    def test_hash_is_different_from_plaintext(self):
        """A02-002: 해시된 값이 원본 패스워드와 다르다."""
        from app.api.auth.password import hash_password

        plain = "SecurePassword!2026"
        hashed = hash_password(plain)
        assert plain != hashed

    def test_verify_password_correct(self):
        """A02-003: verify_password()가 올바른 패스워드를 승인한다."""
        from app.api.auth.password import hash_password, verify_password

        plain = "Correct$Password123"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True

    def test_verify_password_wrong_rejected(self):
        """A02-004: verify_password()가 잘못된 패스워드를 거부한다."""
        from app.api.auth.password import hash_password, verify_password

        plain = "Correct$Password123"
        wrong = "WrongPassword456!"
        hashed = hash_password(plain)
        assert verify_password(wrong, hashed) is False

    def test_same_password_different_hashes(self):
        """A02-005: 동일한 패스워드라도 다른 salt로 다른 해시가 생성된다."""
        from app.api.auth.password import hash_password

        plain = "SamePassword!123"
        hash1 = hash_password(plain)
        hash2 = hash_password(plain)
        # salt가 다르면 해시도 달라야 함
        assert hash1 != hash2, "salt 없이 동일 해시 생성됨 (취약)"


# ---------------------------------------------------------------------------
# A02-006~010: JWT 토큰 서명 및 검증
# ---------------------------------------------------------------------------

class TestA02JwtTokens:
    """JWT HS256 토큰 검증."""

    def test_access_token_is_jwt_format(self):
        """A02-006: create_access_token()이 JWT 형식(header.payload.signature)을 반환한다."""
        from app.api.auth.tokens import create_access_token

        token = create_access_token(actor_id="user-001", role="VIEWER")
        parts = token.split(".")
        assert len(parts) == 3, f"JWT 형식 아님 (파트 수: {len(parts)})"

    def test_access_token_valid_decode(self):
        """A02-007: decode_access_token()이 유효한 토큰을 올바르게 디코딩한다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        token = create_access_token(actor_id="user-test", role="AUTHOR")
        payload = decode_access_token(token)

        assert payload is not None, "유효한 토큰 디코딩 실패"
        assert payload.get("sub") == "user-test"
        assert payload.get("role") == "AUTHOR"

    def test_tampered_token_rejected(self):
        """A02-008: 변조된 토큰이 검증에서 거부된다."""
        from app.api.auth.tokens import create_access_token, decode_access_token

        token = create_access_token(actor_id="user-001", role="VIEWER")
        # signature 변조
        parts = token.split(".")
        tampered = parts[0] + "." + parts[1] + "." + "invalidSignatureXXXXXXXX"

        result = decode_access_token(tampered)
        assert result is None, "변조된 토큰이 통과됨 (취약)"

    def test_token_uses_hs256_algorithm(self):
        """A02-009: JWT 토큰이 HS256 알고리즘을 사용한다."""
        import base64
        import json
        from app.api.auth.tokens import create_access_token

        token = create_access_token(actor_id="user-001", role="VIEWER")
        header_b64 = token.split(".")[0]
        # base64 패딩 보정
        padding = 4 - len(header_b64) % 4
        if padding != 4:
            header_b64 += "=" * padding

        header = json.loads(base64.b64decode(header_b64).decode("utf-8"))
        assert header.get("alg") == "HS256", f"알고리즘이 HS256이 아님: {header.get('alg')}"

    def test_jwt_secret_from_environment(self):
        """A02-010: JWT 시크릿이 환경 변수(JWT_SECRET)에서 로드된다."""
        tokens_path = ROOT / "backend/app/api/auth/tokens.py"
        source = tokens_path.read_text(encoding="utf-8")

        # 환경 변수 또는 settings를 통해 로드하는지 확인
        assert "settings.jwt_secret" in source or "JWT_SECRET" in source, (
            "JWT 시크릿이 환경 변수에서 로드되지 않음"
        )

    def test_no_hardcoded_jwt_secret(self):
        """A02-011: 소스 코드에 하드코딩된 JWT 시크릿이 없다."""
        tokens_path = ROOT / "backend/app/api/auth/tokens.py"
        source = tokens_path.read_text(encoding="utf-8")

        import re
        # 'secret' = "..." 패턴 (하드코딩 감지)
        hardcoded = re.findall(r'["\'](?:jwt_secret|JWT_SECRET)["\'].*?["\'][A-Za-z0-9+/]{16,}["\']', source)
        assert not hardcoded, f"하드코딩된 JWT 시크릿 발견: {hardcoded}"


# ---------------------------------------------------------------------------
# A02-012~014: Refresh Token 해싱
# ---------------------------------------------------------------------------

class TestA02RefreshToken:
    """Refresh Token SHA-256 해싱 검증."""

    def test_create_refresh_token_returns_tuple(self):
        """A02-012: create_refresh_token()이 (raw_token, hash) 튜플을 반환한다."""
        from app.api.auth.tokens import create_refresh_token

        result = create_refresh_token()
        assert isinstance(result, tuple) and len(result) == 2, "튜플 반환 아님"

        raw, token_hash = result
        assert isinstance(raw, str) and len(raw) > 32
        assert isinstance(token_hash, str) and len(token_hash) == 64  # SHA-256 hex

    def test_refresh_token_hash_is_sha256(self):
        """A02-013: Refresh Token hash가 SHA-256이다."""
        from app.api.auth.tokens import create_refresh_token

        raw, token_hash = create_refresh_token()
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert hmac.compare_digest(token_hash, expected_hash), "SHA-256 해시 불일치"

    def test_verify_refresh_token_hash_timing_safe(self):
        """A02-014: verify_refresh_token_hash()가 timing-safe compare_digest를 사용한다."""
        tokens_path = ROOT / "backend/app/api/auth/tokens.py"
        source = tokens_path.read_text(encoding="utf-8")

        assert "compare_digest" in source, (
            "timing-safe compare_digest 없음 — 타이밍 공격 취약"
        )


# ---------------------------------------------------------------------------
# A02-015: HTTPS enforcement 설정
# ---------------------------------------------------------------------------

class TestA02HttpsEnforcement:
    """HTTPS 강제 설정 검증."""

    def test_hsts_header_in_production(self):
        """A02-015: SecurityHeadersMiddleware가 production 환경에서 HSTS 헤더를 추가한다."""
        headers_path = ROOT / "backend/app/api/security/headers.py"
        source = headers_path.read_text(encoding="utf-8")

        assert "Strict-Transport-Security" in source, "HSTS 헤더 설정 없음"
        assert "production" in source, "production 환경 조건 없음"

    def test_security_headers_module_exists(self):
        """A02-016: SecurityHeadersMiddleware가 로드 가능하다."""
        from app.api.security.headers import SecurityHeadersMiddleware
        assert SecurityHeadersMiddleware is not None

    def test_security_headers_include_csp(self):
        """A02-017: Content-Security-Policy 헤더가 설정된다."""
        headers_path = ROOT / "backend/app/api/security/headers.py"
        source = headers_path.read_text(encoding="utf-8")

        assert "Content-Security-Policy" in source
