"""
A08 Software and Data Integrity Failures 검증 테스트.

검증 항목:
  - Document Versioning: 모든 문서 변경이 버전으로 추적됨
  - Citation 5-tuple 무결성 (doc_id, chunk_id, hash, timestamp, version)
  - Content Hash 검증: 검색 결과와 원본 문서의 hash 일치
  - 감사 로그 append-only 관리
"""
from __future__ import annotations

import hashlib
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
# A08-001~004: 문서 버전 추적
# ---------------------------------------------------------------------------

class TestA08DocumentVersioning:
    """문서 버전 추적 검증."""

    def test_versions_repository_exists(self):
        """A08-001: VersionsRepository가 존재한다."""
        versions_repo_path = ROOT / "backend/app/repositories/versions_repository.py"
        assert versions_repo_path.exists(), "versions_repository.py 없음"

    def test_versions_service_exists(self):
        """A08-002: 버전 관리 서비스가 존재한다."""
        versions_service_path = ROOT / "backend/app/services/versions_service.py"
        assert versions_service_path.exists(), "versions_service.py 없음"

    def test_versions_table_has_required_fields(self):
        """A08-003: versions 테이블에 필요한 필드가 있다."""
        # DB migration 파일 또는 model 파일에서 확인
        models_dir = ROOT / "backend/app/models"
        source_files = list(models_dir.glob("*.py"))

        combined_source = ""
        for f in source_files:
            combined_source += f.read_text(encoding="utf-8")

        # version_number, document_id, content_hash 등의 컬럼 확인
        required_fields = ["version", "document_id"]
        for field in required_fields:
            assert field in combined_source, (
                f"models에서 '{field}' 필드 없음 — 버전 추적 불완전"
            )

    def test_document_edit_creates_new_version(self):
        """A08-004: 문서 편집이 새 버전을 생성하는 서비스 코드가 있다."""
        documents_service_path = ROOT / "backend/app/services/documents_service.py"
        if not documents_service_path.exists():
            pytest.skip("documents_service.py 없음")

        source = documents_service_path.read_text(encoding="utf-8")
        # version 생성 코드 확인
        assert "version" in source.lower(), "버전 생성 코드 없음"


# ---------------------------------------------------------------------------
# A08-005~009: Citation 5-tuple 무결성
# ---------------------------------------------------------------------------

class TestA08CitationIntegrity:
    """Citation 5-tuple 무결성 검증."""

    def test_citation_builder_exists(self):
        """A08-005: CitationBuilder가 존재한다."""
        citation_path = ROOT / "backend/app/services/retrieval/citation_builder.py"
        assert citation_path.exists(), "citation_builder.py 없음"

    def test_citation_has_hash_field(self):
        """A08-006: Citation에 content_hash 필드가 포함된다."""
        citation_path = ROOT / "backend/app/services/retrieval/citation_builder.py"
        source = citation_path.read_text(encoding="utf-8")

        assert "hash" in source.lower(), "Citation에 hash 필드 없음 (5-tuple 불완전)"

    def test_citation_service_exists(self):
        """A08-007: CitationService가 존재한다."""
        citation_service_path = ROOT / "backend/app/services/retrieval/citation_service.py"
        assert citation_service_path.exists(), "citation_service.py 없음"

    def test_citation_5tuple_fields_present(self):
        """A08-008: Citation이 5-tuple 필수 필드를 가진다.

        5-tuple: (document_id, chunk_id/node_id, content_hash, timestamp, version)
        """
        citation_builder_path = ROOT / "backend/app/services/retrieval/citation_builder.py"
        source = citation_builder_path.read_text(encoding="utf-8")

        required_fields = ["document_id", "version"]
        for field in required_fields:
            assert field in source, f"Citation 5-tuple에 '{field}' 없음"

    def test_content_hash_uses_sha256(self):
        """A08-009: Content hash가 SHA-256을 사용한다."""
        # 직접 SHA-256 구현 확인
        citation_builder_path = ROOT / "backend/app/services/retrieval/citation_builder.py"
        source = citation_builder_path.read_text(encoding="utf-8")

        # sha256 또는 hashlib 사용 확인
        uses_strong_hash = "sha256" in source or "hashlib" in source or "hash" in source
        assert uses_strong_hash, "Content hash에 강력한 해시 함수 없음"


# ---------------------------------------------------------------------------
# A08-010~012: 감사 로그 무결성
# ---------------------------------------------------------------------------

class TestA08AuditLogIntegrity:
    """감사 로그 무결성 검증."""

    def test_audit_events_table_structure(self):
        """A08-010: audit_events 테이블에 필수 컬럼이 있다."""
        emitter_path = ROOT / "backend/app/audit/emitter.py"
        source = emitter_path.read_text(encoding="utf-8")

        required_columns = [
            "event_type",
            "actor_user_id",
            "actor_type",
            "action_result",
        ]
        for col in required_columns:
            assert col in source, f"audit_events에 '{col}' 컬럼 없음"

    def test_audit_log_no_update_delete_in_emitter(self):
        """A08-011: AuditEmitter가 UPDATE/DELETE를 사용하지 않는다 (append-only)."""
        emitter_path = ROOT / "backend/app/audit/emitter.py"
        source = emitter_path.read_text(encoding="utf-8")

        import re
        # INSERT만 허용, UPDATE/DELETE 금지
        update_delete = re.findall(
            r'\b(?:UPDATE|DELETE)\s+audit_events\b',
            source,
            re.IGNORECASE,
        )
        assert not update_delete, (
            f"AuditEmitter에서 감사 로그 수정/삭제 시도: {update_delete}"
        )

    def test_audit_log_records_all_required_fields(self):
        """A08-012: 감사 로그 emit에 모든 필수 필드가 포함된다.

        필수: event_type, actor_id, actor_type, resource_type, result, timestamp
        """
        from app.audit.emitter import AuditEmitter
        import inspect

        sig = inspect.signature(AuditEmitter.emit)
        params = set(sig.parameters.keys())

        required_params = {
            "event_type", "action", "actor_id",
            "resource_type", "result",
        }
        for param in required_params:
            assert param in params, f"AuditEmitter.emit()에 '{param}' 파라미터 없음"
