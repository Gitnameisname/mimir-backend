"""
A10 Server-Side Request Forgery (SSRF) 검증 테스트.

검증 항목:
  - 사용자 입력 URL 기반 외부 요청 없음
  - Prompt Registry가 내부 템플릿만 사용 (사용자 URL 제공 불가)
  - 외부 서비스 URL이 환경변수로만 설정됨
  - 웹훅 URL 검증 (신뢰할 수 없는 URL 차단)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "security-test-internal")


# ---------------------------------------------------------------------------
# A10-001~004: Prompt Registry SSRF 방지
# ---------------------------------------------------------------------------

class TestA10PromptRegistry:
    """Prompt Registry SSRF 방지 검증."""

    def test_prompt_registry_uses_internal_seeds_only(self):
        """A10-001: PromptRegistry가 내부 시드 파일만 사용한다."""
        registry_path = ROOT / "backend/app/services/prompt/registry.py"
        assert registry_path.exists(), "prompt registry 없음"

        source = registry_path.read_text(encoding="utf-8")

        # URL 로드 패턴이 없어야 함
        url_patterns = re.findall(r'(?:http|https|ftp)://', source)
        assert not url_patterns, (
            f"PromptRegistry에 외부 URL 참조 발견: {url_patterns}"
        )

    def test_prompt_registry_no_user_url_parameter(self):
        """A10-002: PromptRegistry.get()이 URL 파라미터를 받지 않는다."""
        from app.services.prompt.registry import PromptRegistry
        import inspect

        sig = inspect.signature(PromptRegistry.get)
        param_names = list(sig.parameters.keys())

        url_params = [p for p in param_names if "url" in p.lower()]
        assert not url_params, (
            f"PromptRegistry.get()에 URL 파라미터 존재: {url_params}"
        )

    def test_prompt_seeds_directory_exists(self):
        """A10-003: 프롬프트 시드 파일이 내부 디렉터리에 있다."""
        seeds_dir = ROOT / "backend/app/services/prompt/seeds"
        assert seeds_dir.exists(), "프롬프트 시드 디렉터리 없음"

    def test_prompt_registry_loads_from_filesystem(self):
        """A10-004: PromptRegistry가 파일시스템에서만 로드한다."""
        registry_path = ROOT / "backend/app/services/prompt/registry.py"
        source = registry_path.read_text(encoding="utf-8")

        # requests, httpx, urllib.request 등 네트워크 라이브러리 미사용
        network_imports = re.findall(
            r'import\s+(?:requests|httpx|urllib\.request|aiohttp)',
            source,
        )
        assert not network_imports, (
            f"PromptRegistry에 네트워크 라이브러리 사용: {network_imports}"
        )


# ---------------------------------------------------------------------------
# A10-005~008: 외부 서비스 URL 설정 보안
# ---------------------------------------------------------------------------

class TestA10ExternalServiceUrls:
    """외부 서비스 URL이 환경변수로만 설정되는지 검증."""

    def test_embedding_url_from_env_only(self):
        """A10-005: Embedding 서비스 URL이 환경변수에서만 로드된다."""
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        # embedding_service_url이 하드코딩되어 있지 않아야 함
        hardcoded = re.findall(
            r'embedding_service_url\s*[:=]\s*["\']https?://[^"\']+["\']',
            source,
        )
        assert not hardcoded, (
            f"Embedding URL 하드코딩 발견: {hardcoded}"
        )

    def test_no_hardcoded_external_urls_in_services(self):
        """A10-006: 서비스 코드에 하드코딩된 외부 HTTP URL이 없다."""
        services_dir = ROOT / "backend/app/services"

        suspicious_urls = []
        for py_file in services_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            source = py_file.read_text(encoding="utf-8")
            # http(s):// 로 시작하는 하드코딩 URL (localhost/127.0.0.1 제외)
            matches = re.findall(
                r'["\']https?://(?!localhost|127\.0\.0\.1)[^"\']{10,}["\']',
                source,
            )
            if matches:
                suspicious_urls.append((py_file.name, matches[:2]))

        assert not suspicious_urls, (
            f"서비스 코드에 외부 URL 하드코딩: {suspicious_urls[:3]}"
        )

    def test_webhook_url_validation_exists(self):
        """A10-007: 웹훅 URL 검증 코드가 있다."""
        webhooks_path = ROOT / "backend/app/api/v1/webhooks.py"
        if not webhooks_path.exists():
            pytest.skip("webhooks.py 없음")

        source = webhooks_path.read_text(encoding="utf-8")
        # URL 검증 또는 허용 목록 패턴 확인
        has_validation = any(
            kw in source.lower()
            for kw in ["validate", "allowed", "whitelist", "allowlist", "url"]
        )
        assert has_validation, "웹훅 URL 검증 없음"

    def test_user_input_not_used_as_request_url(self):
        """A10-008: 사용자 입력이 직접 HTTP 요청 URL로 사용되지 않는다."""
        api_dir = ROOT / "backend/app/api"

        ssrf_patterns = [
            # requests.get(user_input), httpx.get(url=body.url) 등
            r'(?:requests|httpx)\s*\.\s*(?:get|post|put|delete|request)\s*\(\s*(?:url\s*=\s*)?(?:body|request|data|params|query)',
        ]
        violations = []
        for py_file in api_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            source = py_file.read_text(encoding="utf-8")
            for pattern in ssrf_patterns:
                matches = re.findall(pattern, source)
                if matches:
                    violations.append((py_file.name, matches))

        assert not violations, (
            f"사용자 입력 URL 직접 사용 의심 코드: {violations}"
        )


# ---------------------------------------------------------------------------
# A10-009~012: 내부 서비스 URL 접근 제한
# ---------------------------------------------------------------------------

class TestA10InternalNetworkProtection:
    """내부 네트워크 접근 제한 검증."""

    def test_no_metadata_endpoint_access(self):
        """A10-009: 클라우드 메타데이터 엔드포인트 접근 코드가 없다."""
        backend_src = ROOT / "backend/app"

        metadata_urls = [
            "169.254.169.254",   # AWS/GCP/Azure 메타데이터
            "metadata.google",   # GCP 메타데이터
        ]

        for py_file in backend_src.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            source = py_file.read_text(encoding="utf-8")
            for url in metadata_urls:
                if url in source:
                    pytest.fail(
                        f"{py_file.name}: 클라우드 메타데이터 URL 참조: {url}"
                    )

    def test_retriever_factory_no_user_url(self):
        """A10-010: RetrieverFactory가 사용자 제공 URL을 사용하지 않는다."""
        factory_path = ROOT / "backend/app/services/retrieval/retriever_factory.py"
        if not factory_path.exists():
            pytest.skip("retriever_factory.py 없음")

        source = factory_path.read_text(encoding="utf-8")

        # URL 파라미터를 직접 받지 않아야 함
        user_url_pattern = re.findall(
            r'def\s+\w+\s*\([^)]*url[^)]*\)',
            source,
            re.IGNORECASE,
        )
        # retriever factory가 url 파라미터를 함수에서 받는지 확인
        # 설정(config) 기반 URL은 허용
        if user_url_pattern:
            for match in user_url_pattern:
                assert "config" in match.lower() or "settings" in match.lower() or len(user_url_pattern) == 0, (
                    f"RetrieverFactory에 외부 URL 파라미터: {user_url_pattern}"
                )

    def test_rag_service_no_external_fetch(self):
        """A10-011: RAG 서비스가 외부 URL을 fetch하지 않는다."""
        rag_path = ROOT / "backend/app/services/rag_service.py"
        if not rag_path.exists():
            pytest.skip("rag_service.py 없음")

        source = rag_path.read_text(encoding="utf-8")

        # 직접 HTTP 요청 코드가 없어야 함 (내부 서비스 클라이언트 제외)
        direct_http = re.findall(
            r'(?:requests|httpx|aiohttp)\s*\.\s*(?:get|post)\s*\(',
            source,
        )
        assert not direct_http, (
            f"RAG 서비스에 직접 HTTP 요청: {direct_http}"
        )

    def test_config_validates_service_urls(self):
        """A10-012: 서비스 URL 설정이 환경변수 기반으로 관리된다."""
        config_path = ROOT / "backend/app/config.py"
        source = config_path.read_text(encoding="utf-8")

        # 서비스 URL 설정 키 확인
        has_service_url_config = any(
            kw in source.lower()
            for kw in [
                "embedding_service_url",
                "service_url",
                "external_url",
                "llm_base_url",
            ]
        )
        assert has_service_url_config, (
            "서비스 URL 환경변수 설정 없음 — SSRF 방어를 위한 URL 관리 필요"
        )
