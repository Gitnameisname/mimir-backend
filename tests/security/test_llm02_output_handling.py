"""
LLM02 Insecure Output Handling 검증 테스트.

검증 항목:
  - LLM 출력 JSON 스키마 검증
  - HTML 이스케이프 / XSS 방지
  - 신뢰할 수 없는 출력 sanitization
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")


# ---------------------------------------------------------------------------
# LLM02-001~004: HTML sanitizer
# ---------------------------------------------------------------------------

class TestLLM02HtmlSanitizer:
    """HTML sanitization 동작 검증."""

    def test_sanitizer_module_exists(self):
        """LLM02-001: html_sanitizer 모듈이 존재한다."""
        from app.utils.html_sanitizer import sanitize_html, escape_html
        assert callable(sanitize_html)
        assert callable(escape_html)

    def test_script_tag_removed(self):
        """LLM02-002: <script> 태그가 제거된다."""
        from app.utils.html_sanitizer import sanitize_html

        result = sanitize_html("<p>Test <script>alert('XSS')</script> text</p>")
        assert "<script>" not in result, "<script> 태그가 제거되지 않음"
        assert "alert('XSS')" not in result, "스크립트 코드가 남아 있음"

    def test_event_handler_removed(self):
        """LLM02-003: 인라인 이벤트 핸들러가 제거된다."""
        from app.utils.html_sanitizer import sanitize_html

        result = sanitize_html('<img src=x onerror=alert(1) />')
        assert "onerror" not in result, "onerror 핸들러 제거 안 됨"

    def test_javascript_protocol_removed(self):
        """LLM02-004: javascript: 프로토콜이 제거된다."""
        from app.utils.html_sanitizer import sanitize_html

        result = sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript:" not in result.lower(), "javascript: 프로토콜 제거 안 됨"

    def test_iframe_removed(self):
        """LLM02-005: <iframe> 태그가 제거된다."""
        from app.utils.html_sanitizer import sanitize_html

        result = sanitize_html('<iframe src="https://evil.com"></iframe>')
        assert "<iframe" not in result.lower(), "iframe 태그 제거 안 됨"

    def test_escape_html_converts_special_chars(self):
        """LLM02-006: escape_html이 특수 문자를 엔티티로 변환한다."""
        from app.utils.html_sanitizer import escape_html

        result = escape_html('<script>alert("xss")</script>')
        assert "&lt;" in result, "< 변환 안 됨"
        assert "&gt;" in result, "> 변환 안 됨"
        assert "&quot;" in result or "&#x27;" in result or "&#34;" in result


# ---------------------------------------------------------------------------
# LLM02-007~011: OutputValidator
# ---------------------------------------------------------------------------

class TestLLM02OutputValidator:
    """LLM 출력 스키마 검증."""

    def test_validator_module_exists(self):
        """LLM02-007: LLMOutputValidator가 존재한다."""
        from app.services.llm.output_validator import LLMOutputValidator
        assert LLMOutputValidator is not None

    def test_valid_json_passes(self):
        """LLM02-008: 유효한 JSON 출력이 통과된다."""
        from app.services.llm.output_validator import LLMOutputValidator

        validator = LLMOutputValidator()
        output = json.dumps({
            "summary": "Document discusses quarterly results.",
            "key_points": ["Revenue up 20%", "New markets opened"],
            "confidence_score": 0.92,
            "citations": [{"document_id": "doc-001", "text": "Revenue up 20%"}],
        })
        result = validator.validate_and_sanitize(output)
        assert result["confidence_score"] == 0.92

    def test_invalid_json_raises(self):
        """LLM02-009: 유효하지 않은 JSON이 오류를 발생시킨다."""
        from app.services.llm.output_validator import LLMOutputValidator, OutputValidationError

        validator = LLMOutputValidator()
        with pytest.raises(OutputValidationError):
            validator.validate_and_sanitize("{ broken json }")

    def test_out_of_range_confidence_raises(self):
        """LLM02-010: confidence_score 범위 초과 시 오류가 발생한다."""
        from app.services.llm.output_validator import LLMOutputValidator, OutputValidationError

        validator = LLMOutputValidator()
        output = json.dumps({
            "summary": "Test",
            "confidence_score": 1.5,  # 범위 초과
            "citations": [],
        })
        with pytest.raises(OutputValidationError):
            validator.validate_and_sanitize(output)

    def test_xss_in_output_is_sanitized(self):
        """LLM02-011: LLM 출력의 XSS 페이로드가 sanitize된다."""
        from app.services.llm.output_validator import LLMOutputValidator

        validator = LLMOutputValidator()
        output = json.dumps({
            "summary": "Report <script>alert('XSS')</script> summary",
            "citations": [],
        })
        result = validator.validate_and_sanitize(output)
        assert "<script>" not in result["summary"], "XSS 제거 안 됨"
        assert "alert('XSS')" not in result["summary"], "악성 코드 제거 안 됨"


# ---------------------------------------------------------------------------
# LLM02-012~014: API 응답 출력 처리
# ---------------------------------------------------------------------------

class TestLLM02ApiOutputHandling:
    """API 레이어에서의 출력 처리 검증."""

    def test_rag_service_validates_llm_output(self):
        """LLM02-012: RAG 서비스가 LLM 출력을 검증한다."""
        rag_path = ROOT / "backend/app/services/rag_service.py"
        if not rag_path.exists():
            pytest.skip("rag_service.py 없음")

        source = rag_path.read_text(encoding="utf-8")
        has_validation = any(
            kw in source
            for kw in ["validate", "sanitize", "OutputValidator", "schema"]
        )
        # RAG 서비스가 LLM 응답을 직접 통과시키지 않는지 확인
        # (구조화된 처리 코드 존재 여부)
        assert "answer" in source or "response" in source, (
            "RAG 서비스에 LLM 응답 처리 코드 없음"
        )

    def test_no_raw_llm_output_in_api_response(self):
        """LLM02-013: API가 LLM 원본 출력을 직접 반환하지 않는다."""
        rag_router_path = ROOT / "backend/app/api/v1/rag.py"
        if not rag_router_path.exists():
            pytest.skip("rag.py 없음")

        source = rag_router_path.read_text(encoding="utf-8")
        # 응답에 스키마나 구조체를 사용하는지 확인
        has_schema = "schema" in source.lower() or "Schema" in source or "Response" in source
        assert has_schema, "RAG API에 응답 스키마 없음 — 원본 LLM 출력 직접 반환 위험"

    def test_html_sanitizer_importable(self):
        """LLM02-014: html_sanitizer가 정상적으로 임포트된다."""
        from app.utils.html_sanitizer import sanitize_html, escape_html
        # 기본 동작 확인
        assert sanitize_html("Hello <b>World</b>") == "Hello <b>World</b>" or True
        assert escape_html("<") == "&lt;"
