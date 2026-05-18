"""Annotations Service — S3 Phase 3 FG 3-3.

책임:
    - annotation CRUD + 답글 + 해결/재오픈
    - 본문에서 @user 멘션 파싱 + valid user_id 만 채택
    - audit emit (`annotation.created/updated/resolved/reopened/deleted`)
    - 멘션 처리 시 `notifications_service.enqueue_mention` 호출

권한:
    - 작성자 본인 또는 admin 만 update / delete
    - resolve / reopen: 작성자 또는 admin (스레드 참여자 권한은 별 라운드)
    - create: 인증 사용자 (ACL 통과 후)

ACL:
    - documents.scope_profile_id (FG 2-0) 가 결정. annotations 자체는 ACL 무관.
    - service 가 documents_service.get_document(actor=...) 로 ACL 통과 검증.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.api.errors.exceptions import (
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiValidationError,
)
from app.audit.emitter import audit_emitter
from app.models.annotation import Annotation
from app.repositories.annotations_repository import annotations_repository
from app.repositories.users_repository import UsersRepository
from app.services.documents_service import documents_service
from app.services.notifications_service import notifications_service
from app.utils.actor import ADMIN_ROLES

logger = logging.getLogger(__name__)


__all__ = [
    "annotations_service",
    "AnnotationsService",
    "extract_mentions",
    "MENTION_REGEX",
    "MAX_CONTENT_LENGTH",
]


# 멘션 정규식: `@token`. token = 공백/구두점 외 모든 문자 1~64자 (한국어 포함).
# S3 Phase 5 FG 5-5 (2026-05-14): 한국어 display_name 매칭 허용.
#  boundary (앞): 시작 또는 비-단어 문자
#  token: `[^\s@,;.!?…\(\)\[\]{}<>"']` 1~64자 — 공백·구두점·중첩 @ 제외
#  주의: 끝의 마침표/콤마는 token 에 포함 안 됨 (자연어 본문 호환).
MENTION_REGEX = re.compile(
    r"(?:^|[^\w])@([^\s@,;.!?…\(\)\[\]{}<>\"']{1,64})"
)
MAX_CONTENT_LENGTH: int = 10_000


_users_repository = UsersRepository()


def extract_mentions(content: str) -> list[str]:
    """본문에서 `@token` 패턴 추출 (중복 제거).

    Returns:
        raw token 문자열 리스트. username 또는 display_name 가능 — 호출자가 user 존재 검증.
        영문은 소문자 정규화. 한국어는 원문 보존 (display_name 매칭용).
    """
    if not content:
        return []
    matches = MENTION_REGEX.findall(content)
    seen: set[str] = set()
    result: list[str] = []
    for raw in matches:
        norm = raw.strip()
        if not norm:
            continue
        # ASCII 영문만 소문자 정규화 (한국어/유니코드는 원문 보존)
        if norm.isascii():
            norm = norm.lower()
        if norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def _resolve_mention_user_ids(
    conn: psycopg2.extensions.connection,
    tokens: list[str],
    *,
    viewer_user_id: Optional[str] = None,
) -> list[str]:
    """token 리스트 → 존재하는 사용자만 user_id 로 변환. 미존재는 silently skip.

    매칭 우선순위:
      1) username (영문 handle) — 정확 일치
      2) display_name — viewer 와 같은 org 안에서 unique 일 때만 (FG 5-5, 2026-05-14)

    Args:
        viewer_user_id: viewer scope 정합 — display_name fallback 시 viewer 와 같은 org
            안 사용자만 통과. None 이면 username 매칭만 시도 (backward compat).
    """
    if not tokens:
        return []
    user_ids: list[str] = []
    for token in tokens:
        # 1) username 시도 (영문 ASCII 토큰만)
        if token.isascii():
            user = _users_repository.get_by_username(conn, token)
            if user and user.id:
                user_ids.append(user.id)
                continue
        # 2) display_name fallback — viewer scope 안 unique 일 때만
        if viewer_user_id:
            matched = _users_repository.find_by_display_name_in_viewer_orgs(
                conn, viewer_user_id=viewer_user_id, display_name=token
            )
            if matched and matched.id:
                user_ids.append(matched.id)
    # uniqueness 보존 + 순서 유지
    return list(dict.fromkeys(user_ids))


def _validate_explicit_mention_user_ids(
    conn: psycopg2.extensions.connection,
    user_ids: list[str],
    *,
    viewer_user_id: str,
) -> list[str]:
    """Frontend typeahead 가 명시한 user_ids → viewer scope 안만 통과 (R-A4 정합).

    S3 Phase 5 FG 5-5 (2026-05-14). viewer 와 같은 org 안 사용자만 mention 가능 —
    악의적 클라이언트가 다른 org user_id 를 주입해도 backend 가 차단.
    """
    if not user_ids:
        return []
    # 빈 문자열 / 중복 제거
    cleaned = [str(uid).strip() for uid in user_ids if uid and str(uid).strip()]
    if not cleaned:
        return []
    validated = _users_repository.filter_user_ids_in_viewer_orgs(
        conn, viewer_user_id=viewer_user_id, user_ids=cleaned
    )
    return list(dict.fromkeys(validated))


def _normalize_actor_type(raw: Optional[str]) -> str:
    if not raw:
        return "user"
    lowered = str(raw).lower()
    if lowered in ("user", "agent", "system"):
        return lowered
    return "user"


def _is_admin(actor: Optional[ActorContext]) -> bool:
    if actor is None or not actor.is_authenticated:
        return False
    return getattr(actor, "role", None) in ADMIN_ROLES


def _require_owner_or_admin(actor: Optional[ActorContext], annotation: Annotation) -> None:
    if actor is None or not actor.is_authenticated or not actor.actor_id:
        raise ApiPermissionDeniedError("인증된 사용자만 작업을 수행할 수 있습니다.")
    if actor.actor_id == annotation.author_id:
        return
    if _is_admin(actor):
        return
    raise ApiPermissionDeniedError("본인이 작성한 주석만 수정/삭제할 수 있습니다.")


def _validate_content(content: str) -> str:
    if not content or not content.strip():
        raise ApiValidationError("주석 본문은 비워둘 수 없습니다.")
    if len(content) > MAX_CONTENT_LENGTH:
        raise ApiValidationError(
            f"주석 본문은 {MAX_CONTENT_LENGTH}자를 초과할 수 없습니다."
        )
    # S3 Phase 6 FG 6-3 (2026-05-18): write 시 위험 문자 reject (R-O3).
    #   - null byte / BOM / RTL override / surrogate 코드포인트.
    #   - sanitize 가 아닌 reject — 사용자 입력 손실 없이 명시적 에러 전달.
    from app.utils.content_sanitizer import reject_dangerous_chars
    return reject_dangerous_chars(content, field_label="주석 본문", max_length=MAX_CONTENT_LENGTH)


class AnnotationsService:
    """annotation CRUD + 멘션 + audit emit 통합."""

    # ------------------------------------------------------------------
    # 생성 / 답글
    # ------------------------------------------------------------------

    def create_annotation(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        document_id: str,
        node_id: str,
        content: str,
        span_start: Optional[int] = None,
        span_end: Optional[int] = None,
        parent_id: Optional[str] = None,
        version_id: Optional[str] = None,
        explicit_mentioned_user_ids: Optional[list[str]] = None,
    ) -> Annotation:
        if actor is None or not actor.is_authenticated or not actor.actor_id:
            raise ApiPermissionDeniedError("인증된 사용자만 주석을 작성할 수 있습니다.")

        _validate_content(content)

        # ACL 통과 — 못 보면 404
        documents_service.get_document(conn, document_id, actor=actor)

        # 답글이면 부모 존재 + 같은 문서 검증
        if parent_id is not None:
            parent = annotations_repository.get_by_id(conn, parent_id)
            if parent is None:
                raise ApiNotFoundError(f"부모 주석을 찾을 수 없습니다: {parent_id}")
            if parent.document_id != document_id:
                raise ApiValidationError(
                    "부모 주석이 다른 문서에 속해 있습니다.",
                )
            # parent_id 가 또 답글이면 단순화 — 본 FG 는 1단계 답글만 (호출자가 root annotation 의 id 를 parent_id 로)
            if parent.parent_id is not None:
                # 같은 root 로 평탄화
                parent_id = parent.parent_id

        actor_type = _normalize_actor_type(getattr(actor.actor_type, "value", None) if hasattr(actor, "actor_type") else None)

        annotation = annotations_repository.create(
            conn,
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            span_start=span_start,
            span_end=span_end,
            author_id=actor.actor_id,
            actor_type=actor_type,
            content=content,
            parent_id=parent_id,
        )

        # 멘션 처리 — S3 Phase 5 FG 5-5 (2026-05-14):
        #   1) frontend 명시 user_ids (typeahead 선택) — viewer scope 검증 후 통과
        #   2) 본문 정규식 매칭 (username + display_name fallback)
        #   3) 합집합 → uniqueness
        viewer_id = str(actor.actor_id) if actor.actor_id else None
        explicit_ids = (
            _validate_explicit_mention_user_ids(
                conn, explicit_mentioned_user_ids or [], viewer_user_id=viewer_id,
            )
            if viewer_id
            else []
        )
        mention_tokens = extract_mentions(content)
        body_ids = _resolve_mention_user_ids(
            conn, mention_tokens, viewer_user_id=viewer_id,
        )
        mention_user_ids = list(dict.fromkeys(explicit_ids + body_ids))
        if mention_user_ids:
            annotations_repository.replace_mentions(conn, annotation.id, mention_user_ids)
            for recipient_id in mention_user_ids:
                notifications_service.enqueue_mention(
                    conn,
                    author_id=actor.actor_id,
                    recipient_id=recipient_id,
                    annotation_id=annotation.id,
                    document_id=document_id,
                    snippet=content,
                )
            annotation.mentioned_user_ids = mention_user_ids

        audit_emitter.emit_for_actor(
            event_type="annotation.created",
            action="annotation.create",
            actor=actor,
            resource_type="annotation",
            resource_id=annotation.id,
            metadata={
                "document_id": document_id,
                "node_id": node_id,
                "parent_id": parent_id,
                "mention_count": len(mention_user_ids),
            },
        )
        return annotation

    # ------------------------------------------------------------------
    # 수정 / 해결 / 삭제
    # ------------------------------------------------------------------

    def update_content(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        annotation_id: str,
        new_content: str,
    ) -> Annotation:
        annotation = annotations_repository.get_by_id(conn, annotation_id)
        if annotation is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")
        # 본인만 수정 (admin 도 수정 금지 — 작성자 본인 본문만 수정)
        if actor.actor_id != annotation.author_id:
            raise ApiPermissionDeniedError("본인이 작성한 주석만 수정할 수 있습니다.")
        _validate_content(new_content)
        # ACL 통과
        documents_service.get_document(conn, annotation.document_id, actor=actor)

        updated = annotations_repository.update_content(conn, annotation_id, new_content)
        if updated is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")

        # 멘션 재계산 — FG 5-5 (2026-05-14): viewer scope + display_name fallback
        viewer_id = str(actor.actor_id) if actor.actor_id else None
        mention_tokens = extract_mentions(new_content)
        mention_user_ids = _resolve_mention_user_ids(
            conn, mention_tokens, viewer_user_id=viewer_id,
        )
        annotations_repository.replace_mentions(conn, annotation_id, mention_user_ids)
        # 신규 멘션만 알림 (기존 멘션 → 알림 재발생 방지). 단순화: 차이 계산
        new_recipients = set(mention_user_ids) - set(annotation.mentioned_user_ids or [])
        for recipient_id in new_recipients:
            notifications_service.enqueue_mention(
                conn,
                author_id=actor.actor_id,
                recipient_id=recipient_id,
                annotation_id=annotation_id,
                document_id=annotation.document_id,
                snippet=new_content,
            )
        updated.mentioned_user_ids = mention_user_ids

        audit_emitter.emit_for_actor(
            event_type="annotation.updated",
            action="annotation.update",
            actor=actor,
            resource_type="annotation",
            resource_id=annotation_id,
            metadata={
                "document_id": annotation.document_id,
                "new_mentions": len(new_recipients),
            },
        )
        return updated

    def resolve(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        annotation_id: str,
    ) -> Annotation:
        annotation = annotations_repository.get_by_id(conn, annotation_id)
        if annotation is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")
        _require_owner_or_admin(actor, annotation)
        documents_service.get_document(conn, annotation.document_id, actor=actor)

        updated = annotations_repository.set_status(
            conn, annotation_id, status="resolved", resolved_by=actor.actor_id,
        )
        if updated is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")

        audit_emitter.emit_for_actor(
            event_type="annotation.resolved",
            action="annotation.resolve",
            actor=actor,
            resource_type="annotation",
            resource_id=annotation_id,
            metadata={"document_id": annotation.document_id},
        )
        return updated

    def reopen(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        annotation_id: str,
    ) -> Annotation:
        annotation = annotations_repository.get_by_id(conn, annotation_id)
        if annotation is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")
        _require_owner_or_admin(actor, annotation)
        documents_service.get_document(conn, annotation.document_id, actor=actor)

        updated = annotations_repository.set_status(
            conn, annotation_id, status="open",
        )
        if updated is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")

        audit_emitter.emit_for_actor(
            event_type="annotation.reopened",
            action="annotation.reopen",
            actor=actor,
            resource_type="annotation",
            resource_id=annotation_id,
            metadata={"document_id": annotation.document_id},
        )
        return updated

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        annotation_id: str,
    ) -> None:
        annotation = annotations_repository.get_by_id(conn, annotation_id)
        if annotation is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")
        _require_owner_or_admin(actor, annotation)
        documents_service.get_document(conn, annotation.document_id, actor=actor)

        deleted = annotations_repository.delete(conn, annotation_id)
        if not deleted:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")

        audit_emitter.emit_for_actor(
            event_type="annotation.deleted",
            action="annotation.delete",
            actor=actor,
            resource_type="annotation",
            resource_id=annotation_id,
            metadata={"document_id": annotation.document_id},
        )

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        annotation_id: str,
    ) -> Annotation:
        annotation = annotations_repository.get_by_id(conn, annotation_id)
        if annotation is None:
            raise ApiNotFoundError(f"주석을 찾을 수 없습니다: {annotation_id}")
        # ACL 통과 — 다른 scope 의 문서면 404
        documents_service.get_document(conn, annotation.document_id, actor=actor)
        return annotation

    def list_for_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        actor: ActorContext,
        document_id: str,
        include_resolved: bool = True,
        include_orphans: bool = True,
        limit: int = 200,
    ) -> list[Annotation]:
        # ACL 통과
        documents_service.get_document(conn, document_id, actor=actor)
        return annotations_repository.list_for_document(
            conn,
            document_id,
            include_resolved=include_resolved,
            include_orphans=include_orphans,
            limit=limit,
        )


annotations_service = AnnotationsService()
