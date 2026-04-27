"""
Versions 서비스 계층.

책임:
  - versions 비즈니스 흐름 제어
  - document 존재 여부 검증 (document-version 관계 무결성)
  - version_number 계산 및 부여
  - node snapshot 저장 조합 (같은 트랜잭션 내)
  - router에 반환할 VersionResponse DTO 생성

설계 원칙:
  - service는 raw FastAPI Request에 의존하지 않는다.
  - actor_id (Optional[str]) 형태로 actor 정보를 받는다.
  - DB 연결(conn)은 router에서 주입받는다.
  - version create = 구조 스냅샷 생성 (diff 계산 아님).
  - diff/restore/publish semantics는 이번 단계 범위 밖.
"""

import logging
from typing import Any, Optional

import psycopg2.extensions

from app.api.errors.exceptions import ApiNotFoundError
from app.api.query.models import ParsedListQuery
from app.models.version import Version
from app.repositories.documents_repository import documents_repository
from app.repositories.nodes_repository import nodes_repository
from app.repositories.versions_repository import versions_repository
from app.schemas.versions import VersionCreateRequest, VersionResponse
from app.utils.http_errors import not_found_resource

logger = logging.getLogger(__name__)


def _to_response(version: Version) -> VersionResponse:
    """Version 도메인 모델 → VersionResponse DTO 변환."""
    return VersionResponse(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        label=version.label,
        status=version.status,
        change_summary=version.change_summary,
        source=version.source,
        metadata=version.metadata,
        created_by=version.created_by,
        created_at=version.created_at,
    )


def _resolve_node_items(
    request_nodes: list[Any],
) -> list[dict[str, Any]]:
    """NodeCreateItem 목록을 parent_index → parent_id 매핑을 포함한 dict 목록으로 변환.

    parent_index는 nodes 배열 내 인덱스를 가리킨다.
    버전 생성 시 nodes를 순서대로 INSERT하므로,
    parent_index 참조는 이번 단계에서 지원하지 않고 None으로 처리한다.

    (parent_index 기반 트리 해석은 이후 Task에서 확장 가능.)
    """
    items = []
    for node in request_nodes:
        items.append(
            {
                "parent_id": None,  # TODO: parent_index 기반 해석 후속 Task에서 확장
                "node_type": node.node_type,
                "order_index": node.order_index,
                "title": node.title,
                "content": node.content,
                "metadata": node.metadata,
            }
        )
    return items


class VersionsService:
    """Versions 핵심 비즈니스 로직 서비스."""

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_versions(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        query: ParsedListQuery,
    ) -> tuple[list[VersionResponse], int]:
        """문서별 버전 목록과 전체 건수를 반환한다.

        document 존재 여부를 먼저 검증한다.
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise not_found_resource("문서", document_id)

        # ParsedListQuery에서 sort/page 추출
        sort_field = "created_at"
        sort_dir = "DESC"
        if query.sort_orders:
            first = query.sort_orders[0]
            sort_field = first.field
            sort_dir = "DESC" if first.direction == "desc" else "ASC"

        page = query.page if query.page else 1
        page_size = query.page_size if query.page_size else 20

        versions, total = versions_repository.list_by_document_id(
            conn,
            document_id,
            page=page,
            page_size=page_size,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
        return [_to_response(v) for v in versions], total

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create_version(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        request: VersionCreateRequest,
        *,
        actor_id: Optional[str] = None,
    ) -> VersionResponse:
        """문서에 새 버전(구조 스냅샷)을 생성하고 VersionResponse를 반환한다.

        흐름:
          1. document 존재 검증
          2. 다음 version_number 계산
          3. version row 생성
          4. node snapshot 일괄 생성 (같은 conn/트랜잭션)

        TODO (Task I-9): 이 메서드 상단이 idempotency hook 삽입 슬롯.
        """
        doc = documents_repository.get_by_id(conn, document_id)
        if doc is None:
            raise not_found_resource("문서", document_id)

        next_number = versions_repository.get_next_version_number(conn, document_id)

        version = versions_repository.create(
            conn,
            document_id=document_id,
            version_number=next_number,
            label=request.label,
            status="draft",
            change_summary=request.change_summary,
            source=request.source.value,
            metadata=request.metadata,
            created_by=actor_id,
        )

        # node snapshot 저장 (같은 트랜잭션)
        node_items = _resolve_node_items(request.nodes)
        if node_items:
            nodes_repository.bulk_create_for_version(conn, version.id, node_items)

        logger.info(
            "Version created: id=%s, document_id=%s, version_number=%d, nodes=%d, actor=%s",
            version.id,
            document_id,
            next_number,
            len(node_items),
            actor_id,
        )
        return _to_response(version)

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def get_version(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> VersionResponse:
        """버전을 조회한다. 존재하지 않으면 ApiNotFoundError."""
        version = versions_repository.get_by_id(conn, version_id)
        if version is None:
            raise not_found_resource("버전", version_id)
        return _to_response(version)


# 모듈 수준 싱글턴
versions_service = VersionsService()
