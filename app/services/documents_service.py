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

import json
import logging
from typing import Any, Optional, Sequence

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.api.errors.exceptions import ApiNotFoundError, ApiValidationError
from app.api.query.models import ParsedListQuery
from app.models.document import Document
from app.repositories.documents_repository import documents_repository
from app.schemas.documents import DocumentCreateRequest, DocumentResponse, DocumentUpdateRequest
from app.utils.http_errors import not_found_resource

logger = logging.getLogger(__name__)

# S3 Phase 2 FG 2-0 (2026-04-24): Scope Profile 필터를 우회하는 관리자 role
# 주의: Scope 어휘가 아닌 **role 어휘** — S2 ⑤ "scope 어휘 하드코딩 금지" 와 무관.
_SCOPE_FILTER_BYPASS_ROLES = frozenset({"SUPER_ADMIN", "ORG_ADMIN"})


def _resolve_viewer_scope_profile_ids(
    actor: Optional[ActorContext],
) -> Optional[Sequence[str]]:
    """ActorContext → DocumentsRepository.list/get_by_id 의 viewer_scope_profile_ids.

    반환 규약:
      * ``None``: 필터 skip (bypass role 또는 actor=None 내부 호출)
      * ``[]``: scope 없음 → 결과 없음
      * ``[<uuid>, ...]``: 해당 scope set 내 문서만 접근

    관리자 확장 여지: 향후 admin 이 "자기 프로파일 + 위임받은 프로파일" 집합을 보려면
    본 함수가 scope_profile_id 단건이 아닌 union 집합을 반환하도록 확장한다.
    """
    if actor is None:
        return None  # 레거시/내부 호출 — 필터 skip
    if actor.role and actor.role in _SCOPE_FILTER_BYPASS_ROLES:
        return None
    if actor.scope_profile_id:
        return [actor.scope_profile_id]
    # 인증은 됐으나 scope_profile_id 가 없는 사용자 — 결과 없음 (S2 ⑥ 에 따른 차단)
    return []


