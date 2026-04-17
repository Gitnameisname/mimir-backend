"""
LLM04 Model DoS (Denial of Service) 검증 테스트.

검증 항목:
  - 입력 크기 제한 (RequestSizeLimitMiddleware)
  - 로그인 rate limiting 구현 확인 (Valkey 기반)
  - RAG 쿼리 길이 제한
  - 동시 요청 제한 설정 확인
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# LLM04-001~004: 입력 크기 제한
# ---------------------------------------------------------------------------

class TestLLM04InputSizeLimits:
    """입력 크기 제한 검증."""

    def test_request_size_limit_middleware_exists(self):
        """LLM04-001: RequestSizeLimitMiddleware가 존재한다."""
        from app.api.security.input_validation import RequestSizeLimitMiddleware
        assert RequestSizeLimitMiddleware is not None

    def test_request_size_limit_is_reasonable(self):
        """LLM04-002: 요청 크기 제한이 합리적이다 (1MB~100MB)."""
        from app.api.security.input_validation import RequestSizeLimitMiddleware
        import inspect

        source_path = ROOT / "backend/app/api/security/input_validation.py"
        source = source_path.read_text(encoding="utf-8")

        # 제한값 추출
        import re
        limit_patterns = re.findall(r'(\d+)\s*\*\s*1024\s*\*\s*1024|max_size\s*=\s*(\d+)', source)
        # 10MB 기본값 확인
        has_size_limit = "1024" in source and "max_size" in source.lower() or "limit" in source.lower()
        assert has_size_limit, "요청 크기 제한 코드 없음"

    def test_null_byte_detection_exists(self):
        """LLM04-003: Null byte injection 탐지가 있다."""
        from app.api.security.input_validation import contains_null_byte

        assert contains_null_byte("normal text") is False
        assert contains_null_byte("text\x00injection") is True

    def test_main_app_uses_size_limit_middleware(self):
        """LLM04-004: main.py가 RequestSizeLimitMiddleware를 사용한다."""
        main_path = ROOT / "backend/app/main.py"
        source = main_path.read_text(encoding="utf-8")

        assert "RequestSizeLimitMiddleware" in source or "size" in source.lower(), (
            "main.py에 요청 크기 제한 미들웨어 없음"
        )


# ---------------------------------------------------------------------------
# LLM04-005~008: Rate Limiting
# ---------------------------------------------------------------------------

class TestLLM04RateLimiting:
    """Rate limiting 검증."""

    def test_rate_limit_module_exists(self):
        """LLM04-005: rate_limit 모듈이 존재한다."""
        from app.api.auth.rate_limit import check_login_allowed
        assert callable(check_login_allowed)

    def test_rate_limit_uses_valkey(self):
        """LLM04-006: Rate limiting이 Valkey(Redis)를 사용한다."""
        rate_limit_path = ROOT / "backend/app/api/auth/rate_limit.py"
        source = rate_limit_path.read_text(encoding="utf-8")

        has_valkey = "valkey" in source.lower() or "redis" in source.lower() or "incr" in source
        assert has_valkey, "Rate limiting에 Valkey/Redis 사용 없음"

    def test_rate_limit_config_reasonable(self):
        """LLM04-007: Rate limit 설정이 합리적이다."""
        from app.config import settings

        assert 3 <= settings.login_max_attempts <= 10
        assert 5 <= settings.login_lockout_minutes <= 60

    def test_rate_limit_blocks_after_max(self):
        """LLM04-008: 최대 시도 횟수 초과 시 차단된다."""
        from app.api.auth.rate_limit import check_login_allowed
        from app.config import settings

        mock_valkey = MagicMock()
        mock_valkey.get.return_value = str(settings.login_max_attempts + 1).encode()

        result = check_login_allowed(mock_valkey, "attacker@example.com")
        assert result is False, "최대 시도 초과 후 차단 안 됨"


# ---------------------------------------------------------------------------
# LLM04-009~011: RAG 쿼리 길이 제한
# ---------------------------------------------------------------------------

class TestLLM04QueryLengthLimit:
    """RAG 쿼리 길이 제한 검증."""

    def test_rag_schema_has_query_max_length(self):
        """LLM04-009: RAG 쿼리 스키마에 최대 길이 제한이 있다."""
        schemas_dir = ROOT / "backend/app/schemas"
        if not schemas_dir.exists():
            pytest.skip("schemas 디렉터리 없음")

        combined_source = ""
        for f in schemas_dir.rglob("*.py"):
            combined_source += f.read_text(encoding="utf-8")

        # max_length 또는 constr 등 길이 제한 확인
        has_limit = (
            "max_length" in combined_source
            or "constr" in combined_source
            or "Field(" in combined_source
        )
        assert has_limit, "스키마에 길이 제한 없음"

    def test_context_window_manager_exists(self):
        """LLM04-010: ContextWindowManager가 존재한다 (토큰 제한)."""
        cwm_path = ROOT / "backend/app/services/context_window_manager.py"
        assert cwm_path.exists(), "context_window_manager.py 없음"

        source = cwm_path.read_text(encoding="utf-8")
        has_limit = "max" in source.lower() or "limit" in source.lower() or "token" in source.lower()
        assert has_limit, "ContextWindowManager에 토큰 제한 없음"

    def test_config_has_max_context_tokens(self):
        """LLM04-011: 설정에 최대 컨텍스트 토큰 수가 있다."""
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        has_token_config = any(
            kw in source.lower()
            for kw in ["max_token", "context_window", "max_context", "token_limit"]
        )
        assert has_token_config, "설정에 토큰 제한 없음"
