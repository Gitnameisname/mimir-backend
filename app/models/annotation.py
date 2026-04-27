"""Annotation 도메인 모델 — S3 Phase 3 FG 3-3.

인라인 주석 + 답글 + 멘션 + 알림.

좌표 시스템:
    - node_id (UUID): Phase 1 의 안정성 위에 얹힘. 노드가 사라지면 is_orphan=True.
    - span_start / span_end (int, optional): 노드 텍스트 내 문자 오프셋 [start, end).
      둘 다 NULL 이면 노드 전체 주석.

ACL:
    - documents.scope_profile_id (FG 2-0) 가 결정. annotations 자체는 ACL 무관.
    - service 레이어가 documents_service.get_document(actor) 로 ACL 통과 검증.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

AnnotationStatus = Literal["open", "resolved"]
AnnotationActorType = Literal["user", "agent", "system"]


@dataclass
class Annotation:
    """단일 인라인 주석 (또는 답글).

    Attributes:
        id              : UUID
        document_id     : 대상 문서 UUID
        version_id      : 작성 시점의 version UUID (FK SET NULL — version 삭제 시 보존)
        node_id         : 부착 노드 UUID (Phase 1 안정성)
        span_start      : 노드 텍스트 내 문자 오프셋 시작 (NULL = 노드 전체)
        span_end        : 노드 텍스트 내 문자 오프셋 끝 exclusive (NULL = 노드 전체)
        author_id       : 작성자 식별자 (user_id 또는 agent_id)
        actor_type      : user / agent / system
        content         : 주석 본문 (1~10000자)
        status          : open | resolved
        resolved_at     : 해결 시각 (resolved 일 때만)
        resolved_by     : 해결한 사용자 (resolved 일 때만)
        parent_id       : 답글의 경우 부모 annotation UUID (cascade 삭제)
        is_orphan       : 부착 node_id 가 더 이상 snapshot 에 없으면 True
        orphaned_at     : orphan 으로 표시된 시각
        created_at      : 생성 시각
        updated_at      : 최종 수정 시각
        mentioned_user_ids: 멘션된 user_id 목록 (load 시 join 으로 채움; 없으면 빈 리스트)
    """

    id: str
    document_id: str
    version_id: Optional[str]
    node_id: str
    span_start: Optional[int]
    span_end: Optional[int]
    author_id: str
    actor_type: AnnotationActorType
    content: str
    status: AnnotationStatus
    resolved_at: Optional[datetime]
    resolved_by: Optional[str]
    parent_id: Optional[str]
    is_orphan: bool
    orphaned_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    mentioned_user_ids: list[str] = field(default_factory=list)


@dataclass
class Notification:
    """In-app 알림 (annotation.mention 등).

    Attributes:
        id          : UUID
        user_id     : 알림 수신자
        kind        : 알림 종류 (예: 'annotation.mention')
        payload     : 알림별 부가 데이터 (annotation_id, document_id, snippet 등)
        read_at     : 읽음 시각 (NULL = 미읽음)
        created_at  : 생성 시각
    """

    id: str
    user_id: str
    kind: str
    payload: dict
    read_at: Optional[datetime]
    created_at: datetime
