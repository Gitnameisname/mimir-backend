"""
SavedViewsService — S3 Phase 2 FG 2-5.

비즈니스 로직:
  - owner 당 상한 50 강제 (task2-5.md §7 R-03)
  - UNIQUE (owner_id, name) 위반 → 409 ApiConflictError (R-04)
  - PATCH/DELETE 는 owner 본인만 (repository 의 WHERE 절에서 강제)

ACL 정책:
  - GET 단건 (`get_for_share`): 인증된 모든 사용자 접근 허용. 응답에서 owner_id 마스킹
    (SavedViewResponse 가 owner_id 필드 자체 미포함 — task2-5.md §2.1 (5) / §7 R-02)
  - POST/PATCH/DELETE: 인증 + owner 본인만 (라우터 단에서 actor_id == owner_id 검증)
  - GET 목록: owner 본인만 (`list_for_owner`)

본 서비스는 view 의 정의만 다룬다 — view 적용 시 documents API 가 viewer 의 ScopeProfile
로 재필터하므로 본 서비스 자체는 documents ACL 결정점이 아님 (R2 단일 결정점 정합).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import psycopg2
import psycopg2.extensions

from app.api.errors.exceptions import ApiConflictError, ApiNotFoundError
from app.models.saved_view import SavedView
from app.repositories.saved_views_repository import saved_views_repository
from app.schemas.saved_views import (
    SavedViewCreateRequest,
    SavedViewUpdateRequest,
)

logger = logging.getLogger(__name__)


# 사용자당 상한 — task2-5.md §7 R-03
MAX_VIEWS_PER_USER = 50


class SavedViewsService:
    """SavedView 생성 / 수정 / 삭제 / 조회."""

    def list_for_owner(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[SavedView], int]:
        return saved_views_repository.list_by_owner(
            conn, owner_id=owner_id, page=page, page_size=page_size,
        )

    def get_for_share(
        self,
        conn: psycopg2.extensions.connection,
        view_id: str,
    ) -> SavedView:
        """공유 URL 접근 — 인증된 viewer 가 정의를 읽는다 (owner 검증 없음).

        응답 마스킹은 라우터 단에서 SavedViewResponse 로 직렬화하며 자동 (owner_id 필드 없음).
        """
        view = saved_views_repository.get_by_id(conn, view_id)
        if view is None:
            raise ApiNotFoundError(f"Saved view '{view_id}' not found")
        return view

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        request: SavedViewCreateRequest,
    ) -> SavedView:
        # 상한 검증
        cnt = saved_views_repository.count_by_owner(conn, owner_id)
        if cnt >= MAX_VIEWS_PER_USER:
            raise ApiConflictError(
                f"저장된 뷰 상한 ({MAX_VIEWS_PER_USER}) 을 초과했습니다. "
                "기존 뷰를 삭제 후 다시 시도해 주세요.",
            )

        try:
            return saved_views_repository.create(
                conn,
                owner_id=owner_id,
                name=request.name,
                filter=request.filter.model_dump(exclude_none=True),
                sort=[s.model_dump() for s in request.sort],
                layout=request.layout,
                include_tag_nodes=request.include_tag_nodes,
            )
        except psycopg2.errors.UniqueViolation as e:
            raise ApiConflictError(
                f"같은 이름의 뷰가 이미 존재합니다: '{request.name}'",
            ) from e

    def update(
        self,
        conn: psycopg2.extensions.connection,
        *,
        view_id: str,
        owner_id: str,
        request: SavedViewUpdateRequest,
    ) -> SavedView:
        try:
            updated = saved_views_repository.update(
                conn,
                view_id=view_id,
                owner_id=owner_id,
                name=request.name,
                filter=(
                    request.filter.model_dump(exclude_none=True)
                    if request.filter is not None else None
                ),
                sort=(
                    [s.model_dump() for s in request.sort]
                    if request.sort is not None else None
                ),
                layout=request.layout,
                include_tag_nodes=request.include_tag_nodes,
            )
        except psycopg2.errors.UniqueViolation as e:
            raise ApiConflictError(
                f"같은 이름의 뷰가 이미 존재합니다: '{request.name}'",
            ) from e
        if updated is None:
            # owner 가 아니거나 view_id 가 없는 경우 동일 — 존재 유출 차단
            raise ApiNotFoundError(f"Saved view '{view_id}' not found")
        return updated

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        *,
        view_id: str,
        owner_id: str,
    ) -> None:
        ok = saved_views_repository.delete(conn, view_id=view_id, owner_id=owner_id)
        if not ok:
            raise ApiNotFoundError(f"Saved view '{view_id}' not found")


# 모듈 수준 싱글턴
saved_views_service = SavedViewsService()
