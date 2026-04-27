"""
Collections 서비스 — S3 Phase 2 FG 2-1.

책임:
  - 컬렉션 CRUD 비즈니스 로직
  - 이름 정규화 (앞뒤 공백 제거, 길이 상한 재확인)
  - 소유권 검증 (owner 만 편집. admin 는 bypass 가능하나 FG 2-1 범위에서는 owner 고정)
  - 문서 추가 시 **viewer Scope ACL 통과한 문서만** 연결 (documents.scope_profile_id)

절대 규칙 (Phase 2):
  - 컬렉션은 순수 뷰 레이어 — ACL 에 영향을 주지 않는다
  - 다만 컬렉션에 **Scope 밖 문서를 넣는 행위** 는 거부해야 한다 (우회 수단 방지)
"""

import logging
from typing import Optional, Sequence

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.api.errors.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ApiValidationError,
)
from app.models.collection import Collection
from app.repositories.collections_repository import collections_repository
from app.repositories.documents_repository import documents_repository
from app.services.documents_service import _resolve_viewer_scope_profile_ids
from app.utils.actor import require_actor_id
from app.utils.strings import normalize_display_name
from app.utils.http_errors import not_found_resource

logger = logging.getLogger(__name__)

_NAME_MIN = 1
_NAME_MAX = 200
_NAME_LABEL = "컬렉션 이름"
_DESCRIPTION_MAX = 2000


def _normalize_name(raw: str) -> str:
    """컬렉션 이름 정규화 (공백 압축 + 길이 검사).

    대소문자는 보존(표시용). UNIQUE 제약은 DB 에서 그대로 비교.
    내부적으로 :func:`app.utils.strings.normalize_display_name` 에 위임한다.
    Docs: ``docs/함수도서관/backend.md`` §1.4 B6.
    """
    return normalize_display_name(
        raw,
        _NAME_MIN,
        _NAME_MAX,
        label=_NAME_LABEL,
    )


def _validate_description(desc: Optional[str]) -> Optional[str]:
    if desc is None:
        return None
    if len(desc) > _DESCRIPTION_MAX:
        raise ApiValidationError(
            f"설명은 {_DESCRIPTION_MAX}자를 초과할 수 없습니다",
        )
    return desc


def _require_actor(actor: Optional[ActorContext]) -> str:
    """actor 검증 + owner_id 반환 — 도서관 §1.10 BE-G6 (2026-04-25): require_actor_id 위임.

    기존 호출지 호환을 위한 thin wrapper. 메시지는 helper 표준 "인증된 컬렉션만
    작업을 수행할 수 있습니다" 로 변경.
    """
    return require_actor_id(actor, label="컬렉션")


class CollectionsService:
    """컬렉션 비즈니스 로직."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_collection(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        name: str,
        description: Optional[str] = None,
    ) -> Collection:
        owner_id = _require_actor(actor)
        name = _normalize_name(name)
        description = _validate_description(description)
        try:
            return collections_repository.create(
                conn, owner_id=owner_id, name=name, description=description,
            )
        except psycopg2.errors.UniqueViolation as exc:
            raise ApiConflictError(
                f"같은 이름의 컬렉션이 이미 존재합니다: '{name}'",
            ) from exc

    def get_collection(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
    ) -> Collection:
        owner_id = _require_actor(actor)
        coll = collections_repository.get_by_id(
            conn, collection_id, owner_id=owner_id,
        )
        if coll is None:
            raise not_found_resource("컬렉션", collection_id)
        return coll

    def list_collections(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Collection], int]:
        owner_id = _require_actor(actor)
        return collections_repository.list_by_owner(
            conn, owner_id, limit=limit, offset=offset, include_counts=True,
        )

    def update_collection(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Collection:
        # 소유권 확인 (없으면 404)
        self.get_collection(conn, collection_id, actor=actor)

        normalized_name = _normalize_name(name) if name is not None else None
        validated_desc = _validate_description(description) if description is not None else None

        try:
            updated = collections_repository.update(
                conn, collection_id,
                name=normalized_name,
                description=validated_desc,
            )
        except psycopg2.errors.UniqueViolation as exc:
            raise ApiConflictError(
                f"같은 이름의 컬렉션이 이미 존재합니다: '{normalized_name}'",
            ) from exc
        if updated is None:
            raise not_found_resource("컬렉션", collection_id)
        return updated

    def delete_collection(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
    ) -> None:
        # 소유권 확인
        self.get_collection(conn, collection_id, actor=actor)
        collections_repository.delete(conn, collection_id)

    # ------------------------------------------------------------------
    # 문서 연결 (N:M) — Scope ACL 통과 문서만 허용
    # ------------------------------------------------------------------

    def add_documents(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
        document_ids: Sequence[str],
    ) -> dict[str, int]:
        """문서를 컬렉션에 추가.

        viewer Scope 밖의 문서 id 는 **조용히 제외** (존재 유출 방지). 호출자에게는
        삽입된 수 / 요청된 수 / 제외된 수를 리포트.
        """
        self.get_collection(conn, collection_id, actor=actor)
        viewer_ids = _resolve_viewer_scope_profile_ids(actor)

        # Scope 통과 문서만 필터링
        accepted: list[str] = []
        for doc_id in document_ids:
            doc = documents_repository.get_by_id(
                conn, doc_id, viewer_scope_profile_ids=viewer_ids,
            )
            if doc is not None:
                accepted.append(doc.id)

        inserted = collections_repository.add_documents(
            conn, collection_id=collection_id, document_ids=accepted,
        )
        return {
            "requested": len(document_ids),
            "accepted": len(accepted),
            "inserted": inserted,
            "rejected": len(document_ids) - len(accepted),
        }

    def remove_document(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
        document_id: str,
    ) -> bool:
        self.get_collection(conn, collection_id, actor=actor)
        return collections_repository.remove_document(
            conn, collection_id=collection_id, document_id=document_id,
        )

    def list_document_ids(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        actor: ActorContext,
    ) -> list[str]:
        """컬렉션 내 문서 id 목록 — viewer Scope 로 필터."""
        self.get_collection(conn, collection_id, actor=actor)
        viewer_ids = _resolve_viewer_scope_profile_ids(actor)
        return collections_repository.list_document_ids(
            conn, collection_id, viewer_scope_profile_ids=viewer_ids,
        )


# 모듈 수준 싱글턴
collections_service = CollectionsService()
