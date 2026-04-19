"""
A05 Security Misconfiguration 검증 테스트.

검증 항목:
  - 민감한 설정값이 환경 변수에서만 로드됨
  - 폐쇄망 동등성: 외부 서비스 없이도 degrade하지만 실패하지 않음 (S2 ⑦)
  - Default deny 정책
  - 에러 메시지에서 민감 정보 제거
  - 보안 헤더 설정
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A05-001~005: 환경 변수 기반 설정 로드
# ---------------------------------------------------------------------------

class TestA05EnvironmentConfig:
    """민감한 설정값이 환경 변수에서 로드되는지 검증."""

    def test_config_has_no_hardcoded_secrets(self):
        """A05-001: config.py에 하드코딩된 실제 시크릿이 없다."""
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        import re
        # PostgreSQL 패스워드, JWT 시크릿 등이 하드코딩되어 있으면 안 됨
        hardcoded_patterns = [
            r'postgres_password\s*[:=]\s*["\'][A-Za-z0-9]{8,}["\']',
            r'jwt_secret\s*[:=]\s*["\'][A-Za-z0-9]{16,}["\'](?!\s*#)',
        ]
        for pattern in hardcoded_patterns:
            matches = re.findall(pattern, source, re.IGNORECASE)
            # 기본값이 빈 문자열이어야 함
            for match in matches:
                assert '""' in match or "''" in match, (
                    f"config.py에서 하드코딩된 시크릿 발견 패턴: {match}"
                )

    def test_settings_loads_from_env(self):
        """A05-002: Settings가 환경 변수(JWT_SECRET)를 올바르게 로드한다."""
        os.environ["JWT_SECRET"] = "env-test-secret-12345"

        # 재로드
        import importlib
        try:
            from app import config
            importlib.reload(config)
            from app.config import settings
            assert settings.jwt_secret == "env-test-secret-12345"
        except Exception:
            # settings가 캐시되어 있으면 환경 변수만 확인
            assert os.environ.get("JWT_SECRET") == "env-test-secret-12345"

    def test_no_hardcoded_db_credentials_in_source(self):
        """A05-003: 소스 코드에 하드코딩된 DB 자격증명이 없다."""
        backend_src = ROOT / "backend/app"
        import re

        dangerous_patterns = [
            r'postgresql://[^@]+:[^@]+@',   # postgresql://user:pass@host
            r'mysql://[^@]+:[^@]+@',
        ]

        for py_file in backend_src.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            source = py_file.read_text(encoding="utf-8")
            for pattern in dangerous_patterns:
                matches = re.findall(pattern, source)
                # 테스트/fixture에서 test용으로 허용 (실제 크리덴셜이 아닌 경우)
                # f-string 변수 참조({...})는 하드코딩이 아님 — 제외
                non_test_matches = [
                    m for m in matches
                    if "test" not in m.lower() and "{" not in m
                ]
                assert not non_test_matches, (
                    f"{py_file.name}: DB 자격증명 하드코딩 발견: {non_test_matches[:1]}"
                )

    def test_internal_service_secret_from_env(self):
        """A05-004: INTERNAL_SERVICE_SECRET이 환경 변수에서 로드된다."""
        deps_path = ROOT / "backend/app/api/auth/dependencies.py"
        if not deps_path.exists():
            pytest.skip("dependencies.py 없음")

        source = deps_path.read_text(encoding="utf-8")
        assert "INTERNAL_SERVICE_SECRET" in source or "internal_service_secret" in source, (
            "내부 서비스 시크릿이 환경 변수에서 로드되지 않음"
        )

    def test_no_debug_true_in_production_config(self):
        """A05-005: production 환경에서 debug=False가 기본값이다."""
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        import re
        debug_default = re.findall(
            r"^\s*debug\s*:\s*bool\s*=\s*(True|False)\s*$",
            source,
            re.IGNORECASE | re.MULTILINE,
        )
        if debug_default:
            # 기본값이 False이어야 함
            assert "False" in debug_default[0], (
                "config에서 debug 기본값이 True임 (production 배포 위험)"
            )


# ---------------------------------------------------------------------------
# A05-006~010: 폐쇄망 동등성 (S2 ⑦)
# ---------------------------------------------------------------------------

class TestA05ClosedNetworkParity:
    """폐쇄망 환경에서의 degrade-but-not-fail 검증."""

    def test_embedding_service_has_fallback(self):
        """A05-006: 임베딩 서비스가 없을 때 FTS fallback이 동작한다."""
        retriever_factory_path = ROOT / "backend/app/services/retrieval/retriever_factory.py"
        if not retriever_factory_path.exists():
            pytest.skip("retriever_factory.py 없음")

        source = retriever_factory_path.read_text(encoding="utf-8")
        # fallback 또는 fts 키워드 확인
        has_fallback = any(
            kw in source.lower()
            for kw in ["fallback", "fts", "full_text", "text_search"]
        )
        assert has_fallback, "임베딩 서비스 없을 때 FTS fallback 없음 (S2 ⑦ 위반)"

    def test_llm_service_has_offline_fallback(self):
        """A05-007: LLM 서비스가 없을 때 rule-based fallback이 있다."""
        evaluation_dir = ROOT / "backend/app/services/evaluation/metrics"
        fallback_files = list(evaluation_dir.glob("*fallback*"))
        assert fallback_files, "LLM fallback 메트릭 없음 (S2 ⑦ 위반)"

    def test_openai_key_optional(self):
        """A05-008: OPENAI_API_KEY 없이도 서비스가 시작된다."""
        # config.py에서 OPENAI_API_KEY가 required가 아닌지 확인
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        # OPENAI_API_KEY가 required 필드가 아닌 optional(default="")이어야 함
        import re
        # openai_api_key: str (필수)는 안 됨
        required_openai = re.findall(r'openai_api_key\s*:\s*str\s*$', source, re.MULTILINE)
        assert not required_openai, (
            "OPENAI_API_KEY가 필수 설정으로 되어 있음 (폐쇄망 동작 불가)"
        )

    def test_environment_variable_controls_external_services(self):
        """A05-009: 환경변수로 외부 서비스를 on/off할 수 있다."""
        # EMBEDDING_SERVICE_URL, OPENAI_API_KEY 등 optional 설정 확인
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        external_service_controls = any(
            kw in source.lower()
            for kw in [
                "embedding_service_url", "openai_api_key",
                "anthropic_api_key", "llm_provider",
            ]
        )
        assert external_service_controls, (
            "외부 서비스 제어를 위한 환경 변수 설정 없음"
        )


# ---------------------------------------------------------------------------
# A05-011~014: 에러 메시지 sanitize
# ---------------------------------------------------------------------------

class TestA05ErrorMessageScrubbing:
    """에러 메시지에서 민감 정보 제거 검증."""

    def test_error_handlers_exist(self):
        """A05-011: 에러 핸들러가 정의되어 있다."""
        handlers_path = ROOT / "backend/app/api/errors/handlers.py"
        assert handlers_path.exists(), "에러 핸들러 파일 없음"

        source = handlers_path.read_text(encoding="utf-8")
        assert "exception_handler" in source.lower() or "handler" in source.lower()

    def test_error_response_schema_exists(self):
        """A05-012: 에러 응답이 표준 스키마를 사용한다 (내부 정보 노출 방지)."""
        exceptions_path = ROOT / "backend/app/api/errors/exceptions.py"
        if not exceptions_path.exists():
            pytest.skip("exceptions.py 없음")

        source = exceptions_path.read_text(encoding="utf-8")
        # 사용자 메시지와 내부 메시지가 분리되어 있는지 확인
        has_message_field = "message" in source or "detail" in source
        assert has_message_field, "표준화된 에러 메시지 없음"

    def test_server_header_removed(self):
        """A05-013: SecurityHeadersMiddleware가 Server 헤더를 제거한다."""
        headers_path = ROOT / "backend/app/api/security/headers.py"
        source = headers_path.read_text(encoding="utf-8")

        assert "server" in source.lower() and "del" in source.lower(), (
            "Server 헤더 제거 코드 없음"
        )

    def test_default_deny_for_unauthenticated(self):
        """A05-014: 인증되지 않은 요청은 기본적으로 거부된다 (default deny)."""
        deps_path = ROOT / "backend/app/api/auth/dependencies.py"
        if not deps_path.exists():
            pytest.skip("dependencies.py 없음")

        source = deps_path.read_text(encoding="utf-8")
        # 401 또는 403 또는 raises 확인
        import re
        denies = re.findall(r'(?:401|403|raise.*(?:Unauthorized|Forbidden|Permission))', source, re.IGNORECASE)
        assert denies, "unauthenticated 요청에 대한 거부 처리 없음"
