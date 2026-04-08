"""
Documents 서비스 계층.

책임:
  - documents 비즈니스 흐름 제어
  - 입력 정규화 및 도메인 검증
  - repository 호출 조합
  - 상태/metadata 처리 정책 적용
  - router에 반환할 DocumentResponse DTO 생성

설계 원칙:
  - service는 raw FastAPI Request에 의존하지 않는다.
  - actor_id (Optional[str]) 형태로 actor 정보를 받는다.
  - DB 연결(conn)은 router에서 주입받는다 — get_db() 컨텍스트가 트랜잭션 경계를 관리.
  - 이후 idempotency hook 삽입 슬롯은 create/update 메서드 상단에 표시.
  - versions / audit logging은 이 서비스를 확장하거나 별도 서비스로 분리해 붙인다.
"""

import logging
from typing import Optional

import psycopg2.extensions

from app.api.errors.exceptions import ApiNotFoundError, ApiValidationError
from app.api.query.models import ParsedListQuery
from app.models.document import Document
from app.repositories.documents_repository import documents_repository
from app.schemas.documents import DocumentCreateRequest, DocumentResponse, DocumentUpdateRequest

logger = logging.getLogger(__name__)


def _to_response(doc: Document) -> DocumentResponse:
    """Document 도메인 모델 → DocumentResponse DTO 변환."""
    return DocumentResponse(
        id=doc.id,
        title=doc.title,
        document_type=doc.document_type,
        status=doc.status,
        metadata=doc.metadata,
        summary=doc.summary,
        created_by=doc.created_by,
        updated_by=doc.updated_by,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        current_draft_version_id=doc.current_draft_version_id,
        current_published_version_id=doc.current_published_version_id,
    )


class DocumentsService:
    """Documents 핵심 비즈니스 로직 서비스."""

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create_document(
        self,
        conn: psycopg2.extensions.connection,
        request: DocumentCreateRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> DocumentResponse:
        """문서를 생성하고 생성된 DocumentResponse를 반환한다.

        TODO (Task I-9): 이 메서드 상단이 idempotency hook 삽입 슬롯이다.
            idempotency_key가 주어지면 replay 여부를 먼저 확인한다.
        """
        doc = documents_repository.create(
            conn,
            title=request.title,
            document_type=request.document_type,
            status=request.status.value,
            metadata=request.metadata,
            summary=request.summary,
            created_by=actor_id,
        )
        logger.info("Document created: id=%s, type=%s, actor=%s", doc.id, doc.document_type, actor_id)
        return _to_response(doc)

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def get_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> DocumentResponse:
        """문서를 조회한다. 존재하지 않으면 ApiNotFoundError."""
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")
        return _to_response(doc)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_documents(
        self,
        conn: psycopg2.extensions.connection,
        query: ParsedListQuery,
    ) -> tuple[list[DocumentResponse], int]:
        """문서 목록과 전체 건수를 반환한다.

        Returns:
            (document_responses, total_count)
        """
        docs, total = documents_repository.list(conn, query)
        return [_to_response(doc) for doc in docs], total

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    def update_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        request: DocumentUpdateRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> DocumentResponse:
        """문서를 부분 수정한다.

        수정 정책:
          - 명시된 필드만 수정 (None = 수정하지 않음).
          - document_type은 immutable — request에 포함해도 무시.
          - metadata: 전체 replace 정책 (shallow merge 아님).
          - 수정할 필드가 없으면 현재 상태 그대로 반환.

        TODO (Task I-9): idempotency hook 삽입 슬롯 (write 경로).
        TODO (Task I-10): audit logging — updated_by 강화 예정.
        """
        if not request.has_updates():
            # 수정할 내용이 없으면 현재 문서 반환 (no-op)
            return self.get_document(conn, document_id)

        updated = documents_repository.update(
            conn,
            document_id,
            title=request.title,
            status=request.status.value if request.status else None,
            metadata=request.metadata,
            summary=request.summary,
            updated_by=actor_id,
        )
        if updated is None:
            raise ApiNotFoundError(f"Document '{document_id}' not found")

        logger.info("Document updated: id=%s, actor=%s", document_id, actor_id)
        return _to_response(updated)


# 모듈 수준 싱글턴
documents_service = DocumentsService()