def _validate_metadata_against_schema(
    conn: psycopg2.extensions.connection,
    document_type: str,
    metadata: dict[str, Any],
) -> None:
    """document_types.schema_fields의 required 필드가 metadata에 모두 존재하는지 검증.

    document_type이 DB에 없거나 schema_fields가 비어있으면 검증을 건너뛴다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_fields FROM document_types WHERE type_code = %s AND status = 'active'",
            (document_type,),
        )
        row = cur.fetchone()

    if row is None or not row[0]:
        return  # 미등록 타입이거나 스키마 없음 — 검증 생략

    schema_fields = row[0] if isinstance(row[0], list) else json.loads(row[0])
    missing = [
        f["name"]
        for f in schema_fields
        if f.get("required") and f["name"] not in metadata
    ]
    if missing:
        raise ApiValidationError(
            f"metadata에 필수 필드가 없습니다: {', '.join(missing)}"
        )


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
        scope_profile_id=doc.scope_profile_id,
        folder_id=doc.folder_id,
        in_collection_ids=list(doc.in_collection_ids),
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
        actor: Optional[ActorContext] = None,
    ) -> DocumentResponse:
        """문서를 생성하고 생성된 DocumentResponse를 반환한다.

        ``actor`` 가 제공되면 생성 시점에 actor.scope_profile_id 를 documents.scope_profile_id
        기본값으로 주입한다 (FG 2-0). ``actor_id`` 만 전달되는 레거시 호출자는 scope_profile_id
        가 NULL 로 남고 admin-only 상태가 된다 (운영자 수동 바인딩 필요).

        TODO (Task I-9): 이 메서드 상단이 idempotency hook 삽입 슬롯이다.
        """
        _validate_metadata_against_schema(conn, request.document_type, request.metadata)

        scope_profile_id: Optional[str] = None
        if actor is not None and actor.scope_profile_id:
            scope_profile_id = actor.scope_profile_id

        doc = documents_repository.create(
            conn,
            title=request.title,
            document_type=request.document_type,
            status=request.status.value,
            metadata=request.metadata,
            summary=request.summary,
            created_by=actor_id,
            scope_profile_id=scope_profile_id,
        )
        logger.info(
            "Document created: id=%s, type=%s, actor=%s, scope_profile_id=%s",
            doc.id, doc.document_type, actor_id, scope_profile_id,
        )
        return _to_response(doc)

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def get_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        actor: Optional[ActorContext] = None,
    ) -> DocumentResponse:
        """문서를 조회한다. 존재하지 않거나 viewer Scope 밖이면 ApiNotFoundError.

        Scope 밖 문서를 **404 로 반환**하는 이유: 존재 유출 방지 (403 으로 알리지 않음).

        FG 2-1 UX 2차 (2026-04-24): actor 가 전달되면 현재 배치된 폴더 id 와 요청자 소유
        컬렉션 포함 목록도 함께 채워 응답한다. actor 없는 내부 호출 경로에서는 기본값
        (None / []) 으로 남는다.
        """
        viewer_ids = _resolve_viewer_scope_profile_ids(actor)
        doc = documents_repository.get_by_id(
            conn, document_id, viewer_scope_profile_ids=viewer_ids,
        )
        if doc is None:
            raise not_found_resource("문서", document_id)

        # 배치 상태 채우기 — owner 범위에서만 (Scope 어휘 하드코딩 아닌 owner 기반 격리)
        document_tags: list[dict[str, Any]] = []
        if actor is not None and actor.actor_id:
            # 순환 import 회피: 함수 내부 import
            from app.repositories.collections_repository import collections_repository
            from app.repositories.folders_repository import folders_repository
            from app.repositories.tags_repository import tags_repository

            doc.folder_id = folders_repository.get_folder_of_document(conn, document_id)
            doc.in_collection_ids = collections_repository.list_collection_ids_for_document(
                conn, document_id=document_id, owner_id=str(actor.actor_id),
            )
            # S3 Phase 2 FG 2-2 (2026-04-24): 서버 파서가 계산한 태그 목록 포함
            for tag, source in tags_repository.list_for_document(conn, document_id):
                document_tags.append({
                    "id": tag.id,
                    "name": tag.name_normalized,
                    "source": source,
                })
        response = _to_response(doc)
        response.document_tags = document_tags
        return response

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_documents(
        self,
        conn: psycopg2.extensions.connection,
        query: ParsedListQuery,
        *,
        actor: Optional[ActorContext] = None,
    ) -> tuple[list[DocumentResponse], int]:
        """문서 목록과 전체 건수를 반환한다.

        FG 2-0 ACL: actor 가 전달되면 viewer Scope 로 자동 필터.

        Returns:
            (document_responses, total_count)
        """
        viewer_ids = _resolve_viewer_scope_profile_ids(actor)
        docs, total = documents_repository.list(
            conn, query, viewer_scope_profile_ids=viewer_ids,
        )
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
        actor: Optional[ActorContext] = None,
    ) -> DocumentResponse:
        """문서를 부분 수정한다.

        수정 정책:
          - 명시된 필드만 수정 (None = 수정하지 않음).
          - document_type은 immutable — request에 포함해도 무시.
          - metadata: 전체 replace 정책 (shallow merge 아님).
          - 수정할 필드가 없으면 현재 상태 그대로 반환.
          - **scope_profile_id 는 본 메서드에서 변경하지 않는다** (FG 2-0).
            Scope 재할당은 별도 admin API 에서 수행 예정.

        FG 2-0: 업데이트 전 조회 단계에서 viewer Scope ACL 적용 → Scope 밖 문서는
        ApiNotFoundError.

        TODO (Task I-9): idempotency hook 삽입 슬롯 (write 경로).
        """
        if not request.has_updates():
            # 수정할 내용이 없으면 현재 문서 반환 (no-op) — ACL 도 여기서 적용됨
            return self.get_document(conn, document_id, actor=actor)

        # 수정 전 존재 및 접근 권한 확인 (ACL 통과 시에만 update 로 진행)
        current = self.get_document(conn, document_id, actor=actor)

        # metadata가 교체되는 경우 schema 검증 수행
        if request.metadata is not None:
            _validate_metadata_against_schema(conn, current.document_type, request.metadata)

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
            raise not_found_resource("문서", document_id)

        # S3 Phase 2 FG 2-2 (2026-04-25): metadata 가 교체된 경우 frontmatter 기반
        # 태그가 바뀔 수 있으므로 document_tags 를 재계산한다. 인라인 태그 보존을 위해
        # 현재 활성 draft (없으면 published) 스냅샷을 함께 전달. 같은 트랜잭션 내에서
        # 실행되어 부분 상태 노출을 차단 (블로커1 결정서 §3.2 "서버 파서가 정본").
        if request.metadata is not None:
            from app.repositories.versions_repository import versions_repository
            from app.services.snapshot_sync_service import rebuild_tags_for_document

            snapshot: Any = None
            active_version_id = (
                updated.current_draft_version_id
                or updated.current_published_version_id
            )
            if active_version_id:
                v = versions_repository.get_by_id(conn, active_version_id)
                if v is not None:
                    snapshot = v.content_snapshot

            rebuild_tags_for_document(
                conn,
                document_id=document_id,
                snapshot=snapshot,
                metadata=updated.metadata or {},
            )

        logger.info("Document updated: id=%s, actor=%s", document_id, actor_id)
        return _to_response(updated)


# 모듈 수준 싱글턴
documents_service = DocumentsService()
