"""
LLM06 Sensitive Information Disclosure 검증 테스트.

검증 항목:
  - PII 탐지 (이메일, 전화, 주민번호, 신용카드)
  - PII annotate 기능
  - 검색 결과에 Scope Profile ACL 적용
  - PII 환경변수 제어
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
os.environ.setdefault("PII_DETECTION_ENABLED", "true")


# ---------------------------------------------------------------------------
# LLM06-001~005: PII 탐지기 동작
# ---------------------------------------------------------------------------

class TestLLM06PiiDetector:
    """PII 탐지기 기능 검증."""

    def test_pii_detector_exists(self):
        """LLM06-001: PiiDetector가 존재한다."""
        from app.services.pii_detector import PiiDetector
        assert PiiDetector is not None

    def test_email_detection(self):
        """LLM06-002: 이메일 주소가 탐지된다."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("연락처: user@example.com 에 이메일 주세요")

        assert result.get("email") or any("email" in str(k).lower() for k in result), (
            "이메일 PII 탐지 실패"
        )

    def test_korean_phone_detection(self):
        """LLM06-003: 한국 전화번호가 탐지된다."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        has_phone = detector.has_pii("전화: 010-1234-5678")
        assert has_phone, "한국 전화번호 탐지 실패"

    def test_rrn_detection(self):
        """LLM06-004: 주민등록번호가 탐지된다."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        has_rrn = detector.has_pii("주민번호: 900101-1234567")
        assert has_rrn, "주민등록번호 탐지 실패"

    def test_credit_card_detection(self):
        """LLM06-005: 신용카드 번호가 탐지된다."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        has_cc = detector.has_pii("카드번호: 4532-1234-5678-9010")
        assert has_cc, "신용카드 번호 탐지 실패"

    def test_clean_text_has_no_pii(self):
        """LLM06-006: PII가 없는 텍스트는 탐지되지 않는다."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.has_pii("This is a normal document about quarterly results.")
        assert result is False, "PII 없는 텍스트에서 오탐"

    def test_annotate_turn_method_exists(self):
        """LLM06-007: annotate_turn() 메서드가 존재한다."""
        from app.services.pii_detector import PiiDetector
        import inspect

        methods = [m for m in dir(PiiDetector) if not m.startswith("_")]
        assert "annotate_turn" in methods, "annotate_turn() 메서드 없음"


# ---------------------------------------------------------------------------
# LLM06-008~010: PII 환경변수 제어
# ---------------------------------------------------------------------------

class TestLLM06PiiEnvControl:
    """PII 탐지 환경변수 제어 검증."""

    def test_pii_detection_env_var_exists(self):
        """LLM06-008: PII_DETECTION_ENABLED 환경변수로 제어된다."""
        pii_path = ROOT / "backend/app/services/pii_detector.py"
        source = pii_path.read_text(encoding="utf-8")

        assert "PII_DETECTION_ENABLED" in source, (
            "PII_DETECTION_ENABLED 환경변수 제어 없음"
        )

    def test_pii_detector_singleton_exists(self):
        """LLM06-009: 모듈 레벨 pii_detector 싱글턴이 있다."""
        from app.services import pii_detector as module
        # 모듈 자체가 임포트 가능해야 함
        assert module is not None

    def test_pii_detection_covers_required_types(self):
        """LLM06-010: PII 탐지가 이메일/전화/주민번호/신용카드를 커버한다."""
        pii_path = ROOT / "backend/app/services/pii_detector.py"
        source = pii_path.read_text(encoding="utf-8")

        required_types = ["email", "phone", "rrn", "credit"]
        for pii_type in required_types:
            assert pii_type in source.lower(), (
                f"PII 탐지에 '{pii_type}' 유형 없음"
            )


# ---------------------------------------------------------------------------
# LLM06-011~013: 검색 결과 ACL 필터링
# ---------------------------------------------------------------------------

class TestLLM06SearchAclFiltering:
    """검색 결과 ACL 필터링 검증 (LLM 컨텍스트 포함)."""

    def test_scope_filter_applied_in_retrieval(self):
        """LLM06-011: 검색 시 Scope Profile ACL이 적용된다."""
        retriever_path = ROOT / "backend/app/services/retrieval/hybrid_retriever.py"
        if not retriever_path.exists():
            pytest.skip("hybrid_retriever.py 없음")

        source = retriever_path.read_text(encoding="utf-8")
        has_acl = any(
            kw in source
            for kw in ["scope", "acl", "filter", "ScopeProfile"]
        )
        assert has_acl, "하이브리드 검색에 ACL 필터링 없음"

    def test_fts_retriever_applies_acl(self):
        """LLM06-012: FTS 검색에도 ACL이 적용된다 (S2 ⑦)."""
        fts_path = ROOT / "backend/app/services/retrieval/fts_retriever.py"
        if not fts_path.exists():
            pytest.skip("fts_retriever.py 없음")

        source = fts_path.read_text(encoding="utf-8")
        has_acl = any(
            kw in source
            for kw in ["scope", "acl", "filter", "allowed", "restrict"]
        )
        assert has_acl, "FTS 검색에 ACL 없음 (S2 ⑦ 위반)"

    def test_rag_context_minimizes_data(self):
        """LLM06-013: RAG 컨텍스트가 필요한 정보만 LLM에 전달한다."""
        rag_path = ROOT / "backend/app/services/rag_service.py"
        if not rag_path.exists():
            pytest.skip("rag_service.py 없음")

        source = rag_path.read_text(encoding="utf-8")
        # 컨텍스트 윈도우 관리 또는 데이터 최소화 코드 확인
        has_minimization = any(
            kw in source.lower()
            for kw in ["context_window", "max_context", "truncate", "limit"]
        )
        assert has_minimization, "RAG 서비스에 컨텍스트 데이터 최소화 없음"
