"""Contributors 서비스 — S3 Phase 3 FG 3-1.

문서 한 건의 4 카테고리 contributors 묶음을 반환한다.

흐름:
    1. ACL 통과 검증 — `documents_service.get_document` 를 호출해 viewer scope 밖이면 404.
    2. Repository 4 개 호출 (creator / editors / approvers / viewers).
    3. Distinct user_id batch fetch → display_name 매핑.
    4. creator 와 editors 중복 제거 (creator id 가 editors 에도 있으면 editors 에서 빠짐).
    5. include_viewers 결정 — 본 FG 단계에서는 호출자 의사 그대로.
       (FG 3-2 가 정책 게이트 결합 시 별도 모듈에서 강제 false 가능)
    6. ContributorsBundle 반환.

표시명 결정 우선순위:
    - actor_type == 'user'  → users.display_name → "(알 수 없는 사용자)"
    - actor_type == 'agent' → "에이전트" (TODO: agents 테이블 join 으로 agent.name 보강은 별 라운드)
    - actor_type == 'system' → "Mimir 시스템"
    - actor_type 누락       → "user" 로 default 후 위 규칙 적용

함수 도서관: ``docs/함수도서관/backend.md`` §3-fg31 (도메인 service — 도서관 §0 등록 대상은 아님).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.models.contributors import Contributor, ContributorActorType, ContributorsBundle
from app.repositories.contributors_repository import contributors_repository
from app.repositories.users_repository import UsersRepository
from app.services.documents_service import documents_service
from app.services.scope_profile_policy import should_expose_viewers
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# 호출자가 since 를 지정하지 않은 경우 viewers 카테고리에 적용할 기본 윈도우.
DEFAULT_VIEWER_SINCE_DAYS: int = 30
DEFAULT_LIMIT_PER_SECTION: int = 50
MAX_LIMIT_PER_SECTION: int = 200


_users_repository = UsersRepository()


def _normalize_actor_type(raw: Optional[str]) -> ContributorActorType:
    """audit_events.actor_type 의 자유 문자열을 Literal 3 분류로 좁힌다.

    DB 가 enum 강제를 하지 않으므로 alien 값은 'user' 로 기본 매핑.
    """
    if not raw:
        return "user"
    lowered = str(raw).lower()
    if lowered == "agent":
        return "agent"
    if lowered == "system":
        return "system"
    # 'service' (BE-G4 이전 잔존) / 'anonymous' / 기타 → 'user'
    return "user"


def _placeholder_display_name(actor_type: ContributorActorType) -> str:
    if actor_type == "system":
        return "Mimir 시스템"
    if actor_type == "agent":
        return "에이전트"
    return "(알 수 없는 사용자)"


def _resolve_display_name(
    actor_id: str,
    actor_type: ContributorActorType,
    user_map,
) -> str:
    """actor_type 에 따라 표시명을 결정. user 면 users.display_name, 그 외 placeholder."""
    if actor_type == "user":
        user = user_map.get(actor_id)
        if user and user.display_name:
            return user.display_name
        return _placeholder_display_name("user")
    return _placeholder_display_name(actor_type)


def _to_contributor(
    actor_id: str,
    actor_type_raw: Optional[str],
    actor_role: Optional[str],
    last_activity_at: Optional[datetime],
    user_map,
) -> Contributor:
    actor_type = _normalize_actor_type(actor_type_raw)
    display_name = _resolve_display_name(actor_id, actor_type, user_map)
    return Contributor(
        actor_id=actor_id,
        display_name=display_name,
        actor_type=actor_type,
        last_activity_at=last_activity_at,
        role_badge=actor_role,
    )


class ContributorsService:
    """문서 contributors 4 카테고리 집계 서비스."""

    def get_contributors(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        viewer_actor: Optional[ActorContext] = None,
        since: Optional[datetime] = None,
        include_viewers: bool = False,
        limit_per_section: int = DEFAULT_LIMIT_PER_SECTION,
    ) -> ContributorsBundle:
        """문서의 contributor 묶음을 반환한다.

        Args:
            document_id        : 대상 문서 UUID 문자열.
            viewer_actor       : ACL 검증용 viewer. 누락이면 anonymous (404 발생 가능).
            since              : 카테고리별 occurred_at/created_at 하한.
                                 None → editors/approvers 는 무제한, viewers 는 기본 30일.
            include_viewers    : viewers 섹션 포함 여부 (기본 False).
                                 FG 3-2 가 결합되면 정책 게이트가 강제 False 가능.
            limit_per_section  : 카테고리당 최대 건수. 1~MAX_LIMIT_PER_SECTION 범위로 clamp.

        Raises:
            ApiNotFoundError: viewer 가 문서를 못 보거나 문서가 없으면 404.
        """
        # ACL — get_document 가 scope 밖 문서를 404 로 차단.
        documents_service.get_document(conn, document_id, actor=viewer_actor)

        limit = max(1, min(int(limit_per_section), MAX_LIMIT_PER_SECTION))

        # S3 Phase 3 FG 3-2 (2026-04-27): 정책 게이트 결합.
        # 사용자 의사 (include_viewers) AND viewer 의 ScopeProfile.expose_viewers 둘 다 true 일 때만
        # viewer 섹션을 fetch / 응답에 포함. fail-closed.
        effective_include_viewers = bool(include_viewers) and should_expose_viewers(viewer_actor)

        viewer_since = since
        if effective_include_viewers and viewer_since is None:
            viewer_since = utcnow() - timedelta(days=DEFAULT_VIEWER_SINCE_DAYS)

        creator_row = contributors_repository.get_creator(conn, document_id)
        editor_rows = contributors_repository.list_editors(
            conn, document_id, since=since, limit=limit,
        )
        approver_rows = contributors_repository.list_approvers(
            conn, document_id, since=since, limit=limit,
        )
        viewer_rows: list = []
        if effective_include_viewers:
            viewer_rows = contributors_repository.list_viewers(
                conn, document_id, since=viewer_since, limit=limit,
            )

        # creator id 를 editors / approvers / viewers 에서 분리 (중복 제거).
        creator_id: Optional[str] = creator_row["actor_id"] if creator_row else None

        # batch user fetch — 모든 카테고리의 user-typed actor_id 를 모아 한 번에.
        user_id_set: set[str] = set()
        if creator_id:
            user_id_set.add(creator_id)
        for row in (*editor_rows, *approver_rows, *viewer_rows):
            actor_type_norm = _normalize_actor_type(row.get("actor_type"))
            if actor_type_norm == "user" and row.get("actor_id"):
                user_id_set.add(str(row["actor_id"]))
        user_map = _users_repository.get_many_by_ids(conn, list(user_id_set))

        # creator 는 documents.created_by 단일 값이라 actor_type / role 컬럼이 없음 →
        # users 테이블에서 직접 조회해 role_name 을 채움.
        creator: Optional[Contributor] = None
        if creator_row:
            creator_user = user_map.get(creator_id) if creator_id else None
            creator = Contributor(
                actor_id=creator_id,  # type: ignore[arg-type]
                display_name=(
                    creator_user.display_name if creator_user and creator_user.display_name
                    else _placeholder_display_name("user")
                ),
                actor_type="user",  # documents.created_by 는 사용자만 (시스템 생성 케이스도 여기서는 user 표시)
                last_activity_at=creator_row.get("last_activity_at"),
                role_badge=creator_user.role_name if creator_user else None,
            )

        editors = [
            _to_contributor(
                actor_id=str(row["actor_id"]),
                actor_type_raw=row.get("actor_type"),
                actor_role=row.get("actor_role"),
                last_activity_at=row.get("last_activity_at"),
                user_map=user_map,
            )
            for row in editor_rows
            if str(row["actor_id"]) != creator_id  # creator 는 editors 에서 분리
        ]

        approvers = [
            _to_contributor(
                actor_id=str(row["actor_id"]),
                # workflow_history 는 actor_type 컬럼 없음 → 'user' default
                actor_type_raw=row.get("actor_type"),
                actor_role=row.get("actor_role"),
                last_activity_at=row.get("last_activity_at"),
                user_map=user_map,
            )
            for row in approver_rows
        ]

        viewers = [
            _to_contributor(
                actor_id=str(row["actor_id"]),
                actor_type_raw=row.get("actor_type"),
                actor_role=row.get("actor_role"),
                last_activity_at=row.get("last_activity_at"),
                user_map=user_map,
            )
            for row in viewer_rows
        ]

        return ContributorsBundle(
            creator=creator,
            editors=editors,
            approvers=approvers,
            viewers=viewers,
            viewers_included=effective_include_viewers,
        )


contributors_service = ContributorsService()
